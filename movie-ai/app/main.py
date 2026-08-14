import json
import os
import random
import time
import requests

from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel
from rapidfuzz import fuzz

from google import genai
from google.genai import types

from app.database import (
    init_database,
    get_all,
    add_movie,
    delete_movie,
    add_recommendation_history,
    get_recent_recommendation_ids,
    add_not_interested,
    get_not_interested_ids,
)


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="My Movie AI"
)

templates = Jinja2Templates(
    directory="app/templates"
)

app.mount(
    "/static",
    StaticFiles(
        directory="app/static"
    ),
    name="static",
)

init_database()


# ============================================================
# SETTINGS
# ============================================================

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

AI_MOVIE_CANDIDATES = 30
AI_SERIES_CANDIDATES = 30

AI_MOVIES_TARGET = 8
AI_SERIES_TARGET = 8

TMDB_MOVIES_TARGET = 8
TMDB_SERIES_TARGET = 8

RECENT_HISTORY_LIMIT = 12

WATCHLIST_FILE = Path("/data/watchlist.json")

recommendation_state = {
    "status": "idle",
    "data": None,
    "error": None,
    "tmdb_data": None,
}


# ============================================================
# RECOMMENDATION BACKGROUND STATE
# ============================================================

recommendation_state = {
    "status": "idle",
    "data": None,
    "error": None,
    "tmdb_data": None,
}


# ============================================================
# BACKGROUND RECOMMENDATION JOB
# ============================================================

def run_recommendation_job(prefetched_tmdb=None):

    global recommendation_state

    try:

        print("Background AI recommendation job started...")

        recommendation_state["status"] = "loading"
        recommendation_state["error"] = None

        movies = get_all()

        result = generate_recommendations(
            movies,
            prefetched_tmdb=prefetched_tmdb,
        )

        recommendation_state["data"] = result
        recommendation_state["status"] = "ready"

        print("Background AI recommendation job finished.")

    except Exception as error:

        print(
            f"Background AI recommendation error: {error}"
        )

        recommendation_state["status"] = "error"
        recommendation_state["error"] = str(error)


# ============================================================
# WATCHLIST
# ============================================================

def load_watchlist():
    try:
        if WATCHLIST_FILE.exists():
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as error:
        print(f"Watchlist load error: {error}")
    return []


def save_watchlist(items):
    try:
        WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        WATCHLIST_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        print(f"Watchlist save error: {error}")


def watchlist_key(media_type, tmdb_id):
    return f"{media_type}:{tmdb_id}"


def is_in_watchlist(media_type, tmdb_id):
    key = watchlist_key(media_type, tmdb_id)
    return any(item.get("key") == key for item in load_watchlist())


def toggle_watchlist_item(item):
    items = load_watchlist()
    key = watchlist_key(item["media_type"], item["tmdb_id"])
    items = [x for x in items if x.get("key") != key]
    if not item.get("remove", False):
        items.append({
            "key": key,
            "tmdb_id": item["tmdb_id"],
            "media_type": item["media_type"],
            "title": item.get("title", ""),
            "year": item.get("year"),
            "poster": item.get("poster"),
            "backdrop": item.get("backdrop"),
            "overview": item.get("overview", ""),
            "vote_average": item.get("vote_average", 0),
        })
    save_watchlist(items)
    return not item.get("remove", False)


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(
        name,
        ""
    ).strip()


# ============================================================
# INGRESS-AWARE PATH
# ============================================================

def get_ingress_path(request: Request) -> str:

    path = request.headers.get(
        "x-ingress-path",
        ""
    )

    if not path:
        return ""

    return path.rstrip("/")


def build_app_url(request: Request, path: str = "") -> str:

    ingress_path = get_ingress_path(request)
    clean_path = path.strip("/")

    if ingress_path:
        return f"{ingress_path}/{clean_path}" if clean_path else ingress_path

    return f"/{clean_path}" if clean_path else "/"


def app_redirect(request: Request, path: str = ""):

    return RedirectResponse(
        build_app_url(request, path),
        status_code=303,
    )


# ============================================================
# GEMINI SCHEMA
# ============================================================

class RecommendationItem(BaseModel):

    title: str

    year: Optional[int] = None

    reason: str

    match_percentage: Optional[int] = None


class RecommendationResponse(BaseModel):

    movies: List[
        RecommendationItem
    ]

    series: List[
        RecommendationItem
    ]


# ============================================================
# GEMINI CLIENT
# ============================================================

def get_gemini_client():

    api_key = get_env(
        "GEMINI_API_KEY"
    )

    if not api_key:
        return None

    return genai.Client(
        api_key=api_key
    )


# ============================================================
# USER PROFILE
# ============================================================

def build_user_profile(watched):

    movies = []
    series = []

    for item in watched:

        data = {
            "title": item["title"],
            "rating": float(
                item["rating"]
            ),
            "year": item["year"],
        }

        if item["type"] == "Movie":
            movies.append(data)
        else:
            series.append(data)

    movies.sort(
        key=lambda x: x["rating"],
        reverse=True,
    )

    series.sort(
        key=lambda x: x["rating"],
        reverse=True,
    )

    return {
        "movies": movies,
        "series": series,
    }


# ============================================================
# GEMINI PROMPT
# ============================================================

def build_gemini_prompt(watched):

    profile = build_user_profile(
        watched
    )

    recent_movie_ids = (
        get_recent_recommendation_ids(
            "Movie",
            RECENT_HISTORY_LIMIT,
        )
    )

    recent_series_ids = (
        get_recent_recommendation_ids(
            "Series",
            RECENT_HISTORY_LIMIT,
        )
    )

    not_interested_movie_ids = (
        get_not_interested_ids(
            "Movie"
        )
    )

    not_interested_series_ids = (
        get_not_interested_ids(
            "Series"
        )
    )

    watched_movie_titles = [
        item["title"]
        for item in watched
        if item["type"] == "Movie"
    ]

    watched_series_titles = [
        item["title"]
        for item in watched
        if item["type"] == "Series"
    ]

    random_nonce = random.randint(
        1,
        999999999,
    )

    prompt = f"""
You are the personalized recommendation engine
for a private movie and TV library.

Study the user's watched titles and ratings.

Return:

- EXACTLY {AI_MOVIE_CANDIDATES} movie candidates
- EXACTLY {AI_SERIES_CANDIDATES} series candidates

The application verifies every title through TMDB.

RULES:

1. Never recommend a watched title.
2. Never recommend a recently recommended title.
3. Never recommend a NOT INTERESTED title.
4. Movies and series are completely separate.
5. Never put a movie in the series list.
6. Never put a series in the movie list.
7. Strongly prioritize titles rated 8/10 or higher.
8. Use low ratings as negative taste signals.
9. Consider:
   - genre
   - tone
   - themes
   - pacing
   - storytelling
   - actors
   - directors
   - franchises
   - audience appeal
10. Include strong matches and interesting discoveries.
11. Avoid returning only famous mainstream titles.
12. Avoid overusing one franchise.
13. Avoid overusing one actor.
14. Avoid overusing one director.
15. Avoid overusing one genre.
16. Every title must be a real existing movie or series.
17. Every title needs a personalized reason.
18. Explain why the specific user may like the title.
19. Give a personalized match percentage from 55 to 99.
20. The match percentage must reflect how strongly the title fits the user profile, not TMDB popularity.
21. Return structured data only.

RANDOMIZATION VALUE:
{random_nonce}

WATCHED MOVIES:

{json.dumps(
    profile["movies"],
    ensure_ascii=False,
    indent=2
)}

WATCHED SERIES:

{json.dumps(
    profile["series"],
    ensure_ascii=False,
    indent=2
)}

ALREADY WATCHED MOVIES:

{json.dumps(
    watched_movie_titles,
    ensure_ascii=False
)}

ALREADY WATCHED SERIES:

{json.dumps(
    watched_series_titles,
    ensure_ascii=False
)}

RECENT MOVIE RECOMMENDATION IDS:

{json.dumps(
    recent_movie_ids
)}

RECENT SERIES RECOMMENDATION IDS:

{json.dumps(
    recent_series_ids
)}

NOT INTERESTED MOVIE IDS:

{json.dumps(
    not_interested_movie_ids
)}

NOT INTERESTED SERIES IDS:

{json.dumps(
    not_interested_series_ids
)}

Return only the structured response.
"""

    return prompt


# ============================================================
# GEMINI
# ============================================================

def get_ai_recommendations(watched):

    client = get_gemini_client()

    if client is None:

        print(
            "Gemini API key is not configured"
        )

        return None

    prompt = build_gemini_prompt(
        watched
    )

    for model in GEMINI_MODELS:

        print(
            f"Trying Gemini model: {model}"
        )

        for attempt in range(2):

            try:

                response = (
                    client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=RecommendationResponse,
                            temperature=1.2,
                        ),
                    )
                )

                parsed = getattr(
                    response,
                    "parsed",
                    None,
                )

                if parsed is not None:

                    if isinstance(
                        parsed,
                        RecommendationResponse,
                    ):

                        result = parsed

                    else:

                        result = (
                            RecommendationResponse
                            .model_validate(
                                parsed
                            )
                        )

                else:

                    if not response.text:

                        print(
                            f"{model}: empty response"
                        )

                        continue

                    result = (
                        RecommendationResponse
                        .model_validate_json(
                            response.text
                        )
                    )

                print(
                    f"{model}: Gemini success"
                )

                print(
                    f"Gemini returned "
                    f"{len(result.movies)} movies "
                    f"and "
                    f"{len(result.series)} series"
                )

                return result

            except Exception as error:

                error_text = str(
                    error
                ).lower()

                print(
                    f"{model} attempt "
                    f"{attempt + 1}/2 failed: "
                    f"{error}"
                )

                temporary_error = (
                    "503" in error_text
                    or "unavailable" in error_text
                    or "429" in error_text
                    or "rate limit" in error_text
                    or "too many requests" in error_text
                    or "overloaded" in error_text
                    or "internal server error" in error_text
                )

                if temporary_error:

                    time.sleep(
                        1.5 * (
                            attempt + 1
                        )
                    )

                    continue

                break

    print(
        "All Gemini models unavailable."
    )

    return None


# ============================================================
# TMDB REQUEST
# ============================================================

def tmdb_request(
    endpoint,
    token,
    params=None,
):

    headers = {
        "Authorization":
            f"Bearer {token}",
        "accept":
            "application/json",
    }

    response = requests.get(
        endpoint,
        headers=headers,
        params=params or {},
        timeout=12,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# NORMALIZE TMDB RESULT
# ============================================================

def normalise_tmdb_result(
    result,
    media_type,
):

    if media_type == "Movie":

        canonical_title = result.get(
            "title",
            "",
        )

        date = result.get(
            "release_date",
            "",
        )

        tmdb_url = (
            "https://www.themoviedb.org/movie/"
            f"{result.get('id')}"
        )

    else:

        canonical_title = result.get(
            "name",
            "",
        )

        date = result.get(
            "first_air_date",
            "",
        )

        tmdb_url = (
            "https://www.themoviedb.org/tv/"
            f"{result.get('id')}"
        )

    year = None

    if date:

        try:

            year = int(
                date[:4]
            )

        except (
            ValueError,
            TypeError,
        ):

            pass

    poster = None

    if result.get(
        "poster_path"
    ):

        poster = (
            "https://image.tmdb.org/t/p/w500"
            + result["poster_path"]
        )

    backdrop = None

    if result.get(
        "backdrop_path"
    ):

        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + result["backdrop_path"]
        )

    return {
        "media_type":
            media_type,

        "tmdb_id":
            result.get(
                "id"
            ),

        "title":
            canonical_title,

        "year":
            year,

        "poster":
            poster,

        "backdrop":
            backdrop,

        "overview":
            (
                result.get(
                    "overview"
                )
                or ""
            ),

        "vote_average":
            float(
                result.get(
                    "vote_average",
                    0,
                )
                or 0
            ),

        "vote_count":
            int(
                result.get(
                    "vote_count",
                    0,
                )
                or 0
            ),

        "tmdb_url":
            tmdb_url,

        "genres": [
            g.get("name")
            for g in result.get("genres", [])
            if g.get("name")
        ],

        "cast": [],

        "director": "",

        "creators": [],

        "similar": [],
    }


# ============================================================
# TMDB EXACT SEARCH
# ============================================================

def tmdb_search(
    title,
    media_type,
    year=None,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:
        return None

    endpoint = (
        "https://api.themoviedb.org/3/search/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
    )

    params = {
        "query":
            title,
        "language":
            "en-US",
        "include_adult":
            "false",
    }

    if year:

        if media_type == "Movie":

            params["year"] = year

        else:

            params[
                "first_air_date_year"
            ] = year

    try:

        data = tmdb_request(
            endpoint,
            token,
            params,
        )

        results = data.get(
            "results",
            [],
        )

        if not results:
            return None

        requested = (
            title
            .strip()
            .lower()
        )

        best = None
        best_score = -1

        for result in results[:20]:

            if media_type == "Movie":

                result_title = result.get(
                    "title",
                    "",
                )

                date = result.get(
                    "release_date",
                    "",
                )

            else:

                result_title = result.get(
                    "name",
                    "",
                )

                date = result.get(
                    "first_air_date",
                    "",
                )

            if not result_title:
                continue

            normalized = (
                result_title
                .strip()
                .lower()
            )

            if (
                requested ==
                normalized
            ):

                score = 100

            else:

                ratio = fuzz.ratio(
                    requested,
                    normalized,
                )

                token_score = (
                    fuzz.token_sort_ratio(
                        requested,
                        normalized,
                    )
                )

                score = max(
                    ratio,
                    token_score * 0.95,
                )

            if year and date:

                try:

                    result_year = int(
                        date[:4]
                    )

                    if result_year == year:
                        score += 20

                except (
                    ValueError,
                    TypeError,
                ):

                    pass

            if score > best_score:

                best_score = score
                best = result

        if not best:
            return None

        if best_score < 55:
            return None

        return normalise_tmdb_result(
            best,
            media_type,
        )

    except Exception as error:

        print(
            f"TMDB search error "
            f"for '{title}': {error}"
        )

        return None


# ============================================================
# LIVE TMDB SEARCH
# ============================================================

def tmdb_live_search(
    query,
    media_type,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:
        return []

    query = query.strip()

    if not query:
        return []

    endpoint = (
        "https://api.themoviedb.org/3/search/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
    )

    try:

        data = tmdb_request(
            endpoint,
            token,
            {
                "query":
                    query,
                "language":
                    "en-US",
                "include_adult":
                    "false",
                "page":
                    1,
            },
        )

        results = data.get(
            "results",
            [],
        )

        output = []

        for item in results[:8]:

            normalized = (
                normalise_tmdb_result(
                    item,
                    media_type,
                )
            )

            if not normalized.get(
                "tmdb_id"
            ):
                continue

            output.append(
                {
                    "tmdb_id":
                        normalized["tmdb_id"],

                    "title":
                        normalized["title"],

                    "year":
                        normalized["year"],

                    "poster":
                        normalized["poster"],

                    "overview":
                        normalized["overview"],

                    "vote_average":
                        normalized[
                            "vote_average"
                        ],

                    "tmdb_url":
                        normalized[
                            "tmdb_url"
                        ],
                }
            )

        return output

    except Exception as error:

        print(
            f"TMDB live search error: "
            f"{error}"
        )

        return []


# ============================================================
# TMDB DETAILS
# ============================================================

def tmdb_get_details(
    tmdb_id,
    media_type,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:
        return None

    endpoint = (
        "https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        f"/{tmdb_id}"
    )

    try:

        data = tmdb_request(
            endpoint,
            token,
            {
                "language":
                    "en-US"
            },
        )

        return normalise_tmdb_result(
            data,
            media_type,
        )

    except Exception as error:

        print(
            f"TMDB details error: "
            f"{error}"
        )

        return None


# ============================================================
# TMDB DETAIL PAGE DATA
# ============================================================

def tmdb_get_detail_page(tmdb_id, media_type):
    token = get_env("TMDB_TOKEN")
    if not token:
        return None

    endpoint = (
        "https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        f"/{tmdb_id}"
    )

    try:
        data = tmdb_request(
            endpoint,
            token,
            {
                "language": "en-US",
                "append_to_response": "credits,similar",
            },
        )

        base = normalise_tmdb_result(data, media_type)

        credits = data.get("credits", {}) or {}
        cast = []
        for person in credits.get("cast", [])[:12]:
            cast.append({
                "name": person.get("name", ""),
                "character": person.get("character", ""),
                "photo": (
                    "https://image.tmdb.org/t/p/w185" + person["profile_path"]
                    if person.get("profile_path") else None
                ),
            })

        director = ""
        if media_type == "Movie":
            crew = credits.get("crew", []) or []
            directors = [
                p.get("name") for p in crew
                if p.get("job") == "Director" and p.get("name")
            ]
            director = ", ".join(dict.fromkeys(directors[:3]))
        else:
            creators = [
                p.get("name") for p in data.get("created_by", [])
                if p.get("name")
            ]
            director = ", ".join(dict.fromkeys(creators[:3]))

        similar = []
        for item in (data.get("similar", {}) or {}).get("results", [])[:10]:
            normalized = normalise_tmdb_result(item, media_type)
            if normalized.get("tmdb_id"):
                similar.append(normalized)

        base.update({
            "cast": cast,
            "director": director,
            "similar": similar,
            "genres": [
                g.get("name")
                for g in data.get("genres", [])
                if g.get("name")
            ],
            "runtime": data.get("runtime") if media_type == "Movie" else None,
            "episodes": data.get("number_of_episodes") if media_type == "Series" else None,
            "seasons": data.get("number_of_seasons") if media_type == "Series" else None,
            "status": data.get("status", ""),
            "tagline": data.get("tagline", ""),
            "match_percentage": None,
        })

        return base

    except Exception as error:
        print(f"TMDB detail page error: {error}")
        return None


# ============================================================
# VERIFY GEMINI RESULT
# ============================================================

def verify_recommendation(
    item,
    media_type,
):

    result = tmdb_search(
        item.title,
        media_type,
        item.year,
    )

    if not result:
        return None

    result["reason"] = (
        item.reason
        or
        "Recommended because it matches "
        "your watched titles and ratings."
    )

    result["source"] = "AI"
    result["match_percentage"] = max(55, min(99, int(item.match_percentage or 82)))

    return result


def verify_all_recommendations(
    ai_result,
):

    movies = []
    series = []
    jobs = []

    with ThreadPoolExecutor(
        max_workers=16
    ) as executor:

        for item in ai_result.movies:

            jobs.append(
                (
                    "Movie",
                    executor.submit(
                        verify_recommendation,
                        item,
                        "Movie",
                    ),
                )
            )

        for item in ai_result.series:

            jobs.append(
                (
                    "Series",
                    executor.submit(
                        verify_recommendation,
                        item,
                        "Series",
                    ),
                )
            )

        for media_type, future in jobs:

            try:

                result = future.result()

                if not result:
                    continue

                if media_type == "Movie":

                    movies.append(
                        result
                    )

                else:

                    series.append(
                        result
                    )

            except Exception as error:

                print(
                    "TMDB verification error: "
                    f"{error}"
                )

    return (
        unique_results(movies),
        unique_results(series),
    )


# ============================================================
# UNIQUE
# ============================================================

def unique_results(results):

    seen = set()
    output = []

    for item in results:

        key = (
            item.get(
                "media_type"
            ),
            item.get(
                "tmdb_id"
            ),
        )

        if not item.get(
            "tmdb_id"
        ):
            continue

        if key in seen:
            continue

        seen.add(
            key
        )

        output.append(
            item
        )

    return output


# ============================================================
# FILTER
# ============================================================

def filter_new_results(
    results,
    blocked_ids,
):

    blocked_ids = set(
        blocked_ids
    )

    output = []
    seen = set()

    for item in results:

        tmdb_id = item.get(
            "tmdb_id"
        )

        if not tmdb_id:
            continue

        if tmdb_id in blocked_ids:
            continue

        if tmdb_id in seen:
            continue

        seen.add(
            tmdb_id
        )

        output.append(
            item
        )

    return output


# ============================================================
# TMDB DISCOVERY
# ============================================================

def tmdb_fallback(
    watched,
    blocked_movie_ids=None,
    blocked_series_ids=None,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:

        return {
            "movies": [],
            "series": [],
        }

    watched_movie_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Movie"
            and item["tmdb_id"]
        )
    }

    watched_series_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Series"
            and item["tmdb_id"]
        )
    }

    blocked_movie_ids = set(
        blocked_movie_ids or []
    )

    blocked_series_ids = set(
        blocked_series_ids or []
    )

    blocked_movie_ids.update(
        watched_movie_ids
    )

    blocked_series_ids.update(
        watched_series_ids
    )

    def discover(
        media_type,
        page,
    ):

        endpoint = (
            "https://api.themoviedb.org/3/discover/"
            f"{'movie' if media_type == 'Movie' else 'tv'}"
        )

        try:

            data = tmdb_request(
                endpoint,
                token,
                {
                    "language":
                        "en-US",
                    "page":
                        page,
                    "sort_by":
                        "popularity.desc",
                    "include_adult":
                        "false",
                    "vote_count.gte":
                        50,
                },
            )

            return (
                media_type,
                data.get(
                    "results",
                    [],
                ),
            )

        except Exception as error:

            print(
                "TMDB discovery error: "
                f"{error}"
            )

            return (
                media_type,
                [],
            )

    jobs = [
        ("Movie", 1),
        ("Movie", 2),
        ("Movie", 3),
        ("Movie", 4),
        ("Movie", 5),
        ("Movie", 6),
        ("Movie", 7),
        ("Movie", 8),
        ("Series", 1),
        ("Series", 2),
        ("Series", 3),
        ("Series", 4),
        ("Series", 5),
        ("Series", 6),
        ("Series", 7),
        ("Series", 8),
    ]

    movies = []
    series = []

    with ThreadPoolExecutor(
        max_workers=16
    ) as executor:

        futures = [
            executor.submit(
                discover,
                media_type,
                page,
            )
            for media_type, page in jobs
        ]

        for future in futures:

            media_type, items = (
                future.result()
            )

            blocked = (
                blocked_movie_ids
                if media_type == "Movie"
                else blocked_series_ids
            )

            for item in items:

                tmdb_id = item.get(
                    "id"
                )

                if not tmdb_id:
                    continue

                if tmdb_id in blocked:
                    continue

                result = normalise_tmdb_result(
                    item,
                    media_type,
                )

                result["source"] = "TMDB"
                result["match_percentage"] = max(55, min(95, int(round(result.get("vote_average", 7.0) * 10 + 10))))

                result["reason"] = (
                    "Selected as a TMDB discovery after excluding your watched and rejected titles."
                )

                if media_type == "Movie":

                    movies.append(
                        result
                    )

                else:

                    series.append(
                        result
                    )

    movies = unique_results(
        movies
    )

    series = unique_results(
        series
    )

    random.shuffle(
        movies
    )

    random.shuffle(
        series
    )

    return {
        "movies":
            movies,

        "series":
            series,
    }


# ============================================================
# GENERATE RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    watched,
    prefetched_tmdb=None,
):

    print(
        "Starting separate "
        "AI + TMDB engine..."
    )

    watched_movie_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Movie"
            and item["tmdb_id"]
        )
    }

    watched_series_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Series"
            and item["tmdb_id"]
        )
    }

    recent_movie_ids = set(
        get_recent_recommendation_ids(
            "Movie",
            RECENT_HISTORY_LIMIT,
        )
    )

    recent_series_ids = set(
        get_recent_recommendation_ids(
            "Series",
            RECENT_HISTORY_LIMIT,
        )
    )

    not_interested_movie_ids = set(
        get_not_interested_ids(
            "Movie"
        )
    )

    not_interested_series_ids = set(
        get_not_interested_ids(
            "Series"
        )
    )

    blocked_movies = (
        watched_movie_ids
        | recent_movie_ids
        | not_interested_movie_ids
    )

    blocked_series = (
        watched_series_ids
        | recent_series_ids
        | not_interested_series_ids
    )

    ai_result = (
        get_ai_recommendations(
            watched
        )
    )

    ai_movies = []
    ai_series = []

    if ai_result:

        print(
            "Gemini succeeded."
        )

        verified_movies, verified_series = (
            verify_all_recommendations(
                ai_result
            )
        )

        ai_movies = filter_new_results(
            verified_movies,
            blocked_movies,
        )

        ai_series = filter_new_results(
            verified_series,
            blocked_series,
        )

        random.shuffle(
            ai_movies
        )

        random.shuffle(
            ai_series
        )

        ai_movies = ai_movies[
            :AI_MOVIES_TARGET
        ]

        ai_series = ai_series[
            :AI_SERIES_TARGET
        ]

        print(
            f"AI movies: "
            f"{len(ai_movies)}"
        )

        print(
            f"AI series: "
            f"{len(ai_series)}"
        )

    else:

        print(
            "Gemini unavailable."
        )

    tmdb_results = (
        prefetched_tmdb
        if prefetched_tmdb is not None
        else tmdb_fallback(
            watched,
            blocked_movie_ids=(
            blocked_movies
            | {
                item["tmdb_id"]
                for item in ai_movies
            }
        ),
            blocked_series_ids=(
                blocked_series
                | {
                    item["tmdb_id"]
                    for item in ai_series
                }
            ),
        )
    )

    tmdb_movies = filter_new_results(
        tmdb_results["movies"],
        blocked_movies
        | {
            item["tmdb_id"]
            for item in ai_movies
        },
    )

    tmdb_series = filter_new_results(
        tmdb_results["series"],
        blocked_series
        | {
            item["tmdb_id"]
            for item in ai_series
        },
    )

    tmdb_movies = tmdb_movies[
        :TMDB_MOVIES_TARGET
    ]

    tmdb_series = tmdb_series[
        :TMDB_SERIES_TARGET
    ]

    for item in ai_movies:

        add_recommendation_history(
            tmdb_id=item["tmdb_id"],
            media_type="Movie",
            title=item["title"],
        )

    for item in tmdb_movies:

        add_recommendation_history(
            tmdb_id=item["tmdb_id"],
            media_type="Movie",
            title=item["title"],
        )

    for item in ai_series:

        add_recommendation_history(
            tmdb_id=item["tmdb_id"],
            media_type="Series",
            title=item["title"],
        )

    for item in tmdb_series:

        add_recommendation_history(
            tmdb_id=item["tmdb_id"],
            media_type="Series",
            title=item["title"],
        )

    print(
        f"FINAL MOVIES: "
        f"{len(ai_movies)} AI + "
        f"{len(tmdb_movies)} TMDB"
    )

    print(
        f"FINAL SERIES: "
        f"{len(ai_series)} AI + "
        f"{len(tmdb_series)} TMDB"
    )

    return {
        "ai_movies":
            ai_movies,

        "tmdb_movies":
            tmdb_movies,

        "ai_series":
            ai_series,

        "tmdb_series":
            tmdb_series,

        "source":
            (
                "Gemini + TMDB"
                if ai_result
                else "TMDB"
            ),
    }


# ============================================================
# HOME
#
# Opening the normal URL automatically starts discovery.
# ============================================================

@app.get("/")
@app.get("//")
def home(request: Request):

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# TRENDING NOW
# ============================================================

def get_trending_titles(media_type, limit=8):
    """Return current TMDB daily trending titles, excluding watched/rejected items."""

    token = get_env("TMDB_TOKEN")

    if not token:
        return []

    endpoint = (
        "https://api.themoviedb.org/3/trending/"
        f"{'movie' if media_type == 'Movie' else 'tv'}/day"
    )

    try:
        data = tmdb_request(
            endpoint,
            token,
            {"language": "en-US"},
        )

        watched_ids = {
            int(item["tmdb_id"])
            for item in get_all()
            if item["tmdb_id"] is not None
            and item["type"] == media_type
        }

        rejected_ids = {
            int(item_id)
            for item_id in get_not_interested_ids(media_type)
        }

        blocked_ids = watched_ids | rejected_ids

        output = []
        seen = set()

        for item in data.get("results", []):
            tmdb_id = item.get("id")

            if not tmdb_id:
                continue

            tmdb_id = int(tmdb_id)

            if tmdb_id in blocked_ids or tmdb_id in seen:
                continue

            normalized = normalise_tmdb_result(
                item,
                media_type,
            )

            if not normalized.get("title"):
                continue

            normalized["source"] = "TRENDING"
            normalized["reason"] = (
                "Trending on TMDB right now."
            )

            output.append(normalized)
            seen.add(tmdb_id)

            if len(output) >= limit:
                break

        return output

    except Exception as error:
        print(
            f"TMDB trending {media_type.lower()} error: {error}"
        )
        return []


@app.get("/api/trending")
def api_trending():
    """Return separate current trending movie and TV rails."""

    with ThreadPoolExecutor(max_workers=2) as executor:
        movie_future = executor.submit(
            get_trending_titles,
            "Movie",
            8,
        )
        series_future = executor.submit(
            get_trending_titles,
            "Series",
            8,
        )

        return {
            "movies": movie_future.result(),
            "series": series_future.result(),
        }


# ============================================================
# LIVE SEARCH API
# ============================================================

@app.get("/api/search")
def api_search(
    q: str = "",
    media_type: str = "Movie",
):

    q = q.strip()

    if media_type not in (
        "Movie",
        "Series",
    ):

        media_type = "Movie"

    if len(q) < 2:

        return {
            "results": []
        }

    results = tmdb_live_search(
        q,
        media_type,
    )

    return {
        "results":
            results
    }


# ============================================================
# ADD WATCHED
# ============================================================

@app.post("/add")
def add(
    request: Request,
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: Optional[int] = Form(None),
):

    title = title.strip()

    if (
        not title
        or not 0 <= rating <= 10
        or media_type not in (
            "Movie",
            "Series",
        )
    ):

        return app_redirect(
            request,
            "",
        )

    tmdb_data = None

    # Exact selected TMDB title.
    if tmdb_id:

        tmdb_data = tmdb_get_details(
            tmdb_id,
            media_type,
        )

    # Fallback for manually typed titles.
    if not tmdb_data:

        tmdb_data = tmdb_search(
            title,
            media_type,
        )

    if tmdb_data:

        add_movie(
            title=
                tmdb_data["title"],

            rating=
                rating,

            media_type=
                media_type,

            poster=
                tmdb_data.get(
                    "poster"
                ),

            backdrop=
                tmdb_data.get(
                    "backdrop"
                ),

            year=
                tmdb_data.get(
                    "year"
                ),

            overview=
                tmdb_data.get(
                    "overview"
                ),

            tmdb_id=
                tmdb_data.get(
                    "tmdb_id"
                ),
        )

    else:

        add_movie(
            title=
                title,

            rating=
                rating,

            media_type=
                media_type,
        )

    return app_redirect(
        request,
        "",
    )


# ============================================================
# FIND JUST-ADDED LIBRARY ROW
# ============================================================

def find_library_row(tmdb_id, media_type, title):
    rows = get_all()

    matches = []

    for row in rows:
        if row["type"] != media_type:
            continue

        same_tmdb = (
            tmdb_id is not None
            and row["tmdb_id"] is not None
            and int(row["tmdb_id"]) == int(tmdb_id)
        )

        same_title = (
            str(row["title"] or "").strip().lower()
            == str(title or "").strip().lower()
        )

        if same_tmdb or same_title:
            matches.append(row)

    if not matches:
        return None

    return max(
        matches,
        key=lambda row: int(row["id"] or 0),
    )


# ============================================================
# AJAX ADD WATCHED
# ============================================================

@app.post("/api/add")
def api_add(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: Optional[int] = Form(None),
):

    title = title.strip()

    if (
        not title
        or not 0 <= rating <= 10
        or media_type not in ("Movie", "Series")
    ):
        return {
            "ok": False,
            "error": "Invalid title, rating or media type."
        }

    tmdb_data = None

    if tmdb_id:
        tmdb_data = tmdb_get_details(
            tmdb_id,
            media_type,
        )

    if not tmdb_data:
        tmdb_data = tmdb_search(
            title,
            media_type,
        )

    if tmdb_data:
        canonical = tmdb_data
        add_movie(
            title=canonical["title"],
            rating=rating,
            media_type=media_type,
            poster=canonical.get("poster"),
            backdrop=canonical.get("backdrop"),
            year=canonical.get("year"),
            overview=canonical.get("overview"),
            tmdb_id=canonical.get("tmdb_id"),
        )
    else:
        add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )
        canonical = {
            "title": title,
            "tmdb_id": tmdb_id,
            "year": None,
            "poster": None,
            "overview": "",
            "vote_average": 0,
            "tmdb_url": None,
        }

    library_row = find_library_row(
        canonical.get("tmdb_id") or tmdb_id,
        media_type,
        canonical.get("title") or title,
    )

    return {
        "ok": True,
        "item": {
            "id": library_row["id"] if library_row else None,
            "title": canonical.get("title") or title,
            "tmdb_id": canonical.get("tmdb_id"),
            "year": canonical.get("year"),
            "poster": canonical.get("poster"),
            "overview": canonical.get("overview") or "",
            "vote_average": canonical.get("vote_average", 0) or 0,
            "media_type": media_type,
            "rating": rating,
        },
    }


# ============================================================
# AJAX MARK RECOMMENDATION WATCHED
# ============================================================

@app.post("/api/recommendation/watched")
def api_recommendation_watched(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    if media_type not in ("Movie", "Series"):
        return {
            "ok": False,
            "error": "Invalid media type."
        }

    token = get_env("TMDB_TOKEN")
    tmdb_data = None

    if token:
        try:
            endpoint = (
                "https://api.themoviedb.org/3/"
                f"{'movie' if media_type == 'Movie' else 'tv'}"
                f"/{tmdb_id}"
            )

            data = tmdb_request(
                endpoint,
                token,
                {"language": "en-US"},
            )

            tmdb_data = normalise_tmdb_result(
                data,
                media_type,
            )

        except Exception as error:
            print(
                "TMDB AJAX watched lookup error: "
                f"{error}"
            )

    if tmdb_data:
        add_movie(
            title=tmdb_data["title"],
            rating=rating,
            media_type=media_type,
            poster=tmdb_data.get("poster"),
            backdrop=tmdb_data.get("backdrop"),
            year=tmdb_data.get("year"),
            overview=tmdb_data.get("overview"),
            tmdb_id=tmdb_data.get("tmdb_id"),
        )
    else:
        add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )

    final_title = (
        tmdb_data.get("title")
        if tmdb_data
        else title
    )

    library_row = find_library_row(
        tmdb_id,
        media_type,
        final_title,
    )

    return {
        "ok": True,
        "id": library_row["id"] if library_row else None,
        "tmdb_id": tmdb_id,
        "title": final_title,
        "media_type": media_type,
        "poster": (
            tmdb_data.get("poster")
            if tmdb_data
            else None
        ),
        "year": (
            tmdb_data.get("year")
            if tmdb_data
            else None
        ),
        "overview": (
            tmdb_data.get("overview")
            if tmdb_data
            else ""
        ),
    }


# ============================================================
# TITLE DETAIL PAGE
# ============================================================

@app.get("/title/{media_type}/{tmdb_id}")
def title_detail(
    request: Request,
    media_type: str,
    tmdb_id: int,
    match: Optional[int] = None,
):
    if media_type not in ("Movie", "Series"):
        return app_redirect(request, "recommendations")

    detail = tmdb_get_detail_page(
        tmdb_id,
        media_type,
    )

    if not detail:
        return app_redirect(request, "recommendations")

    watched = get_all()
    watched_item = next(
        (
            item for item in watched
            if item["tmdb_id"] is not None
            and int(item["tmdb_id"]) == int(tmdb_id)
            and item["type"] == media_type
        ),
        None,
    )

    # Keep the recommendation card's match percentage when the user
    # clicked from an AI/TMDB recommendation rail. For direct visits,
    # calculate a fallback score from the user's library.
    if match is not None:
        match_percentage = max(1, min(99, int(match)))
    elif watched_item:
        match_percentage = 100
    else:
        recent_scores = []
        for item in watched:
            rating = float(item["rating"] or 0)
            if rating < 7:
                continue
            overview = (item["overview"] or "").strip()
            if overview and detail.get("overview"):
                score = fuzz.token_set_ratio(
                    overview.lower(),
                    detail["overview"].lower(),
                )
                recent_scores.append(
                    score * (0.65 + 0.35 * (rating / 10.0))
                )
        if recent_scores:
            match_percentage = int(max(58, min(96, round(max(recent_scores) + 5))))
        else:
            match_percentage = int(max(60, min(88, round((detail.get("vote_average", 7) or 7) * 10))))

    detail["match_percentage"] = match_percentage
    detail["is_watchlisted"] = is_in_watchlist(
        media_type,
        tmdb_id,
    )
    detail["is_watched"] = bool(watched_item)
    detail["user_rating"] = watched_item["rating"] if watched_item else None

    response = templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "detail": detail,
            "media_type": media_type,
            "ingress_path": get_ingress_path(request),
        },
    )

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


# ============================================================
# WATCHLIST TOGGLE
# ============================================================

@app.post("/watchlist/toggle")
def watchlist_toggle(
    request: Request,
    tmdb_id: int = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
    year: Optional[int] = Form(None),
    poster: Optional[str] = Form(None),
    backdrop: Optional[str] = Form(None),
    overview: Optional[str] = Form(""),
    vote_average: float = Form(0),
    remove: bool = Form(False),
):
    toggle_watchlist_item({
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "poster": poster,
        "backdrop": backdrop,
        "overview": overview or "",
        "vote_average": vote_average,
        "remove": remove,
    })

    return app_redirect(
        request,
        f"title/{media_type}/{tmdb_id}",
    )


# ============================================================
# RECOMMENDATIONS
# ============================================================

@app.get(
    "/recommendations"
)
def recommendations(
    request: Request,
    background_tasks: BackgroundTasks,
):

    global recommendation_state

    movies = get_all()

    watched_movies = [
        item
        for item in movies
        if item["type"] == "Movie"
    ]

    watched_series = [
        item
        for item in movies
        if item["type"] == "Series"
    ]

    # Finished AI job: render the complete recommendation result.
    if recommendation_state["status"] == "ready":

        recommendations_data = (
            recommendation_state["data"]
        )

        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "movies": movies,
                "watched_movies": watched_movies,
                "watched_series": watched_series,
                "recommendations": recommendations_data,
                "recommendations_loading": False,
                "tmdb_discoveries": None,
                "ingress_path": get_ingress_path(request),
            },
        )

        recommendation_state = {
            "status": "idle",
            "data": None,
            "error": None,
            "tmdb_data": None,
        }

    else:

        # Fetch TMDB discovery immediately so the user can see the
        # page and TMDB rails while Gemini continues in the background.
        tmdb_discoveries = recommendation_state.get("tmdb_data")

        if tmdb_discoveries is None:
            tmdb_discoveries = tmdb_fallback(movies)

        if recommendation_state["status"] != "loading":

            recommendation_state = {
                "status": "loading",
                "data": None,
                "error": None,
                "tmdb_data": tmdb_discoveries,
            }

            background_tasks.add_task(
                run_recommendation_job,
                tmdb_discoveries,
            )

        else:
            recommendation_state["tmdb_data"] = tmdb_discoveries

        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "movies": movies,
                "watched_movies": watched_movies,
                "watched_series": watched_series,
                "recommendations": None,
                "recommendations_loading": True,
                "tmdb_discoveries": tmdb_discoveries,
                "ingress_path": get_ingress_path(request),
            },
        )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


# ============================================================
# RECOMMENDATION STATUS
# ============================================================

@app.get("/api/recommendations/status")
def recommendation_status():

    return {
        "status": recommendation_state["status"],
        "error": recommendation_state["error"],
    }


# ============================================================
# MARK RECOMMENDATION WATCHED
# ============================================================

@app.post(
    "/recommendation/watched"
)
def recommendation_watched(
    request: Request,
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    token = get_env(
        "TMDB_TOKEN"
    )

    tmdb_data = None

    if token:

        try:

            endpoint = (
                "https://api.themoviedb.org/3/"
                f"{'movie' if media_type == 'Movie' else 'tv'}"
                f"/{tmdb_id}"
            )

            data = tmdb_request(
                endpoint,
                token,
                {
                    "language":
                        "en-US",
                },
            )

            tmdb_data = (
                normalise_tmdb_result(
                    data,
                    media_type,
                )
            )

        except Exception as error:

            print(
                "TMDB watched lookup error: "
                f"{error}"
            )

    if tmdb_data:

        add_movie(
            title=
                tmdb_data["title"],

            rating=
                rating,

            media_type=
                media_type,

            poster=
                tmdb_data.get(
                    "poster"
                ),

            backdrop=
                tmdb_data.get(
                    "backdrop"
                ),

            year=
                tmdb_data.get(
                    "year"
                ),

            overview=
                tmdb_data.get(
                    "overview"
                ),

            tmdb_id=
                tmdb_data.get(
                    "tmdb_id"
                ),
        )

    else:

        add_movie(
            title=
                title,

            rating=
                rating,

            media_type=
                media_type,

            tmdb_id=
                tmdb_id,
        )

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# NOT INTERESTED
# ============================================================

@app.post(
    "/recommendation/not-interested"
)
def recommendation_not_interested(
    request: Request,
    title: str = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    add_not_interested(
        tmdb_id=
            tmdb_id,

        media_type=
            media_type,

        title=
            title,
    )

    print(
        f"Not interested: "
        f"{title} "
        f"[{media_type}] "
        f"TMDB={tmdb_id}"
    )

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# DELETE
# ============================================================

@app.post(
    "/delete/{movie_id}"
)
def delete(
    request: Request,
    movie_id: int,
):

    delete_movie(
        movie_id
    )

    return app_redirect(
        request,
        "",
    )

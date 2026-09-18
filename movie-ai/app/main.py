import json
import os
import random
import time
import hashlib
import logging
import threading
import uuid
import requests

from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse, Response
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
    get_display_statistics,
    set_recommendation_statistics,
    set_trending_statistics,
    set_display_statistics,
    get_lifetime_statistics,
    increment_lifetime_recommendations,
    increment_lifetime_trending,
    upsert_watchlist, remove_watchlist, is_watchlisted, get_watchlist,
    add_recommendation_event, get_recommendation_events,
    get_analytics, set_setting, get_setting, get_not_interested, remove_not_interested,
    get_series_progress, set_series_progress, cache_get, cache_set, cache_clear,
)


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="My Movie AI"
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("movie_ai")

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

WATCHLIST_FILE = Path("/data/watchlist.json")  # legacy migration source only
RECOMMENDATION_LOCK = threading.Lock()
RECOMMENDATION_JOBS = {}
RECOMMENDATION_MAX_HISTORY = 50

recommendation_state = {
    "status": "idle",
    "data": None,
    "error": None,
    "tmdb_data": None,
    "job_id": None,
    "progress": 0,
    "stage": "Idle",
}


def _job_update(job_id, **updates):
    with RECOMMENDATION_LOCK:
        job = RECOMMENDATION_JOBS.setdefault(job_id, {})
        job.update(updates)
        recommendation_state.update(updates)
        recommendation_state["job_id"] = job_id


def _migrate_legacy_watchlist():
    if not WATCHLIST_FILE.exists():
        return
    try:
        items = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        for item in items if isinstance(items, list) else []:
            if item.get("tmdb_id") and item.get("media_type"):
                upsert_watchlist(item)
        WATCHLIST_FILE.rename(WATCHLIST_FILE.with_suffix('.json.migrated'))
        print("Migrated legacy watchlist.json to SQLite")
    except Exception as error:
        print(f"Watchlist migration skipped: {error}")

_migrate_legacy_watchlist()


# ============================================================
# BACKGROUND RECOMMENDATION JOB
# ============================================================

def run_recommendation_job(prefetched_tmdb=None, job_id=None):
    global recommendation_state
    job_id = job_id or str(uuid.uuid4())
    try:
        _job_update(job_id, status="loading", error=None, progress=3, stage="Building your taste profile")
        movies = get_all()
        _job_update(job_id, progress=12, stage="Generating AI candidates")
        result = generate_recommendations(movies, prefetched_tmdb=prefetched_tmdb, job_id=job_id)
        increment_lifetime_recommendations(
            ai_movies=len(result.get("ai_movies", [])), ai_series=len(result.get("ai_series", [])),
            tmdb_movies=len(result.get("tmdb_movies", [])), tmdb_series=len(result.get("tmdb_series", [])),
        )
        _job_update(job_id, data=result, status="ready", error=None, progress=100, stage="Ready")
        print(f"Recommendation job {job_id} finished")
    except Exception as error:
        logging.exception("Recommendation job failed")
        _job_update(job_id, status="error", error=str(error), progress=100, stage="Failed")


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
    genres: List[str] = []
    director: Optional[str] = None
    cast: List[str] = []
    keywords: List[str] = []


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
    def decode(value):
        try:
            return json.loads(value or "[]") if isinstance(value, str) else (value or [])
        except Exception:
            return []
    movies, series = [], []
    for item in watched:
        record = {
            "title": item["title"], "rating": float(item["rating"]), "year": item["year"],
            "genres": decode(item["genres"]), "director": item["director"] or "",
            "cast": decode(item["cast"])[:8], "creators": decode(item["creators"]),
            "keywords": decode(item["keywords"])[:12], "runtime": item["runtime"],
        }
        (movies if item["type"] == "Movie" else series).append(record)
    return {"movies": movies, "series": series}


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
22. Include likely genres, director when known, up to 5 notable cast names, and up to 8 useful thematic keywords for every candidate.

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

def tmdb_request(endpoint, token, params=None, ttl=1800):
    params = params or {}
    key_source = endpoint + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    cache_key = "tmdb:" + hashlib.sha256(key_source.encode()).hexdigest()
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(endpoint, headers=headers, params=params, timeout=12)
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(f"TMDB temporary HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
            cache_set(cache_key, data, ttl=ttl)
            return data
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.8 * (2 ** attempt))
    raise last_error or RuntimeError("TMDB request failed")


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

        "vote_count": int(result.get("vote_count", 0) or 0),
        "popularity": float(result.get("popularity", 0) or 0),
        "original_language": result.get("original_language", ""),

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
    page=1,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:
        return {"results": [], "page": int(page), "total_pages": 1, "total_results": 0}

    query = query.strip()

    if not query:
        return {"results": [], "page": int(page), "total_pages": 1, "total_results": 0}

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
                    max(1, int(page)),
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

                    "media_type":
                        media_type,
                }
            )

        return {
            "results": output,
            "page": int(data.get("page") or page),
            "total_pages": int(data.get("total_pages") or 1),
            "total_results": int(data.get("total_results") or len(output)),
        }

    except Exception as error:

        logger.exception("TMDB live search error")
        return {"results": [], "page": int(page), "total_pages": 1, "total_results": 0}


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

        data = tmdb_request(endpoint, token, {"language":"en-US","append_to_response":"credits,keywords"}, ttl=21600)
        result = normalise_tmdb_result(data, media_type)
        credits=data.get("credits",{}) or {}
        if media_type == "Movie":
            people=[{"name":p.get("name",""),"character":p.get("character","")} for p in credits.get("cast",[])[:12] if p.get("name")]
            directors=[p.get("name") for p in credits.get("crew",[]) if p.get("job")=="Director" and p.get("name")]
            result["director"]=", ".join(dict.fromkeys(directors[:3]))
        else:
            people=[{"name":p.get("name",""),"character":p.get("character","")} for p in credits.get("cast",[])[:12] if p.get("name")]
            creators=[p.get("name") for p in data.get("created_by",[]) if p.get("name")]
            result["creators"]=list(dict.fromkeys(creators[:5])); result["director"]=", ".join(dict.fromkeys(creators[:3]))
        result["cast"]=people
        kw=(data.get("keywords") or {}).get("keywords",[]) or (data.get("keywords") or {}).get("results",[]) or []
        result["keywords"]=[x.get("name") for x in kw if x.get("name")][:20]
        return result

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
                "append_to_response": "credits,similar,videos,watch/providers",
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
            "trailer": next((v for v in (data.get("videos", {}) or {}).get("results", []) if v.get("site") == "YouTube" and v.get("type") == "Trailer"), None),
            "providers": (data.get("watch/providers", {}) or {}).get("results", {}),
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

    result["reason"] = item.reason or "Recommended because it matches your watched titles and ratings."
    result["source"] = "AI"
    result["ai_genres"] = list(item.genres or [])
    result["ai_director"] = item.director or ""
    result["ai_cast"] = list(item.cast or [])
    result["ai_keywords"] = list(item.keywords or [])
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


def rank_recommendations(items, watched):
    """Calculate a reproducible local taste score rather than trusting an AI percentage."""
    def decode(v):
        try: return json.loads(v or "[]") if isinstance(v,str) else (v or [])
        except Exception: return []
    high=[x for x in watched if float(x["rating"] or 0)>=8]
    all_genres={}
    all_people={}
    all_keywords={}
    years=[]
    for x in high:
        w=max(.2,float(x["rating"])/10)
        for g in decode(x["genres"]): all_genres[str(g).lower()]=all_genres.get(str(g).lower(),0)+w
        for person in decode(x["cast"])[:10]:
            name=(person.get("name") if isinstance(person,dict) else str(person)) or ""
            if name: all_people[name.lower()]=all_people.get(name.lower(),0)+w
        for k in decode(x["keywords"]): all_keywords[str(k).lower()]=all_keywords.get(str(k).lower(),0)+w
        if x["year"]: years.append(int(x["year"]))
    def score(item):
        genres=[str(g).lower() for g in (item.get("genres") or item.get("ai_genres") or [])]
        genre_score=min(1,sum(all_genres.get(g,0) for g in genres)/max(1,sum(all_genres.values()))) if all_genres else 0
        people=[str(x).lower() for x in (item.get("ai_cast") or [])]
        people_score=min(1,sum(all_people.get(x,0) for x in people)/max(1,sum(all_people.values()))) if all_people else 0
        keywords=[str(x).lower() for x in (item.get("ai_keywords") or [])]
        keyword_score=min(1,sum(all_keywords.get(x,0) for x in keywords)/max(1,sum(all_keywords.values()))) if all_keywords else 0
        title=str(item.get("title") or "").lower()
        title_similarity=max((fuzz.token_set_ratio(title,str(x["title"]).lower())/100 for x in watched),default=0)
        # Similarity is intentionally limited so the engine still discovers new titles.
        popularity=min(1,float(item.get("vote_average") or 0)/10)
        year_score=0
        if years and item.get("year"):
            distance=min(abs(int(item["year"])-y) for y in years); year_score=max(0,1-distance/40)
        ai_signal=float(item.get("match_percentage") or 70)/100
        final=(genre_score*.30+people_score*.12+keyword_score*.10+title_similarity*.08+popularity*.12+year_score*.08+ai_signal*.20)
        item["match_percentage"]=max(55,min(99,round(final*100)))
        return item["match_percentage"]
    return sorted(items,key=score,reverse=True)


# ============================================================
# GENERATE RECOMMENDATIONS
# ============================================================

def generate_recommendations(watched, prefetched_tmdb=None, job_id=None):

    print("Starting AI + TMDB recommendation engine")
    if job_id: _job_update(job_id, progress=18, stage="Analyzing ratings and preferences")

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

    recent_limit = max(0, min(500, int(get_setting("avoid_recent", RECENT_HISTORY_LIMIT) or RECENT_HISTORY_LIMIT)))
    recent_movie_ids = set(get_recent_recommendation_ids("Movie", recent_limit)) if recent_limit else set()
    recent_series_ids = set(get_recent_recommendation_ids("Series", recent_limit)) if recent_limit else set()
    ai_target = max(4, min(20, int(get_setting("recommendation_count", AI_MOVIES_TARGET) or AI_MOVIES_TARGET)))

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

    if job_id: _job_update(job_id, progress=30, stage="Asking AI for candidates")
    ai_result = get_ai_recommendations(watched)

    ai_movies = []
    ai_series = []

    if ai_result:

        print(
            "Gemini succeeded."
        )

        if job_id: _job_update(job_id, progress=52, stage="Verifying candidates with TMDB")
        verified_movies, verified_series = verify_all_recommendations(ai_result)

        ai_movies = filter_new_results(
            verified_movies,
            blocked_movies,
        )

        ai_series = filter_new_results(
            verified_series,
            blocked_series,
        )

        ai_movies = rank_recommendations(ai_movies, watched)
        ai_series = rank_recommendations(ai_series, watched)

        ai_movies = ai_movies[:ai_target]
        ai_series = ai_series[:ai_target]

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

    if job_id: _job_update(job_id, progress=72, stage="Finding fresh discoveries")
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
        add_recommendation_history(item["tmdb_id"], "Movie", item["title"], job_id, "AI", item.get("match_percentage"))
        add_recommendation_event(item["tmdb_id"], "Movie", item["title"], "shown", job_id, {"source":"AI","match":item.get("match_percentage")})

    for item in tmdb_movies:
        add_recommendation_history(item["tmdb_id"], "Movie", item["title"], job_id, "TMDB", item.get("match_percentage"))
        add_recommendation_event(item["tmdb_id"], "Movie", item["title"], "shown", job_id, {"source":"TMDB"})

    for item in ai_series:
        add_recommendation_history(item["tmdb_id"], "Series", item["title"], job_id, "AI", item.get("match_percentage"))
        add_recommendation_event(item["tmdb_id"], "Series", item["title"], "shown", job_id, {"source":"AI","match":item.get("match_percentage")})

    for item in tmdb_series:
        add_recommendation_history(item["tmdb_id"], "Series", item["title"], job_id, "TMDB", item.get("match_percentage"))
        add_recommendation_event(item["tmdb_id"], "Series", item["title"], "shown", job_id, {"source":"TMDB"})

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
    return app_redirect(request, "recommendations")


def _page_context(request: Request, **extra):
    context={"ingress_path":get_ingress_path(request)}
    context.update(extra)
    return context


@app.get("/search")
def search_page(request: Request):
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="search"))


@app.get("/watched")
def watched_page(request: Request):
    movies=get_all()
    watched_movies=[x for x in movies if x["type"]=="Movie"]
    watched_series=[x for x in movies if x["type"]=="Series"]
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="watched", movies=movies, watched_movies=watched_movies, watched_series=watched_series))


@app.get("/watchlist")
def watchlist_page(request: Request):
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="watchlist", items=[dict(x) for x in get_watchlist()]))


@app.get("/stats")
def stats_page(request: Request):
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="stats", analytics=get_analytics()))


def _taste_profile_data():
    rows=get_all(); genre_scores={}; people_scores={}; decade_scores={}
    for r in rows:
        rating=float(r["rating"] or 0); weight=max(0.1,rating/10)
        try: genres=json.loads(r["genres"] or "[]")
        except Exception: genres=[]
        for g in genres:
            name=g.get("name") if isinstance(g,dict) else str(g)
            if name: genre_scores[name]=genre_scores.get(name,0)+weight
        try: cast=json.loads(r["cast"] or "[]")
        except Exception: cast=[]
        for person in cast[:10]:
            name=person.get("name") if isinstance(person,dict) else str(person)
            if name: people_scores[name]=people_scores.get(name,0)+weight
        if r["year"]:
            name=str((int(r["year"])//10)*10)
            decade_scores[name]=decade_scores.get(name,0)+weight
    def top(d): return [{"name":k,"score":round(v,2)} for k,v in sorted(d.items(),key=lambda x:x[1],reverse=True)[:15]]
    return {"genres":top(genre_scores),"people":top(people_scores),"decades":top(decade_scores)}


@app.get("/taste")
def taste_page(request: Request):
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="taste", profile=_taste_profile_data()))


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="settings", settings={"recommendation_count":get_setting("recommendation_count",8),"avoid_recent":get_setting("avoid_recent",50),"diversity":get_setting("diversity",0.7),"discovery":get_setting("discovery",0.3)}, health=health(), saved=False))


@app.post("/settings/save")
def settings_save(request: Request, recommendation_count:int=Form(8), avoid_recent:int=Form(50), diversity:float=Form(0.7), discovery:float=Form(0.3)):
    set_setting("recommendation_count", max(4,min(20,int(recommendation_count))))
    set_setting("avoid_recent", max(0,min(500,int(avoid_recent))))
    set_setting("diversity", max(0,min(1,float(diversity))))
    set_setting("discovery", max(0,min(1,float(discovery))))
    return templates.TemplateResponse(request=request, name="app.html", context=_page_context(request, page="settings", settings={"recommendation_count":get_setting("recommendation_count",8),"avoid_recent":get_setting("avoid_recent",50),"diversity":get_setting("diversity",0.7),"discovery":get_setting("discovery",0.3)}, health=health(), saved=True))


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

        movies = movie_future.result()
        series = series_future.result()

        increment_lifetime_trending("Movie", [item.get("tmdb_id") for item in movies])
        increment_lifetime_trending("Series", [item.get("tmdb_id") for item in series])

        return {
            "movies": movies,
            "series": series,
        }


# ============================================================
# DISPLAY STATISTICS API
# ============================================================

@app.get("/api/lifetime-statistics")
def api_lifetime_statistics():
    return get_lifetime_statistics()


@app.get("/api/display-statistics")
def api_display_statistics():
    return get_display_statistics()


@app.post("/api/display-statistics")
def api_display_statistics_sync(
    ai_movies: int = Form(0),
    ai_series: int = Form(0),
    tmdb_movies: int = Form(0),
    tmdb_series: int = Form(0),
    trending_movies: int = Form(0),
    trending_series: int = Form(0),
):
    set_display_statistics(
        ai_movies=ai_movies,
        ai_series=ai_series,
        tmdb_movies=tmdb_movies,
        tmdb_series=tmdb_series,
        trending_movies=trending_movies,
        trending_series=trending_series,
    )
    return get_display_statistics()


# ============================================================
# LIVE SEARCH API
# ============================================================

@app.get("/api/search")
def api_search(
    q: str = "",
    media_type: str = "All",
    page: int = 1,
):
    """Search movies, series, or both. Page is delegated to TMDB for real pagination."""

    q = q.strip()
    page = max(1, min(int(page or 1), 500))

    if len(q) < 2:
        return {"results": [], "page": page, "total_pages": 1, "total_results": 0}

    if media_type == "All":
        with ThreadPoolExecutor(max_workers=2) as executor:
            movie_future = executor.submit(tmdb_live_search, q, "Movie", page)
            series_future = executor.submit(tmdb_live_search, q, "Series", page)
            movie_data = movie_future.result()
            series_data = series_future.result()
        movies = movie_data.get("results", [])
        series = series_data.get("results", [])
        for item in movies:
            item["media_type"] = "Movie"
        for item in series:
            item["media_type"] = "Series"
        # Interleave types so All does not feel movie-first on every page.
        results=[]
        for i in range(max(len(movies), len(series))):
            if i < len(movies): results.append(movies[i])
            if i < len(series): results.append(series[i])
        return {
            "results": results,
            "media_type": "All",
            "page": page,
            "total_pages": max(movie_data.get("total_pages",1), series_data.get("total_pages",1)),
            "total_results": movie_data.get("total_results",0) + series_data.get("total_results",0),
        }

    if media_type not in ("Movie", "Series"):
        return JSONResponse({"results": [], "error": "media_type must be All, Movie or Series"}, status_code=400)

    data = tmdb_live_search(q, media_type, page)
    results = data.get("results", [])
    for item in results:
        item["media_type"] = media_type
    return {
        "results": results,
        "media_type": media_type,
        "page": data.get("page", page),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", len(results)),
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

            tmdb_id=tmdb_data.get("tmdb_id"),
            genres=tmdb_data.get("genres"), cast=tmdb_data.get("cast"),
            director=tmdb_data.get("director"), creators=tmdb_data.get("creators"),
            keywords=tmdb_data.get("keywords"), runtime=tmdb_data.get("runtime"), status=tmdb_data.get("status"),
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
            genres=canonical.get("genres"), cast=canonical.get("cast"),
            director=canonical.get("director"), creators=canonical.get("creators"),
            keywords=canonical.get("keywords"), runtime=canonical.get("runtime"), status=canonical.get("status"),
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
    detail["is_watchlisted"] = is_watchlisted(tmdb_id, media_type)
    detail["is_watched"] = bool(watched_item)
    detail["user_rating"] = watched_item["rating"] if watched_item else None

    response = templates.TemplateResponse(
        request=request,
        name="app.html",
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
    item = {"tmdb_id":tmdb_id,"media_type":media_type,"title":title,"year":year,"poster":poster,"backdrop":backdrop,"overview":overview or "","vote_average":vote_average}
    if remove: remove_watchlist(tmdb_id, media_type)
    else: upsert_watchlist(item)

    return app_redirect(
        request,
        f"title/{media_type}/{tmdb_id}",
    )


@app.post("/watchlist/remove")
def watchlist_remove_page(request: Request, tmdb_id:int=Form(...), media_type:str=Form(...)):
    if media_type in ("Movie","Series"):
        remove_watchlist(tmdb_id, media_type)
    return app_redirect(request, "watchlist")


# ============================================================
# RECOMMENDATIONS
# ============================================================

@app.get("/recommendations")
def recommendations(request: Request, background_tasks: BackgroundTasks):
    global recommendation_state
    movies = get_all()
    watched_movies = [x for x in movies if x["type"] == "Movie"]
    watched_series = [x for x in movies if x["type"] == "Series"]
    with RECOMMENDATION_LOCK:
        state = dict(recommendation_state)
    if state["status"] == "ready" and state.get("data") is not None:
        data = state["data"]
        loading = False
        tmdb_discoveries = None
    else:
        tmdb_discoveries = state.get("tmdb_data")
        if tmdb_discoveries is None:
            tmdb_discoveries = tmdb_fallback(movies)
        if state["status"] in ("idle", "error"):
            job_id = str(uuid.uuid4())
            with RECOMMENDATION_LOCK:
                recommendation_state.update({"status":"loading","data":None,"error":None,"tmdb_data":tmdb_discoveries,"job_id":job_id,"progress":1,"stage":"Starting"})
            background_tasks.add_task(run_recommendation_job, tmdb_discoveries, job_id)
        data = None
        loading = True
    response = templates.TemplateResponse(request=request, name="app.html", context={
        "page": "recommendations",
        "movies": movies, "watched_movies": watched_movies, "watched_series": watched_series,
        "recommendations": data, "recommendations_loading": loading, "tmdb_discoveries": tmdb_discoveries,
        "display_statistics": get_display_statistics(), "lifetime_statistics": get_lifetime_statistics(),
        "ingress_path": get_ingress_path(request),
    })
    if state["status"] == "ready":
        with RECOMMENDATION_LOCK:
            recommendation_state.update({"status":"idle","data":None,"error":None,"tmdb_data":None,"progress":0,"stage":"Idle"})
    response.headers.update({"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0"})
    return response


# ============================================================
# RECOMMENDATION STATUS
# ============================================================

@app.get("/api/recommendations/status")
def recommendation_status():

    with RECOMMENDATION_LOCK:
        return {
            "status": recommendation_state["status"],
            "error": recommendation_state["error"],
            "job_id": recommendation_state.get("job_id"),
            "progress": recommendation_state.get("progress", 0),
            "stage": recommendation_state.get("stage", "Idle"),
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

            tmdb_id=tmdb_data.get("tmdb_id"),
            genres=tmdb_data.get("genres"), cast=tmdb_data.get("cast"),
            director=tmdb_data.get("director"), creators=tmdb_data.get("creators"),
            keywords=tmdb_data.get("keywords"), runtime=tmdb_data.get("runtime"), status=tmdb_data.get("status"),
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

    # AJAX requests get JSON so the browser stays on the current page.
    # Normal non-AJAX form submissions keep the old redirect behavior.
    accept = (request.headers.get("accept") or "").lower()

    if "application/json" in accept:
        return JSONResponse({
            "ok": True,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": title,
        })

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# AJAX NOT INTERESTED
# Same database action as the normal route, but returns JSON.
# ============================================================

@app.post("/api/recommendation/not-interested")
def api_recommendation_not_interested(
    title: str = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    if media_type not in ("Movie", "Series"):
        return JSONResponse(
            {
                "ok": False,
                "error": "Invalid media type.",
            },
            status_code=400,
        )

    add_not_interested(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
    )

    print(
        f"AJAX not interested: {title} "
        f"[{media_type}] TMDB={tmdb_id}"
    )

    return JSONResponse({
        "ok": True,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
    })


# ============================================================
# HEALTH / ANALYTICS / PERSONALIZATION API
# ============================================================

@app.get("/health")
def health():
    tmdb_ok = bool(get_env("TMDB_TOKEN"))
    gemini_ok = bool(get_env("GEMINI_API_KEY"))
    try:
        stats = get_analytics()
        db_ok = True
    except Exception:
        stats = {}
        db_ok = False
    return {"status":"healthy" if db_ok else "degraded","database":db_ok,"tmdb_configured":tmdb_ok,"gemini_configured":gemini_ok,"version":"1.9.0"}

@app.post("/api/recommendations/refresh")
def api_recommendations_refresh(background_tasks: BackgroundTasks):
    with RECOMMENDATION_LOCK:
        if recommendation_state.get("status") == "loading":
            return {"ok":False,"busy":True,"job_id":recommendation_state.get("job_id")}
        job_id=str(uuid.uuid4())
        recommendation_state.update({"status":"loading","data":None,"error":None,"tmdb_data":None,"job_id":job_id,"progress":1,"stage":"Starting"})
    background_tasks.add_task(run_recommendation_job, None, job_id)
    return {"ok":True,"job_id":job_id}

@app.get("/api/analytics")
def api_analytics(): return get_analytics()

@app.get("/api/watchlist")
def api_watchlist(media_type: str = ""):
    return {"items":[dict(x) for x in get_watchlist(media_type if media_type in ("Movie","Series") else None)]}

@app.get("/api/not-interested")
def api_not_interested(media_type: str = ""):
    return {"items":[dict(x) for x in get_not_interested(media_type if media_type in ("Movie","Series") else None)]}

@app.delete("/api/not-interested/{media_type}/{tmdb_id}")
def api_remove_not_interested(media_type: str, tmdb_id: int):
    if media_type not in ("Movie","Series"): return JSONResponse({"ok":False,"error":"Invalid media type"},status_code=400)
    remove_not_interested(tmdb_id, media_type); return {"ok":True}

@app.post("/api/recommendation/feedback")
def api_recommendation_feedback(tmdb_id: int = Form(...), media_type: str = Form(...), title: str = Form(...), event: str = Form(...)):
    allowed={"clicked","liked","disliked","dismissed","deep_dive"}
    if media_type not in ("Movie","Series") or event not in allowed: return JSONResponse({"ok":False,"error":"Invalid feedback"},status_code=400)
    add_recommendation_event(tmdb_id,media_type,title,event); return {"ok":True}

@app.get("/api/taste-profile")
def api_taste_profile():
    return _taste_profile_data()

@app.post("/api/settings")
def api_settings(payload: dict):
    for key,value in payload.items(): set_setting(key,value)
    return {"ok":True,"settings":payload}

@app.get("/api/settings")
def api_get_settings():
    return {"recommendation_count":get_setting("recommendation_count",8),"avoid_recent":get_setting("avoid_recent",50),"diversity":get_setting("diversity",0.7),"discovery":get_setting("discovery",0.3)}

@app.post("/api/series/progress")
def api_series_progress(tmdb_id:int=Form(...),season:int=Form(1),episode:int=Form(1),progress:float=Form(0),status:str=Form("Watching")):
    if status not in ("Not started","Watching","Completed","Paused","Dropped"): return JSONResponse({"ok":False,"error":"Invalid status"},status_code=400)
    set_series_progress(tmdb_id,season,episode,max(0,min(100,progress)),status); return {"ok":True}

@app.get("/api/series/progress/{tmdb_id}")
def api_get_series_progress(tmdb_id:int):
    row=get_series_progress(tmdb_id); return dict(row) if row else {"tmdb_id":tmdb_id,"status":"Not started","season":1,"episode":1,"progress":0}

# ============================================================
# DISCOVERY / DATA PORTABILITY
# ============================================================

@app.get("/api/surprise")
def api_surprise(media_type: str = "Movie"):
    if media_type not in ("Movie","Series"): media_type="Movie"
    pool=tmdb_fallback(get_all()).get("movies" if media_type=="Movie" else "series",[])
    if not pool: return {"ok":False,"error":"No discovery data available"}
    item=random.choice(pool[:20]); item["source"]="SURPRISE"; item["match_percentage"]=random.randint(70,92)
    return {"ok":True,"item":item}

@app.get("/api/movie-night")
def api_movie_night(mood: str = "balanced", runtime: str = "any"):
    pool=tmdb_fallback(get_all()).get("movies",[])
    if not pool:return {"ok":False,"error":"No movie data available"}
    if runtime == "short": pool=[x for x in pool if not x.get("runtime") or x.get("runtime")<=100] or pool
    elif runtime == "long": pool=[x for x in pool if not x.get("runtime") or x.get("runtime")>=130] or pool
    picks=sorted(pool,key=lambda x:(x.get("vote_average") or 0),reverse=True)[:12]
    random.shuffle(picks); return {"ok":True,"items":picks[:2],"mood":mood,"runtime":runtime}

@app.get("/api/providers/{media_type}/{tmdb_id}")
def api_providers(media_type:str,tmdb_id:int,country:str="MA"):
    if media_type not in ("Movie","Series"): return JSONResponse({"ok":False,"error":"Invalid media type"},status_code=400)
    token=get_env("TMDB_TOKEN")
    if not token:return {"ok":False,"providers":{}}
    endpoint=f"https://api.themoviedb.org/3/{'movie' if media_type=='Movie' else 'tv'}/{tmdb_id}/watch/providers"
    try:
        data=tmdb_request(endpoint,token,{"watch_region":country.upper()},ttl=21600)
        return {"ok":True,"country":country.upper(),"providers":(data.get("results") or {}).get(country.upper(),{})}
    except Exception as error:return {"ok":False,"error":str(error),"providers":{}}

@app.get("/api/export")
def api_export():
    payload={"version":2,"exported_at":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"watched":[dict(x) for x in get_all()],"watchlist":[dict(x) for x in get_watchlist()],"not_interested":[dict(x) for x in get_not_interested()],"settings":api_get_settings()}
    return Response(content=json.dumps(payload,ensure_ascii=False,indent=2),media_type="application/json",headers={"Content-Disposition":"attachment; filename=movie-ai-backup.json"})

@app.post("/api/import")
def api_import(payload:dict):
    imported=0
    for item in payload.get("watched",[]):
        if item.get("title") and item.get("type") in ("Movie","Series"):
            add_movie(item["title"],float(item.get("rating",0)),item["type"],item.get("poster"),item.get("backdrop"),item.get("year"),item.get("overview"),item.get("tmdb_id"),item.get("genres"),item.get("cast"),item.get("director"),item.get("creators"),item.get("keywords"),item.get("runtime"),item.get("status")); imported+=1
    for item in payload.get("watchlist",[]):
        if item.get("tmdb_id") and item.get("media_type") in ("Movie","Series"):upsert_watchlist(item)
    for item in payload.get("not_interested",[]):
        if item.get("tmdb_id") and item.get("media_type") in ("Movie","Series"):add_not_interested(item["tmdb_id"],item["media_type"],item.get("title", ""))
    return {"ok":True,"imported":imported}

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

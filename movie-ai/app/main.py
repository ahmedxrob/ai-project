import json
import os
import random
import time
import requests

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from fastapi import FastAPI, Request, Form
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

app = FastAPI(title="My Movie AI")

templates = Jinja2Templates(
    directory="app/templates"
)

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
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

# Gemini generates a large candidate pool.
AI_MOVIE_CANDIDATES = 30
AI_SERIES_CANDIDATES = 30

# Final number shown in EACH source section.
AI_MOVIES_TARGET = 8
AI_SERIES_TARGET = 8
TMDB_MOVIES_TARGET = 8
TMDB_SERIES_TARGET = 8

# Recent recommendations only.
# Old recommendations can become available again.
RECENT_HISTORY_LIMIT = 12


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(name, "").strip()


# ============================================================
# GEMINI RESPONSE SCHEMA
# ============================================================

class RecommendationItem(BaseModel):
    title: str
    year: Optional[int] = None
    reason: str


class RecommendationResponse(BaseModel):
    movies: List[RecommendationItem]
    series: List[RecommendationItem]


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

The application will verify all titles with TMDB.

RULES:

1. Never recommend a watched title.
2. Never recommend a recently recommended title.
3. Never recommend a NOT INTERESTED title.
4. Movies and series are completely separate.
5. Never put a movie in the series list.
6. Never put a series in the movie list.
7. Strongly prioritize titles rated 8/10 or higher.
8. Use lower ratings as negative taste signals.
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
16. Every title must be a real existing title.
17. Every title needs a personalized reason.
18. Reasons should explain why THIS user may like it.
19. Return structured data only.

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

                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RecommendationResponse,
                        temperature=1.2,
                    ),
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
                            RecommendationResponse.model_validate(
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
                        RecommendationResponse.model_validate_json(
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
                    or "too many requests"
                    in error_text
                    or "overloaded" in error_text
                    or "internal server error"
                    in error_text
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
            result.get("id"),

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
    }


# ============================================================
# TMDB SEARCH
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

            if requested == normalized:

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
# VERIFY GEMINI
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
        "Recommended because it matches your watched titles and ratings."
    )

    result["source"] = "AI"

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

                    movies.append(result)

                else:

                    series.append(result)

            except Exception as error:

                print(
                    f"TMDB verification error: "
                    f"{error}"
                )

    return (
        unique_results(movies),
        unique_results(series),
    )


# ============================================================
# UNIQUE
# ============================================================

def unique_results(
    results,
):

    seen = set()

    output = []

    for item in results:

        key = (
            item.get("media_type"),
            item.get("tmdb_id"),
        )

        if not item.get(
            "tmdb_id"
        ):
            continue

        if key in seen:
            continue

        seen.add(key)

        output.append(item)

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

        seen.add(tmdb_id)

        output.append(item)

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
                f"TMDB discovery error: "
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

                result = (
                    normalise_tmdb_result(
                        item,
                        media_type,
                    )
                )

                result["source"] = "TMDB"

                result["reason"] = (
                    "TMDB discovery."
                )

                if media_type == "Movie":
                    movies.append(result)
                else:
                    series.append(result)

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
        "movies": movies,
        "series": series,
    }


# ============================================================
# GENERATE RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    watched,
):

    print(
        "Starting separate AI + TMDB "
        "recommendation engine..."
    )

    # ========================================================
    # BLOCK LIST
    # ========================================================

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

    # ========================================================
    # GEMINI
    # ========================================================

    ai_result = (
        get_ai_recommendations(
            watched
        )
    )

    # ========================================================
    # DEFAULT EMPTY GROUPS
    # ========================================================

    ai_movies = []
    ai_series = []

    tmdb_movies = []
    tmdb_series = []

    # ========================================================
    # AI SUCCESS
    # ========================================================

    if ai_result:

        print(
            "Gemini succeeded."
        )

        print(
            "Verifying AI candidates with TMDB..."
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

        # Keep exactly up to 8 AI picks.
        ai_movies = ai_movies[
            :AI_MOVIES_TARGET
        ]

        ai_series = ai_series[
            :AI_SERIES_TARGET
        ]

        print(
            f"AI selected: "
            f"{len(ai_movies)} movies, "
            f"{len(ai_series)} series"
        )

    else:

        print(
            "Gemini unavailable."
        )

    # ========================================================
    # TMDB SECTION
    #
    # TMDB supplies a separate 8-card section.
    # ========================================================

    tmdb_results = tmdb_fallback(
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

    print(
        f"TMDB selected: "
        f"{len(tmdb_movies)} movies, "
        f"{len(tmdb_series)} series"
    )

    # ========================================================
    # SAVE HISTORY
    #
    # Save both AI and TMDB results because both have been
    # shown to the user.
    # ========================================================

    for item in ai_movies:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],

            media_type="Movie",

            title=item[
                "title"
            ],
        )

    for item in tmdb_movies:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],

            media_type="Movie",

            title=item[
                "title"
            ],
        )

    for item in ai_series:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],

            media_type="Series",

            title=item[
                "title"
            ],
        )

    for item in tmdb_series:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],

            media_type="Series",

            title=item[
                "title"
            ],
        )

    # ========================================================
    # RETURN FOUR SEPARATE GROUPS
    # ========================================================

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
# ============================================================

@app.get("/")
@app.get("//")
def home(
    request: Request,
):

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

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies":
                movies,

            "watched_movies":
                watched_movies,

            "watched_series":
                watched_series,

            "recommendations":
                None,
        },
    )

    response.headers[
        "Cache-Control"
    ] = (
        "no-store, no-cache, "
        "must-revalidate, max-age=0"
    )

    response.headers[
        "Pragma"
    ] = "no-cache"

    response.headers[
        "Expires"
    ] = "0"

    return response


# ============================================================
# ADD WATCHED
# ============================================================

@app.post("/add")
def add(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
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

        return RedirectResponse(
            "/",
            status_code=303,
        )

    tmdb_data = tmdb_search(
        title,
        media_type,
    )

    if tmdb_data:

        add_movie(
            title=tmdb_data[
                "title"
            ],

            rating=rating,

            media_type=media_type,

            poster=tmdb_data.get(
                "poster"
            ),

            backdrop=tmdb_data.get(
                "backdrop"
            ),

            year=tmdb_data.get(
                "year"
            ),

            overview=tmdb_data.get(
                "overview"
            ),

            tmdb_id=tmdb_data.get(
                "tmdb_id"
            ),
        )

    else:

        add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
        )

    return RedirectResponse(
        "/",
        status_code=303,
    )


# ============================================================
# RECOMMENDATIONS PAGE
# ============================================================

@app.get("/recommendations")
def recommendations(
    request: Request,
):

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

    recommendations_data = (
        generate_recommendations(
            movies
        )
    )

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies":
                movies,

            "watched_movies":
                watched_movies,

            "watched_series":
                watched_series,

            "recommendations":
                recommendations_data,
        },
    )

    response.headers[
        "Cache-Control"
    ] = (
        "no-store, no-cache, "
        "must-revalidate, max-age=0"
    )

    response.headers[
        "Pragma"
    ] = "no-cache"

    response.headers[
        "Expires"
    ] = "0"

    return response


# ============================================================
# MARK RECOMMENDATION AS WATCHED
# ============================================================

@app.post(
    "/recommendation/watched"
)
def recommendation_watched(
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
                f"TMDB watched lookup error: "
                f"{error}"
            )

    if tmdb_data:

        add_movie(
            title=tmdb_data[
                "title"
            ],

            rating=rating,

            media_type=media_type,

            poster=tmdb_data.get(
                "poster"
            ),

            backdrop=tmdb_data.get(
                "backdrop"
            ),

            year=tmdb_data.get(
                "year"
            ),

            overview=tmdb_data.get(
                "overview"
            ),

            tmdb_id=tmdb_data.get(
                "tmdb_id"
            ),
        )

    else:

        add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )

    return RedirectResponse(
        "/recommendations",
        status_code=303,
    )


# ============================================================
# NOT INTERESTED
# ============================================================

@app.post(
    "/recommendation/not-interested"
)
def recommendation_not_interested(
    title: str = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    add_not_interested(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
    )

    print(
        f"Not interested: "
        f"{title} "
        f"[{media_type}] "
        f"TMDB={tmdb_id}"
    )

    return RedirectResponse(
        "/recommendations",
        status_code=303,
    )


# ============================================================
# DELETE
# ============================================================

@app.post(
    "/delete/{movie_id}"
)
def delete(
    movie_id: int,
):

    delete_movie(
        movie_id
    )

    return RedirectResponse(
        "/",
        status_code=303,
    )


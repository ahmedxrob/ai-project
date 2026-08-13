import json
import os
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

MOVIE_COUNT = 12
SERIES_COUNT = 12

RECENT_HISTORY_LIMIT = 50


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

    prompt = f"""
You are the personalized recommendation engine
for a private movie and TV library.

The user personally rated the titles below.

Use the highest-rated titles as the strongest
signals for understanding their taste.

Return exactly:
- {MOVIE_COUNT} movies
- {SERIES_COUNT} TV series

RULES:

1. Never recommend something already watched.
2. Never recommend a recently recommended title.
3. Movies and TV series are separate media types.
4. Never classify a movie as a series.
5. Never classify a series as a movie.
6. Strongly weight highly rated titles.
7. Consider:
   - genre
   - tone
   - themes
   - story style
   - pacing
   - actors
   - directors
   - audience reception
8. Include obvious matches and interesting discoveries.
9. Avoid filling all results with one franchise,
   actor, director, genre or decade.
10. Every title must be a real existing movie or TV series.
11. Reasons should be short and personalized.
12. Return structured data only.

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

Return only the requested structured result.
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
                    or "internal server error"
                    in error_text
                )

                if temporary_error:

                    time.sleep(
                        1.5
                        * (
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
        "query": title,
        "language": "en-US",
        "include_adult": "false",
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

            result_normalized = (
                result_title
                .strip()
                .lower()
            )

            if (
                requested
                == result_normalized
            ):

                score = 100

            else:

                ratio = fuzz.ratio(
                    requested,
                    result_normalized,
                )

                token_score = fuzz.token_sort_ratio(
                    requested,
                    result_normalized,
                )

                score = max(
                    ratio,
                    token_score * 0.95,
                )

            # Year bonus
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

            print(
                f"TMDB match rejected: "
                f"{title} "
                f"(score={best_score:.1f})"
            )

            return None

        tmdb_id = best.get(
            "id"
        )

        if not tmdb_id:
            return None

        return normalise_tmdb_result(
            best,
            media_type,
        )

    except Exception as error:

        print(
            f"TMDB search error "
            f"for '{title}': "
            f"{error}"
        )

        return None


# ============================================================
# NORMALISE TMDB RESULT
#
# THIS FUNCTION WAS MISSING IN YOUR CODE.
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

    poster_path = result.get(
        "poster_path"
    )

    if poster_path:

        poster = (
            "https://image.tmdb.org/t/p/w500"
            + poster_path
        )

    backdrop = None

    backdrop_path = result.get(
        "backdrop_path"
    )

    if backdrop_path:

        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + backdrop_path
        )

    return {
        "media_type":
            media_type,

        "tmdb_id":
            result.get("id"),

        # ALWAYS canonical TMDB title.
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
# VERIFY GEMINI RECOMMENDATION
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
    )

    return result


# ============================================================
# VERIFY ALL
# ============================================================

def verify_all_recommendations(
    ai_result,
):

    movies = []
    series = []

    jobs = []

    with ThreadPoolExecutor(
        max_workers=12
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
                    f"TMDB verification error: "
                    f"{error}"
                )

    return (
        unique_results(
            movies
        ),
        unique_results(
            series
        ),
    )


# ============================================================
# UNIQUE RESULTS
# ============================================================

def unique_results(
    results,
):

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
# TMDB FALLBACK
# ============================================================

def tmdb_fallback(
    watched,
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
                    "language": "en-US",
                    "page": page,
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
                f"Fallback discovery error: "
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
        ("Series", 1),
        ("Series", 2),
        ("Series", 3),
    ]

    movies = []
    series = []

    with ThreadPoolExecutor(
        max_workers=6
    ) as executor:

        futures = [
            executor.submit(
                discover,
                media_type,
                page,
            )
            for media_type, page
            in jobs
        ]

        for future in futures:

            media_type, items = (
                future.result()
            )

            blocked = (
                watched_movie_ids
                if media_type == "Movie"
                else watched_series_ids
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

                if media_type == "Movie":

                    movies.append(
                        result
                    )

                else:

                    series.append(
                        result
                    )

    return {
        "movies":
            unique_results(
                movies
            )[:MOVIE_COUNT],

        "series":
            unique_results(
                series
            )[:SERIES_COUNT],
    }


# ============================================================
# GENERATE RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    watched,
):

    print(
        "Starting ONE-request AI "
        "recommendation engine..."
    )

    ai_result = (
        get_ai_recommendations(
            watched
        )
    )

    if ai_result:

        print(
            "Gemini succeeded."
        )

        print(
            "Verifying AI recommendations "
            "with concurrent TMDB searches..."
        )

        movies, series = (
            verify_all_recommendations(
                ai_result
            )
        )

        print(
            f"Verified results: "
            f"{len(movies)} movies, "
            f"{len(series)} series"
        )

        for item in movies:

            add_recommendation_history(
                tmdb_id=item[
                    "tmdb_id"
                ],
                media_type="Movie",
                title=item[
                    "title"
                ],
            )

        for item in series:

            add_recommendation_history(
                tmdb_id=item[
                    "tmdb_id"
                ],
                media_type="Series",
                title=item[
                    "title"
                ],
            )

        if movies or series:

            return {
                "movies":
                    movies[:MOVIE_COUNT],

                "series":
                    series[:SERIES_COUNT],

                "source":
                    "Gemini",
            }

    print(
        "Gemini unavailable."
    )

    print(
        "Using TMDB fallback..."
    )

    fallback = tmdb_fallback(
        watched
    )

    for item in fallback[
        "movies"
    ]:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],
            media_type="Movie",
            title=item[
                "title"
            ],
        )

    for item in fallback[
        "series"
    ]:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],
            media_type="Series",
            title=item[
                "title"
            ],
        )

    return {
        "movies":
            fallback["movies"],

        "series":
            fallback["series"],

        "source":
            "TMDB fallback",
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

    response = (
        templates.TemplateResponse(
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

    print(
        f"Adding watched: "
        f"{title} [{media_type}] "
        f"rating={rating}"
    )

    tmdb_data = search_tmdb(
        title,
        media_type,
    )

    if tmdb_data:

        print(
            f"Saving TMDB title: "
            f"{tmdb_data['title']}"
        )

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

        print(
            f"TMDB match not found "
            f"for '{title}'"
        )

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
# RECOMMENDATIONS
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

    response = (
        templates.TemplateResponse(
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

    title = title.strip()

    if (
        not title
        or not 0 <= rating <= 10
        or media_type not in (
            "Movie",
            "Series",
        )
        or not tmdb_id
    ):

        return RedirectResponse(
            "/",
            status_code=303,
        )

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

            # IMPORTANT:
            # This now definitely exists.
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

        print(
            f"Saving watched TMDB item: "
            f"{tmdb_data['title']} "
            f"[{media_type}] "
            f"ID={tmdb_data['tmdb_id']}"
        )

        add_movie(
            # Canonical TMDB title
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

        print(
            f"Could not retrieve TMDB "
            f"details for {title}. "
            f"Saving basic record."
        )

        add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )

    # Immediately generate another batch.
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


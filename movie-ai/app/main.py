import json
import os
import random
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel
from typing import List

from rapidfuzz import fuzz
from google import genai

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

GEMINI_MODEL = "gemini-3.6-flash"

MOVIE_COUNT = 12
SERIES_COUNT = 12

RECENT_HISTORY_LIMIT = 50

CURRENT_YEAR = datetime.now().year


# ============================================================
# ENV
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(name, "").strip()


# ============================================================
# GEMINI RESPONSE SCHEMA
# ============================================================

class RecommendationItem(BaseModel):
    title: str
    year: int | None = None
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
# BUILD USER PROFILE
# ============================================================

def build_user_profile(watched):

    movies = [
        {
            "title": movie["title"],
            "rating": float(movie["rating"]),
            "year": movie["year"],
        }
        for movie in watched
        if movie["type"] == "Movie"
    ]

    series = [
        {
            "title": movie["title"],
            "rating": float(movie["rating"]),
            "year": movie["year"],
        }
        for movie in watched
        if movie["type"] == "Series"
    ]

    # Highest-rated titles first.
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

    watched_movie_titles = {
        movie["title"].strip().lower()
        for movie in watched
        if movie["type"] == "Movie"
    }

    watched_series_titles = {
        movie["title"].strip().lower()
        for movie in watched
        if movie["type"] == "Series"
    }

    random_nonce = random.randint(
        100000,
        999999999,
    )

    now = datetime.now().isoformat()

    prompt = f"""
You are the recommendation engine for a personal movie and TV library.

IMPORTANT:
This is recommendation generation, not a generic popular-content list.

The user has personally rated the titles below.
Learn their taste primarily from HIGH ratings.

Return exactly:
- {MOVIE_COUNT} movies
- {SERIES_COUNT} TV series

Do NOT return anything the user already watched.

Do NOT repeat titles from the user's recently recommended history.

Prefer:
- strong similarity to their highest-rated titles
- similar themes and storytelling
- compatible genres
- similar tone
- directors or actors they repeatedly enjoyed
- highly regarded titles
- a mixture of obvious matches and interesting discoveries

Do not make every recommendation from the same franchise,
director, actor, genre, or decade.

VERY IMPORTANT:
Movies and TV series are separate media types.
A movie title must not be returned as a series unless that is
actually a TV series.
A series must not be returned as a movie.

Each reason should be short, specific and based on the user's ratings.

The title must be a real movie or real TV series.

CURRENT TIME:
{now}

RANDOMIZATION NONCE:
{random_nonce}

WATCHED MOVIES:
{json.dumps(profile["movies"], ensure_ascii=False)}

WATCHED SERIES:
{json.dumps(profile["series"], ensure_ascii=False)}

WATCHED MOVIE TITLES:
{json.dumps(sorted(watched_movie_titles), ensure_ascii=False)}

WATCHED SERIES TITLES:
{json.dumps(sorted(watched_series_titles), ensure_ascii=False)}

RECENT MOVIE RECOMMENDATION IDs:
{json.dumps(recent_movie_ids)}

RECENT SERIES RECOMMENDATION IDs:
{json.dumps(recent_series_ids)}

Return ONLY the structured response.
"""

    return prompt


# ============================================================
# ONE GEMINI REQUEST
# ============================================================

def get_ai_recommendations(watched):

    client = get_gemini_client()

    if client is None:

        print(
            "Gemini API key is not configured"
        )

        return None

    print(
        "Sending ONE recommendation request "
        "to Gemini..."
    )

    try:

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=build_gemini_prompt(
                watched
            ),
            config={
                "response_format": {
                    "text": {
                        "mime_type": "application/json",
                        "schema": (
                            RecommendationResponse
                            .model_json_schema()
                        ),
                    }
                }
            },
        )

        if not response.text:
            return None

        data = json.loads(
            response.text
        )

        parsed = (
            RecommendationResponse.model_validate(
                data
            )
        )

        print(
            f"Gemini returned "
            f"{len(parsed.movies)} movies "
            f"and "
            f"{len(parsed.series)} series"
        )

        return parsed

    except Exception as error:

        print(
            f"Gemini recommendation error: "
            f"{error}"
        )

        return None


# ============================================================
# TMDB
# ============================================================

def tmdb_request(
    endpoint,
    token,
    params=None,
):
    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }

    response = requests.get(
        endpoint,
        headers=headers,
        params=params or {},
        timeout=12,
    )

    response.raise_for_status()

    return response.json()


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
            params["first_air_date_year"] = year

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

        requested = title.lower().strip()

        best = None
        best_score = -1

        for result in results[:10]:

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

            score = fuzz.ratio(
                requested,
                result_title.lower().strip(),
            )

            if year and date:

                try:

                    result_year = int(
                        date[:4]
                    )

                    if result_year == year:
                        score += 15

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

        # Don't accept a completely unrelated TMDB result.
        if best_score < 55:
            return None

        if media_type == "Movie":

            matched_title = best.get(
                "title",
                title,
            )

            date = best.get(
                "release_date",
                "",
            )

            tmdb_url = (
                f"https://www.themoviedb.org/movie/"
                f"{best.get('id')}"
            )

        else:

            matched_title = best.get(
                "name",
                title,
            )

            date = best.get(
                "first_air_date",
                "",
            )

            tmdb_url = (
                f"https://www.themoviedb.org/tv/"
                f"{best.get('id')}"
            )

        result_year = None

        if date:

            try:
                result_year = int(
                    date[:4]
                )
            except (
                ValueError,
                TypeError,
            ):
                pass

        poster = None

        if best.get(
            "poster_path"
        ):

            poster = (
                "https://image.tmdb.org/t/p/w500"
                + best["poster_path"]
            )

        backdrop = None

        if best.get(
            "backdrop_path"
        ):

            backdrop = (
                "https://image.tmdb.org/t/p/w1280"
                + best["backdrop_path"]
            )

        return {
            "media_type": media_type,
            "tmdb_id": best.get(
                "id"
            ),
            "title": matched_title,
            "year": result_year,
            "poster": poster,
            "backdrop": backdrop,
            "overview": (
                best.get(
                    "overview"
                )
                or ""
            ),
            "vote_average": float(
                best.get(
                    "vote_average",
                    0,
                )
                or 0
            ),
            "vote_count": int(
                best.get(
                    "vote_count",
                    0,
                )
                or 0
            ),
            "tmdb_url": tmdb_url,
        }

    except Exception as error:

        print(
            f"TMDB search error for "
            f"{title}: {error}"
        )

        return None


# ============================================================
# CONCURRENT TMDB VERIFICATION
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


def verify_all_recommendations(
    ai_result,
):
    verified_movies = []
    verified_series = []

    tasks = []

    with ThreadPoolExecutor(
        max_workers=12
    ) as executor:

        for item in ai_result.movies:

            tasks.append(
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

            tasks.append(
                (
                    "Series",
                    executor.submit(
                        verify_recommendation,
                        item,
                        "Series",
                    ),
                )
            )

        for media_type, future in tasks:

            try:

                result = future.result()

                if not result:
                    continue

                if media_type == "Movie":

                    verified_movies.append(
                        result
                    )

                else:

                    verified_series.append(
                        result
                    )

            except Exception as error:

                print(
                    f"Verification error: "
                    f"{error}"
                )

    # Remove duplicate TMDB IDs.
    verified_movies = unique_results(
        verified_movies
    )

    verified_series = unique_results(
        verified_series
    )

    return (
        verified_movies,
        verified_series,
    )


# ============================================================
# UNIQUE RESULTS
# ============================================================

def unique_results(
    results
):
    seen = set()

    output = []

    for item in results:

        tmdb_id = item.get(
            "tmdb_id"
        )

        media_type = item.get(
            "media_type"
        )

        key = (
            media_type,
            tmdb_id,
        )

        if not tmdb_id:
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
# FALLBACK
# ============================================================

def tmdb_fallback(
    watched,
):
    """
    Lightweight fallback if Gemini is unavailable.

    Only a few concurrent TMDB searches are needed.
    This is deliberately much smaller than the old
    100+ request recommendation engine.
    """

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:
        return {
            "movies": [],
            "series": [],
        }

    watched_movie_ids = {
        movie["tmdb_id"]
        for movie in watched
        if (
            movie["type"] == "Movie"
            and movie["tmdb_id"]
        )
    }

    watched_series_ids = {
        movie["tmdb_id"]
        for movie in watched
        if (
            movie["type"] == "Series"
            and movie["tmdb_id"]
        )
    }

    result = {
        "movies": [],
        "series": [],
    }

    def discover_one(
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
                    "sort_by": "popularity.desc",
                    "include_adult": "false",
                    "vote_count.gte": 50,
                },
            )

            return media_type, data.get(
                "results",
                [],
            )

        except Exception:
            return media_type, []

    pages = [
        ("Movie", random.randint(1, 10)),
        ("Movie", random.randint(1, 10)),
        ("Series", random.randint(1, 10)),
        ("Series", random.randint(1, 10)),
    ]

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        futures = [
            executor.submit(
                discover_one,
                media_type,
                page,
            )
            for media_type, page in pages
        ]

        for future in futures:

            media_type, items = (
                future.result()
            )

            if media_type == "Movie":
                blocked = watched_movie_ids
            else:
                blocked = watched_series_ids

            for item in items:

                tmdb_id = item.get(
                    "id"
                )

                if not tmdb_id:
                    continue

                if tmdb_id in blocked:
                    continue

                result_item = (
                    normalise_discovery_result(
                        item,
                        media_type,
                    )
                )

                if not result_item:
                    continue

                result[
                    "movies"
                    if media_type == "Movie"
                    else "series"
                ].append(
                    result_item
                )

    result["movies"] = unique_results(
        result["movies"]
    )[:MOVIE_COUNT]

    result["series"] = unique_results(
        result["series"]
    )[:SERIES_COUNT]

    return result


def normalise_discovery_result(
    item,
    media_type,
):
    if media_type == "Movie":

        title = item.get(
            "title",
            "",
        )

        date = item.get(
            "release_date",
            "",
        )

        url = (
            f"https://www.themoviedb.org/movie/"
            f"{item.get('id')}"
        )

    else:

        title = item.get(
            "name",
            "",
        )

        date = item.get(
            "first_air_date",
            "",
        )

        url = (
            f"https://www.themoviedb.org/tv/"
            f"{item.get('id')}"
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

    if item.get(
        "poster_path"
    ):

        poster = (
            "https://image.tmdb.org/t/p/w500"
            + item["poster_path"]
        )

    backdrop = None

    if item.get(
        "backdrop_path"
    ):

        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + item["backdrop_path"]
        )

    return {
        "media_type": media_type,
        "tmdb_id": item.get(
            "id"
        ),
        "title": title,
        "year": year,
        "poster": poster,
        "backdrop": backdrop,
        "overview": (
            item.get(
                "overview"
            )
            or ""
        ),
        "vote_average": float(
            item.get(
                "vote_average",
                0,
            )
            or 0
        ),
        "vote_count": int(
            item.get(
                "vote_count",
                0,
            )
            or 0
        ),
        "tmdb_url": url,
        "reason": (
            "Recommended from TMDB popular "
            "content as a fallback."
        ),
    }


# ============================================================
# FINAL RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    watched,
):
    print(
        "Starting ONE-request AI "
        "recommendation engine..."
    )

    ai_result = get_ai_recommendations(
        watched
    )

    if ai_result:

        print(
            "Gemini succeeded. "
            "Verifying recommendations "
            "with TMDB concurrently..."
        )

        movies, series = (
            verify_all_recommendations(
                ai_result
            )
        )

        # Save only verified items.
        for item in movies:

            add_recommendation_history(
                tmdb_id=item["tmdb_id"],
                media_type="Movie",
                title=item["title"],
            )

        for item in series:

            add_recommendation_history(
                tmdb_id=item["tmdb_id"],
                media_type="Series",
                title=item["title"],
            )

        # If Gemini returned enough valid results,
        # use them directly.
        if len(movies) > 0 or len(series) > 0:

            return {
                "movies": movies[:MOVIE_COUNT],
                "series": series[:SERIES_COUNT],
                "source": "Gemini",
            }

    print(
        "Gemini unavailable. "
        "Using TMDB fallback."
    )

    fallback = tmdb_fallback(
        watched
    )

    for item in fallback["movies"]:

        add_recommendation_history(
            tmdb_id=item["tmdb_id"],
            media_type="Movie",
            title=item["title"],
        )

    for item in fallback["series"]:

        add_recommendation_history(
            tmdb_id=item["tmdb_id"],
            media_type="Series",
            title=item["title"],
        )

    return {
        "movies": fallback["movies"],
        "series": fallback["series"],
        "source": "TMDB fallback",
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
        movie
        for movie in movies
        if movie["type"] == "Movie"
    ]

    watched_series = [
        movie
        for movie in movies
        if movie["type"] == "Series"
    ]

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": movies,
            "watched_movies": watched_movies,
            "watched_series": watched_series,
            "recommendations": None,
        },
    )

    response.headers[
        "Cache-Control"
    ] = (
        "no-store, no-cache, "
        "must-revalidate, max-age=0"
    )

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

    tmdb_data = search_tmdb(
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
# RECOMMENDATIONS
# ============================================================

@app.get("/recommendations")
def recommendations(
    request: Request,
):
    movies = get_all()

    watched_movies = [
        movie
        for movie in movies
        if movie["type"] == "Movie"
    ]

    watched_series = [
        movie
        for movie in movies
        if movie["type"] == "Series"
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
            "movies": movies,
            "watched_movies": watched_movies,
            "watched_series": watched_series,
            "recommendations": recommendations_data,
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
# MARK AS WATCHED
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
                    "language": "en-US",
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

    # Immediately generate a fresh batch.
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


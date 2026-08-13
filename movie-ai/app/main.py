import json
import os
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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

GEMINI_MODEL = "gemini-3.6-flash"

MOVIE_COUNT = 12
SERIES_COUNT = 12

RECENT_HISTORY_LIMIT = 50


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(name, "").strip()


# ============================================================
# GEMINI STRUCTURED RESPONSE
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
    api_key = get_env("GEMINI_API_KEY")

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
            "rating": float(item["rating"]),
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

Your job is to recommend things this specific user
is likely to enjoy.

Return EXACTLY:
- {MOVIE_COUNT} movies
- {SERIES_COUNT} TV series

IMPORTANT RULES:

1. Never recommend something already watched.
2. Never recommend a recently recommended title.
3. Movies and TV series are separate types.
4. Do not turn a movie into a series.
5. Do not turn a TV series into a movie.
6. Prefer the user's HIGHLY rated titles as the strongest signal.
7. Use patterns in ratings, genres, themes, tone,
   storytelling style, actors and directors when useful.
8. Include a mixture of:
   - very strong matches
   - less obvious discoveries
9. Do not give 12 titles from the same director,
   franchise, genre or actor.
10. Every title must be a real existing movie or TV series.
11. Reasons must be short and personalized.
12. Do not return explanations outside the structured result.

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

ALREADY WATCHED MOVIE TITLES:

{json.dumps(
    watched_movie_titles,
    ensure_ascii=False
)}

ALREADY WATCHED SERIES TITLES:

{json.dumps(
    watched_series_titles,
    ensure_ascii=False
)}

RECENT MOVIE RECOMMENDATION IDS:

{json.dumps(recent_movie_ids)}

RECENT SERIES RECOMMENDATION IDS:

{json.dumps(recent_series_ids)}

Return exactly the requested number of movies
and series whenever possible.
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
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RecommendationResponse,
            ),
        )

        # Current SDK can return the typed
        # parsed object when a Pydantic schema
        # is supplied.
        if getattr(
            response,
            "parsed",
            None,
        ) is not None:

            parsed = response.parsed

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
                    "Gemini returned empty response"
                )
                return None

            result = (
                RecommendationResponse.model_validate_json(
                    response.text
                )
            )

        print(
            f"Gemini returned "
            f"{len(result.movies)} movies "
            f"and "
            f"{len(result.series)} series"
        )

        return result

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

            # Prefer matching year.
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

        # Reject clearly unrelated results.
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
# VERIFY ONE GEMINI RESULT
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

    result["reason"] = item.reason

    return result


# ============================================================
# VERIFY ALL 24 RESULTS CONCURRENTLY
# ============================================================

def verify_all_recommendations(
    ai_result,
):
    movies = []
    series = []

    futures = []

    with ThreadPoolExecutor(
        max_workers=12
    ) as executor:

        for item in ai_result.movies:

            futures.append(
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

            futures.append(
                (
                    "Series",
                    executor.submit(
                        verify_recommendation,
                        item,
                        "Series",
                    ),
                )
            )

        for media_type, future in futures:

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
        unique_results(
            movies
        ),
        unique_results(
            series
        ),
    )


# ============================================================
# REMOVE DUPLICATES
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

        if not item.get("tmdb_id"):
            continue

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

    return output


# ============================================================
# FALLBACK
# ============================================================

def tmdb_fallback(
    watched,
):
    """
    Small fallback if Gemini is unavailable.
    Much faster than the previous recommendation engine.
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
                    "sort_by": "popularity.desc",
                    "include_adult": "false",
                    "vote_count.gte": 50,
                },
            )

            return (
                media_type,
                data.get(
                    "results",
                    [],
                ),
            )

        except Exception:
            return media_type, []

    jobs = [
        (
            "Movie",
            1,
        ),
        (
            "Movie",
            2,
        ),
        (
            "Movie",
            3,
        ),
        (
            "Series",
            1,
        ),
        (
            "Series",
            2,
        ),
        (
            "Series",
            3,
        ),
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
            for media_type, page in jobs
        ]

        for future in as_completed(
            futures
        ):

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
                        f"{tmdb_id}"
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
                        f"{tmdb_id}"
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

                result = {
                    "media_type": media_type,
                    "tmdb_id": tmdb_id,
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
                        "TMDB fallback recommendation."
                    ),
                }

                if media_type == "Movie":
                    movies.append(result)
                else:
                    series.append(result)

    movies = unique_results(
        movies
    )[:MOVIE_COUNT]

    series = unique_results(
        series
    )[:SERIES_COUNT]

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
        "Starting ONE-request AI "
        "recommendation engine..."
    )

    ai_result = get_ai_recommendations(
        watched
    )

    if ai_result:

        print(
            "Gemini succeeded."
        )

        print(
            "Verifying recommendations "
            "with concurrent TMDB lookups..."
        )

        movies, series = (
            verify_all_recommendations(
                ai_result
            )
        )

        # Save only verified results.
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

        print(
            f"Verified: "
            f"{len(movies)} movies, "
            f"{len(series)} series"
        )

        if movies or series:

            return {
                "movies": movies[:MOVIE_COUNT],
                "series": series[:SERIES_COUNT],
                "source": "Gemini",
            }

    print(
        "Gemini unavailable. "
        "Using TMDB fallback..."
    )

    fallback = tmdb_fallback(
        watched
    )

    for item in fallback["movies"]:

        add_recommendation_history(
            tmdb_id=item[
                "tmdb_id"
            ],
            media_type="Movie",
            title=item[
                "title"
            ],
        )

    for item in fallback["series"]:

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

import os
import json
import requests

from rapidfuzz import fuzz

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.database import (
    init_database,
    get_all,
    add_movie,
    delete_movie,
    movie_exists,
)


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
# RECENT RECOMMENDATIONS
# ============================================================

recent_recommendations = {
    "Movie": [],
    "Series": [],
}


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(name, "").strip()


# ============================================================
# TMDB
# ============================================================

def tmdb_search(
    query: str,
    media_type: str,
    token: str,
):
    if media_type == "Movie":
        endpoint = (
            "https://api.themoviedb.org/3/search/movie"
        )
    else:
        endpoint = (
            "https://api.themoviedb.org/3/search/tv"
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }

    params = {
        "query": query,
        "language": "en-US",
        "include_adult": "false",
    }

    response = requests.get(
        endpoint,
        headers=headers,
        params=params,
        timeout=10,
    )

    response.raise_for_status()

    return response.json().get(
        "results",
        [],
    )


def tmdb_get_by_id(
    tmdb_id: int,
    media_type: str,
    token: str,
):
    if media_type == "Movie":
        endpoint = (
            f"https://api.themoviedb.org/3/movie/{tmdb_id}"
        )
    else:
        endpoint = (
            f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }

    params = {
        "language": "en-US",
    }

    response = requests.get(
        endpoint,
        headers=headers,
        params=params,
        timeout=10,
    )

    response.raise_for_status()

    return response.json()


def get_spelling_suggestions(title: str):

    try:
        response = requests.get(
            "https://api.datamuse.com/words",
            params={
                "sp": title,
                "max": 10,
            },
            timeout=5,
        )

        response.raise_for_status()

        return [
            item.get("word", "").strip()
            for item in response.json()
            if item.get("word", "").strip()
        ]

    except Exception:
        return []


def get_result_title(
    result,
    media_type,
):
    if media_type == "Movie":
        return result.get("title", "")

    return result.get("name", "")


def score_tmdb_result(
    search_title,
    result_title,
):
    search_title = search_title.lower().strip()
    result_title = result_title.lower().strip()

    ratio = fuzz.ratio(
        search_title,
        result_title,
    )

    token_score = fuzz.token_sort_ratio(
        search_title,
        result_title,
    )

    score = (
        ratio * 0.65
        + token_score * 0.35
    )

    if search_title == result_title:
        score = 100

    return score


def choose_best_tmdb_result(
    results,
    title,
    media_type,
):
    best_result = None
    best_score = 0

    for result in results[:20]:

        result_title = get_result_title(
            result,
            media_type,
        )

        if not result_title:
            continue

        score = score_tmdb_result(
            title,
            result_title,
        )

        print(
            f"TMDB candidate: {result_title} "
            f"(score={score:.1f})"
        )

        if score > best_score:
            best_score = score
            best_result = result

    return best_result, best_score


def build_tmdb_data(
    result,
    media_type,
    score=100,
):
    if media_type == "Movie":
        title = result.get("title", "")
        date = result.get("release_date", "")
    else:
        title = result.get("name", "")
        date = result.get("first_air_date", "")

    poster = None
    backdrop = None

    if result.get("poster_path"):
        poster = (
            "https://image.tmdb.org/t/p/w500"
            + result["poster_path"]
        )

    if result.get("backdrop_path"):
        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + result["backdrop_path"]
        )

    year = None

    if date:
        try:
            year = int(date[:4])
        except (ValueError, TypeError):
            year = None

    print(
        f"TMDB match found: {title} "
        f"(ID={result.get('id')}, "
        f"score={score:.1f})"
    )

    return {
        "title": title,
        "poster": poster,
        "backdrop": backdrop,
        "year": year,
        "overview": result.get("overview") or "",
        "tmdb_id": result.get("id"),
    }


def search_tmdb(
    title: str,
    media_type: str,
):
    token = get_env("TMDB_TOKEN")

    if not token:
        print("TMDB_TOKEN is not configured")
        return None

    print(
        f"Searching TMDB: "
        f"{title} [{media_type}]"
    )

    try:
        results = tmdb_search(
            title,
            media_type,
            token,
        )
    except requests.RequestException as error:
        print(f"TMDB request error: {error}")
        return None

    if results:

        best_result, best_score = (
            choose_best_tmdb_result(
                results,
                title,
                media_type,
            )
        )

        if best_result and best_score >= 60:
            return build_tmdb_data(
                best_result,
                media_type,
                best_score,
            )

    suggestions = get_spelling_suggestions(title)

    print(
        f"Spelling suggestions: {suggestions}"
    )

    for suggestion in suggestions:

        if suggestion.lower() == title.lower():
            continue

        try:
            results = tmdb_search(
                suggestion,
                media_type,
                token,
            )
        except requests.RequestException:
            continue

        if not results:
            continue

        best_result, best_score = (
            choose_best_tmdb_result(
                results,
                suggestion,
                media_type,
            )
        )

        if best_result and best_score >= 60:
            return build_tmdb_data(
                best_result,
                media_type,
                best_score,
            )

    print(
        f"No confident TMDB match for: {title}"
    )

    return None


# ============================================================
# GEMINI
# ============================================================

def get_gemini_batch_recommendations(watched):

    api_key = get_env("GEMINI_API_KEY")

    if not api_key:
        return {
            "error": (
                "GEMINI_API_KEY is not configured."
            )
        }

    watched_text = "\n".join(
        f"- {movie['title']} — "
        f"{movie['rating']}/10 "
        f"[{movie['type']}]"
        for movie in watched
    )

    if not watched_text:
        watched_text = "Nothing watched yet."

    recent_movies = recent_recommendations["Movie"]
    recent_series = recent_recommendations["Series"]

    recent_text = (
        "Movies:\n"
        + (
            "\n".join(
                f"- {title}"
                for title in recent_movies
            )
            or "None"
        )
        + "\n\nSeries:\n"
        + (
            "\n".join(
                f"- {title}"
                for title in recent_series
            )
            or "None"
        )
    )

    prompt = f"""
You are a personal movie and TV recommendation assistant.

Return exactly:
- 4 movies
- 4 TV series

WATCHED TITLES:
{watched_text}

RECENT AI RECOMMENDATIONS:
{recent_text}

Rules:
- Never recommend something already watched.
- Never repeat a recent recommendation.
- Use the ratings to understand the user's taste.
- Give extra importance to highly rated titles.
- Choose different titles.
- Recommend real released titles.
- Do not invent titles.
- Return ONLY valid JSON.

JSON format:

{{
  "movies": [
    {{
      "title": "Movie title",
      "year": 2024,
      "reason": "Short reason"
    }}
  ],
  "series": [
    {{
      "title": "Series title",
      "year": 2024,
      "reason": "Short reason"
    }}
  ]
}}
"""

    url = (
        "https://generativelanguage.googleapis.com"
        "/v1/interactions"
    )

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    payload = {
        "model": "gemini-3.6-flash",
        "input": prompt,
        "store": False,
    }

    try:
        print(
            "Asking Gemini for "
            "4 movies + 4 series..."
        )

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if response.status_code == 429:
            print(
                "Gemini quota/rate limit reached"
            )

            return {
                "error": (
                    "Gemini free quota is "
                    "temporarily exhausted."
                )
            }

        if not response.ok:
            print(
                "Gemini API error:",
                response.status_code,
                response.text,
            )

            return {
                "error": (
                    "Gemini API returned an error."
                )
            }

        data = response.json()

        text = str(
            data.get(
                "output_text",
                "",
            )
        ).strip()

        if not text:
            parts = []

            for step in data.get(
                "steps",
                [],
            ):

                if step.get(
                    "type"
                ) != "model_output":
                    continue

                for item in step.get(
                    "content",
                    [],
                ):

                    if item.get(
                        "type"
                    ) == "text":

                        parts.append(
                            item.get(
                                "text",
                                "",
                            )
                        )

            text = "\n".join(
                parts
            ).strip()

        if not text:
            return {
                "error": (
                    "Gemini returned no recommendations."
                )
            }

        recommendations = json.loads(text)

        movies = recommendations.get(
            "movies",
            [],
        )[:4]

        series = recommendations.get(
            "series",
            [],
        )[:4]

        return {
            "movies": movies,
            "series": series,
        }

    except json.JSONDecodeError as error:
        print(
            f"Gemini JSON error: {error}"
        )

        return {
            "error": (
                "Gemini returned invalid "
                "recommendation data."
            )
        }

    except requests.RequestException as error:
        print(
            f"Gemini request error: {error}"
        )

        return {
            "error": (
                "Could not connect to Gemini."
            )
        }

    except Exception as error:
        print(
            f"Gemini error: {error}"
        )

        return {
            "error": (
                "Could not generate recommendations."
            )
        }


# ============================================================
# RESOLVE RECOMMENDATIONS THROUGH TMDB
# ============================================================

def resolve_recommendation(
    recommendation,
    media_type,
):
    title = recommendation.get(
        "title",
        "",
    ).strip()

    if not title:
        return None

    tmdb_data = search_tmdb(
        title,
        media_type,
    )

    if not tmdb_data:
        return None

    if movie_exists(
        media_type=media_type,
        tmdb_id=tmdb_data.get("tmdb_id"),
    ):
        return None

    return {
        "media_type": media_type,
        "title": tmdb_data["title"],
        "tmdb_title": tmdb_data["title"],
        "tmdb_id": tmdb_data.get("tmdb_id"),
        "poster": tmdb_data.get("poster"),
        "backdrop": tmdb_data.get("backdrop"),
        "year": tmdb_data.get("year"),
        "overview": tmdb_data.get("overview"),
        "reason": recommendation.get(
            "reason",
            "",
        ),
    }


def resolve_all_recommendations(batch):

    movies = []
    series = []

    used_movie_ids = set()
    used_series_ids = set()

    for recommendation in batch.get(
        "movies",
        [],
    ):

        result = resolve_recommendation(
            recommendation,
            "Movie",
        )

        if not result:
            continue

        tmdb_id = result.get("tmdb_id")

        if tmdb_id in used_movie_ids:
            continue

        used_movie_ids.add(tmdb_id)

        movies.append(result)

        if len(movies) >= 4:
            break

    for recommendation in batch.get(
        "series",
        [],
    ):

        result = resolve_recommendation(
            recommendation,
            "Series",
        )

        if not result:
            continue

        tmdb_id = result.get("tmdb_id")

        if tmdb_id in used_series_ids:
            continue

        used_series_ids.add(tmdb_id)

        series.append(result)

        if len(series) >= 4:
            break

    for item in movies:

        title = item["tmdb_title"]

        if title.lower() not in {
            x.lower()
            for x in recent_recommendations["Movie"]
        }:
            recent_recommendations["Movie"].append(
                title
            )

    for item in series:

        title = item["tmdb_title"]

        if title.lower() not in {
            x.lower()
            for x in recent_recommendations["Series"]
        }:
            recent_recommendations["Series"].append(
                title
            )

    recent_recommendations["Movie"] = (
        recent_recommendations["Movie"][-20:]
    )

    recent_recommendations["Series"] = (
        recent_recommendations["Series"][-20:]
    )

    return {
        "movies": movies,
        "series": series,
    }


# ============================================================
# HOME
# ============================================================

@app.get("/")
@app.get("//")
def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": get_all(),
            "recommendations": None,
        },
    )


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
        title
        and 0 <= rating <= 10
        and media_type in (
            "Movie",
            "Series",
        )
    ):

        tmdb_data = search_tmdb(
            title,
            media_type,
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
            )

    return RedirectResponse(
        "/",
        status_code=303,
    )


# ============================================================
# 4 MOVIES + 4 SERIES
# ============================================================

@app.get("/recommendations")
def recommendations(request: Request):

    watched = get_all()

    batch = get_gemini_batch_recommendations(
        watched
    )

    if batch.get("error"):

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "movies": watched,
                "recommendations": {
                    "error": batch["error"],
                },
            },
        )

    resolved = resolve_all_recommendations(
        batch
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": watched,
            "recommendations": resolved,
        },
    )


# ============================================================
# MARK RECOMMENDATION AS WATCHED
# ============================================================

@app.post("/recommendation/watched")
def recommendation_watched(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(None),
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

    token = get_env(
        "TMDB_TOKEN"
    )

    tmdb_data = None

    if tmdb_id and token:

        try:

            exact_result = tmdb_get_by_id(
                tmdb_id,
                media_type,
                token,
            )

            tmdb_data = build_tmdb_data(
                exact_result,
                media_type,
            )

        except requests.RequestException as error:

            print(
                f"Exact TMDB lookup failed: {error}"
            )

    if not tmdb_data:

        tmdb_data = search_tmdb(
            title,
            media_type,
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

    return RedirectResponse(
        "/recommendations",
        status_code=303,
    )


# ============================================================
# DELETE
# ============================================================

@app.post("/delete/{movie_id}")
def delete(movie_id: int):

    delete_movie(
        movie_id
    )

    return RedirectResponse(
        "/",
        status_code=303,
    )


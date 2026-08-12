import os
import re
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
)


# ============================================================
# APP
# ============================================================

app = FastAPI(title="My Movie AI")

templates = Jinja2Templates(directory="app/templates")

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)

init_database()


# ============================================================
# TMDB
# ============================================================

def search_tmdb(title, media_type):
    token = os.getenv("TMDB_TOKEN")

    if not token:
        print("TMDB_TOKEN is not configured")
        return None

    print(f"Searching TMDB for: {title}")

    if media_type == "Movie":
        endpoint = "https://api.themoviedb.org/3/search/movie"
    else:
        endpoint = "https://api.themoviedb.org/3/search/tv"

    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }

    params = {
        "query": title,
        "language": "en-US",
        "include_adult": "false",
    }

    try:
        response = requests.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=10,
        )

        response.raise_for_status()

        results = response.json().get("results", [])

        if not results:
            print(f"TMDB returned no results for: {title}")
            return None

        search_title = title.lower().strip()

        best_result = None
        best_score = 0

        for result in results[:20]:
            if media_type == "Movie":
                result_title = result.get("title", "")
            else:
                result_title = result.get("name", "")

            if not result_title:
                continue

            result_title_lower = result_title.lower().strip()

            ratio = fuzz.ratio(
                search_title,
                result_title_lower,
            )

            token_score = fuzz.token_sort_ratio(
                search_title,
                result_title_lower,
            )

            score = (
                ratio * 0.6
                + token_score * 0.4
            )

            print(
                f"TMDB candidate: {result_title} "
                f"(score={score:.1f})"
            )

            # Exact title gets absolute priority.
            if result_title_lower == search_title:
                score = 100

            if score > best_score:
                best_score = score
                best_result = result

        if not best_result or best_score < 60:
            print(
                f"No confident TMDB match for "
                f"'{title}' (score={best_score:.1f})"
            )
            return None

        if media_type == "Movie":
            matched_title = best_result.get(
                "title",
                title,
            )
            date = best_result.get(
                "release_date",
                "",
            )
        else:
            matched_title = best_result.get(
                "name",
                title,
            )
            date = best_result.get(
                "first_air_date",
                "",
            )

        print(
            f"TMDB match found: {matched_title} "
            f"(ID={best_result.get('id')}, "
            f"score={best_score:.1f})"
        )

        poster_path = best_result.get("poster_path")
        backdrop_path = best_result.get("backdrop_path")

        poster = None
        backdrop = None

        if poster_path:
            poster = (
                "https://image.tmdb.org/t/p/w500"
                + poster_path
            )

        if backdrop_path:
            backdrop = (
                "https://image.tmdb.org/t/p/w1280"
                + backdrop_path
            )

        year = None

        if date:
            try:
                year = int(date[:4])
            except (ValueError, TypeError):
                year = None

        return {
            "title": matched_title,
            "poster": poster,
            "backdrop": backdrop,
            "year": year,
            "overview": best_result.get("overview") or "",
            "tmdb_id": best_result.get("id"),
        }

    except requests.RequestException as error:
        print(f"TMDB request error: {error}")
        return None

    except Exception as error:
        print(f"TMDB error: {error}")
        return None


# ============================================================
# GEMINI
# ============================================================

def get_gemini_recommendation(media_type, watched):
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print("GEMINI_API_KEY is not configured")
        return None

    watched_items = [
        movie
        for movie in watched
        if movie["type"] == media_type
    ]

    if not watched_items:
        print(
            f"No watched {media_type.lower()} entries "
            "available for recommendation"
        )
        return None

    watched_text = "\n".join(
        f"- {movie['title']} — {movie['rating']}/10"
        for movie in watched_items
    )

    media_word = (
        "movie"
        if media_type == "Movie"
        else "TV series"
    )

    prompt = f"""
You are a movie recommendation assistant.

Recommend exactly ONE {media_word} that the user has NOT already watched.

Watched {media_word}s and personal ratings:

{watched_text}

Rules:
- Do not recommend any title from the watched list.
- Use the ratings to infer the user's taste.
- Search the web before deciding.
- Prefer a real, released title.
- Prefer a strong match to the user's highest-rated titles.
- Return ONLY these three lines:

TITLE: exact official title
YEAR: four digit release year
REASON: one short sentence explaining why this user may like it
"""

    print(
        f"Asking Gemini for a {media_type} recommendation..."
    )

    # Current Gemini Interactions REST API.
    # Gemini 3.6 Flash is a current stable model and
    # supports search grounding. See Google's current docs.
    url = (
        "https://generativelanguage.googleapis.com/"
        "v1/interactions"
    )

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    payload = {
        "model": "gemini-3.6-flash",
        "input": prompt,
        "tools": [
            {
                "type": "google_search"
            }
        ],
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if not response.ok:
            print(
                "Gemini API error:",
                response.status_code,
                response.text,
            )
            return None

        data = response.json()

        # Interactions API returns model output in steps.
        text_parts = []

        for step in data.get("steps", []):
            if step.get("type") != "model_output":
                continue

            for item in step.get("content", []):
                if item.get("type") == "text":
                    text_parts.append(
                        item.get("text", "")
                    )

        text = "\n".join(
            text_parts
        ).strip()

        if not text:
            print("Gemini returned no text")
            print(data)
            return None

        print("Gemini response:")
        print(text)

        title_match = re.search(
            r"^\s*TITLE:\s*(.+)$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )

        year_match = re.search(
            r"^\s*YEAR:\s*(\d{4})$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )

        reason_match = re.search(
            r"^\s*REASON:\s*(.+)$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )

        if not title_match:
            print(
                "Could not extract recommendation "
                "from Gemini response"
            )
            return None

        title = title_match.group(1).strip()

        year = None

        if year_match:
            try:
                year = int(
                    year_match.group(1)
                )
            except ValueError:
                year = None

        reason = ""

        if reason_match:
            reason = reason_match.group(1).strip()

        # Final safety check: don't recommend something already watched.
        watched_titles = {
            movie["title"].strip().lower()
            for movie in watched_items
        }

        if title.strip().lower() in watched_titles:
            print(
                f"Gemini recommended an already watched title: "
                f"{title}"
            )
            return None

        return {
            "title": title,
            "year": year,
            "reason": reason,
        }

    except requests.RequestException as error:
        print(
            f"Gemini request error: {error}"
        )
        return None

    except Exception as error:
        print(
            f"Gemini error: {error}"
        )
        return None


# ============================================================
# HOME
# ============================================================

@app.get("/")
@app.get("//")
def home(request: Request):
    movies = get_all()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": movies,
            "recommendation": None,
        },
    )


# ============================================================
# ADD MOVIE / SERIES
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
        and media_type in ("Movie", "Series")
    ):
        tmdb_data = search_tmdb(
            title,
            media_type,
        )

        if tmdb_data:
            add_movie(
                title=title,
                rating=rating,
                media_type=media_type,
                poster=tmdb_data["poster"],
                backdrop=tmdb_data["backdrop"],
                year=tmdb_data["year"],
                overview=tmdb_data["overview"],
                tmdb_id=tmdb_data["tmdb_id"],
            )

        else:
            print(
                f"No TMDB result for '{title}'. "
                "Saving without poster."
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
# AI RECOMMENDATION
# ============================================================

@app.get("/recommend/{media_type}")
def recommend(
    request: Request,
    media_type: str,
):
    if media_type == "movie":
        selected_type = "Movie"
    elif media_type == "series":
        selected_type = "Series"
    else:
        return RedirectResponse(
            "/",
            status_code=303,
        )

    movies = get_all()

    watched = [
        movie
        for movie in movies
        if movie["type"] == selected_type
    ]

    recommendation = get_gemini_recommendation(
        selected_type,
        watched,
    )

    if not recommendation:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "movies": movies,
                "recommendation": {
                    "error": (
                        "I couldn't get a recommendation "
                        "right now. Check the Gemini API "
                        "key and app logs."
                    )
                },
            },
        )

    # Get TMDB poster and metadata for Gemini's pick.
    tmdb_data = search_tmdb(
        recommendation["title"],
        selected_type,
    )

    if tmdb_data:
        recommendation.update(
            {
                "poster": tmdb_data["poster"],
                "backdrop": tmdb_data["backdrop"],
                "tmdb_title": tmdb_data["title"],
                "tmdb_year": tmdb_data["year"],
                "overview": tmdb_data["overview"],
                "tmdb_id": tmdb_data["tmdb_id"],
            }
        )
    else:
        recommendation.update(
            {
                "poster": None,
                "backdrop": None,
                "tmdb_title": recommendation["title"],
                "tmdb_year": recommendation["year"],
                "overview": "",
                "tmdb_id": None,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": movies,
            "recommendation": recommendation,
        },
    )


# ============================================================
# DELETE
# ============================================================

@app.post("/delete/{movie_id}")
def delete(movie_id: int):
    delete_movie(movie_id)

    return RedirectResponse(
        "/",
        status_code=303,
    )


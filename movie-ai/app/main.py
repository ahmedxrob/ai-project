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
# RECENT AI RECOMMENDATIONS
# ============================================================

recent_recommendations = {
    "Movie": [],
    "Series": [],
}


# ============================================================
# ENV
# ============================================================

def get_env(name):
    return os.getenv(name, "").strip()


# ============================================================
# TMDB
# ============================================================

def tmdb_search(query, media_type, token):

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


def get_spelling_suggestions(title):

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

    except Exception as error:

        print(
            f"Spelling suggestion error: {error}"
        )

        return []


def get_result_title(
    result,
    media_type,
):

    if media_type == "Movie":
        return result.get(
            "title",
            "",
        )

    return result.get(
        "name",
        "",
    )


def score_tmdb_result(
    search_title,
    result_title,
):

    search_title = (
        search_title
        .lower()
        .strip()
    )

    result_title = (
        result_title
        .lower()
        .strip()
    )

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
            f"TMDB candidate: "
            f"{result_title} "
            f"(score={score:.1f})"
        )

        if score > best_score:

            best_score = score
            best_result = result

    return (
        best_result,
        best_score,
    )


def build_tmdb_data(
    result,
    media_type,
    score,
):

    if media_type == "Movie":

        title = result.get(
            "title",
            "",
        )

        date = result.get(
            "release_date",
            "",
        )

    else:

        title = result.get(
            "name",
            "",
        )

        date = result.get(
            "first_air_date",
            "",
        )

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
        except (
            ValueError,
            TypeError,
        ):
            pass

    print(
        f"TMDB match found: "
        f"{title} "
        f"(ID={result.get('id')}, "
        f"score={score:.1f})"
    )

    return {
        "title": title,
        "poster": poster,
        "backdrop": backdrop,
        "year": year,
        "overview": (
            result.get("overview")
            or ""
        ),
        "tmdb_id": result.get("id"),
    }


def search_tmdb(
    title,
    media_type,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:

        print(
            "TMDB_TOKEN is not configured"
        )

        return None

    print(
        f"Searching TMDB for: "
        f"{title} [{media_type}]"
    )

    try:

        results = tmdb_search(
            title,
            media_type,
            token,
        )

    except requests.RequestException as error:

        print(
            f"TMDB request error: {error}"
        )

        return None

    if results:

        (
            best_result,
            best_score,
        ) = choose_best_tmdb_result(
            results,
            title,
            media_type,
        )

        if (
            best_result
            and best_score >= 60
        ):

            return build_tmdb_data(
                best_result,
                media_type,
                best_score,
            )

    print(
        f"TMDB returned no confident "
        f"result for: {title}"
    )

    # Spelling correction
    suggestions = get_spelling_suggestions(
        title
    )

    for suggestion in suggestions:

        if (
            suggestion.lower().strip()
            == title.lower().strip()
        ):
            continue

        print(
            f"Trying corrected title: "
            f"{suggestion}"
        )

        try:

            corrected_results = tmdb_search(
                suggestion,
                media_type,
                token,
            )

        except requests.RequestException:

            continue

        if not corrected_results:
            continue

        (
            best_result,
            best_score,
        ) = choose_best_tmdb_result(
            corrected_results,
            suggestion,
            media_type,
        )

        if (
            best_result
            and best_score >= 60
        ):

            return build_tmdb_data(
                best_result,
                media_type,
                best_score,
            )

    return None


# ============================================================
# GEMINI
# ============================================================

def get_gemini_recommendation(
    media_type,
    watched,
):

    api_key = get_env(
        "GEMINI_API_KEY"
    )

    if not api_key:
        print(
            "GEMINI_API_KEY is not configured"
        )
        return None

    watched_items = [
        movie
        for movie in watched
        if movie["type"] == media_type
    ]

    if not watched_items:
        return None

    watched_text = "\n".join(
        f"- {movie['title']} — "
        f"{movie['rating']}/10"
        for movie in watched_items
    )

    recent = recent_recommendations.get(
        media_type,
        [],
    )

    recent_text = "\n".join(
        f"- {title}"
        for title in recent
    ) or "None"

    media_word = (
        "movie"
        if media_type == "Movie"
        else "TV series"
    )

    prompt = f"""
You are a personal {media_word} recommendation assistant.

Recommend ONE {media_word} the user has NOT watched.

WATCHED:
{watched_text}

RECENT RECOMMENDATIONS:
{recent_text}

Rules:
- Never recommend a watched title.
- Never repeat a recent recommendation.
- Use the user's ratings to understand taste.
- Choose a different title.
- Recommend a real released {media_word}.
- Do not invent a title.

Return exactly:

TITLE: exact official title
YEAR: four digit year
REASON: one short sentence
"""

    print(
        f"Asking Gemini for a "
        f"{media_type} recommendation..."
    )

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

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if response.status_code == 429:

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

        title_match = re.search(
            r"^\s*TITLE:\s*(.+)$",
            text,
            re.IGNORECASE |
            re.MULTILINE,
        )

        year_match = re.search(
            r"^\s*YEAR:\s*(\d{4})$",
            text,
            re.IGNORECASE |
            re.MULTILINE,
        )

        reason_match = re.search(
            r"^\s*REASON:\s*(.+)$",
            text,
            re.IGNORECASE |
            re.MULTILINE,
        )

        if not title_match:
            return None

        title = title_match.group(
            1
        ).strip()

        year = None

        if year_match:

            try:
                year = int(
                    year_match.group(1)
                )
            except ValueError:
                pass

        reason = ""

        if reason_match:
            reason = reason_match.group(
                1
            ).strip()

        # Never recommend something already watched.
        watched_titles = {
            movie["title"]
            .strip()
            .lower()
            for movie in watched_items
        }

        if title.lower() in watched_titles:
            return None

        # Never repeat recent recommendation.
        if title.lower() in {
            item.lower()
            for item in recent
        }:
            return None

        return {
            "title": title,
            "year": year,
            "reason": reason,
            "media_type": media_type,
        }

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

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": get_all(),
            "recommendation": None,
        },
    )


# ============================================================
# ADD
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
# RECOMMEND
# ============================================================

@app.get(
    "/recommend/{media_type}"
)
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

    recommendation = (
        get_gemini_recommendation(
            selected_type,
            watched,
        )
    )

    if (
        not recommendation
        or recommendation.get("error")
    ):

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "movies": movies,
                "recommendation": {
                    "error": (
                        recommendation.get(
                            "error"
                        )
                        if recommendation
                        else
                        "Could not get a recommendation."
                    )
                },
            },
        )

    # VERY IMPORTANT:
    # Preserve whether this is Movie or Series.
    recommendation["media_type"] = selected_type

    # Search TMDB using the CORRECT type.
    tmdb_data = search_tmdb(
        recommendation["title"],
        selected_type,
    )

    if tmdb_data:

        recommendation.update(
            {
                "poster": tmdb_data.get(
                    "poster"
                ),
                "backdrop": tmdb_data.get(
                    "backdrop"
                ),
                "tmdb_title": tmdb_data.get(
                    "title"
                ),
                "tmdb_year": tmdb_data.get(
                    "year"
                ),
                "overview": tmdb_data.get(
                    "overview"
                ),
                "tmdb_id": tmdb_data.get(
                    "tmdb_id"
                ),
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

    # Add to recent list ONLY after success.
    recent = recent_recommendations[
        selected_type
    ]

    title_to_store = (
        recommendation["tmdb_title"]
    )

    if title_to_store.lower() not in {
        item.lower()
        for item in recent
    }:

        recent.append(
            title_to_store
        )

    if len(recent) > 10:
        recent.pop(0)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": movies,
            "recommendation": recommendation,
        },
    )


# ============================================================
# ALREADY WATCHED
# ============================================================

@app.post(
    "/recommendation/watched"
)
def recommendation_watched(
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

    # Search using the EXACT media type.
    tmdb_data = search_tmdb(
        title,
        media_type,
    )

    if tmdb_data:

        saved = add_movie(
            title=tmdb_data["title"],
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

        if saved:
            print(
                f"Added watched {media_type}: "
                f"{tmdb_data['title']}"
            )
        else:
            print(
                f"Already exists: "
                f"{tmdb_data['title']} "
                f"[{media_type}]"
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
# DELETE
# ============================================================

@app.post(
    "/delete/{movie_id}"
)
def delete(movie_id: int):

    delete_movie(
        movie_id
    )

    return RedirectResponse(
        "/",
        status_code=303,
    )


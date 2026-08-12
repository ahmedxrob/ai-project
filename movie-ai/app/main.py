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
# Keeps Gemini from immediately returning the same title.
# This resets if the app restarts.

recent_recommendations = {
    "Movie": [],
    "Series": [],
}


# ============================================================
# ENVIRONMENT
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

        suggestions = []

        for item in response.json():

            word = item.get(
                "word",
                "",
            ).strip()

            if word:
                suggestions.append(word)

        return suggestions

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

    tmdb_id = result.get("id")

    poster = None
    backdrop = None

    poster_path = result.get(
        "poster_path"
    )

    backdrop_path = result.get(
        "backdrop_path"
    )

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
            year = int(
                date[:4]
            )

        except (
            ValueError,
            TypeError,
        ):
            year = None

    print(
        f"TMDB match found: "
        f"{title} "
        f"(ID={tmdb_id}, "
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
        "tmdb_id": tmdb_id,
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
        f"Searching TMDB for: {title}"
    )

    # --------------------------------------------------------
    # Normal search
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Spelling correction
    # --------------------------------------------------------

    print(
        f"TMDB returned no confident result "
        f"for: {title}"
    )

    suggestions = get_spelling_suggestions(
        title
    )

    print(
        f"Spelling suggestions: "
        f"{suggestions}"
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

        except requests.RequestException as error:

            print(
                f"TMDB correction search error: "
                f"{error}"
            )

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

            print(
                f"Corrected title matched: "
                f"{suggestion}"
            )

            return build_tmdb_data(
                best_result,
                media_type,
                best_score,
            )

    print(
        f"No confident TMDB match for: "
        f"{title}"
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

        print(
            f"No watched {media_type.lower()} "
            f"entries available"
        )

        return None

    watched_text = "\n".join(
        f"- {movie['title']} — "
        f"{movie['rating']}/10"
        for movie in watched_items
    )

    # --------------------------------------------------------
    # Exclude previous AI recommendations
    # --------------------------------------------------------

    recent = recent_recommendations.get(
        media_type,
        [],
    )

    recent_text = "\n".join(
        f"- {title}"
        for title in recent
    )

    if not recent_text:
        recent_text = "None"

    media_word = (
        "movie"
        if media_type == "Movie"
        else "TV series"
    )

    prompt = f"""
You are a personal {media_word} recommendation assistant.

Recommend ONE {media_word} that the user has NOT watched.

WATCHED TITLES AND RATINGS:
{watched_text}

RECENT AI RECOMMENDATIONS THAT MUST NOT BE REPEATED:
{recent_text}

Rules:

1. Never recommend anything already watched.
2. Never repeat one of the recent AI recommendations.
3. Use the user's high ratings to understand their taste.
4. Choose a different title each time.
5. Recommend a real released {media_word}.
6. Do not invent a title.
7. Return exactly:

TITLE: exact official title
YEAR: four digit release year
REASON: one short sentence explaining why this user may like it
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

        # ----------------------------------------------------
        # Quota
        # ----------------------------------------------------

        if response.status_code == 429:

            print(
                "Gemini quota/rate limit reached"
            )

            return {
                "error": (
                    "Gemini's free quota is "
                    "temporarily exhausted. "
                    "Please try again later."
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

        # ----------------------------------------------------
        # Extract output
        # ----------------------------------------------------

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
                                ""
                            )
                        )

            text = "\n".join(
                parts
            ).strip()

        if not text:

            print(
                "Gemini returned no text"
            )

            return None

        print(
            "Gemini response:"
        )

        print(text)

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

            print(
                "Could not extract Gemini "
                "recommendation"
            )

            return None

        title = (
            title_match
            .group(1)
            .strip()
        )

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

            reason = (
                reason_match
                .group(1)
                .strip()
            )

        # ----------------------------------------------------
        # Check against watched titles
        # ----------------------------------------------------

        watched_titles = {
            movie["title"]
            .strip()
            .lower()
            for movie in watched_items
        }

        if (
            title.lower()
            in watched_titles
        ):

            print(
                "Gemini recommended an "
                "already watched title: "
                f"{title}"
            )

            return None

        # ----------------------------------------------------
        # Check against recent recommendations
        # ----------------------------------------------------

        recent_titles = {
            item.strip().lower()
            for item in recent
        }

        if (
            title.lower()
            in recent_titles
        ):

            print(
                "Gemini repeated a recent "
                f"recommendation: {title}"
            )

            return None

        return {
            "title": title,
            "year": year,
            "reason": reason,
        }

    except requests.RequestException as error:

        print(
            f"Gemini request error: "
            f"{error}"
        )

        return {
            "error": (
                "Could not connect to Gemini."
            )
        }

    except Exception as error:

        print(
            f"Gemini error: "
            f"{error}"
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
                title=title,
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
# AI RECOMMENDATION
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

    # --------------------------------------------------------
    # Error
    # --------------------------------------------------------

    if (
        not recommendation
        or recommendation.get("error")
    ):

        error_message = (
            recommendation.get(
                "error"
            )
            if recommendation
            else
            "I couldn't get an AI "
            "recommendation right now."
        )

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "movies": movies,
                "recommendation": {
                    "error": error_message,
                },
            },
        )

    # --------------------------------------------------------
    # TMDB details
    # --------------------------------------------------------

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
                "tmdb_title":
                    recommendation["title"],
                "tmdb_year":
                    recommendation["year"],
                "overview": "",
                "tmdb_id": None,
            }
        )

    # --------------------------------------------------------
    # Save recent recommendation
    # --------------------------------------------------------

    recent = recent_recommendations[
        selected_type
    ]

    recommendation_title = (
        recommendation["tmdb_title"]
    )

    if (
        recommendation_title.lower()
        not in {
            item.lower()
            for item in recent
        }
    ):

        recent.append(
            recommendation_title
        )

    # Keep only the latest 10.
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
# MARK RECOMMENDATION AS WATCHED
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

    # Validate
    if (
        not title
        or rating < 0
        or rating > 10
        or media_type
        not in (
            "Movie",
            "Series",
        )
    ):

        return RedirectResponse(
            "/",
            status_code=303,
        )

    # Search TMDB again to get artwork/info.
    tmdb_data = search_tmdb(
        title,
        media_type,
    )

    if tmdb_data:

        add_movie(
            title=tmdb_data.get(
                "title"
            ) or title,

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
# DELETE
# ============================================================

@app.post(
    "/delete/{movie_id}"
)
def delete(
    movie_id: int
):

    delete_movie(
        movie_id
    )

    return RedirectResponse(
        "/",
        status_code=303,
    )


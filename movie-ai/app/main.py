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
# TMDB SEARCH
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
    """
    Fetch the exact TMDB item by ID.
    This prevents a Series from accidentally
    becoming a Movie with the same title.
    """

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
    score=100,
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
    title: str,
    media_type: str,
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
        f"TMDB returned no confident "
        f"result for: {title}"
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
                f"TMDB correction error: "
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

            return build_tmdb_data(
                best_result,
                media_type,
                best_score,
            )

    return None


# ============================================================
# GEMINI
# ============================================================

def extract_gemini_text(data):
    """
    Extract text from the current Interactions API response.
    """

    output_text = data.get(
        "output_text",
        "",
    )

    if output_text:
        return output_text.strip()

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

                text = item.get(
                    "text",
                    "",
                )

                if text:
                    parts.append(text)

    return "\n".join(
        parts
    ).strip()


def call_gemini(
    prompt: str,
):
    api_key = get_env(
        "GEMINI_API_KEY"
    )

    if not api_key:

        print(
            "GEMINI_API_KEY is not configured"
        )

        return None

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

            print(
                "Gemini quota/rate limit reached"
            )

            return {
                "error": (
                    "Gemini's free quota is "
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

        return response.json()

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


def get_gemini_recommendation(
    media_type,
    watched,
):
    watched_items = [
        movie
        for movie in watched
        if movie["type"] == media_type
    ]

    if not watched_items:
        return {
            "error": (
                f"You don't have any watched "
                f"{media_type.lower()}s yet."
            )
        }

    recent = recent_recommendations.get(
        media_type,
        [],
    )

    watched_text = "\n".join(
        f"- {movie['title']} — "
        f"{movie['rating']}/10"
        for movie in watched_items
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

Recommend ONE {media_word} the user has NOT watched.

WATCHED:
{watched_text}

RECENT AI RECOMMENDATIONS:
{recent_text}

Rules:
- Never recommend a title already watched.
- Never repeat a recent AI recommendation.
- Use the user's ratings to infer their taste.
- Give strong preference to highly rated titles.
- Choose a genuinely different title.
- Recommend a real released {media_word}.
- Do not invent a title.
- Return exactly:

TITLE: exact official title
YEAR: four digit release year
REASON: one short sentence explaining why the user may like it
"""

    # Try up to three times so a repeated Gemini answer
    # doesn't immediately become an error.
    for attempt in range(1, 4):

        print(
            f"Gemini recommendation attempt "
            f"{attempt}/3 for {media_type}"
        )

        data = call_gemini(
            prompt
        )

        if not data:
            continue

        if data.get("error"):
            return data

        text = extract_gemini_text(
            data
        )

        if not text:
            print(
                "Gemini returned no text"
            )
            continue

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
            continue

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

        watched_titles = {
            movie["title"]
            .strip()
            .lower()
            for movie in watched_items
        }

        recent_titles = {
            item
            .strip()
            .lower()
            for item in recent
        }

        if title.lower() in watched_titles:

            print(
                f"Gemini selected watched title: "
                f"{title}"
            )

            prompt += (
                f"\nIMPORTANT: Do not choose "
                f"'{title}'. Choose another title."
            )

            continue

        if title.lower() in recent_titles:

            print(
                f"Gemini repeated recent title: "
                f"{title}"
            )

            prompt += (
                f"\nIMPORTANT: Do not choose "
                f"'{title}'. Choose another title."
            )

            continue

        return {
            "title": title,
            "year": year,
            "reason": reason,
            "media_type": media_type,
        }

    return {
        "error": (
            "Gemini could not produce a new "
            "recommendation. Try again."
        )
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
            "recommendation": None,
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

            added = add_movie(
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

            if not added:

                print(
                    f"Duplicate prevented: "
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

    recommendation["media_type"] = selected_type

    # Search TMDB using the exact requested type.
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
                "tmdb_title": recommendation[
                    "title"
                ],
                "tmdb_year": recommendation[
                    "year"
                ],
                "overview": "",
                "tmdb_id": None,
            }
        )

    # --------------------------------------------------------
    # Do not display a recommendation already in database.
    # --------------------------------------------------------

    if recommendation.get("tmdb_id"):

        if movie_exists(
            media_type=selected_type,
            tmdb_id=recommendation[
                "tmdb_id"
            ],
        ):

            print(
                "Recommended title is already watched:"
                f" {recommendation['tmdb_title']}"
            )

            return RedirectResponse(
                f"/recommend/{media_type}",
                status_code=303,
            )

    # Save it into temporary in-memory history.
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

    if len(recent) > 15:
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

    # --------------------------------------------------------
    # BEST OPTION:
    # Use exact TMDB ID sent by the recommendation.
    # --------------------------------------------------------

    if (
        tmdb_id
        and token
    ):

        try:

            exact_result = tmdb_get_by_id(
                tmdb_id,
                media_type,
                token,
            )

            tmdb_data = build_tmdb_data(
                exact_result,
                media_type,
                100,
            )

        except requests.RequestException as error:

            print(
                f"Could not fetch exact TMDB "
                f"item: {error}"
            )

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    if not tmdb_data:

        tmdb_data = search_tmdb(
            title,
            media_type,
        )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

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

        print(
            f"Watched action: "
            f"{tmdb_data['title']} "
            f"[{media_type}] "
            f"saved={saved}"
        )

    else:

        saved = add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )

        print(
            f"Watched action without TMDB: "
            f"{title} [{media_type}] "
            f"saved={saved}"
        )

    # --------------------------------------------------------
    # Automatically get another recommendation.
    # --------------------------------------------------------

    if media_type == "Movie":

        return RedirectResponse(
            "/recommend/movie",
            status_code=303,
        )

    return RedirectResponse(
        "/recommend/series",
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

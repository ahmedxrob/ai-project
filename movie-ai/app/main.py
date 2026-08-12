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
# HOME ASSISTANT OPTIONS
# ============================================================

OPTIONS_FILE = "/data/options.json"


def get_tmdb_token():
    try:
        with open(
            OPTIONS_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            options = json.load(file)

        token = options.get(
            "tmdb_token",
            "",
        )

        if not token:
            print("TMDB token is not configured")
            return None

        print("TMDB token loaded successfully")

        return token.strip()

    except Exception as error:
        print(
            f"Could not read TMDB token: {error}"
        )
        return None


# ============================================================
# TMDB REQUEST
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
        []
    )


# ============================================================
# SPELLING SUGGESTIONS
# ============================================================

def get_spelling_suggestions(title):
    """
    Ask Datamuse for possible spelling corrections.

    Example:
        intersellar
            ->
        interstellar
    """

    try:
        response = requests.get(
            "https://api.datamuse.com/words",
            params={
                "sp": title,
                "max": 8,
            },
            timeout=5,
        )

        response.raise_for_status()

        results = response.json()

        suggestions = []

        for result in results:

            word = result.get(
                "word",
                ""
            ).strip()

            if word:
                suggestions.append(word)

        return suggestions

    except Exception as error:

        print(
            f"Spelling suggestion error: {error}"
        )

        return []


# ============================================================
# FIND BEST TMDB RESULT
# ============================================================

def choose_best_result(
    results,
    title,
    media_type,
):
    if not results:
        return None, 0

    search_title = (
        title
        .lower()
        .strip()
    )

    best_result = None
    best_score = 0

    for result in results[:20]:

        if media_type == "Movie":

            result_title = result.get(
                "title",
                ""
            )

        else:

            result_title = result.get(
                "name",
                ""
            )

        if not result_title:
            continue

        result_title_lower = (
            result_title
            .lower()
            .strip()
        )

        ratio = fuzz.ratio(
            search_title,
            result_title_lower,
        )

        token_score = fuzz.token_sort_ratio(
            search_title,
            result_title_lower,
        )

        partial_score = fuzz.partial_ratio(
            search_title,
            result_title_lower,
        )

        # Strongly favor the actual title similarity.
        score = (
            ratio * 0.55
            + token_score * 0.35
            + partial_score * 0.10
        )

        print(
            f"TMDB candidate: "
            f"{result_title} "
            f"(score={score:.1f})"
        )

        if score > best_score:

            best_score = score
            best_result = result

    return best_result, best_score


# ============================================================
# SEARCH TMDB
# ============================================================

def search_tmdb(title, media_type):

    token = get_tmdb_token()

    if not token:
        return None

    print(
        f"Searching TMDB for: {title}"
    )

    # --------------------------------------------------------
    # 1. Normal TMDB search
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

    # --------------------------------------------------------
    # 2. If TMDB finds something, choose best match
    # --------------------------------------------------------

    if results:

        best_result, best_score = choose_best_result(
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
    # 3. TMDB returned nothing.
    #
    # Try spelling correction.
    # --------------------------------------------------------

    print(
        f"TMDB returned no results for: {title}"
    )

    suggestions = get_spelling_suggestions(
        title
    )

    print(
        f"Spelling suggestions: {suggestions}"
    )

    # --------------------------------------------------------
    # 4. Try every spelling suggestion
    # --------------------------------------------------------

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

        best_result, best_score = choose_best_result(
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

    # --------------------------------------------------------
    # Nothing found
    # --------------------------------------------------------

    print(
        f"No confident TMDB match for: "
        f"{title}"
    )

    return None


# ============================================================
# BUILD TMDB DATA
# ============================================================

def build_tmdb_data(
    result,
    media_type,
    score,
):

    if media_type == "Movie":

        matched_title = result.get(
            "title",
            ""
        )

        date = result.get(
            "release_date",
            ""
        )

    else:

        matched_title = result.get(
            "name",
            ""
        )

        date = result.get(
            "first_air_date",
            ""
        )

    tmdb_id = result.get(
        "id"
    )

    print(
        f"TMDB match found: "
        f"{matched_title} "
        f"(ID={tmdb_id}, "
        f"score={score:.1f})"
    )

    # --------------------------------------------------------
    # Poster
    # --------------------------------------------------------

    poster_path = result.get(
        "poster_path"
    )

    poster = None

    if poster_path:

        poster = (
            "https://image.tmdb.org/t/p/w500"
            + poster_path
        )

    # --------------------------------------------------------
    # Backdrop
    # --------------------------------------------------------

    backdrop_path = result.get(
        "backdrop_path"
    )

    backdrop = None

    if backdrop_path:

        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + backdrop_path
        )

    # --------------------------------------------------------
    # Year
    # --------------------------------------------------------

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

    return {
        "poster": poster,
        "backdrop": backdrop,
        "year": year,
        "overview": (
            result.get(
                "overview"
            )
            or ""
        ),
        "tmdb_id": tmdb_id,
    }


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

            print(
                f"No TMDB result for "
                f"'{title}'. "
                f"Saving without poster."
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


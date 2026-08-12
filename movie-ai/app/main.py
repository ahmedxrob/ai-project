import os
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

app = FastAPI(title="My Movie AI")

templates = Jinja2Templates(directory="app/templates")

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)

init_database()


TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


def get_tmdb_token():
    """
    Get TMDB token from environment.
    """
    token = os.getenv("TMDB_TOKEN")

    if not token:
        print("TMDB_TOKEN is not configured")

    return token


def tmdb_request(endpoint, query):
    """
    Perform a TMDB search request.
    """

    token = get_tmdb_token()

    if not token:
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }

    params = {
        "query": query,
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

        return response.json().get("results", [])

    except requests.RequestException as error:
        print(f"TMDB request error: {error}")
        return []

    except Exception as error:
        print(f"TMDB error: {error}")
        return []


def get_result_title(result, media_type):
    """
    Get the correct title field from a TMDB result.
    """

    if media_type == "Movie":
        return result.get("title", "")

    return result.get("name", "")


def score_result(search_title, result_title):
    """
    Calculate a safe fuzzy score.

    We deliberately don't use partial_ratio as the main score,
    because it can make unrelated titles score 100.
    """

    search_title = search_title.lower().strip()
    result_title = result_title.lower().strip()

    ratio = fuzz.ratio(
        search_title,
        result_title,
    )

    token_ratio = fuzz.token_set_ratio(
        search_title,
        result_title,
    )

    weighted_score = (
        ratio * 0.7
        + token_ratio * 0.3
    )

    return weighted_score


def find_best_result(title, results, media_type):
    """
    Find the best TMDB result.
    """

    best_result = None
    best_score = -1

    for result in results[:20]:

        result_title = get_result_title(
            result,
            media_type,
        )

        if not result_title:
            continue

        score = score_result(
            title,
            result_title,
        )

        print(
            f"TMDB candidate: {result_title} "
            f"(score={score:.1f})"
        )

        # Exact match gets priority.
        if (
            title.lower().strip()
            == result_title.lower().strip()
        ):
            score = 100

        if score > best_score:
            best_score = score
            best_result = result

    return best_result, best_score


def search_tmdb(title, media_type):
    """
    Search TMDB and find the best matching movie/series.

    Handles:
    - Exact titles
    - Misspellings
    - Partial titles
    - Alternative TMDB search attempts
    """

    title = title.strip()

    if not title:
        return None

    print(f"Searching TMDB for: {title}")

    if media_type == "Movie":
        endpoint = (
            "https://api.themoviedb.org/3/search/movie"
        )
    else:
        endpoint = (
            "https://api.themoviedb.org/3/search/tv"
        )

    # -------------------------------------------------
    # FIRST SEARCH
    # -------------------------------------------------

    results = tmdb_request(
        endpoint,
        title,
    )

    # -------------------------------------------------
    # NORMAL MATCH
    # -------------------------------------------------

    if results:

        best_result, best_score = find_best_result(
            title,
            results,
            media_type,
        )

        if best_result and best_score >= 60:
            return build_tmdb_data(
                best_result,
                media_type,
            )

    # -------------------------------------------------
    # TYPO SEARCH
    # -------------------------------------------------
    #
    # TMDB itself cannot always correct spelling.
    #
    # We therefore create a few simplified searches.
    #

    words = title.split()

    alternative_queries = []

    # Remove duplicate spaces
    cleaned = " ".join(words)

    if cleaned.lower() != title.lower():
        alternative_queries.append(cleaned)

    # Try individual words for multi-word titles
    if len(words) > 1:
        alternative_queries.append(
            " ".join(words[:2])
        )

    # Try removing common punctuation
    simplified = (
        title
        .replace("-", " ")
        .replace("_", " ")
        .replace(".", " ")
        .replace(",", " ")
    )

    simplified = " ".join(
        simplified.split()
    )

    if simplified.lower() != title.lower():
        alternative_queries.append(simplified)

    # -------------------------------------------------
    # SECONDARY SEARCHES
    # -------------------------------------------------

    for query in alternative_queries:

        print(
            f"Trying alternative TMDB search: {query}"
        )

        results = tmdb_request(
            endpoint,
            query,
        )

        if not results:
            continue

        best_result, best_score = find_best_result(
            title,
            results,
            media_type,
        )

        if best_result and best_score >= 60:

            print(
                f"TMDB match found using alternative "
                f"search: "
                f"{get_result_title(best_result, media_type)} "
                f"(score={best_score:.1f})"
            )

            return build_tmdb_data(
                best_result,
                media_type,
            )

    # -------------------------------------------------
    # NO MATCH
    # -------------------------------------------------

    print(
        f"No confident TMDB match for '{title}'"
    )

    return None


def build_tmdb_data(result, media_type):
    """
    Convert TMDB result into our database format.
    """

    if media_type == "Movie":
        matched_title = result.get(
            "title",
            "",
        )

        date = result.get(
            "release_date",
            "",
        )

    else:
        matched_title = result.get(
            "name",
            "",
        )

        date = result.get(
            "first_air_date",
            "",
        )

    print(
        f"TMDB match found: "
        f"{matched_title} "
        f"(ID={result.get('id')})"
    )

    # -------------------------------------------------
    # POSTER
    # -------------------------------------------------

    poster_path = result.get(
        "poster_path"
    )

    poster = None

    if poster_path:
        poster = (
            f"{TMDB_IMAGE_BASE}/w500"
            f"{poster_path}"
        )

    # -------------------------------------------------
    # BACKDROP
    # -------------------------------------------------

    backdrop_path = result.get(
        "backdrop_path"
    )

    backdrop = None

    if backdrop_path:
        backdrop = (
            f"{TMDB_IMAGE_BASE}/w1280"
            f"{backdrop_path}"
        )

    # -------------------------------------------------
    # YEAR
    # -------------------------------------------------

    year = None

    if date:
        try:
            year = int(date[:4])
        except (
            ValueError,
            TypeError,
        ):
            pass

    return {
        "poster": poster,
        "backdrop": backdrop,
        "year": year,
        "overview": (
            result.get("overview")
            or ""
        ),
        "tmdb_id": result.get("id"),
    }


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
                poster=tmdb_data["poster"],
                backdrop=tmdb_data["backdrop"],
                year=tmdb_data["year"],
                overview=tmdb_data["overview"],
                tmdb_id=tmdb_data["tmdb_id"],
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


@app.post("/delete/{movie_id}")
def delete(movie_id: int):

    delete_movie(movie_id)

    return RedirectResponse(
        "/",
        status_code=303,
    )

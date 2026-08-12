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


# ============================================================
# DATABASE
# ============================================================

init_database()


# ============================================================
# HOME ASSISTANT OPTIONS
# ============================================================

OPTIONS_FILE = "/data/options.json"


def get_tmdb_token():
    """
    Read the TMDB token configured in the
    Home Assistant add-on Configuration page.
    """

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
            print(
                "TMDB token is not configured "
                "in Home Assistant"
            )

            return None

        token = token.strip()

        print("TMDB token loaded successfully")

        return token

    except FileNotFoundError:

        print(
            f"Home Assistant options file "
            f"not found: {OPTIONS_FILE}"
        )

        return None

    except Exception as error:

        print(
            f"Could not read Home Assistant "
            f"options: {error}"
        )

        return None


# ============================================================
# TMDB SEARCH
# ============================================================

def search_tmdb(title, media_type):

    token = get_tmdb_token()

    if not token:
        return None

    print(
        f"Searching TMDB for: {title}"
    )

    # --------------------------------------------------------
    # Endpoint
    # --------------------------------------------------------

    if media_type == "Movie":

        endpoint = (
            "https://api.themoviedb.org/3/search/movie"
        )

    else:

        endpoint = (
            "https://api.themoviedb.org/3/search/tv"
        )

    # --------------------------------------------------------
    # Authentication
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # HTTP errors
        # ----------------------------------------------------

        response.raise_for_status()

        results = response.json().get(
            "results",
            []
        )

        if not results:

            print(
                f"TMDB returned no results for: "
                f"{title}"
            )

            return None

        # ----------------------------------------------------
        # Find best matching result
        # ----------------------------------------------------

        search_title = (
            title
            .lower()
            .strip()
        )

        best_result = None
        best_score = 0

        for result in results[:10]:

            # -----------------------------------------------
            # Get result title
            # -----------------------------------------------

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

            # -----------------------------------------------
            # Similarity scores
            # -----------------------------------------------

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

            # -----------------------------------------------
            # Weighted score
            #
            # Ratio + token score are the main scores.
            # Partial score is only a small bonus.
            #
            # This prevents things such as:
            #
            # "Interstellar"
            #
            # from incorrectly selecting:
            #
            # "Inside 'Interstellar'"
            # -----------------------------------------------

            score = (
                (ratio * 0.50)
                + (token_score * 0.35)
                + (partial_score * 0.15)
            )

            print(
                f"TMDB candidate: "
                f"{result_title} "
                f"(score={score:.1f})"
            )

            if score > best_score:

                best_score = score
                best_result = result

        # ----------------------------------------------------
        # Minimum confidence
        # ----------------------------------------------------

        if (
            not best_result
            or best_score < 60
        ):

            print(
                f"No confident TMDB match for "
                f"'{title}' "
                f"(score={best_score:.1f})"
            )

            return None

        # ----------------------------------------------------
        # Matched title + date
        # ----------------------------------------------------

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
            f"TMDB match found: "
            f"{matched_title} "
            f"(ID={best_result.get('id')}, "
            f"score={best_score:.1f})"
        )

        # ----------------------------------------------------
        # Poster
        # ----------------------------------------------------

        poster_path = best_result.get(
            "poster_path"
        )

        poster = None

        if poster_path:

            poster = (
                "https://image.tmdb.org/t/p/w500"
                + poster_path
            )

        # ----------------------------------------------------
        # Backdrop
        # ----------------------------------------------------

        backdrop_path = best_result.get(
            "backdrop_path"
        )

        backdrop = None

        if backdrop_path:

            backdrop = (
                "https://image.tmdb.org/t/p/w1280"
                + backdrop_path
            )

        # ----------------------------------------------------
        # Year
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Return TMDB data
        # ----------------------------------------------------

        return {

            "poster": poster,

            "backdrop": backdrop,

            "year": year,

            "overview": (
                best_result.get(
                    "overview"
                )
                or ""
            ),

            "tmdb_id": best_result.get(
                "id"
            ),
        }

    # ========================================================
    # REQUEST ERROR
    # ========================================================

    except requests.RequestException as error:

        print(
            f"TMDB request error: "
            f"{error}"
        )

        return None

    # ========================================================
    # OTHER ERROR
    # ========================================================

    except Exception as error:

        print(
            f"TMDB error: "
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

    # --------------------------------------------------------
    # Validate input
    # --------------------------------------------------------

    if (
        title
        and 0 <= rating <= 10
        and media_type in (
            "Movie",
            "Series",
        )
    ):

        # ----------------------------------------------------
        # Search TMDB
        # ----------------------------------------------------

        tmdb_data = search_tmdb(
            title,
            media_type,
        )

        # ----------------------------------------------------
        # TMDB found
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # TMDB failed
        #
        # Still save the movie/series.
        # ----------------------------------------------------

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

    # --------------------------------------------------------
    # Redirect back to UI
    # --------------------------------------------------------

    return RedirectResponse(
        "/",
        status_code=303,
    )


# ============================================================
# DELETE
# ============================================================

@app.post("/delete/{movie_id}")
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

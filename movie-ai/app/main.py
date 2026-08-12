import json
import requests

from pathlib import Path
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


# ==========================================
# HOME ASSISTANT APP CONFIG
# ==========================================

def get_tmdb_token():
    options_file = Path("/data/options.json")

    try:
        if not options_file.exists():
            print("ERROR: /data/options.json not found")
            return None

        with open(options_file, "r", encoding="utf-8") as file:
            options = json.load(file)

        token = options.get("tmdb_token")

        if not token:
            print("ERROR: TMDB token is not configured")
            return None

        return token.strip()

    except Exception as error:
        print(f"ERROR reading TMDB token: {error}")
        return None


# ==========================================
# TMDB SEARCH
# ==========================================

def search_tmdb(title, media_type):

    token = get_tmdb_token()

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

        # ==========================================
        # FIND BEST MATCH
        # ==========================================

        best_result = None
        best_score = 0

        search_title = title.lower().strip()

        for result in results[:10]:

            if media_type == "Movie":
                result_title = result.get("title", "")
            else:
                result_title = result.get("name", "")

            if not result_title:
                continue

            result_title_lower = result_title.lower().strip()

            score = fuzz.ratio(
                search_title,
                result_title_lower,
            )

            partial_score = fuzz.partial_ratio(
                search_title,
                result_title_lower,
            )

            token_score = fuzz.token_set_ratio(
                search_title,
                result_title_lower,
            )

            final_score = max(
                score,
                partial_score,
                token_score,
            )

            print(
                f"TMDB candidate: {result_title} "
                f"(score={final_score:.1f})"
            )

            if final_score > best_score:
                best_score = final_score
                best_result = result

        # ==========================================
        # CONFIDENCE CHECK
        # ==========================================

        if not best_result or best_score < 60:

            print(
                f"No confident TMDB match for "
                f"'{title}' "
                f"(score={best_score:.1f})"
            )

            return None

        # ==========================================
        # TITLE / DATE
        # ==========================================

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

        # ==========================================
        # POSTER
        # ==========================================

        poster = best_result.get("poster_path")

        if poster:

            poster = (
                "https://image.tmdb.org/t/p/w500"
                + poster
            )

        # ==========================================
        # BACKDROP
        # ==========================================

        backdrop = best_result.get(
            "backdrop_path"
        )

        if backdrop:

            backdrop = (
                "https://image.tmdb.org/t/p/w1280"
                + backdrop
            )

        # ==========================================
        # YEAR
        # ==========================================

        year = None

        if date:

            try:
                year = int(date[:4])

            except (ValueError, TypeError):
                year = None

        # ==========================================
        # RETURN
        # ==========================================

        return {
            "poster": poster,
            "backdrop": backdrop,
            "year": year,
            "overview": (
                best_result.get("overview")
                or ""
            ),
            "tmdb_id": best_result.get("id"),
        }

    except requests.RequestException as error:

        print(
            f"TMDB request error: {error}"
        )

        return None

    except Exception as error:

        print(
            f"TMDB error: {error}"
        )

        return None


# ==========================================
# HOME
# ==========================================

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


# ==========================================
# ADD MOVIE / SERIES
# ==========================================

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


# ==========================================
# DELETE
# ==========================================

@app.post("/delete/{movie_id}")
def delete(movie_id: int):

    delete_movie(movie_id)

    return RedirectResponse(
        "/",
        status_code=303,
    )

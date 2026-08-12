import json
from pathlib import Path

import requests

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


# --------------------------------------------------
# Home Assistant configuration
# --------------------------------------------------

def get_tmdb_token():
    options_file = Path("/data/options.json")

    try:
        if not options_file.exists():
            print("Home Assistant options.json not found")
            return None

        with options_file.open("r", encoding="utf-8") as file:
            options = json.load(file)

        token = options.get("tmdb_token")

        if not token:
            print("TMDB token is not configured")
            return None

        return token.strip()

    except Exception as error:
        print(f"Could not read Home Assistant configuration: {error}")
        return None


# --------------------------------------------------
# TMDB search
# --------------------------------------------------

def search_tmdb(title, media_type):
    token = get_tmdb_token()

    if not token:
        return None

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
            print(f"TMDB: no results found for '{title}'")
            return None

        result = results[0]

        poster = result.get("poster_path")
        backdrop = result.get("backdrop_path")

        if poster:
            poster = f"https://image.tmdb.org/t/p/w500{poster}"

        if backdrop:
            backdrop = f"https://image.tmdb.org/t/p/w1280{backdrop}"

        if media_type == "Movie":
            date = result.get("release_date", "")
        else:
            date = result.get("first_air_date", "")

        year = None

        if date:
            try:
                year = int(date[:4])
            except (ValueError, TypeError):
                year = None

        return {
            "poster": poster,
            "backdrop": backdrop,
            "year": year,
            "overview": result.get("overview") or "",
            "tmdb_id": result.get("id"),
        }

    except requests.RequestException as error:
        print(f"TMDB request error: {error}")
        return None

    except Exception as error:
        print(f"TMDB error: {error}")
        return None


# --------------------------------------------------
# Home page
# --------------------------------------------------

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


# --------------------------------------------------
# Add movie / series
# --------------------------------------------------

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
        print(f"Searching TMDB for: {title}")

        tmdb_data = search_tmdb(title, media_type)

        if tmdb_data:
            print(
                f"TMDB match found: "
                f"{tmdb_data.get('tmdb_id')}"
            )

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
                f"Saving without poster."
            )

            add_movie(
                title=title,
                rating=rating,
                media_type=media_type,
            )

    return RedirectResponse("/", status_code=303)


# --------------------------------------------------
# Delete
# --------------------------------------------------

@app.post("/delete/{movie_id}")
def delete(movie_id: int):
    delete_movie(movie_id)

    return RedirectResponse("/", status_code=303)

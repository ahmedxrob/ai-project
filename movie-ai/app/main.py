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

        for result in results[:10]:

            if media_type == "Movie":
                result_title = result.get("title", "")
            else:
                result_title = result.get("name", "")

            if not result_title:
                continue

            result_title_lower = result_title.lower().strip()

            # Normal similarity
            ratio = fuzz.ratio(
                search_title,
                result_title_lower,
            )

            # Token similarity
            token_score = fuzz.token_sort_ratio(
                search_title,
                result_title_lower,
            )

            # Use average instead of max.
            # This prevents unrelated titles containing the
            # searched word from getting 100%.
            score = (ratio * 0.6) + (token_score * 0.4)

            print(
                f"TMDB candidate: {result_title} "
                f"(score={score:.1f})"
            )

            if score > best_score:
                best_score = score
                best_result = result

        # Require a reasonably strong match
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

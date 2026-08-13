import os
import random
import math
import requests

from datetime import datetime

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
    add_recommendation_history,
    get_recent_recommendation_ids,
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
# CONSTANTS
# ============================================================

RESULTS_PER_TYPE = 12
MAX_HISTORY = 500

CURRENT_YEAR = datetime.now().year


# ============================================================
# ENV
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(name, "").strip()


# ============================================================
# TMDB REQUEST
# ============================================================

def tmdb_request(
    endpoint: str,
    token: str,
    params=None,
):
    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }

    response = requests.get(
        endpoint,
        headers=headers,
        params=params or {},
        timeout=12,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# TMDB DETAILS
# ============================================================

def tmdb_get_details(
    tmdb_id: int,
    media_type: str,
    token: str,
):
    if media_type == "Movie":

        endpoint = (
            f"https://api.themoviedb.org/3/movie/{tmdb_id}"
        )

    else:

        endpoint = (
            f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        )

    return tmdb_request(
        endpoint,
        token,
        {
            "language": "en-US",
        },
    )


# ============================================================
# TMDB SIMILAR
# ============================================================

def tmdb_get_similar(
    tmdb_id: int,
    media_type: str,
    token: str,
    page: int = 1,
):
    if media_type == "Movie":

        endpoint = (
            f"https://api.themoviedb.org/3/movie/"
            f"{tmdb_id}/similar"
        )

    else:

        endpoint = (
            f"https://api.themoviedb.org/3/tv/"
            f"{tmdb_id}/similar"
        )

    return tmdb_request(
        endpoint,
        token,
        {
            "language": "en-US",
            "page": page,
        },
    ).get(
        "results",
        [],
    )


# ============================================================
# TMDB DISCOVER
# ============================================================

def tmdb_discover(
    media_type: str,
    token: str,
    genre_ids=None,
    page: int = 1,
):
    if media_type == "Movie":

        endpoint = (
            "https://api.themoviedb.org/3/discover/movie"
        )

    else:

        endpoint = (
            "https://api.themoviedb.org/3/discover/tv"
        )

    params = {
        "language": "en-US",
        "page": page,
        "include_adult": "false",
        "sort_by": "popularity.desc",
        "vote_count.gte": 200,
    }

    if genre_ids:

        params["with_genres"] = "|".join(
            str(x)
            for x in genre_ids
        )

    if media_type == "Movie":

        params["with_original_language"] = "en"

    return tmdb_request(
        endpoint,
        token,
        params,
    ).get(
        "results",
        [],
    )


# ============================================================
# TMDB SEARCH
# ============================================================

def tmdb_search(
    title: str,
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

    return tmdb_request(
        endpoint,
        token,
        {
            "query": title,
            "language": "en-US",
            "include_adult": "false",
        }
    ).get(
        "results",
        [],
    )


# ============================================================
# SPELLING
# ============================================================

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

        return [
            item.get("word", "").strip()
            for item in response.json()
            if item.get("word", "").strip()
        ]

    except Exception:

        return []


# ============================================================
# TMDB SEARCH RESULT SCORING
# ============================================================

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


def search_score(
    requested,
    actual,
):

    requested = requested.lower().strip()
    actual = actual.lower().strip()

    ratio = fuzz.ratio(
        requested,
        actual,
    )

    token_score = fuzz.token_sort_ratio(
        requested,
        actual,
    )

    score = (
        ratio * 0.65
        + token_score * 0.35
    )

    if requested == actual:
        score = 100

    return score


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

    try:

        results = tmdb_search(
            title,
            media_type,
            token,
        )

        best_result = None
        best_score = 0

        for result in results[:20]:

            result_title = get_result_title(
                result,
                media_type,
            )

            if not result_title:
                continue

            score = search_score(
                title,
                result_title,
            )

            if score > best_score:

                best_score = score
                best_result = result

        if (
            best_result
            and best_score >= 60
        ):

            return normalise_tmdb_result(
                best_result,
                media_type,
            )

    except requests.RequestException as error:

        print(
            f"TMDB search error: {error}"
        )

    # Spelling fallback

    for suggestion in get_spelling_suggestions(
        title
    ):

        if (
            suggestion.lower()
            == title.lower()
        ):
            continue

        try:

            results = tmdb_search(
                suggestion,
                media_type,
                token,
            )

            best_result = None
            best_score = 0

            for result in results[:20]:

                result_title = get_result_title(
                    result,
                    media_type,
                )

                if not result_title:
                    continue

                score = search_score(
                    suggestion,
                    result_title,
                )

                if score > best_score:

                    best_score = score
                    best_result = result

            if (
                best_result
                and best_score >= 60
            ):

                return normalise_tmdb_result(
                    best_result,
                    media_type,
                )

        except requests.RequestException:
            continue

    return None


# ============================================================
# NORMALISE TMDB RESULT
# ============================================================

def normalise_tmdb_result(
    result,
    media_type,
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
            pass

    poster = None

    if result.get(
        "poster_path"
    ):

        poster = (
            "https://image.tmdb.org/t/p/w500"
            + result["poster_path"]
        )

    backdrop = None

    if result.get(
        "backdrop_path"
    ):

        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + result["backdrop_path"]
        )

    return {
        "title": title,
        "tmdb_id": result.get(
            "id"
        ),
        "poster": poster,
        "backdrop": backdrop,
        "year": year,
        "overview": result.get(
            "overview"
        ) or "",
        "vote_average": result.get(
            "vote_average",
            0,
        ),
        "vote_count": result.get(
            "vote_count",
            0,
        ),
        "popularity": result.get(
            "popularity",
            0,
        ),
        "genre_ids": result.get(
            "genre_ids",
            [],
        ),
    }


# ============================================================
# USER TASTE PROFILE
# ============================================================

def build_taste_profile(
    watched,
    media_type,
    token,
):
    relevant = [
        movie
        for movie in watched
        if movie["type"] == media_type
    ]

    relevant.sort(
        key=lambda x: float(
            x["rating"]
        ),
        reverse=True,
    )

    relevant = relevant[:6]

    genre_scores = {}

    source_titles = []

    for movie in relevant:

        tmdb_id = movie["tmdb_id"]

        if not tmdb_id:
            continue

        try:

            details = tmdb_get_details(
                tmdb_id,
                media_type,
                token,
            )

        except requests.RequestException:

            continue

        rating = float(
            movie["rating"]
        )

        # High ratings have much more influence.
        rating_weight = (
            rating / 10
        ) ** 2

        for genre in details.get(
            "genres",
            [],
        ):

            genre_id = genre.get(
                "id"
            )

            if genre_id is None:
                continue

            genre_scores[genre_id] = (
                genre_scores.get(
                    genre_id,
                    0,
                )
                + rating_weight
            )

        source_titles.append(
            {
                "id": tmdb_id,
                "rating_weight": rating_weight,
                "title": movie["title"],
            }
        )

    ranked_genres = sorted(
        genre_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    return {
        "genres": ranked_genres,
        "sources": source_titles,
    }


# ============================================================
# CANDIDATE COLLECTION
# ============================================================

def collect_candidates(
    watched,
    media_type,
    token,
):
    profile = build_taste_profile(
        watched,
        media_type,
        token,
    )

    candidates = {}

    watched_ids = {
        movie["tmdb_id"]
        for movie in watched
        if (
            movie["type"] == media_type
            and movie["tmdb_id"]
        )
    }

    recent_ids = get_recent_recommendation_ids(
        media_type,
        MAX_HISTORY,
    )

    excluded_ids = (
        watched_ids
        | set(recent_ids)
    )

    # --------------------------------------------------------
    # Similar titles from highly rated watched titles
    # --------------------------------------------------------

    for source in profile["sources"]:

        try:

            similar = tmdb_get_similar(
                source["id"],
                media_type,
                token,
                page=1,
            )

        except requests.RequestException:

            continue

        for index, item in enumerate(
            similar[:20]
        ):

            tmdb_id = item.get(
                "id"
            )

            if not tmdb_id:
                continue

            if tmdb_id in excluded_ids:
                continue

            if media_type == "Movie":

                title = item.get(
                    "title",
                    "",
                )

                date = item.get(
                    "release_date",
                    "",
                )

            else:

                title = item.get(
                    "name",
                    "",
                )

                date = item.get(
                    "first_air_date",
                    "",
                )

            if not title:
                continue

            candidate = candidates.setdefault(
                tmdb_id,
                {
                    "id": tmdb_id,
                    "title": title,
                    "date": date,
                    "poster_path": item.get(
                        "poster_path"
                    ),
                    "backdrop_path": item.get(
                        "backdrop_path"
                    ),
                    "overview": item.get(
                        "overview"
                    ) or "",
                    "vote_average": item.get(
                        "vote_average",
                        0,
                    ),
                    "vote_count": item.get(
                        "vote_count",
                        0,
                    ),
                    "popularity": item.get(
                        "popularity",
                        0,
                    ),
                    "genre_ids": item.get(
                        "genre_ids",
                        [],
                    ),
                    "similarity": 0,
                },
            )

            rank_score = max(
                0.35,
                1
                - (
                    index
                    * 0.035
                ),
            )

            candidate[
                "similarity"
            ] += (
                source["rating_weight"]
                * rank_score
            )

    # --------------------------------------------------------
    # Discover using top user genres
    # --------------------------------------------------------

    top_genres = [
        genre_id
        for genre_id, score
        in profile["genres"][:3]
    ]

    # Add individual genre discoveries
    for genre_id in top_genres[:3]:

        try:

            discovered = tmdb_discover(
                media_type,
                token,
                [genre_id],
                page=1,
            )

        except requests.RequestException:

            continue

        for item in discovered:

            tmdb_id = item.get(
                "id"
            )

            if not tmdb_id:
                continue

            if tmdb_id in excluded_ids:
                continue

            if media_type == "Movie":

                title = item.get(
                    "title",
                    "",
                )

                date = item.get(
                    "release_date",
                    "",
                )

            else:

                title = item.get(
                    "name",
                    "",
                )

                date = item.get(
                    "first_air_date",
                    "",
                )

            if not title:
                continue

            candidate = candidates.setdefault(
                tmdb_id,
                {
                    "id": tmdb_id,
                    "title": title,
                    "date": date,
                    "poster_path": item.get(
                        "poster_path"
                    ),
                    "backdrop_path": item.get(
                        "backdrop_path"
                    ),
                    "overview": item.get(
                        "overview"
                    ) or "",
                    "vote_average": item.get(
                        "vote_average",
                        0,
                    ),
                    "vote_count": item.get(
                        "vote_count",
                        0,
                    ),
                    "popularity": item.get(
                        "popularity",
                        0,
                    ),
                    "genre_ids": item.get(
                        "genre_ids",
                        [],
                    ),
                    "similarity": 0,
                },
            )

            candidate[
                "genre_discovery"
            ] = candidate.get(
                "genre_discovery",
                0,
            ) + 1

    return (
        candidates,
        profile,
    )


# ============================================================
# SCORE CANDIDATES
# ============================================================

def calculate_genre_score(
    candidate,
    genre_profile,
):
    genre_ids = set(
        candidate.get(
            "genre_ids",
            [],
        )
    )

    if not genre_ids:
        return 0

    total = 0

    for genre_id, weight in genre_profile:

        if genre_id in genre_ids:

            total += weight

    if not genre_profile:
        return 0

    maximum = sum(
        weight
        for _, weight
        in genre_profile[:3]
    )

    if maximum <= 0:
        return 0

    return min(
        total / maximum,
        1.0,
    )


def calculate_quality_score(
    candidate,
):
    vote_average = float(
        candidate.get(
            "vote_average",
            0,
        )
        or 0
    )

    vote_count = int(
        candidate.get(
            "vote_count",
            0,
        )
        or 0
    )

    rating_score = min(
        vote_average / 10,
        1,
    )

    confidence = min(
        math.log1p(
            vote_count
        )
        /
        math.log1p(
            5000
        ),
        1,
    )

    return (
        rating_score
        * (
            0.45
            + 0.55
            * confidence
        )
    )


def calculate_popularity_score(
    candidate,
    all_candidates,
):
    values = [
        float(
            item.get(
                "popularity",
                0,
            )
            or 0
        )
        for item in all_candidates.values()
    ]

    if not values:
        return 0

    maximum = max(
        values
    )

    if maximum <= 0:
        return 0

    value = float(
        candidate.get(
            "popularity",
            0,
        )
        or 0
    )

    return min(
        value / maximum,
        1,
    )


def calculate_recency_score(
    candidate,
):
    date = candidate.get(
        "date",
        "",
    )

    if not date:
        return 0.5

    try:

        year = int(
            date[:4]
        )

    except (
        ValueError,
        TypeError,
    ):

        return 0.5

    age = max(
        0,
        CURRENT_YEAR - year,
    )

    return max(
        0.25,
        1
        - (
            age / 20
        ),
    )


def score_candidate(
    candidate,
    profile,
    all_candidates,
):
    similarity = min(
        float(
            candidate.get(
                "similarity",
                0,
            )
        ),
        1,
    )

    genre_score = calculate_genre_score(
        candidate,
        profile["genres"],
    )

    quality = calculate_quality_score(
        candidate,
    )

    popularity = calculate_popularity_score(
        candidate,
        all_candidates,
    )

    recency = calculate_recency_score(
        candidate,
    )

    # Main recommendation formula.
    #
    # Similarity = strongest signal.
    # Genre = second strongest.
    # Quality = helps avoid bad/unknown titles.
    # Popularity = small quality-of-discovery signal.
    # Recency = small freshness bonus.

    score = (
        similarity * 0.42
        + genre_score * 0.28
        + quality * 0.18
        + popularity * 0.07
        + recency * 0.05
    )

    # Tiny random component so repeated
    # refreshes can produce different results.

    score += random.uniform(
        0,
        0.035,
    )

    candidate["score"] = score

    return candidate


# ============================================================
# DIVERSITY RE-RANK
# ============================================================

def diversify(
    candidates,
    limit,
):
    ranked = sorted(
        candidates,
        key=lambda x: x["score"],
        reverse=True,
    )

    selected = []

    used_genres = set()

    for candidate in ranked:

        genres = set(
            candidate.get(
                "genre_ids",
                [],
            )
        )

        # Prefer candidates that add
        # something different to the list.

        overlap = len(
            genres
            & used_genres
        )

        if (
            selected
            and overlap >= 3
        ):
            continue

        selected.append(
            candidate
        )

        used_genres.update(
            genres
        )

        if len(selected) >= limit:
            break

    # If diversity reduced the list too much,
    # fill remaining slots by score.

    if len(selected) < limit:

        selected_ids = {
            item["id"]
            for item in selected
        }

        for candidate in ranked:

            if candidate["id"] in selected_ids:
                continue

            selected.append(
                candidate
            )

            if len(selected) >= limit:
                break

    return selected


# ============================================================
# BUILD RECOMMENDATIONS
# ============================================================

def build_recommendations(
    watched,
    media_type,
):
    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:

        return []

    try:

        candidates, profile = (
            collect_candidates(
                watched,
                media_type,
                token,
            )
        )

    except requests.RequestException as error:

        print(
            f"TMDB candidate error: {error}"
        )

        return []

    if not candidates:
        return []

    scored = []

    for candidate in candidates.values():

        scored.append(
            score_candidate(
                candidate,
                profile,
                candidates,
            )
        )

    selected = diversify(
        scored,
        RESULTS_PER_TYPE,
    )

    results = []

    for candidate in selected:

        result = {
            "media_type": media_type,
            "tmdb_id": candidate["id"],
            "title": candidate["title"],
            "year": None,
            "poster": None,
            "backdrop": None,
            "overview": candidate["overview"],
            "vote_average": candidate[
                "vote_average"
            ],
            "vote_count": candidate[
                "vote_count"
            ],
            "popularity": candidate[
                "popularity"
            ],
            "score": candidate[
                "score"
            ],
        }

        date = candidate.get(
            "date",
            "",
        )

        if date:

            try:
                result["year"] = int(
                    date[:4]
                )
            except (
                ValueError,
                TypeError,
            ):
                pass

        if candidate.get(
            "poster_path"
        ):

            result["poster"] = (
                "https://image.tmdb.org/t/p/w500"
                + candidate["poster_path"]
            )

        if candidate.get(
            "backdrop_path"
        ):

            result["backdrop"] = (
                "https://image.tmdb.org/t/p/w1280"
                + candidate["backdrop_path"]
            )

        results.append(
            result
        )

    # Store recommendation history.
    for result in results:

        add_recommendation_history(
            tmdb_id=result["tmdb_id"],
            media_type=media_type,
            title=result["title"],
        )

    return results


# ============================================================
# GENERATE ALL
# ============================================================

def generate_all_recommendations(
    watched,
):
    movies = build_recommendations(
        watched,
        "Movie",
    )

    series = build_recommendations(
        watched,
        "Series",
    )

    return {
        "movies": movies,
        "series": series,
    }


# ============================================================
# HOME
# ============================================================

@app.get("/")
@app.get("//")
def home(
    request: Request,
):

    movies = get_all()

    watched_movies = [
        movie
        for movie in movies
        if movie["type"] == "Movie"
    ]

    watched_series = [
        movie
        for movie in movies
        if movie["type"] == "Series"
    ]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": movies,
            "watched_movies": watched_movies,
            "watched_series": watched_series,
            "recommendations": None,
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
# RECOMMENDATIONS
# ============================================================

@app.get("/recommendations")
def recommendations(
    request: Request,
):
    movies = get_all()

    watched_movies = [
        movie
        for movie in movies
        if movie["type"] == "Movie"
    ]

    watched_series = [
        movie
        for movie in movies
        if movie["type"] == "Series"
    ]

    recommendations_data = (
        generate_all_recommendations(
            movies
        )
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "movies": movies,
            "watched_movies": watched_movies,
            "watched_series": watched_series,
            "recommendations": recommendations_data,
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
    tmdb_id: int = Form(...),
):
    title = title.strip()

    if (
        not title
        or not 0 <= rating <= 10
        or media_type not in (
            "Movie",
            "Series",
        )
        or not tmdb_id
    ):

        return RedirectResponse(
            "/",
            status_code=303,
        )

    token = get_env(
        "TMDB_TOKEN"
    )

    tmdb_data = None

    if token:

        try:

            result = tmdb_get_details(
                tmdb_id,
                media_type,
                token,
            )

            tmdb_data = normalise_tmdb_result(
                result,
                media_type,
            )

        except requests.RequestException as error:

            print(
                f"TMDB exact lookup failed: "
                f"{error}"
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
            tmdb_id=tmdb_id,
        )

    # After watching something,
    # immediately generate a completely
    # fresh recommendation page.

    return RedirectResponse(
        "/recommendations",
        status_code=303,
    )


# ============================================================
# DELETE
# ============================================================

@app.post(
    "/delete/{movie_id}"
)
def delete(
    movie_id: int,
):

    delete_movie(
        movie_id
    )

    return RedirectResponse(
        "/",
        status_code=303,
    )


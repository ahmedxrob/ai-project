import math
import os
import random
import requests

from datetime import datetime

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from rapidfuzz import fuzz

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
# SETTINGS
# ============================================================

RESULTS_PER_TYPE = 12
RECENT_HISTORY_LIMIT = 40
MIN_VOTE_COUNT = 20

CURRENT_YEAR = datetime.now().year


# ============================================================
# ENVIRONMENT
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
        timeout=15,
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
    endpoint = (
        f"https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        f"/{tmdb_id}"
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
    endpoint = (
        f"https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        f"/{tmdb_id}/similar"
    )

    data = tmdb_request(
        endpoint,
        token,
        {
            "language": "en-US",
            "page": page,
        },
    )

    return data.get(
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
    sort_by="popularity.desc",
):
    endpoint = (
        "https://api.themoviedb.org/3/"
        f"discover/{'movie' if media_type == 'Movie' else 'tv'}"
    )

    params = {
        "language": "en-US",
        "page": page,
        "include_adult": "false",
        "include_video": "false",
        "sort_by": sort_by,
        "vote_count.gte": MIN_VOTE_COUNT,
    }

    if genre_ids:
        params["with_genres"] = "|".join(
            str(x)
            for x in genre_ids
        )

    return tmdb_request(
        endpoint,
        token,
        params,
    ).get(
        "results",
        [],
    )


# ============================================================
# TMDB TOP RATED
# ============================================================

def tmdb_top_rated(
    media_type: str,
    token: str,
    page: int = 1,
):
    endpoint = (
        "https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        "/top_rated"
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
# TMDB SEARCH
# ============================================================

def tmdb_search(
    title: str,
    media_type: str,
    token: str,
):
    endpoint = (
        "https://api.themoviedb.org/3/"
        f"search/{'movie' if media_type == 'Movie' else 'tv'}"
    )

    return tmdb_request(
        endpoint,
        token,
        {
            "query": title,
            "language": "en-US",
            "include_adult": "false",
        },
    ).get(
        "results",
        [],
    )


def get_result_title(
    result,
    media_type,
):
    if media_type == "Movie":
        return result.get("title", "")

    return result.get("name", "")


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


# ============================================================
# NORMALISE TMDB ITEM
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

    if result.get("poster_path"):
        poster = (
            "https://image.tmdb.org/t/p/w500"
            + result["poster_path"]
        )

    backdrop = None

    if result.get("backdrop_path"):
        backdrop = (
            "https://image.tmdb.org/t/p/w1280"
            + result["backdrop_path"]
        )

    return {
        "title": title,
        "tmdb_id": result.get("id"),
        "poster": poster,
        "backdrop": backdrop,
        "year": year,
        "overview": result.get("overview") or "",
        "vote_average": float(
            result.get(
                "vote_average",
                0,
            )
            or 0
        ),
        "vote_count": int(
            result.get(
                "vote_count",
                0,
            )
            or 0
        ),
        "popularity": float(
            result.get(
                "popularity",
                0,
            )
            or 0
        ),
        "genre_ids": result.get(
            "genre_ids",
            [],
        ),
    }


# ============================================================
# SEARCH TMDB FOR A WATCHED TITLE
# ============================================================

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

    suggestions = get_spelling_suggestions(
        title
    )

    print(
        f"Spelling suggestions: "
        f"{suggestions}"
    )

    for suggestion in suggestions:

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
        except requests.RequestException:
            continue

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
            print(
                f"Corrected title matched: "
                f"{suggestion}"
            )

            return normalise_tmdb_result(
                best_result,
                media_type,
            )

    print(
        f"No confident TMDB match for: "
        f"{title}"
    )

    return None


# ============================================================
# BUILD USER TASTE
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
        and movie["tmdb_id"]
    ]

    relevant.sort(
        key=lambda x: float(
            x["rating"]
        ),
        reverse=True,
    )

    # Highest rated titles matter most.
    relevant = relevant[:8]

    genre_scores = {}

    sources = []

    for movie in relevant:

        try:
            details = tmdb_get_details(
                movie["tmdb_id"],
                media_type,
                token,
            )
        except requests.RequestException:
            continue

        rating = float(
            movie["rating"]
        )

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

        sources.append(
            {
                "id": movie["tmdb_id"],
                "rating_weight": rating_weight,
                "title": movie["title"],
            }
        )

    ranked_genres = sorted(
        genre_scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    return {
        "genres": ranked_genres,
        "sources": sources,
    }


# ============================================================
# CREATE CANDIDATE
# ============================================================

def make_candidate(item):
    if "title" in item:
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

    return {
        "id": item.get("id"),
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
        "vote_average": float(
            item.get(
                "vote_average",
                0,
            )
            or 0
        ),
        "vote_count": int(
            item.get(
                "vote_count",
                0,
            )
            or 0
        ),
        "popularity": float(
            item.get(
                "popularity",
                0,
            )
            or 0
        ),
        "genre_ids": item.get(
            "genre_ids",
            [],
        ),
        "similarity": 0,
        "genre_discovery": 0,
    }


# ============================================================
# COLLECT LARGE CANDIDATE POOL
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

    recent_ids = set(
        get_recent_recommendation_ids(
            media_type,
            RECENT_HISTORY_LIMIT,
        )
    )

    excluded_ids = (
        watched_ids
        | recent_ids
    )

    # ========================================================
    # 1. SIMILAR TITLES
    # ========================================================

    for source in profile["sources"]:

        for page in (1, 2, 3):

            try:
                similar = tmdb_get_similar(
                    source["id"],
                    media_type,
                    token,
                    page,
                )
            except requests.RequestException:
                continue

            for index, item in enumerate(
                similar
            ):

                tmdb_id = item.get(
                    "id"
                )

                if not tmdb_id:
                    continue

                if tmdb_id in excluded_ids:
                    continue

                title = (
                    item.get("title", "")
                    if media_type == "Movie"
                    else item.get("name", "")
                )

                if not title:
                    continue

                if tmdb_id not in candidates:

                    candidates[tmdb_id] = (
                        make_candidate(item)
                    )

                rank_score = max(
                    0.25,
                    1
                    - (
                        index * 0.025
                    ),
                )

                candidates[
                    tmdb_id
                ]["similarity"] += (
                    source[
                        "rating_weight"
                    ]
                    * rank_score
                )

    # ========================================================
    # 2. TOP GENRE DISCOVERY
    # ========================================================

    top_genres = [
        genre_id
        for genre_id, _score
        in profile["genres"][:5]
    ]

    for genre_id in top_genres:

        for page in (1, 2):

            try:
                discovered = tmdb_discover(
                    media_type,
                    token,
                    [genre_id],
                    page,
                    "popularity.desc",
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

                title = (
                    item.get("title", "")
                    if media_type == "Movie"
                    else item.get("name", "")
                )

                if not title:
                    continue

                if tmdb_id not in candidates:

                    candidates[tmdb_id] = (
                        make_candidate(item)
                    )

                candidates[
                    tmdb_id
                ]["genre_discovery"] += 1

    # ========================================================
    # 3. POPULAR FALLBACK
    # ========================================================

    for page in (1, 2, 3, 4):

        try:
            popular = tmdb_discover(
                media_type,
                token,
                None,
                page,
                "popularity.desc",
            )
        except requests.RequestException:
            continue

        for item in popular:

            tmdb_id = item.get(
                "id"
            )

            if not tmdb_id:
                continue

            if tmdb_id in excluded_ids:
                continue

            title = (
                item.get("title", "")
                if media_type == "Movie"
                else item.get("name", "")
            )

            if not title:
                continue

            candidates.setdefault(
                tmdb_id,
                make_candidate(item),
            )

    # ========================================================
    # 4. TOP RATED FALLBACK
    # ========================================================

    for page in (1, 2):

        try:
            top_rated = tmdb_top_rated(
                media_type,
                token,
                page,
            )
        except requests.RequestException:
            continue

        for item in top_rated:

            tmdb_id = item.get(
                "id"
            )

            if not tmdb_id:
                continue

            if tmdb_id in excluded_ids:
                continue

            title = (
                item.get("title", "")
                if media_type == "Movie"
                else item.get("name", "")
            )

            if not title:
                continue

            candidates.setdefault(
                tmdb_id,
                make_candidate(item),
            )

    print(
        f"{media_type}: "
        f"{len(candidates)} candidates collected"
    )

    return candidates, profile


# ============================================================
# SCORE FUNCTIONS
# ============================================================

def genre_score(
    candidate,
    profile,
):
    candidate_genres = set(
        candidate.get(
            "genre_ids",
            [],
        )
    )

    if not candidate_genres:
        return 0

    total = 0

    for genre_id, weight in profile[
        "genres"
    ]:

        if genre_id in candidate_genres:
            total += weight

    maximum = sum(
        weight
        for _, weight
        in profile["genres"][:5]
    )

    if maximum <= 0:
        return 0

    return min(
        total / maximum,
        1.0,
    )


def quality_score(
    candidate,
):
    rating = float(
        candidate.get(
            "vote_average",
            0,
        )
        or 0
    )

    votes = int(
        candidate.get(
            "vote_count",
            0,
        )
        or 0
    )

    rating_part = min(
        rating / 10,
        1.0,
    )

    vote_confidence = min(
        math.log1p(votes)
        /
        math.log1p(3000),
        1.0,
    )

    return (
        rating_part
        * (
            0.45
            + 0.55
            * vote_confidence
        )
    )


def popularity_score(
    candidate,
    candidates,
):
    all_values = [
        float(
            item.get(
                "popularity",
                0,
            )
            or 0
        )
        for item in candidates.values()
    ]

    if not all_values:
        return 0

    maximum = max(
        all_values
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
        1.0,
    )


def recency_score(
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
        0.20,
        1
        - (
            age / 20
        ),
    )


def score_candidate(
    candidate,
    profile,
    candidates,
):
    similarity = min(
        float(
            candidate.get(
                "similarity",
                0,
            )
        ),
        1.0,
    )

    genres = genre_score(
        candidate,
        profile,
    )

    quality = quality_score(
        candidate,
    )

    popularity = popularity_score(
        candidate,
        candidates,
    )

    recency = recency_score(
        candidate,
    )

    genre_discovery = min(
        candidate.get(
            "genre_discovery",
            0,
        )
        /
        3,
        1.0,
    )

    score = (
        similarity * 0.38
        + genres * 0.27
        + quality * 0.18
        + popularity * 0.12
        + recency * 0.05
        + genre_discovery * 0.04
    )

    # Small random factor so each refresh
    # can produce a different set.
    score += random.uniform(
        0,
        0.035,
    )

    candidate["score"] = score

    return candidate


# ============================================================
# DIVERSITY
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

        overlap = len(
            genres
            & used_genres
        )

        if (
            len(selected) >= 4
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
# FINAL RECOMMENDATION BUILDER
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
            f"Candidate collection error: "
            f"{error}"
        )
        return []

    if not candidates:
        return []

    scored = [
        score_candidate(
            candidate,
            profile,
            candidates,
        )
        for candidate
        in candidates.values()
    ]

    selected = diversify(
        scored,
        RESULTS_PER_TYPE,
    )

    results = []

    for candidate in selected:

        date = candidate.get(
            "date",
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

        if candidate.get(
            "poster_path"
        ):
            poster = (
                "https://image.tmdb.org/t/p/w500"
                + candidate[
                    "poster_path"
                ]
            )

        backdrop = None

        if candidate.get(
            "backdrop_path"
        ):
            backdrop = (
                "https://image.tmdb.org/t/p/w1280"
                + candidate[
                    "backdrop_path"
                ]
            )

        result = {
            "media_type": media_type,
            "tmdb_id": candidate["id"],
            "title": candidate["title"],
            "year": year,
            "poster": poster,
            "backdrop": backdrop,
            "overview": candidate[
                "overview"
            ],
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

        results.append(
            result
        )

        add_recommendation_history(
            tmdb_id=result["tmdb_id"],
            media_type=media_type,
            title=result["title"],
        )

    return results


def generate_all_recommendations(
    watched,
):
    return {
        "movies": build_recommendations(
            watched,
            "Movie",
        ),
        "series": build_recommendations(
            watched,
            "Series",
        ),
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
            poster=tmdb_data.get("poster"),
            backdrop=tmdb_data.get("backdrop"),
            year=tmdb_data.get("year"),
            overview=tmdb_data.get("overview"),
            tmdb_id=tmdb_data.get("tmdb_id"),
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

            tmdb_data = (
                normalise_tmdb_result(
                    result,
                    media_type,
                )
            )

        except requests.RequestException as error:

            print(
                f"Exact TMDB lookup failed: "
                f"{error}"
            )

    if tmdb_data:

        add_movie(
            title=tmdb_data["title"],
            rating=rating,
            media_type=media_type,
            poster=tmdb_data.get("poster"),
            backdrop=tmdb_data.get("backdrop"),
            year=tmdb_data.get("year"),
            overview=tmdb_data.get("overview"),
            tmdb_id=tmdb_data.get("tmdb_id"),
        )

    else:

        add_movie(
            title=title,
            rating=rating,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )

    # Immediately generate a new batch.
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


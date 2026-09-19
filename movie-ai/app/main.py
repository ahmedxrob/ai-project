import json
import os
import random
import time
import requests
import threading
import hashlib
from urllib.parse import urlparse

from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from pydantic import BaseModel
from rapidfuzz import fuzz

try:
    from google import genai
    from google.genai import types
except ImportError:
    # Gemini is optional at runtime; TMDB/library features should still start
    # when the optional SDK is unavailable or the API key is not configured.
    genai = None
    types = None

from app.database import (
    DATA_DIR,
    init_database,
    get_all,
    add_movie,
    delete_movie,
    add_recommendation_history,
    get_recent_recommendation_ids,
    add_not_interested,
    get_not_interested_ids,
    get_display_statistics,
    set_display_statistics,
    get_lifetime_statistics,
    increment_lifetime_recommendations,
    increment_lifetime_trending,
    get_tmdb_cache,
    set_tmdb_cache,
    get_latest_recommendation_job,
    start_recommendation_job,
    update_recommendation_job,
    is_current_recommendation_job,
)


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="My Movie AI"
)

templates = Jinja2Templates(
    directory="app/templates"
)

app.mount(
    "/static",
    StaticFiles(
        directory="app/static"
    ),
    name="static",
)

init_database()


# ============================================================
# SETTINGS
# ============================================================

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

AI_MOVIE_CANDIDATES = 30
AI_SERIES_CANDIDATES = 30

AI_MOVIES_TARGET = 8
AI_SERIES_TARGET = 8

TMDB_MOVIES_TARGET = 8
TMDB_SERIES_TARGET = 8

RECENT_HISTORY_LIMIT = 12

WATCHLIST_FILE = DATA_DIR / "watchlist.json"
APP_SETTINGS_FILE = DATA_DIR / "app_settings.json"
DEFAULT_RECOMMENDATION_LIMIT = 8
MIN_RECOMMENDATION_LIMIT = 1
MAX_RECOMMENDATION_LIMIT = 20

def load_app_settings():
    defaults = {"recommendation_limit": DEFAULT_RECOMMENDATION_LIMIT}
    try:
        if APP_SETTINGS_FILE.exists():
            data = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
            value = int(data.get("recommendation_limit", DEFAULT_RECOMMENDATION_LIMIT))
            defaults["recommendation_limit"] = max(MIN_RECOMMENDATION_LIMIT, min(MAX_RECOMMENDATION_LIMIT, value))
    except Exception as error:
        print(f"App settings load error: {error}")
    return defaults

def save_app_settings(settings):
    APP_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = APP_SETTINGS_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temp_file.replace(APP_SETTINGS_FILE)

def get_recommendation_limit():
    return int(load_app_settings().get("recommendation_limit", DEFAULT_RECOMMENDATION_LIMIT))

watchlist_lock = threading.Lock()

# A single HTTP session gives TMDB requests connection reuse plus bounded retries.
TMDB_CACHE_SEARCH_TTL = 45
TMDB_CACHE_TRENDING_TTL = 300
TMDB_CACHE_DETAILS_TTL = 3600

_tmdb_session = None
_tmdb_session_lock = threading.Lock()


# ============================================================
# BACKGROUND RECOMMENDATION JOB
# ============================================================

def run_recommendation_job(prefetched_tmdb=None, job_id=None):
    try:
        print(f"Background AI recommendation job started: {job_id}")
        movies = get_all()
        result = generate_recommendations(movies, prefetched_tmdb=prefetched_tmdb)

        # A newer job may already have superseded this one. Do not let an older
        # result overwrite the live homepage statistics.
        if is_current_recommendation_job(job_id):
            set_display_statistics(
                ai_movies=len(result.get("ai_movies", [])),
                ai_series=len(result.get("ai_series", [])),
                tmdb_movies=len(result.get("tmdb_movies", [])),
                tmdb_series=len(result.get("tmdb_series", [])),
                trending_movies=0,
                trending_series=0,
            )
            update_recommendation_job(job_id, "ready", data=result)
        else:
            update_recommendation_job(
                job_id,
                "superseded",
                error="Superseded by a newer recommendation job.",
            )

        print(f"Background AI recommendation job finished: {job_id}")
    except Exception as error:
        print(f"Background AI recommendation error: {error}")
        update_recommendation_job(job_id, "error", error=str(error))


# ============================================================
# WATCHLIST
# ============================================================

def load_watchlist():
    try:
        if WATCHLIST_FILE.exists():
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as error:
        print(f"Watchlist load error: {error}")
    return []


def save_watchlist(items):
    try:
        WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = WATCHLIST_FILE.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(WATCHLIST_FILE)
    except Exception as error:
        print(f"Watchlist save error: {error}")


def watchlist_key(media_type, tmdb_id):
    return f"{media_type}:{tmdb_id}"


def is_in_watchlist(media_type, tmdb_id):
    key = watchlist_key(media_type, tmdb_id)
    return any(item.get("key") == key for item in load_watchlist())


def toggle_watchlist_item(item):
    # Serialize quick toggles so two rapid clicks cannot overwrite each other.
    with watchlist_lock:
        items = load_watchlist()
        key = watchlist_key(item["media_type"], item["tmdb_id"])
        items = [x for x in items if x.get("key") != key]
        if not item.get("remove", False):
            items.append({
                "key": key,
                "tmdb_id": item["tmdb_id"],
                "media_type": item["media_type"],
                "title": item.get("title", ""),
                "year": item.get("year"),
                "poster": item.get("poster"),
                "backdrop": item.get("backdrop"),
                "overview": item.get("overview", ""),
                "vote_average": item.get("vote_average", 0),
            })
        save_watchlist(items)
        return not item.get("remove", False)


templates.env.globals["is_in_watchlist"] = is_in_watchlist
templates.env.globals["watchlist_key"] = watchlist_key


# ============================================================
# REQUEST SAFETY
# ============================================================

class SameOriginMutationMiddleware(BaseHTTPMiddleware):
    """Block browser cross-site POST/PUT/PATCH/DELETE requests.

    Direct clients without Origin/Referer headers remain supported. This is a
    CSRF hardening layer, not an authentication system.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = (request.headers.get("origin") or "").strip()
            referer = (request.headers.get("referer") or "").strip()
            if origin or referer:
                expected = request.url.hostname
                allowed = True
                for candidate in (origin, referer):
                    if not candidate:
                        continue
                    try:
                        parsed = urlparse(candidate)
                        candidate_host = parsed.hostname
                        candidate_port = parsed.port
                    except ValueError:
                        allowed = False
                        break
                    if candidate_host != expected:
                        allowed = False
                        break
                    if candidate_port and candidate_port != request.url.port:
                        allowed = False
                        break
                if not allowed:
                    return JSONResponse(
                        {"ok": False, "error": "Cross-origin mutation blocked."},
                        status_code=403,
                    )
        return await call_next(request)


app.add_middleware(SameOriginMutationMiddleware)


# ============================================================
# ENVIRONMENT
# ============================================================

def get_env(name: str) -> str:
    return os.getenv(
        name,
        ""
    ).strip()


# ============================================================
# INGRESS-AWARE PATH
# ============================================================

def get_ingress_path(request: Request) -> str:

    path = request.headers.get(
        "x-ingress-path",
        ""
    )

    if not path:
        return ""

    return path.rstrip("/")


def build_app_url(request: Request, path: str = "") -> str:

    ingress_path = get_ingress_path(request)
    clean_path = path.strip("/")

    if ingress_path:
        return f"{ingress_path}/{clean_path}" if clean_path else ingress_path

    return f"/{clean_path}" if clean_path else "/"


def app_redirect(request: Request, path: str = ""):

    return RedirectResponse(
        build_app_url(request, path),
        status_code=303,
    )


# ============================================================
# GEMINI SCHEMA
# ============================================================

class RecommendationItem(BaseModel):

    title: str

    year: Optional[int] = None

    reason: str

    match_percentage: Optional[int] = None


class RecommendationResponse(BaseModel):

    movies: List[
        RecommendationItem
    ]

    series: List[
        RecommendationItem
    ]


# ============================================================
# GEMINI CLIENT
# ============================================================

def get_gemini_client():
    api_key = get_env("GEMINI_API_KEY")

    if not api_key or genai is None:
        return None

    return genai.Client(api_key=api_key)


# ============================================================
# USER PROFILE
# ============================================================

def build_user_profile(watched):

    movies = []
    series = []

    for item in watched:

        data = {
            "title": item["title"],
            "rating": float(
                item["rating"]
            ),
            "year": item["year"],
        }

        if item["type"] == "Movie":
            movies.append(data)
        else:
            series.append(data)

    movies.sort(
        key=lambda x: x["rating"],
        reverse=True,
    )

    series.sort(
        key=lambda x: x["rating"],
        reverse=True,
    )

    return {
        "movies": movies,
        "series": series,
    }


# ============================================================
# GEMINI PROMPT
# ============================================================

def build_gemini_prompt(watched):

    profile = build_user_profile(
        watched
    )

    recent_movie_ids = (
        get_recent_recommendation_ids(
            "Movie",
            RECENT_HISTORY_LIMIT,
        )
    )

    recent_series_ids = (
        get_recent_recommendation_ids(
            "Series",
            RECENT_HISTORY_LIMIT,
        )
    )

    not_interested_movie_ids = (
        get_not_interested_ids(
            "Movie"
        )
    )

    not_interested_series_ids = (
        get_not_interested_ids(
            "Series"
        )
    )

    watched_movie_titles = [
        item["title"]
        for item in watched
        if item["type"] == "Movie"
    ]

    watched_series_titles = [
        item["title"]
        for item in watched
        if item["type"] == "Series"
    ]

    random_nonce = random.randint(
        1,
        999999999,
    )

    prompt = f"""
You are the personalized recommendation engine
for a private movie and TV library.

Study the user's watched titles and ratings.

Return:

- EXACTLY {AI_MOVIE_CANDIDATES} movie candidates
- EXACTLY {AI_SERIES_CANDIDATES} series candidates

The application verifies every title through TMDB.

RULES:

1. Never recommend a watched title.
2. Never recommend a recently recommended title.
3. Never recommend a NOT INTERESTED title.
4. Movies and series are completely separate.
5. Never put a movie in the series list.
6. Never put a series in the movie list.
7. Strongly prioritize titles rated 8/10 or higher.
8. Use low ratings as negative taste signals.
9. Consider:
   - genre
   - tone
   - themes
   - pacing
   - storytelling
   - actors
   - directors
   - franchises
   - audience appeal
10. Include strong matches and interesting discoveries.
11. Avoid returning only famous mainstream titles.
12. Avoid overusing one franchise.
13. Avoid overusing one actor.
14. Avoid overusing one director.
15. Avoid overusing one genre.
16. Every title must be a real existing movie or series.
17. Every title needs a personalized reason.
18. Explain why the specific user may like the title.
19. Give a personalized match percentage from 55 to 99.
20. The match percentage must reflect how strongly the title fits the user profile, not TMDB popularity.
21. Return structured data only.

RANDOMIZATION VALUE:
{random_nonce}

WATCHED MOVIES:

{json.dumps(
    profile["movies"],
    ensure_ascii=False,
    indent=2
)}

WATCHED SERIES:

{json.dumps(
    profile["series"],
    ensure_ascii=False,
    indent=2
)}

ALREADY WATCHED MOVIES:

{json.dumps(
    watched_movie_titles,
    ensure_ascii=False
)}

ALREADY WATCHED SERIES:

{json.dumps(
    watched_series_titles,
    ensure_ascii=False
)}

RECENT MOVIE RECOMMENDATION IDS:

{json.dumps(
    recent_movie_ids
)}

RECENT SERIES RECOMMENDATION IDS:

{json.dumps(
    recent_series_ids
)}

NOT INTERESTED MOVIE IDS:

{json.dumps(
    not_interested_movie_ids
)}

NOT INTERESTED SERIES IDS:

{json.dumps(
    not_interested_series_ids
)}

Return only the structured response.
"""

    return prompt


# ============================================================
# GEMINI
# ============================================================

def get_ai_recommendations(watched):

    client = get_gemini_client()

    if client is None:

        print(
            "Gemini API key is not configured"
        )

        return None

    prompt = build_gemini_prompt(
        watched
    )

    for model in GEMINI_MODELS:

        print(
            f"Trying Gemini model: {model}"
        )

        for attempt in range(2):

            try:

                response = (
                    client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=RecommendationResponse,
                        ),
                    )
                )

                parsed = getattr(
                    response,
                    "parsed",
                    None,
                )

                if parsed is not None:

                    if isinstance(
                        parsed,
                        RecommendationResponse,
                    ):

                        result = parsed

                    else:

                        result = (
                            RecommendationResponse
                            .model_validate(
                                parsed
                            )
                        )

                else:

                    if not response.text:

                        print(
                            f"{model}: empty response"
                        )

                        continue

                    result = (
                        RecommendationResponse
                        .model_validate_json(
                            response.text
                        )
                    )

                print(
                    f"{model}: Gemini success"
                )

                return result

            except Exception as error:

                error_text = str(
                    error
                ).lower()

                print(
                    f"{model} attempt "
                    f"{attempt + 1}/2 failed: "
                    f"{error}"
                )

                temporary_error = (
                    "503" in error_text
                    or "unavailable" in error_text
                    or "429" in error_text
                    or "rate limit" in error_text
                    or "too many requests" in error_text
                    or "overloaded" in error_text
                    or "internal server error" in error_text
                )

                if temporary_error:

                    time.sleep(
                        1.5 * (
                            attempt + 1
                        )
                    )

                    continue

                break

    print(
        "All Gemini models unavailable."
    )

    return None


# ============================================================
# TMDB HTTP + CACHE
# ============================================================

def get_tmdb_session():
    global _tmdb_session
    with _tmdb_session_lock:
        if _tmdb_session is None:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            retry = Retry(
                total=3,
                connect=3,
                read=3,
                status=3,
                backoff_factor=0.4,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
            _tmdb_session = requests.Session()
            _tmdb_session.mount("https://", adapter)
            _tmdb_session.mount("http://", adapter)
        return _tmdb_session


def tmdb_cache_key(endpoint, params):
    normalized_params = []
    for key, value in sorted((params or {}).items()):
        normalized_params.append((str(key), str(value)))
    raw = json.dumps({"endpoint": endpoint, "params": normalized_params}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================================================
# TMDB REQUEST
# ============================================================

def tmdb_request(
    endpoint,
    token,
    params=None,
    cache_ttl=TMDB_CACHE_SEARCH_TTL,
):
    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }
    params = params or {}
    cache_key = tmdb_cache_key(endpoint, params)
    cached = get_tmdb_cache(cache_key) if cache_ttl else None
    if cached is not None:
        return cached

    response = get_tmdb_session().get(
        endpoint,
        headers=headers,
        params=params,
        timeout=(5, 20),
    )
    response.raise_for_status()
    payload = response.json()
    if cache_ttl:
        set_tmdb_cache(cache_key, payload, cache_ttl)
    return payload


# ============================================================
# NORMALIZE TMDB RESULT
# ============================================================

def normalise_tmdb_result(
    result,
    media_type,
):

    if media_type == "Movie":

        canonical_title = result.get(
            "title",
            "",
        )

        date = result.get(
            "release_date",
            "",
        )

        tmdb_url = (
            "https://www.themoviedb.org/movie/"
            f"{result.get('id')}"
        )

    else:

        canonical_title = result.get(
            "name",
            "",
        )

        date = result.get(
            "first_air_date",
            "",
        )

        tmdb_url = (
            "https://www.themoviedb.org/tv/"
            f"{result.get('id')}"
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
        "media_type":
            media_type,

        "tmdb_id":
            result.get(
                "id"
            ),

        "title":
            canonical_title,

        "year":
            year,

        "poster":
            poster,

        "backdrop":
            backdrop,

        "overview":
            (
                result.get(
                    "overview"
                )
                or ""
            ),

        "vote_average":
            float(
                result.get(
                    "vote_average",
                    0,
                )
                or 0
            ),

        "vote_count":
            int(
                result.get(
                    "vote_count",
                    0,
                )
                or 0
            ),

        "tmdb_url":
            tmdb_url,

        "genres": [
            g.get("name")
            for g in result.get("genres", [])
            if g.get("name")
        ],

        "cast": [],

        "director": "",

        "creators": [],

        "similar": [],
    }


# ============================================================
# TMDB EXACT SEARCH
# ============================================================

def normalise_search_title(value):
    import re
    value = (value or "").casefold().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def tmdb_search(
    title,
    media_type,
    year=None,
    allow_fuzzy=False,
):
    token = get_env("TMDB_TOKEN")
    if not token or media_type not in ("Movie", "Series"):
        return None

    endpoint = (
        "https://api.themoviedb.org/3/search/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
    )
    params = {
        "query": title,
        "language": "en-US",
        "include_adult": "false",
    }
    if year:
        params["year" if media_type == "Movie" else "first_air_date_year"] = year

    try:
        data = tmdb_request(endpoint, token, params, cache_ttl=TMDB_CACHE_SEARCH_TTL)
    except requests.RequestException as error:
        print(f"TMDB {media_type.lower()} search error: {error}")
        return None

    results = data.get("results", [])
    if not results:
        return None

    requested = normalise_search_title(title)
    best = None
    best_score = -1

    for result in results[:20]:
        result_title = result.get("title", "") if media_type == "Movie" else result.get("name", "")
        if not result_title:
            continue
        normalized = normalise_search_title(result_title)
        score = 100 if requested == normalized else fuzz.ratio(requested, normalized)

        date = result.get("release_date", "") if media_type == "Movie" else result.get("first_air_date", "")
        result_year = None
        if date:
            try:
                result_year = int(date[:4])
            except (TypeError, ValueError):
                pass

        year_bonus = 8 if year and result_year == int(year) else 0
        adjusted = score + year_bonus
        if adjusted > best_score:
            best_score = adjusted
            best = result

    # User-entered additions must never silently bind to a merely similar title.
    # AI verification can explicitly opt into a stricter fuzzy threshold.
    if best is None:
        return None
    threshold = 100 if not allow_fuzzy else 93
    if best_score < threshold:
        return None

    return normalise_tmdb_result(best, media_type)


# ============================================================
# LIVE TMDB SEARCH
# ============================================================

# ============================================================
# TMDB DETAILS
# ============================================================

def tmdb_get_details(
    tmdb_id,
    media_type,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:
        return None

    endpoint = (
        "https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        f"/{tmdb_id}"
    )

    try:

        data = tmdb_request(
            endpoint,
            token,
            {
                "language":
                    "en-US"
            },
            cache_ttl=TMDB_CACHE_DETAILS_TTL,
        )

        return normalise_tmdb_result(
            data,
            media_type,
        )

    except Exception as error:

        print(
            f"TMDB details error: "
            f"{error}"
        )

        return None


# ============================================================
# TMDB DETAIL PAGE DATA
# ============================================================

def tmdb_get_detail_page(tmdb_id, media_type):
    token = get_env("TMDB_TOKEN")
    if not token:
        return None

    endpoint = (
        "https://api.themoviedb.org/3/"
        f"{'movie' if media_type == 'Movie' else 'tv'}"
        f"/{tmdb_id}"
    )

    try:
        data = tmdb_request(
            endpoint,
            token,
            {
                "language": "en-US",
                "append_to_response": "credits,similar",
            },
            cache_ttl=TMDB_CACHE_DETAILS_TTL,
        )

        base = normalise_tmdb_result(data, media_type)

        credits = data.get("credits", {}) or {}
        cast = []
        for person in credits.get("cast", [])[:12]:
            cast.append({
                "name": person.get("name", ""),
                "character": person.get("character", ""),
                "photo": (
                    "https://image.tmdb.org/t/p/w185" + person["profile_path"]
                    if person.get("profile_path") else None
                ),
            })

        director = ""
        if media_type == "Movie":
            crew = credits.get("crew", []) or []
            directors = [
                p.get("name") for p in crew
                if p.get("job") == "Director" and p.get("name")
            ]
            director = ", ".join(dict.fromkeys(directors[:3]))
        else:
            creators = [
                p.get("name") for p in data.get("created_by", [])
                if p.get("name")
            ]
            director = ", ".join(dict.fromkeys(creators[:3]))

        similar = []
        for item in (data.get("similar", {}) or {}).get("results", [])[:10]:
            normalized = normalise_tmdb_result(item, media_type)
            if normalized.get("tmdb_id"):
                similar.append(normalized)

        base.update({
            "cast": cast,
            "director": director,
            "similar": similar,
            "genres": [
                g.get("name")
                for g in data.get("genres", [])
                if g.get("name")
            ],
            "runtime": data.get("runtime") if media_type == "Movie" else None,
            "episodes": data.get("number_of_episodes") if media_type == "Series" else None,
            "seasons": data.get("number_of_seasons") if media_type == "Series" else None,
            "status": data.get("status", ""),
            "tagline": data.get("tagline", ""),
            "match_percentage": None,
        })

        return base

    except Exception as error:
        print(f"TMDB detail page error: {error}")
        return None


# ============================================================
# VERIFY GEMINI RESULT
# ============================================================

def verify_recommendation(
    item,
    media_type,
):

    result = tmdb_search(
        item.title,
        media_type,
        item.year,
        allow_fuzzy=True,
    )

    if not result:
        return None

    result["reason"] = (
        item.reason
        or
        "Recommended because it matches "
        "your watched titles and ratings."
    )

    result["source"] = "AI"
    result["match_percentage"] = max(55, min(99, int(item.match_percentage or 82)))

    return result


def verify_all_recommendations(
    ai_result,
):

    movies = []
    series = []
    jobs = []

    with ThreadPoolExecutor(
        max_workers=8
    ) as executor:

        for item in ai_result.movies:

            jobs.append(
                (
                    "Movie",
                    executor.submit(
                        verify_recommendation,
                        item,
                        "Movie",
                    ),
                )
            )

        for item in ai_result.series:

            jobs.append(
                (
                    "Series",
                    executor.submit(
                        verify_recommendation,
                        item,
                        "Series",
                    ),
                )
            )

        for media_type, future in jobs:

            try:

                result = future.result()

                if not result:
                    continue

                if media_type == "Movie":

                    movies.append(
                        result
                    )

                else:

                    series.append(
                        result
                    )

            except Exception as error:

                print(
                    "TMDB verification error: "
                    f"{error}"
                )

    return (
        unique_results(movies),
        unique_results(series),
    )


# ============================================================
# UNIQUE
# ============================================================

def unique_results(results):

    seen = set()
    output = []

    for item in results:

        key = (
            item.get(
                "media_type"
            ),
            item.get(
                "tmdb_id"
            ),
        )

        if not item.get(
            "tmdb_id"
        ):
            continue

        if key in seen:
            continue

        seen.add(
            key
        )

        output.append(
            item
        )

    return output


# ============================================================
# FILTER
# ============================================================

def filter_new_results(
    results,
    blocked_ids,
):

    blocked_ids = set(
        blocked_ids
    )

    output = []
    seen = set()

    for item in results:

        tmdb_id = item.get(
            "tmdb_id"
        )

        if not tmdb_id:
            continue

        if tmdb_id in blocked_ids:
            continue

        if tmdb_id in seen:
            continue

        seen.add(
            tmdb_id
        )

        output.append(
            item
        )

    return output


# ============================================================
# TMDB DISCOVERY
# ============================================================

def tmdb_fallback(
    watched,
    blocked_movie_ids=None,
    blocked_series_ids=None,
):

    token = get_env(
        "TMDB_TOKEN"
    )

    if not token:

        return {
            "movies": [],
            "series": [],
        }

    watched_movie_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Movie"
            and item["tmdb_id"]
        )
    }

    watched_series_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Series"
            and item["tmdb_id"]
        )
    }

    blocked_movie_ids = set(
        blocked_movie_ids or []
    )

    blocked_series_ids = set(
        blocked_series_ids or []
    )

    blocked_movie_ids.update(
        watched_movie_ids
    )

    blocked_series_ids.update(
        watched_series_ids
    )

    def discover(
        media_type,
        page,
    ):

        endpoint = (
            "https://api.themoviedb.org/3/discover/"
            f"{'movie' if media_type == 'Movie' else 'tv'}"
        )

        try:

            data = tmdb_request(
                endpoint,
                token,
                {
                    "language":
                        "en-US",
                    "page":
                        page,
                    "sort_by":
                        "popularity.desc",
                    "include_adult":
                        "false",
                    "vote_count.gte":
                        50,
                },
                cache_ttl=TMDB_CACHE_TRENDING_TTL,
            )

            return (
                media_type,
                data.get(
                    "results",
                    [],
                ),
            )

        except Exception as error:

            print(
                "TMDB discovery error: "
                f"{error}"
            )

            return (
                media_type,
                [],
            )

    jobs = [
        ("Movie", 1),
        ("Movie", 2),
        ("Movie", 3),
        ("Movie", 4),
        ("Movie", 5),
        ("Movie", 6),
        ("Movie", 7),
        ("Movie", 8),
        ("Series", 1),
        ("Series", 2),
        ("Series", 3),
        ("Series", 4),
        ("Series", 5),
        ("Series", 6),
        ("Series", 7),
        ("Series", 8),
    ]

    movies = []
    series = []

    with ThreadPoolExecutor(
        max_workers=8
    ) as executor:

        futures = [
            executor.submit(
                discover,
                media_type,
                page,
            )
            for media_type, page in jobs
        ]

        for future in futures:

            media_type, items = (
                future.result()
            )

            blocked = (
                blocked_movie_ids
                if media_type == "Movie"
                else blocked_series_ids
            )

            for item in items:

                tmdb_id = item.get(
                    "id"
                )

                if not tmdb_id:
                    continue

                if tmdb_id in blocked:
                    continue

                result = normalise_tmdb_result(
                    item,
                    media_type,
                )

                result["source"] = "TMDB"
                result["match_percentage"] = max(55, min(95, int(round(result.get("vote_average", 7.0) * 10 + 10))))

                result["reason"] = (
                    "Selected as a TMDB discovery after excluding your watched and rejected titles."
                )

                if media_type == "Movie":

                    movies.append(
                        result
                    )

                else:

                    series.append(
                        result
                    )

    movies = unique_results(
        movies
    )

    series = unique_results(
        series
    )

    random.shuffle(
        movies
    )

    random.shuffle(
        series
    )

    return {
        "movies":
            movies,

        "series":
            series,
    }


# ============================================================
# GENERATE RECOMMENDATIONS
# ============================================================

def generate_recommendations(
    watched,
    prefetched_tmdb=None,
):

    print(
        "Starting separate "
        "AI + TMDB engine..."
    )

    recommendation_limit = get_recommendation_limit()

    watched_movie_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Movie"
            and item["tmdb_id"]
        )
    }

    watched_series_ids = {
        item["tmdb_id"]
        for item in watched
        if (
            item["type"] == "Series"
            and item["tmdb_id"]
        )
    }

    recent_movie_ids = set(
        get_recent_recommendation_ids(
            "Movie",
            RECENT_HISTORY_LIMIT,
        )
    )

    recent_series_ids = set(
        get_recent_recommendation_ids(
            "Series",
            RECENT_HISTORY_LIMIT,
        )
    )

    not_interested_movie_ids = set(
        get_not_interested_ids(
            "Movie"
        )
    )

    not_interested_series_ids = set(
        get_not_interested_ids(
            "Series"
        )
    )

    blocked_movies = (
        watched_movie_ids
        | recent_movie_ids
        | not_interested_movie_ids
    )

    blocked_series = (
        watched_series_ids
        | recent_series_ids
        | not_interested_series_ids
    )

    ai_result = (
        get_ai_recommendations(
            watched
        )
    )

    ai_movies = []
    ai_series = []

    if ai_result:

        print(
            "Gemini succeeded."
        )

        verified_movies, verified_series = (
            verify_all_recommendations(
                ai_result
            )
        )

        ai_movies = filter_new_results(
            verified_movies,
            blocked_movies,
        )

        ai_series = filter_new_results(
            verified_series,
            blocked_series,
        )

        random.shuffle(
            ai_movies
        )

        random.shuffle(
            ai_series
        )

        ai_movies = ai_movies[:recommendation_limit]

        ai_series = ai_series[:recommendation_limit]

        print(
            f"AI movies: "
            f"{len(ai_movies)}"
        )

        print(
            f"AI series: "
            f"{len(ai_series)}"
        )

    else:

        print(
            "Gemini unavailable."
        )

    tmdb_results = (
        prefetched_tmdb
        if prefetched_tmdb is not None
        else tmdb_fallback(
            watched,
            blocked_movie_ids=(
            blocked_movies
            | {
                item["tmdb_id"]
                for item in ai_movies
            }
        ),
            blocked_series_ids=(
                blocked_series
                | {
                    item["tmdb_id"]
                    for item in ai_series
                }
            ),
        )
    )

    tmdb_movies = filter_new_results(
        tmdb_results["movies"],
        blocked_movies
        | {
            item["tmdb_id"]
            for item in ai_movies
        },
    )

    tmdb_series = filter_new_results(
        tmdb_results["series"],
        blocked_series
        | {
            item["tmdb_id"]
            for item in ai_series
        },
    )

    tmdb_movies = tmdb_movies[:recommendation_limit]

    tmdb_series = tmdb_series[:recommendation_limit]

    new_ai_movies = 0
    new_ai_series = 0
    new_tmdb_movies = 0
    new_tmdb_series = 0

    for item in ai_movies:
        if add_recommendation_history(item["tmdb_id"], "Movie", item["title"]):
            new_ai_movies += 1

    for item in tmdb_movies:
        if add_recommendation_history(item["tmdb_id"], "Movie", item["title"]):
            new_tmdb_movies += 1

    for item in ai_series:
        if add_recommendation_history(item["tmdb_id"], "Series", item["title"]):
            new_ai_series += 1

    for item in tmdb_series:
        if add_recommendation_history(item["tmdb_id"], "Series", item["title"]):
            new_tmdb_series += 1

    if any((new_ai_movies, new_ai_series, new_tmdb_movies, new_tmdb_series)):
        increment_lifetime_recommendations(
            ai_movies=new_ai_movies,
            ai_series=new_ai_series,
            tmdb_movies=new_tmdb_movies,
            tmdb_series=new_tmdb_series,
        )

    print(
        f"FINAL MOVIES: "
        f"{len(ai_movies)} AI + "
        f"{len(tmdb_movies)} TMDB"
    )

    print(
        f"FINAL SERIES: "
        f"{len(ai_series)} AI + "
        f"{len(tmdb_series)} TMDB"
    )

    return {
        "ai_movies":
            ai_movies,

        "tmdb_movies":
            tmdb_movies,

        "ai_series":
            ai_series,

        "tmdb_series":
            tmdb_series,

        "source":
            (
                "Gemini + TMDB"
                if ai_result
                else "TMDB"
            ),
    }


# ============================================================
# HOME
#
# Opening the normal URL automatically starts discovery.
# ============================================================

@app.get("/")
def home(request: Request):

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# TRENDING NOW
# ============================================================

def get_trending_titles(media_type, limit=None):
    """Return current TMDB daily trending titles, excluding watched/rejected items."""

    if limit is None:
        limit = get_recommendation_limit()

    token = get_env("TMDB_TOKEN")

    if not token:
        return []

    endpoint = (
        "https://api.themoviedb.org/3/trending/"
        f"{'movie' if media_type == 'Movie' else 'tv'}/day"
    )

    try:
        data = tmdb_request(
            endpoint,
            token,
            {"language": "en-US"},
            cache_ttl=TMDB_CACHE_TRENDING_TTL,
        )

        watched_ids = {
            int(item["tmdb_id"])
            for item in get_all()
            if item["tmdb_id"] is not None
            and item["type"] == media_type
        }

        rejected_ids = {
            int(item_id)
            for item_id in get_not_interested_ids(media_type)
        }

        blocked_ids = watched_ids | rejected_ids

        output = []
        seen = set()

        for item in data.get("results", []):
            tmdb_id = item.get("id")

            if not tmdb_id:
                continue

            tmdb_id = int(tmdb_id)

            if tmdb_id in blocked_ids or tmdb_id in seen:
                continue

            normalized = normalise_tmdb_result(
                item,
                media_type,
            )

            if not normalized.get("title"):
                continue

            normalized["source"] = "TRENDING"
            normalized["reason"] = (
                "Trending on TMDB right now."
            )
            normalized["is_watchlisted"] = is_in_watchlist(
                media_type,
                tmdb_id,
            )

            output.append(normalized)
            seen.add(tmdb_id)

            if len(output) >= limit:
                break

        return output

    except Exception as error:
        print(
            f"TMDB trending {media_type.lower()} error: {error}"
        )
        return []


@app.get("/api/trending")
def api_trending():
    """Return separate current trending movie and TV rails."""

    with ThreadPoolExecutor(max_workers=2) as executor:
        trending_limit = get_recommendation_limit()
        movie_future = executor.submit(
            get_trending_titles,
            "Movie",
            trending_limit,
        )
        series_future = executor.submit(
            get_trending_titles,
            "Series",
            trending_limit,
        )

        movies = movie_future.result()
        series = series_future.result()

        increment_lifetime_trending("Movie", [item.get("tmdb_id") for item in movies])
        increment_lifetime_trending("Series", [item.get("tmdb_id") for item in series])

        current = get_display_statistics()
        set_display_statistics(
            ai_movies=current.get("ai_movies", 0),
            ai_series=current.get("ai_series", 0),
            tmdb_movies=current.get("tmdb_movies", 0),
            tmdb_series=current.get("tmdb_series", 0),
            trending_movies=len(movies),
            trending_series=len(series),
        )

        return {
            "movies": movies,
            "series": series,
        }


# ============================================================
# DISPLAY STATISTICS API
# ============================================================

@app.get("/api/lifetime-statistics")
def api_lifetime_statistics():
    return get_lifetime_statistics()


@app.get("/api/display-statistics")
def api_display_statistics():
    return get_display_statistics()


# ============================================================
# LIVE SEARCH API
# ============================================================

@app.get("/api/search")
def api_search(
    q: str = "",
    media_type: str = "",
):
    """Search movies and series independently, then merge stable buckets."""
    q = q.strip()
    if len(q) < 2:
        return {"results": [], "movies": [], "series": []}

    token = get_env("TMDB_TOKEN")
    if not token:
        return JSONResponse(
            {"results": [], "movies": [], "series": [], "error": "TMDB_TOKEN is not configured."},
            status_code=503,
        )

    requested_type = media_type if media_type in ("Movie", "Series") else ""

    def search_type(kind):
        endpoint = "https://api.themoviedb.org/3/search/" + (
            "movie" if kind == "Movie" else "tv"
        )
        try:
            data = tmdb_request(
                endpoint,
                token,
                {
                    "query": q,
                    "language": "en-US",
                    "include_adult": "false",
                    "page": 1,
                },
                cache_ttl=TMDB_CACHE_SEARCH_TTL,
            )
        except requests.RequestException as error:
            print(f"TMDB {kind.lower()} live search error: {error}")
            return []

        output = []
        seen = set()
        for item in data.get("results", []):
            tmdb_id = item.get("id")
            if not tmdb_id or tmdb_id in seen:
                continue
            normalized = normalise_tmdb_result(item, kind)
            if not normalized.get("title"):
                continue
            normalized["media_type"] = kind
            seen.add(tmdb_id)
            output.append(normalized)
            if len(output) >= 10:
                break
        return output

    if requested_type:
        movies = search_type("Movie") if requested_type == "Movie" else []
        series = search_type("Series") if requested_type == "Series" else []
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            movie_future = executor.submit(search_type, "Movie")
            series_future = executor.submit(search_type, "Series")
            movies = movie_future.result()
            series = series_future.result()

    movies = movies[:10]
    series = series[:10]
    return {
        "results": movies + series,
        "movies": movies,
        "series": series,
        "media_type": requested_type or None,
    }


# ============================================================
# ADD WATCHED
# ============================================================

@app.post("/add")
def add(
    request: Request,
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: Optional[int] = Form(None),
):

    title = title.strip()

    if (
        not title
        or len(title) > 300
        or not 0 <= rating <= 10
        or media_type not in ("Movie", "Series")
        or not tmdb_id
        or tmdb_id <= 0
    ):

        return app_redirect(
            request,
            "",
        )

    tmdb_data = None

    tmdb_data = tmdb_get_details(tmdb_id, media_type)

    if not tmdb_data:
        return app_redirect(request, "search")

    if tmdb_data:

        add_movie(
            title=
                tmdb_data["title"],

            rating=
                rating,

            media_type=
                media_type,

            poster=
                tmdb_data.get(
                    "poster"
                ),

            backdrop=
                tmdb_data.get(
                    "backdrop"
                ),

            year=
                tmdb_data.get(
                    "year"
                ),

            overview=
                tmdb_data.get(
                    "overview"
                ),

            tmdb_id=
                tmdb_data.get(
                    "tmdb_id"
                ),
        )

    return app_redirect(
        request,
        "",
    )


# ============================================================
# FIND JUST-ADDED LIBRARY ROW
# ============================================================

def find_library_row(tmdb_id, media_type, title):
    rows = get_all()

    matches = []

    for row in rows:
        if row["type"] != media_type:
            continue

        same_tmdb = (
            tmdb_id is not None
            and row["tmdb_id"] is not None
            and int(row["tmdb_id"]) == int(tmdb_id)
        )

        same_title = (
            str(row["title"] or "").strip().lower()
            == str(title or "").strip().lower()
        )

        if same_tmdb or same_title:
            matches.append(row)

    if not matches:
        return None

    return max(
        matches,
        key=lambda row: int(row["id"] or 0),
    )


# ============================================================
# AJAX ADD WATCHED
# ============================================================

@app.post("/api/add")
def api_add(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: Optional[int] = Form(None),
):

    title = title.strip()

    if (
        not title
        or len(title) > 300
        or not 0 <= rating <= 10
        or media_type not in ("Movie", "Series")
        or not tmdb_id
        or tmdb_id <= 0
    ):
        return JSONResponse(
            {"ok": False, "error": "Invalid title, rating, media type, or TMDB selection."},
            status_code=422,
        )

    tmdb_data = None

    tmdb_data = tmdb_get_details(tmdb_id, media_type)
    if not tmdb_data:
        return JSONResponse({"ok": False, "error": "The selected TMDB title could not be verified."}, status_code=422)

    canonical = tmdb_data
    add_movie(
        title=canonical["title"],
        rating=rating,
        media_type=media_type,
        poster=canonical.get("poster"),
        backdrop=canonical.get("backdrop"),
        year=canonical.get("year"),
        overview=canonical.get("overview"),
        tmdb_id=canonical.get("tmdb_id"),
    )

    library_row = find_library_row(
        canonical.get("tmdb_id") or tmdb_id,
        media_type,
        canonical.get("title") or title,
    )

    return {
        "ok": True,
        "item": {
            "id": library_row["id"] if library_row else None,
            "title": canonical.get("title") or title,
            "tmdb_id": canonical.get("tmdb_id"),
            "year": canonical.get("year"),
            "poster": canonical.get("poster"),
            "overview": canonical.get("overview") or "",
            "vote_average": canonical.get("vote_average", 0) or 0,
            "media_type": media_type,
            "rating": rating,
        },
    }


# ============================================================
# AJAX MARK RECOMMENDATION WATCHED
# ============================================================

@app.post("/api/recommendation/watched")
def api_recommendation_watched(
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    if media_type not in ("Movie", "Series"):
        return {
            "ok": False,
            "error": "Invalid media type."
        }

    token = get_env("TMDB_TOKEN")
    tmdb_data = None

    if token:
        try:
            endpoint = (
                "https://api.themoviedb.org/3/"
                f"{'movie' if media_type == 'Movie' else 'tv'}"
                f"/{tmdb_id}"
            )

            data = tmdb_request(
                endpoint,
                token,
                {"language": "en-US"},
                cache_ttl=TMDB_CACHE_DETAILS_TTL,
            )

            tmdb_data = normalise_tmdb_result(
                data,
                media_type,
            )

        except Exception as error:
            print(
                "TMDB AJAX watched lookup error: "
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

    final_title = (
        tmdb_data.get("title")
        if tmdb_data
        else title
    )

    library_row = find_library_row(
        tmdb_id,
        media_type,
        final_title,
    )

    return {
        "ok": True,
        "id": library_row["id"] if library_row else None,
        "tmdb_id": tmdb_id,
        "title": final_title,
        "media_type": media_type,
        "poster": (
            tmdb_data.get("poster")
            if tmdb_data
            else None
        ),
        "year": (
            tmdb_data.get("year")
            if tmdb_data
            else None
        ),
        "overview": (
            tmdb_data.get("overview")
            if tmdb_data
            else ""
        ),
    }


# ============================================================
# ALREADY WATCHED
# ============================================================

@app.get("/watched")
def watched_page(request: Request):
    movies = get_all()
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "detail": None,
            "watched_page": True,
            "wishlist_page": False,
            "settings_page": False,
            "search_page": False,
            "about_page": False,
            "wishlist_items": load_watchlist(),
            "movies": movies,
            "watched_movies": [item for item in movies if item["type"] == "Movie"],
            "watched_series": [item for item in movies if item["type"] == "Series"],
            "recommendations": None,
            "recommendations_loading": False,
            "recommendation_error": None,
            "tmdb_discoveries": None,
            "display_statistics": get_display_statistics(),
            "lifetime_statistics": get_lifetime_statistics(),
            "recommendation_limit": get_recommendation_limit(),
            "ingress_path": get_ingress_path(request),
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


# ============================================================
# WISHLIST / SETTINGS PAGES
# ============================================================

@app.get("/wishlist")
def wishlist_page(request: Request):
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "detail": None,
            "watched_page": False,
            "wishlist_page": True,
            "settings_page": False,
            "search_page": False,
            "about_page": False,
            "wishlist_items": list(reversed(load_watchlist())),
            "movies": get_all(),
            "watched_movies": [item for item in get_all() if item["type"] == "Movie"],
            "watched_series": [item for item in get_all() if item["type"] == "Series"],
            "recommendations": None,
            "recommendations_loading": False,
            "recommendation_error": None,
            "tmdb_discoveries": None,
            "display_statistics": get_display_statistics(),
            "lifetime_statistics": get_lifetime_statistics(),
            "ingress_path": get_ingress_path(request),
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/search")
def search_page(request: Request):
    movies = get_all()
    response = templates.TemplateResponse(request=request, name="index.html", context={
        "detail": None, "watched_page": False, "wishlist_page": False, "settings_page": False,
        "search_page": True, "about_page": False, "wishlist_items": load_watchlist(), "movies": movies,
        "watched_movies": [item for item in movies if item["type"] == "Movie"],
        "watched_series": [item for item in movies if item["type"] == "Series"],
        "recommendations": None, "recommendations_loading": False, "recommendation_error": None,
        "tmdb_discoveries": None, "display_statistics": get_display_statistics(),
        "lifetime_statistics": get_lifetime_statistics(), "recommendation_limit": get_recommendation_limit(),
        "ingress_path": get_ingress_path(request),
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/about")
def about_page(request: Request):
    movies = get_all()
    response = templates.TemplateResponse(request=request, name="index.html", context={
        "detail": None, "watched_page": False, "wishlist_page": False, "settings_page": False,
        "search_page": False, "about_page": True, "wishlist_items": load_watchlist(), "movies": movies,
        "watched_movies": [item for item in movies if item["type"] == "Movie"],
        "watched_series": [item for item in movies if item["type"] == "Series"],
        "recommendations": None, "recommendations_loading": False, "recommendation_error": None,
        "tmdb_discoveries": None, "display_statistics": get_display_statistics(),
        "lifetime_statistics": get_lifetime_statistics(), "recommendation_limit": get_recommendation_limit(),
        "ingress_path": get_ingress_path(request),
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/settings")
def settings_page(request: Request):
    movies = get_all()
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "detail": None,
            "watched_page": False,
            "wishlist_page": False,
            "settings_page": True,
            "search_page": False,
            "about_page": False,
            "wishlist_items": load_watchlist(),
            "movies": movies,
            "watched_movies": [item for item in movies if item["type"] == "Movie"],
            "watched_series": [item for item in movies if item["type"] == "Series"],
            "recommendations": None,
            "recommendations_loading": False,
            "recommendation_error": None,
            "tmdb_discoveries": None,
            "display_statistics": get_display_statistics(),
            "lifetime_statistics": get_lifetime_statistics(),
            "ingress_path": get_ingress_path(request),
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


# ============================================================
# APP SETTINGS API
# ============================================================

@app.get("/api/settings")
def api_get_settings():
    settings = load_app_settings()
    return JSONResponse({"ok": True, **settings})


@app.post("/api/settings")
def api_update_settings(
    background_tasks: BackgroundTasks,
    recommendation_limit: int = Form(...),
):
    try:
        value = int(recommendation_limit)
    except Exception:
        value = DEFAULT_RECOMMENDATION_LIMIT
    value = max(MIN_RECOMMENDATION_LIMIT, min(MAX_RECOMMENDATION_LIMIT, value))

    settings = load_app_settings()
    previous_value = int(settings.get("recommendation_limit", DEFAULT_RECOMMENDATION_LIMIT))
    settings["recommendation_limit"] = value
    save_app_settings(settings)

    refreshed = value != previous_value
    if refreshed:
        job = start_recommendation_job(force=True)
        background_tasks.add_task(run_recommendation_job, None, job["id"])

    return JSONResponse({"ok": True, "refreshed": refreshed, **settings})


# ============================================================
# TITLE DETAIL PAGE
# ============================================================

@app.get("/title/{media_type}/{tmdb_id}")
def title_detail(
    request: Request,
    media_type: str,
    tmdb_id: int,
    match: Optional[int] = None,
):
    if media_type not in ("Movie", "Series"):
        return app_redirect(request, "recommendations")

    detail = tmdb_get_detail_page(
        tmdb_id,
        media_type,
    )

    if not detail:
        return app_redirect(request, "recommendations")

    watched = get_all()
    watched_item = next(
        (
            item for item in watched
            if item["tmdb_id"] is not None
            and int(item["tmdb_id"]) == int(tmdb_id)
            and item["type"] == media_type
        ),
        None,
    )

    # Keep the recommendation card's match percentage when the user
    # clicked from an AI/TMDB recommendation rail. For direct visits,
    # calculate a fallback score from the user's library.
    if match is not None:
        match_percentage = max(1, min(99, int(match)))
    elif watched_item:
        match_percentage = 100
    else:
        recent_scores = []
        for item in watched:
            rating = float(item["rating"] or 0)
            if rating < 7:
                continue
            overview = (item["overview"] or "").strip()
            if overview and detail.get("overview"):
                score = fuzz.token_set_ratio(
                    overview.lower(),
                    detail["overview"].lower(),
                )
                recent_scores.append(
                    score * (0.65 + 0.35 * (rating / 10.0))
                )
        if recent_scores:
            match_percentage = int(max(58, min(96, round(max(recent_scores) + 5))))
        else:
            match_percentage = int(max(60, min(88, round((detail.get("vote_average", 7) or 7) * 10))))

    detail["match_percentage"] = match_percentage
    detail["is_watchlisted"] = is_in_watchlist(
        media_type,
        tmdb_id,
    )
    detail["is_watched"] = bool(watched_item)
    detail["user_rating"] = watched_item["rating"] if watched_item else None

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "detail": detail,
            "media_type": media_type,
            "watched_page": False,
            "wishlist_page": False,
            "settings_page": False,
            "search_page": False,
            "about_page": False,
            "wishlist_items": load_watchlist(),
            "movies": watched,
            "watched_movies": [item for item in watched if item["type"] == "Movie"],
            "watched_series": [item for item in watched if item["type"] == "Series"],
            "recommendations": None,
            "recommendations_loading": False,
            "recommendation_error": None,
            "tmdb_discoveries": None,
            "display_statistics": get_display_statistics(),
            "lifetime_statistics": get_lifetime_statistics(),
            "ingress_path": get_ingress_path(request),
        },
    )

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    return response


# ============================================================
# WATCHLIST TOGGLE
# ============================================================

@app.post("/api/watchlist/toggle")
def api_watchlist_toggle(
    tmdb_id: int = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
    year: Optional[int] = Form(None),
    poster: Optional[str] = Form(None),
    backdrop: Optional[str] = Form(None),
    overview: Optional[str] = Form(""),
    vote_average: float = Form(0),
    remove: bool = Form(False),
):
    title = title.strip()
    if (
        media_type not in ("Movie", "Series")
        or tmdb_id <= 0
        or not title
        or len(title) > 300
        or not 0 <= vote_average <= 10
        or (year is not None and not 1800 <= year <= 2200)
    ):
        return JSONResponse({"ok": False, "error": "Invalid watchlist item."}, status_code=422)

    saved = toggle_watchlist_item({
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "poster": poster,
        "backdrop": backdrop,
        "overview": overview or "",
        "vote_average": vote_average,
        "remove": remove,
    })

    return JSONResponse({
        "ok": True,
        "saved": saved,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
    })


@app.post("/watchlist/toggle")
def watchlist_toggle(
    request: Request,
    tmdb_id: int = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
    year: Optional[int] = Form(None),
    poster: Optional[str] = Form(None),
    backdrop: Optional[str] = Form(None),
    overview: Optional[str] = Form(""),
    vote_average: float = Form(0),
    remove: bool = Form(False),
):
    toggle_watchlist_item({
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "poster": poster,
        "backdrop": backdrop,
        "overview": overview or "",
        "vote_average": vote_average,
        "remove": remove,
    })

    return app_redirect(
        request,
        f"title/{media_type}/{tmdb_id}",
    )


# ============================================================
# RECOMMENDATIONS
# ============================================================

@app.get("/recommendations")
def recommendations(
    request: Request,
    background_tasks: BackgroundTasks,
    refresh: bool = False,
    fragment: str = "",
):
    """Render the homepage from the persistent recommendation job record."""
    movies = get_all()
    watched_movies = [item for item in movies if item["type"] == "Movie"]
    watched_series = [item for item in movies if item["type"] == "Series"]

    job = get_latest_recommendation_job()
    if refresh:
        tmdb_discoveries = tmdb_fallback(movies)
        job = start_recommendation_job(force=True, tmdb_data=tmdb_discoveries)
        background_tasks.add_task(run_recommendation_job, tmdb_discoveries, job["id"])
    elif job is None:
        tmdb_discoveries = tmdb_fallback(movies)
        job = start_recommendation_job(force=False, tmdb_data=tmdb_discoveries)
        background_tasks.add_task(run_recommendation_job, tmdb_discoveries, job["id"])
    else:
        tmdb_discoveries = job.get("tmdb_data")

    recommendations_data = job.get("data") if job else None
    recommendations_loading = bool(job and job.get("status") == "loading" and recommendations_data is None)
    error = job.get("error") if job else None
    if job and job.get("status") == "ready":
        tmdb_discoveries = None
        recommendations_loading = False

    context = {
        "movies": movies,
        "watched_page": False,
        "wishlist_page": False,
        "settings_page": False,
        "search_page": False,
        "about_page": False,
        "wishlist_items": load_watchlist(),
        "watched_movies": watched_movies,
        "watched_series": watched_series,
        "recommendations": recommendations_data,
        "recommendations_loading": recommendations_loading,
        "recommendation_error": error,
        "tmdb_discoveries": tmdb_discoveries,
        "display_statistics": get_display_statistics(),
        "lifetime_statistics": get_lifetime_statistics(),
        "recommendation_limit": get_recommendation_limit(),
        "ingress_path": get_ingress_path(request),
    }

    response = templates.TemplateResponse(request=request, name="index.html", context=context)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/recommendations/status")
def recommendation_status():
    job = get_latest_recommendation_job()
    if not job:
        return {"status": "idle", "error": None}
    return {
        "status": job["status"],
        "error": job["error"],
        "job_id": job["id"],
        "updated_at": job["updated_at"],
    }


# ============================================================
# MARK RECOMMENDATION WATCHED
# ============================================================

@app.post(
    "/recommendation/watched"
)
def recommendation_watched(
    request: Request,
    title: str = Form(...),
    rating: float = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    title = title.strip()
    if (
        not title or len(title) > 300
        or not 0 <= rating <= 10
        or media_type not in ("Movie", "Series")
        or tmdb_id <= 0
    ):
        return JSONResponse({"ok": False, "error": "Invalid watched item."}, status_code=422)

    token = get_env(
        "TMDB_TOKEN"
    )

    tmdb_data = None

    if token:

        try:

            endpoint = (
                "https://api.themoviedb.org/3/"
                f"{'movie' if media_type == 'Movie' else 'tv'}"
                f"/{tmdb_id}"
            )

            data = tmdb_request(
                endpoint,
                token,
                {
                    "language":
                        "en-US",
                },
                cache_ttl=TMDB_CACHE_DETAILS_TTL,
            )

            tmdb_data = (
                normalise_tmdb_result(
                    data,
                    media_type,
                )
            )

        except Exception as error:

            print(
                "TMDB watched lookup error: "
                f"{error}"
            )

    if tmdb_data:

        add_movie(
            title=
                tmdb_data["title"],

            rating=
                rating,

            media_type=
                media_type,

            poster=
                tmdb_data.get(
                    "poster"
                ),

            backdrop=
                tmdb_data.get(
                    "backdrop"
                ),

            year=
                tmdb_data.get(
                    "year"
                ),

            overview=
                tmdb_data.get(
                    "overview"
                ),

            tmdb_id=
                tmdb_data.get(
                    "tmdb_id"
                ),
        )

    else:

        add_movie(
            title=
                title,

            rating=
                rating,

            media_type=
                media_type,

            tmdb_id=
                tmdb_id,
        )

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# NOT INTERESTED
# ============================================================

@app.post(
    "/recommendation/not-interested"
)
def recommendation_not_interested(
    request: Request,
    title: str = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    title = title.strip()
    if (
        not title or len(title) > 300
        or media_type not in ("Movie", "Series")
        or tmdb_id <= 0
    ):
        return JSONResponse({"ok": False, "error": "Invalid recommendation item."}, status_code=422)

    add_not_interested(
        tmdb_id=
            tmdb_id,

        media_type=
            media_type,

        title=
            title,
    )

    print(
        f"Not interested: "
        f"{title} "
        f"[{media_type}] "
        f"TMDB={tmdb_id}"
    )

    # AJAX requests get JSON so the browser stays on the current page.
    # Normal non-AJAX form submissions keep the old redirect behavior.
    accept = (request.headers.get("accept") or "").lower()

    if "application/json" in accept:
        return JSONResponse({
            "ok": True,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": title,
        })

    return app_redirect(
        request,
        "recommendations",
    )


# ============================================================
# AJAX NOT INTERESTED
# Same database action as the normal route, but returns JSON.
# ============================================================

@app.post("/api/recommendation/not-interested")
def api_recommendation_not_interested(
    title: str = Form(...),
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
):

    title = title.strip()
    if not title or len(title) > 300 or tmdb_id <= 0:
        return JSONResponse({"ok": False, "error": "Invalid recommendation item."}, status_code=422)

    if media_type not in ("Movie", "Series"):
        return JSONResponse(
            {
                "ok": False,
                "error": "Invalid media type.",
            },
            status_code=400,
        )

    add_not_interested(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
    )

    print(
        f"AJAX not interested: {title} "
        f"[{media_type}] TMDB={tmdb_id}"
    )

    return JSONResponse({
        "ok": True,
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
    })


# ============================================================
# DELETE
# ============================================================

@app.post(
    "/delete/{movie_id}"
)
def delete(
    request: Request,
    movie_id: int,
):

    if movie_id <= 0:
        return JSONResponse({"ok": False, "error": "Invalid library item."}, status_code=422)

    delete_movie(movie_id)

    return app_redirect(
        request,
        "",
    )

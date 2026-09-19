import sqlite3
import os
from pathlib import Path

# ============================================================
# DATABASE
# ============================================================

# Use /data in the container/add-on. Allow an override and a local fallback
# so development/test runs do not fail just because /data is unavailable.
_requested_data_dir = Path(os.getenv("MOVIE_AI_DATA_DIR", "/data"))
try:
    _requested_data_dir.mkdir(parents=True, exist_ok=True)
    DATA_DIR = _requested_data_dir
except OSError:
    DATA_DIR = Path.cwd() / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / 'movies.db'


def get_connection():
    connection = sqlite3.connect(
        DB_PATH,
        timeout=15,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


# ============================================================
# INITIALIZE
# ============================================================

def init_database():
    connection = get_connection()

    connection.execute('''
        CREATE TABLE IF NOT EXISTS watched (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            rating REAL NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
            poster TEXT,
            backdrop TEXT,
            year INTEGER,
            overview TEXT,
            tmdb_id INTEGER
        )
    ''')

    connection.execute('''
        CREATE TABLE IF NOT EXISTS recommendation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
            title TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    connection.execute('''
        CREATE TABLE IF NOT EXISTS not_interested (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
            title TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Lifetime statistics. These never decrease when watched titles are removed.
    connection.execute('''
        CREATE TABLE IF NOT EXISTS lifetime_statistics (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            watched_total INTEGER NOT NULL DEFAULT 0,
            movies_total INTEGER NOT NULL DEFAULT 0,
            series_total INTEGER NOT NULL DEFAULT 0,
            recommendations_total INTEGER NOT NULL DEFAULT 0,
            ai_generated_total INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    connection.execute('''
        CREATE TABLE IF NOT EXISTS lifetime_trending_seen (
            day TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
            tmdb_id INTEGER NOT NULL,
            PRIMARY KEY (day, type, tmdb_id)
        )
    ''')

    row = connection.execute('SELECT id FROM lifetime_statistics WHERE id = 1').fetchone()
    if row is None:
        watched_total = connection.execute('SELECT COUNT(*) AS c FROM watched').fetchone()['c']
        movies_total = connection.execute("SELECT COUNT(*) AS c FROM watched WHERE type = 'Movie'").fetchone()['c']
        series_total = connection.execute("SELECT COUNT(*) AS c FROM watched WHERE type = 'Series'").fetchone()['c']
        recommendations_total = connection.execute('SELECT COUNT(*) AS c FROM recommendation_history').fetchone()['c']
        connection.execute('''
            INSERT INTO lifetime_statistics (
                id, watched_total, movies_total, series_total, recommendations_total, ai_generated_total
            ) VALUES (1, ?, ?, ?, ?, 0)
        ''', (int(watched_total or 0), int(movies_total or 0), int(series_total or 0), int(recommendations_total or 0)))

    # Current displayed rail statistics. These are a snapshot of what the
    # homepage is currently showing, not a lifetime counter.
    connection.execute('''
        CREATE TABLE IF NOT EXISTS display_statistics (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ai_movies INTEGER NOT NULL DEFAULT 0,
            ai_series INTEGER NOT NULL DEFAULT 0,
            tmdb_movies INTEGER NOT NULL DEFAULT 0,
            tmdb_series INTEGER NOT NULL DEFAULT 0,
            trending_movies INTEGER NOT NULL DEFAULT 0,
            trending_series INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    connection.execute('''
        INSERT OR IGNORE INTO display_statistics (
            id,
            ai_movies,
            ai_series,
            tmdb_movies,
            tmdb_series,
            trending_movies,
            trending_series
        ) VALUES (1, 0, 0, 0, 0, 0, 0)
    ''')

    # Upgrade old watched databases.
    columns = connection.execute('PRAGMA table_info(watched)').fetchall()
    existing_columns = {column['name'] for column in columns}

    new_columns = {
        'poster': 'TEXT',
        'backdrop': 'TEXT',
        'year': 'INTEGER',
        'overview': 'TEXT',
        'tmdb_id': 'INTEGER',
    }

    for column_name, column_type in new_columns.items():
        if column_name not in existing_columns:
            connection.execute(
                f'ALTER TABLE watched ADD COLUMN {column_name} {column_type}'
            )

    # Repair duplicate rows created by older releases before adding the
    # database-level uniqueness guarantees. Keep the newest row for each
    # canonical identity so user metadata/rating from the latest edit wins.
    connection.execute("BEGIN IMMEDIATE")
    connection.execute('''
        DELETE FROM watched
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM watched
            WHERE tmdb_id IS NOT NULL
            GROUP BY type, tmdb_id
        )
        AND tmdb_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM watched newer
            WHERE newer.type = watched.type
              AND newer.tmdb_id = watched.tmdb_id
              AND newer.id > watched.id
        )
    ''')
    connection.execute('''
        DELETE FROM watched
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM watched
            GROUP BY type, LOWER(TRIM(title))
        )
        AND EXISTS (
            SELECT 1 FROM watched newer
            WHERE newer.type = watched.type
              AND LOWER(TRIM(newer.title)) = LOWER(TRIM(watched.title))
              AND newer.id > watched.id
        )
    ''')

    # The same uniqueness repair is required for historical/rejected
    # recommendation tables before their unique indexes can be created.
    connection.execute('''
        DELETE FROM recommendation_history
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM recommendation_history
            GROUP BY type, tmdb_id
        )
    ''')
    connection.execute('''
        DELETE FROM not_interested
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM not_interested
            GROUP BY type, tmdb_id
        )
    ''')

    connection.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS ux_watched_tmdb
        ON watched(type, tmdb_id)
        WHERE tmdb_id IS NOT NULL
    ''')
    connection.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS ux_watched_title
        ON watched(type, LOWER(TRIM(title)))
    ''')
    connection.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS ux_recommendation_history
        ON recommendation_history(type, tmdb_id)
    ''')
    connection.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS ux_not_interested
        ON not_interested(type, tmdb_id)
    ''')
    connection.commit()
    connection.close()


# ============================================================
# WATCHED
# ============================================================

def get_all():
    connection = get_connection()
    movies = connection.execute('''
        SELECT
            id,
            title,
            rating,
            type,
            poster,
            backdrop,
            year,
            overview,
            tmdb_id
        FROM watched
        ORDER BY id DESC
    ''').fetchall()
    connection.close()
    return movies


def find_movie(title=None, media_type=None, tmdb_id=None):
    connection = get_connection()
    result = None

    if tmdb_id and media_type:
        result = connection.execute('''
            SELECT * FROM watched
            WHERE tmdb_id = ? AND type = ?
            LIMIT 1
        ''', (tmdb_id, media_type)).fetchone()

    if result is None and title and media_type:
        result = connection.execute('''
            SELECT * FROM watched
            WHERE LOWER(TRIM(title)) = LOWER(TRIM(?))
              AND type = ?
            LIMIT 1
        ''', (title, media_type)).fetchone()

    connection.close()
    return result


def movie_exists(title=None, media_type=None, tmdb_id=None):
    return find_movie(
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
    ) is not None


def add_movie(
    title,
    rating,
    media_type,
    poster=None,
    backdrop=None,
    year=None,
    overview=None,
    tmdb_id=None,
):
    if media_type not in ("Movie", "Series"):
        raise ValueError("Invalid media type")
    title = str(title or "").strip()
    if not title:
        raise ValueError("Title is required")
    rating = float(rating)
    if not 0 <= rating <= 10:
        raise ValueError("Rating must be between 0 and 10")
    if tmdb_id is not None:
        tmdb_id = int(tmdb_id)

    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")

        existing = None
        if tmdb_id:
            existing = connection.execute('''
                SELECT * FROM watched
                WHERE tmdb_id = ? AND type = ?
                LIMIT 1
            ''', (tmdb_id, media_type)).fetchone()

        if existing is None:
            existing = connection.execute('''
                SELECT * FROM watched
                WHERE LOWER(TRIM(title)) = LOWER(TRIM(?))
                  AND type = ?
                LIMIT 1
            ''', (title, media_type)).fetchone()

        if existing:
            final_title = title or existing["title"]
            final_poster = poster or existing["poster"]
            final_backdrop = backdrop or existing["backdrop"]
            final_year = year if year is not None else existing["year"]
            final_overview = overview or existing["overview"]
            final_tmdb_id = tmdb_id if tmdb_id else existing["tmdb_id"]

            connection.execute('''
                UPDATE watched
                SET title = ?, rating = ?, type = ?, poster = ?, backdrop = ?,
                    year = ?, overview = ?, tmdb_id = ?
                WHERE id = ?
            ''', (
                final_title, rating, media_type, final_poster, final_backdrop,
                final_year, final_overview, final_tmdb_id, existing["id"],
            ))
            connection.commit()
            return existing["id"]

        cursor = connection.execute('''
            INSERT INTO watched (
                title, rating, type, poster, backdrop, year, overview, tmdb_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            title, rating, media_type, poster, backdrop, year, overview, tmdb_id,
        ))

        connection.execute('''
            UPDATE lifetime_statistics
            SET watched_total = watched_total + 1,
                movies_total = movies_total + ?,
                series_total = series_total + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        ''', (
            1 if media_type == "Movie" else 0,
            1 if media_type == "Series" else 0,
        ))
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_movie(movie_id):
    connection = get_connection()
    connection.execute('DELETE FROM watched WHERE id = ?', (movie_id,))
    connection.commit()
    connection.close()


# ============================================================
# RECOMMENDATION HISTORY
# ============================================================

def add_recommendation_history(tmdb_id, media_type, title):
    if not tmdb_id or media_type not in ("Movie", "Series"):
        return False

    connection = get_connection()
    try:
        cursor = connection.execute(
            '''
            INSERT OR IGNORE INTO recommendation_history (tmdb_id, type, title)
            VALUES (?, ?, ?)
            ''',
            (int(tmdb_id), media_type, title),
        )
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_recent_recommendation_ids(media_type, limit=50):
    connection = get_connection()
    rows = connection.execute('''
        SELECT tmdb_id
        FROM recommendation_history
        WHERE type = ?
        ORDER BY id DESC
        LIMIT ?
    ''', (media_type, limit)).fetchall()
    connection.close()
    return [row['tmdb_id'] for row in rows]


# ============================================================
# NOT INTERESTED
# ============================================================

def add_not_interested(tmdb_id, media_type, title):
    if not tmdb_id or media_type not in ("Movie", "Series"):
        return False
    if media_type not in ("Movie", "Series"):
        return False

    connection = get_connection()
    try:
        cursor = connection.execute('''
            INSERT OR IGNORE INTO not_interested (tmdb_id, type, title)
            VALUES (?, ?, ?)
        ''', (int(tmdb_id), media_type, title))
        return cursor.rowcount == 1
    finally:
        connection.close()


def get_not_interested_ids(media_type):
    connection = get_connection()
    rows = connection.execute('''
        SELECT tmdb_id
        FROM not_interested
        WHERE type = ?
    ''', (media_type,)).fetchall()
    connection.close()
    return [row['tmdb_id'] for row in rows]


# ============================================================
# DISPLAY STATISTICS
# ============================================================

def _ensure_display_statistics_row(connection):
    connection.execute('''
        INSERT OR IGNORE INTO display_statistics (
            id,
            ai_movies,
            ai_series,
            tmdb_movies,
            tmdb_series,
            trending_movies,
            trending_series
        ) VALUES (1, 0, 0, 0, 0, 0, 0)
    ''')


def get_display_statistics():
    connection = get_connection()
    _ensure_display_statistics_row(connection)
    row = connection.execute('''
        SELECT
            ai_movies,
            ai_series,
            tmdb_movies,
            tmdb_series,
            trending_movies,
            trending_series,
            updated_at
        FROM display_statistics
        WHERE id = 1
        LIMIT 1
    ''').fetchone()
    connection.commit()
    connection.close()

    if not row:
        return {
            'ai_movies': 0,
            'ai_series': 0,
            'tmdb_movies': 0,
            'tmdb_series': 0,
            'trending_movies': 0,
            'trending_series': 0,
            'ai_generated': 0,
            'recommendations': 0,
            'updated_at': None,
        }

    ai_movies = max(0, int(row['ai_movies'] or 0))
    ai_series = max(0, int(row['ai_series'] or 0))
    tmdb_movies = max(0, int(row['tmdb_movies'] or 0))
    tmdb_series = max(0, int(row['tmdb_series'] or 0))
    trending_movies = max(0, int(row['trending_movies'] or 0))
    trending_series = max(0, int(row['trending_series'] or 0))

    ai_generated = ai_movies + ai_series
    recommendations = (
        ai_generated
        + tmdb_movies
        + tmdb_series
        + trending_movies
        + trending_series
    )

    return {
        'ai_movies': ai_movies,
        'ai_series': ai_series,
        'tmdb_movies': tmdb_movies,
        'tmdb_series': tmdb_series,
        'trending_movies': trending_movies,
        'trending_series': trending_series,
        'ai_generated': ai_generated,
        'recommendations': recommendations,
        'updated_at': row['updated_at'],
    }


def set_recommendation_statistics(
    ai_movies=0,
    ai_series=0,
    tmdb_movies=0,
    tmdb_series=0,
):
    connection = get_connection()
    _ensure_display_statistics_row(connection)

    connection.execute('''
        UPDATE display_statistics
        SET
            ai_movies = ?,
            ai_series = ?,
            tmdb_movies = ?,
            tmdb_series = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    ''', (
        max(0, int(ai_movies or 0)),
        max(0, int(ai_series or 0)),
        max(0, int(tmdb_movies or 0)),
        max(0, int(tmdb_series or 0)),
    ))

    connection.commit()
    connection.close()


def set_trending_statistics(
    trending_movies=0,
    trending_series=0,
):
    connection = get_connection()
    _ensure_display_statistics_row(connection)

    connection.execute('''
        UPDATE display_statistics
        SET
            trending_movies = ?,
            trending_series = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    ''', (
        max(0, int(trending_movies or 0)),
        max(0, int(trending_series or 0)),
    ))

    connection.commit()
    connection.close()


def set_display_statistics(
    ai_movies=0,
    ai_series=0,
    tmdb_movies=0,
    tmdb_series=0,
    trending_movies=0,
    trending_series=0,
):
    """Replace the complete homepage display-statistics snapshot."""
    connection = get_connection()
    _ensure_display_statistics_row(connection)

    connection.execute('''
        UPDATE display_statistics
        SET
            ai_movies = ?,
            ai_series = ?,
            tmdb_movies = ?,
            tmdb_series = ?,
            trending_movies = ?,
            trending_series = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    ''', (
        max(0, int(ai_movies or 0)),
        max(0, int(ai_series or 0)),
        max(0, int(tmdb_movies or 0)),
        max(0, int(tmdb_series or 0)),
        max(0, int(trending_movies or 0)),
        max(0, int(trending_series or 0)),
    ))

    connection.commit()
    connection.close()


# ============================================================
# LIFETIME STATISTICS
# ============================================================

def get_lifetime_statistics():
    connection = get_connection()
    row = connection.execute('''
        SELECT watched_total, movies_total, series_total,
               recommendations_total, ai_generated_total, updated_at
        FROM lifetime_statistics
        WHERE id = 1
        LIMIT 1
    ''').fetchone()
    connection.close()
    if not row:
        return {
            "watched": 0, "movies": 0, "series": 0,
            "recommendations": 0, "ai_generated": 0,
            "updated_at": None,
        }
    return {
        "watched": max(0, int(row["watched_total"] or 0)),
        "movies": max(0, int(row["movies_total"] or 0)),
        "series": max(0, int(row["series_total"] or 0)),
        "recommendations": max(0, int(row["recommendations_total"] or 0)),
        "ai_generated": max(0, int(row["ai_generated_total"] or 0)),
        "updated_at": row["updated_at"],
    }


def increment_lifetime_recommendations(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0):
    ai_movies = max(0, int(ai_movies or 0))
    ai_series = max(0, int(ai_series or 0))
    tmdb_movies = max(0, int(tmdb_movies or 0))
    tmdb_series = max(0, int(tmdb_series or 0))
    recommendation_increment = ai_movies + ai_series + tmdb_movies + tmdb_series
    if recommendation_increment == 0:
        return get_lifetime_statistics()
    connection = get_connection()
    connection.execute('''
        UPDATE lifetime_statistics
        SET recommendations_total = recommendations_total + ?,
            ai_generated_total = ai_generated_total + ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    ''', (recommendation_increment, ai_movies + ai_series))
    connection.commit()
    connection.close()
    return get_lifetime_statistics()


def increment_lifetime_trending(media_type, tmdb_ids):
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    connection = get_connection()
    added = 0
    for tmdb_id in tmdb_ids or []:
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            continue
        cursor = connection.execute('''
            INSERT OR IGNORE INTO lifetime_trending_seen (day, type, tmdb_id)
            VALUES (?, ?, ?)
        ''', (day, media_type, tmdb_id))
        if cursor.rowcount:
            added += 1
    if added:
        connection.execute('''
            UPDATE lifetime_statistics
            SET recommendations_total = recommendations_total + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        ''', (added,))
    connection.commit()
    connection.close()
    return get_lifetime_statistics()

import json
import os
import sqlite3
from pathlib import Path

# ============================================================
# DATABASE
# ============================================================

_requested_data_dir = Path(os.getenv("MOVIE_AI_DATA_DIR", "/data"))
try:
    _requested_data_dir.mkdir(parents=True, exist_ok=True)
    DATA_DIR = _requested_data_dir
except OSError:
    DATA_DIR = Path.cwd() / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "movies.db"


def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


# ============================================================
# INITIALIZE
# ============================================================


def init_database():
    connection = get_connection()
    try:
        connection.execute("BEGIN")

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

        # Recommendation jobs were introduced after older releases had already
        # created a table with a different primary-key column name. SQLite's
        # CREATE TABLE IF NOT EXISTS does not migrate an existing table, so
        # normalize that legacy schema before any code queries the canonical
        # `id` column.
        jobs_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recommendation_jobs'"
        ).fetchone()
        if jobs_table:
            legacy_columns = {row['name'] for row in connection.execute('PRAGMA table_info(recommendation_jobs)').fetchall()}
            if 'id' not in legacy_columns:
                connection.execute('ALTER TABLE recommendation_jobs RENAME TO recommendation_jobs_legacy')
                connection.execute('''
                    CREATE TABLE recommendation_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL CHECK(status IN ('loading', 'ready', 'error', 'superseded')),
                        data_json TEXT,
                        tmdb_data_json TEXT,
                        error TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                legacy = {row['name'] for row in connection.execute('PRAGMA table_info(recommendation_jobs_legacy)').fetchall()}

                def legacy_expr(*names, default='NULL'):
                    for name in names:
                        if name in legacy:
                            return f'legacy.{name}'
                    return default

                status_expr = legacy_expr('status', 'state', default="'ready'")
                data_expr = legacy_expr('data_json', 'data', 'results_json')
                tmdb_expr = legacy_expr('tmdb_data_json', 'tmdb_json')
                error_expr = legacy_expr('error', 'error_message')
                created_expr = legacy_expr('created_at', 'created', default='CURRENT_TIMESTAMP')
                updated_expr = legacy_expr('updated_at', 'updated', default=created_expr)
                connection.execute(f'''
                    INSERT INTO recommendation_jobs (status, data_json, tmdb_data_json, error, created_at, updated_at)
                    SELECT {status_expr}, {data_expr}, {tmdb_expr}, {error_expr}, {created_expr}, {updated_expr}
                    FROM recommendation_jobs_legacy AS legacy
                ''')
                connection.execute('DROP TABLE recommendation_jobs_legacy')
            else:
                # Add fields introduced by the persistent-job implementation if
                # an intermediate release created a partial table.
                required_job_columns = {
                    'status': "TEXT NOT NULL DEFAULT 'ready'",
                    'data_json': 'TEXT',
                    'tmdb_data_json': 'TEXT',
                    'error': 'TEXT',
                    'created_at': 'DATETIME',
                    'updated_at': 'DATETIME',
                }
                for column_name, column_type in required_job_columns.items():
                    if column_name not in legacy_columns:
                        connection.execute(
                            f'ALTER TABLE recommendation_jobs ADD COLUMN {column_name} {column_type}'
                        )
        else:
            connection.execute('''
                CREATE TABLE recommendation_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL CHECK(status IN ('loading', 'ready', 'error', 'superseded')),
                    data_json TEXT,
                    tmdb_data_json TEXT,
                    error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

        connection.execute('''
            CREATE TABLE IF NOT EXISTS tmdb_cache (
                cache_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        connection.execute('''
            INSERT OR IGNORE INTO display_statistics (
                id, ai_movies, ai_series, tmdb_movies, tmdb_series,
                trending_movies, trending_series
            ) VALUES (1, 0, 0, 0, 0, 0, 0)
        ''')

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

        # Preserve the oldest row when old versions already contain duplicates.
        connection.execute('''
            DELETE FROM watched
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM watched
                WHERE tmdb_id IS NOT NULL
                GROUP BY type, tmdb_id
            )
            AND tmdb_id IS NOT NULL
        ''')
        connection.execute('''
            DELETE FROM watched
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM watched
                WHERE tmdb_id IS NULL
                GROUP BY type, LOWER(TRIM(title))
            )
            AND tmdb_id IS NULL
        ''')

        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_watched_tmdb_type
            ON watched(type, tmdb_id)
            WHERE tmdb_id IS NOT NULL
        ''')
        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_watched_title_type
            ON watched(type, title COLLATE NOCASE)
            WHERE tmdb_id IS NULL
        ''')
        # Older releases may already contain duplicate history entries.
        # Collapse those records before adding the new DB-enforced uniqueness.
        connection.execute('''
            DELETE FROM recommendation_history
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM recommendation_history
                GROUP BY type, tmdb_id
            )
        ''')
        connection.execute('''
            DELETE FROM not_interested
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM not_interested
                GROUP BY type, tmdb_id
            )
        ''')

        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_recommendation_history_media
            ON recommendation_history(type, tmdb_id)
        ''')
        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_not_interested_media
            ON not_interested(type, tmdb_id)
        ''')
        connection.execute("CREATE INDEX IF NOT EXISTS ix_watched_type ON watched(type)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_watched_tmdb ON watched(tmdb_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_jobs_updated ON recommendation_jobs(updated_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_cache_expires ON tmdb_cache(expires_at)")

        row = connection.execute(
            'SELECT id FROM lifetime_statistics WHERE id = 1'
        ).fetchone()
        if row is None:
            watched_total = connection.execute(
                'SELECT COUNT(*) AS c FROM watched'
            ).fetchone()['c']
            movies_total = connection.execute(
                "SELECT COUNT(*) AS c FROM watched WHERE type = 'Movie'"
            ).fetchone()['c']
            series_total = connection.execute(
                "SELECT COUNT(*) AS c FROM watched WHERE type = 'Series'"
            ).fetchone()['c']
            recommendations_total = connection.execute(
                'SELECT COUNT(*) AS c FROM recommendation_history'
            ).fetchone()['c']
            connection.execute('''
                INSERT INTO lifetime_statistics (
                    id, watched_total, movies_total, series_total,
                    recommendations_total, ai_generated_total
                ) VALUES (1, ?, ?, ?, ?, 0)
            ''', (
                int(watched_total or 0),
                int(movies_total or 0),
                int(series_total or 0),
                int(recommendations_total or 0),
            ))

        # A process restart cannot resume an in-flight Python task. Keep the
        # job visible and explicitly mark it as interrupted instead of losing it.
        connection.execute('''
            UPDATE recommendation_jobs
            SET status = 'error',
                error = 'Recommendation job interrupted by application restart.',
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'loading'
        ''')

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ============================================================
# WATCHED
# ============================================================


def get_all():
    connection = get_connection()
    try:
        rows = connection.execute('''
            SELECT id, title, rating, type, poster, backdrop, year, overview, tmdb_id
            FROM watched
            ORDER BY id DESC
        ''').fetchall()
        return rows
    finally:
        connection.close()


def find_movie(title=None, media_type=None, tmdb_id=None):
    connection = get_connection()
    try:
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
        return result
    finally:
        connection.close()


def movie_exists(title=None, media_type=None, tmdb_id=None):
    return find_movie(title=title, media_type=media_type, tmdb_id=tmdb_id) is not None


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
    if media_type not in ('Movie', 'Series'):
        raise ValueError('Invalid media type')

    title = (title or '').strip()
    if not title:
        raise ValueError('Title is required')

    connection = get_connection()
    try:
        connection.execute('BEGIN IMMEDIATE')
        existing = None
        if tmdb_id:
            existing = connection.execute('''
                SELECT * FROM watched
                WHERE tmdb_id = ? AND type = ?
                LIMIT 1
            ''', (int(tmdb_id), media_type)).fetchone()
        if existing is None:
            existing = connection.execute('''
                SELECT * FROM watched
                WHERE LOWER(TRIM(title)) = LOWER(TRIM(?))
                  AND type = ?
                LIMIT 1
            ''', (title, media_type)).fetchone()

        if existing:
            final_title = title or existing['title']
            final_poster = poster or existing['poster']
            final_backdrop = backdrop or existing['backdrop']
            final_year = year if year is not None else existing['year']
            final_overview = overview if overview else existing['overview']
            final_tmdb_id = int(tmdb_id) if tmdb_id else existing['tmdb_id']
            connection.execute('''
                UPDATE watched
                SET title=?, rating=?, type=?, poster=?, backdrop=?, year=?, overview=?, tmdb_id=?
                WHERE id=?
            ''', (
                final_title, float(rating), media_type, final_poster, final_backdrop,
                final_year, final_overview, final_tmdb_id, existing['id']
            ))
            connection.commit()
            return existing['id']

        cursor = connection.execute('''
            INSERT INTO watched (
                title, rating, type, poster, backdrop, year, overview, tmdb_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            title, float(rating), media_type, poster, backdrop, year, overview,
            int(tmdb_id) if tmdb_id else None,
        ))

        connection.execute('''
            UPDATE lifetime_statistics
            SET watched_total = watched_total + 1,
                movies_total = movies_total + ?,
                series_total = series_total + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        ''', (
            1 if media_type == 'Movie' else 0,
            1 if media_type == 'Series' else 0,
        ))
        connection.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        connection.rollback()
        existing = connection.execute('''
            SELECT id FROM watched
            WHERE type = ? AND (
                (? IS NOT NULL AND tmdb_id = ?) OR
                LOWER(TRIM(title)) = LOWER(TRIM(?))
            )
            ORDER BY id ASC LIMIT 1
        ''', (media_type, tmdb_id, tmdb_id, title)).fetchone()
        if existing:
            return existing['id']
        raise
    finally:
        connection.close()


def delete_movie(movie_id):
    connection = get_connection()
    try:
        connection.execute('DELETE FROM watched WHERE id = ?', (movie_id,))
        connection.commit()
    finally:
        connection.close()


# ============================================================
# RECOMMENDATION HISTORY
# ============================================================


def add_recommendation_history(tmdb_id, media_type, title):
    if not tmdb_id or media_type not in ('Movie', 'Series'):
        return False
    connection = get_connection()
    try:
        cursor = connection.execute('''
            INSERT OR IGNORE INTO recommendation_history (tmdb_id, type, title)
            VALUES (?, ?, ?)
        ''', (int(tmdb_id), media_type, title))
        connection.commit()
        return bool(cursor.rowcount)
    finally:
        connection.close()


def get_recent_recommendation_ids(media_type, limit=50):
    connection = get_connection()
    try:
        rows = connection.execute('''
            SELECT tmdb_id
            FROM recommendation_history
            WHERE type = ?
            ORDER BY id DESC
            LIMIT ?
        ''', (media_type, int(limit))).fetchall()
        return [row['tmdb_id'] for row in rows]
    finally:
        connection.close()


# ============================================================
# NOT INTERESTED
# ============================================================


def add_not_interested(tmdb_id, media_type, title):
    if not tmdb_id or media_type not in ('Movie', 'Series'):
        return False
    connection = get_connection()
    try:
        cursor = connection.execute('''
            INSERT OR IGNORE INTO not_interested (tmdb_id, type, title)
            VALUES (?, ?, ?)
        ''', (int(tmdb_id), media_type, title))
        connection.commit()
        return bool(cursor.rowcount)
    finally:
        connection.close()


def get_not_interested_ids(media_type):
    connection = get_connection()
    try:
        rows = connection.execute('''
            SELECT tmdb_id FROM not_interested WHERE type = ?
        ''', (media_type,)).fetchall()
        return [row['tmdb_id'] for row in rows]
    finally:
        connection.close()


# ============================================================
# TMDB CACHE
# ============================================================


def get_tmdb_cache(cache_key, now=None):
    import time
    now = time.time() if now is None else now
    connection = get_connection()
    try:
        row = connection.execute('''
            SELECT payload_json, expires_at
            FROM tmdb_cache
            WHERE cache_key = ?
        ''', (cache_key,)).fetchone()
        if not row:
            return None
        if float(row['expires_at']) <= now:
            connection.execute('DELETE FROM tmdb_cache WHERE cache_key = ?', (cache_key,))
            connection.commit()
            return None
        try:
            return json.loads(row['payload_json'])
        except (TypeError, ValueError, json.JSONDecodeError):
            connection.execute('DELETE FROM tmdb_cache WHERE cache_key = ?', (cache_key,))
            connection.commit()
            return None
    finally:
        connection.close()


def set_tmdb_cache(cache_key, payload, ttl_seconds):
    import time
    if ttl_seconds <= 0:
        return
    connection = get_connection()
    try:
        connection.execute('''
            INSERT INTO tmdb_cache(cache_key, payload_json, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                expires_at = excluded.expires_at
        ''', (cache_key, json.dumps(payload, ensure_ascii=False), time.time() + float(ttl_seconds)))
        connection.commit()
    finally:
        connection.close()


def prune_tmdb_cache(max_rows=2000):
    connection = get_connection()
    try:
        connection.execute('DELETE FROM tmdb_cache WHERE expires_at <= strftime(\'%s\', \'now\')')
        connection.execute('''
            DELETE FROM tmdb_cache
            WHERE cache_key IN (
                SELECT cache_key FROM tmdb_cache
                ORDER BY expires_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM tmdb_cache) - ?)
            )
        ''', (int(max_rows),))
        connection.commit()
    finally:
        connection.close()


# ============================================================
# RECOMMENDATION JOBS
# ============================================================


def _row_to_job(row):
    if not row:
        return None
    def parse(value):
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return {
        'id': int(row['id']),
        'status': row['status'],
        'data': parse(row['data_json']),
        'tmdb_data': parse(row['tmdb_data_json']),
        'error': row['error'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def get_latest_recommendation_job():
    connection = get_connection()
    try:
        row = connection.execute('''
            SELECT * FROM recommendation_jobs
            ORDER BY id DESC LIMIT 1
        ''').fetchone()
        return _row_to_job(row)
    finally:
        connection.close()


def start_recommendation_job(force=False, tmdb_data=None):
    connection = get_connection()
    try:
        connection.execute('BEGIN IMMEDIATE')
        latest = connection.execute('''
            SELECT * FROM recommendation_jobs ORDER BY id DESC LIMIT 1
        ''').fetchone()
        if latest and latest['status'] == 'loading' and not force:
            return _row_to_job(latest)
        if latest and latest['status'] == 'loading':
            connection.execute('''
                UPDATE recommendation_jobs
                SET status='superseded', error='Superseded by a newer recommendation job.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (latest['id'],))
        cursor = connection.execute('''
            INSERT INTO recommendation_jobs(status, tmdb_data_json)
            VALUES('loading', ?)
        ''', (json.dumps(tmdb_data, ensure_ascii=False) if tmdb_data is not None else None,))
        connection.commit()
        row = connection.execute('SELECT * FROM recommendation_jobs WHERE id=?', (cursor.lastrowid,)).fetchone()
        return _row_to_job(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_recommendation_job(job_id, status, data=None, tmdb_data=None, error=None):
    if status not in ('loading', 'ready', 'error', 'superseded'):
        raise ValueError('Invalid recommendation job status')
    connection = get_connection()
    try:
        fields = ['status = ?', 'updated_at = CURRENT_TIMESTAMP']
        values = [status]
        if data is not None:
            fields.append('data_json = ?')
            values.append(json.dumps(data, ensure_ascii=False))
        if tmdb_data is not None:
            fields.append('tmdb_data_json = ?')
            values.append(json.dumps(tmdb_data, ensure_ascii=False))
        if error is not None or status != 'ready':
            fields.append('error = ?')
            values.append(error)
        values.append(int(job_id))
        connection.execute(
            f"UPDATE recommendation_jobs SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        connection.commit()
    finally:
        connection.close()


def is_current_recommendation_job(job_id):
    connection = get_connection()
    try:
        row = connection.execute('SELECT id FROM recommendation_jobs ORDER BY id DESC LIMIT 1').fetchone()
        return bool(row and int(row['id']) == int(job_id))
    finally:
        connection.close()


# ============================================================
# DISPLAY STATISTICS
# ============================================================


def _ensure_display_statistics_row(connection):
    connection.execute('''
        INSERT OR IGNORE INTO display_statistics (
            id, ai_movies, ai_series, tmdb_movies, tmdb_series,
            trending_movies, trending_series
        ) VALUES (1, 0, 0, 0, 0, 0, 0)
    ''')


def get_display_statistics():
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        row = connection.execute('''
            SELECT ai_movies, ai_series, tmdb_movies, tmdb_series,
                   trending_movies, trending_series, updated_at
            FROM display_statistics WHERE id = 1 LIMIT 1
        ''').fetchone()
        if not row:
            return {
                'ai_movies': 0, 'ai_series': 0, 'tmdb_movies': 0,
                'tmdb_series': 0, 'trending_movies': 0, 'trending_series': 0,
                'ai_generated': 0, 'recommendations': 0, 'updated_at': None,
            }
        ai_movies = max(0, int(row['ai_movies'] or 0))
        ai_series = max(0, int(row['ai_series'] or 0))
        tmdb_movies = max(0, int(row['tmdb_movies'] or 0))
        tmdb_series = max(0, int(row['tmdb_series'] or 0))
        trending_movies = max(0, int(row['trending_movies'] or 0))
        trending_series = max(0, int(row['trending_series'] or 0))
        ai_generated = ai_movies + ai_series
        recommendations = ai_generated + tmdb_movies + tmdb_series + trending_movies + trending_series
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
    finally:
        connection.close()


def set_recommendation_statistics(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0):
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        connection.execute('''
            UPDATE display_statistics
            SET ai_movies=?, ai_series=?, tmdb_movies=?, tmdb_series=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=1
        ''', (max(0, int(ai_movies or 0)), max(0, int(ai_series or 0)), max(0, int(tmdb_movies or 0)), max(0, int(tmdb_series or 0))))
        connection.commit()
    finally:
        connection.close()


def set_trending_statistics(trending_movies=0, trending_series=0):
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        connection.execute('''
            UPDATE display_statistics
            SET trending_movies=?, trending_series=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=1
        ''', (max(0, int(trending_movies or 0)), max(0, int(trending_series or 0))))
        connection.commit()
    finally:
        connection.close()


def set_display_statistics(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0, trending_movies=0, trending_series=0):
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        connection.execute('''
            UPDATE display_statistics
            SET ai_movies=?, ai_series=?, tmdb_movies=?, tmdb_series=?,
                trending_movies=?, trending_series=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=1
        ''', (
            max(0, int(ai_movies or 0)), max(0, int(ai_series or 0)),
            max(0, int(tmdb_movies or 0)), max(0, int(tmdb_series or 0)),
            max(0, int(trending_movies or 0)), max(0, int(trending_series or 0)),
        ))
        connection.commit()
    finally:
        connection.close()


# ============================================================
# LIFETIME STATISTICS
# ============================================================


def get_lifetime_statistics():
    connection = get_connection()
    try:
        row = connection.execute('''
            SELECT watched_total, movies_total, series_total,
                   recommendations_total, ai_generated_total, updated_at
            FROM lifetime_statistics WHERE id=1 LIMIT 1
        ''').fetchone()
        if not row:
            return {
                'watched': 0, 'movies': 0, 'series': 0,
                'recommendations': 0, 'ai_generated': 0, 'updated_at': None,
            }
        return {
            'watched': max(0, int(row['watched_total'] or 0)),
            'movies': max(0, int(row['movies_total'] or 0)),
            'series': max(0, int(row['series_total'] or 0)),
            'recommendations': max(0, int(row['recommendations_total'] or 0)),
            'ai_generated': max(0, int(row['ai_generated_total'] or 0)),
            'updated_at': row['updated_at'],
        }
    finally:
        connection.close()


def increment_lifetime_recommendations(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0):
    ai_movies = max(0, int(ai_movies or 0))
    ai_series = max(0, int(ai_series or 0))
    tmdb_movies = max(0, int(tmdb_movies or 0))
    tmdb_series = max(0, int(tmdb_series or 0))
    recommendation_increment = ai_movies + ai_series + tmdb_movies + tmdb_series
    if recommendation_increment == 0:
        return get_lifetime_statistics()
    connection = get_connection()
    try:
        connection.execute('''
            UPDATE lifetime_statistics
            SET recommendations_total=recommendations_total+?,
                ai_generated_total=ai_generated_total+?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=1
        ''', (recommendation_increment, ai_movies + ai_series))
        connection.commit()
        return get_lifetime_statistics()
    finally:
        connection.close()


def increment_lifetime_trending(media_type, tmdb_ids):
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    connection = get_connection()
    try:
        added = 0
        for tmdb_id in tmdb_ids or []:
            try:
                tmdb_id = int(tmdb_id)
            except (TypeError, ValueError):
                continue
            cursor = connection.execute('''
                INSERT OR IGNORE INTO lifetime_trending_seen(day, type, tmdb_id)
                VALUES (?, ?, ?)
            ''', (day, media_type, tmdb_id))
            if cursor.rowcount:
                added += 1
        if added:
            connection.execute('''
                UPDATE lifetime_statistics
                SET recommendations_total=recommendations_total+?, updated_at=CURRENT_TIMESTAMP
                WHERE id=1
            ''', (added,))
        connection.commit()
        return get_lifetime_statistics()
    finally:
        connection.close()

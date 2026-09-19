import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path('/data')
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'movies.db'


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA journal_mode = WAL')
    connection.execute('PRAGMA busy_timeout = 15000')
    return connection


def _add_column_if_missing(connection, table, column, column_type):
    columns = connection.execute(f'PRAGMA table_info({table})').fetchall()
    if column not in {row['name'] for row in columns}:
        connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {column_type}')


def _dedupe_table(connection, table):
    connection.execute(f'''
        DELETE FROM {table}
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM {table}
            GROUP BY tmdb_id, type
        )
    ''')


def init_database():
    connection = get_connection()
    try:
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS watched (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                rating REAL NOT NULL DEFAULT 0,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
                poster TEXT,
                backdrop TEXT,
                year INTEGER,
                overview TEXT,
                tmdb_id INTEGER,
                genres TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                watch_count INTEGER NOT NULL DEFAULT 0,
                last_watched_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
                title TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS not_interested (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
                title TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
                title TEXT NOT NULL,
                feedback TEXT NOT NULL CHECK(feedback IN ('like', 'later', 'dislike', 'already_watched')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS watch_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watched_id INTEGER,
                tmdb_id INTEGER,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
                title TEXT NOT NULL,
                rating REAL,
                event TEXT NOT NULL DEFAULT 'watched',
                watched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lifetime_statistics (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                watched_total INTEGER NOT NULL DEFAULT 0,
                movies_total INTEGER NOT NULL DEFAULT 0,
                series_total INTEGER NOT NULL DEFAULT 0,
                recommendations_total INTEGER NOT NULL DEFAULT 0,
                ai_generated_total INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS lifetime_trending_seen (
                day TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'Series')),
                tmdb_id INTEGER NOT NULL,
                PRIMARY KEY (day, type, tmdb_id)
            );

            CREATE TABLE IF NOT EXISTS display_statistics (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ai_movies INTEGER NOT NULL DEFAULT 0,
                ai_series INTEGER NOT NULL DEFAULT 0,
                tmdb_movies INTEGER NOT NULL DEFAULT 0,
                tmdb_series INTEGER NOT NULL DEFAULT 0,
                trending_movies INTEGER NOT NULL DEFAULT 0,
                trending_series INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_jobs (
                job_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'loading', 'ready', 'error')),
                data_json TEXT,
                tmdb_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        ''')

        # Migrate older databases.
        for column, column_type in {
            'poster': 'TEXT',
            'backdrop': 'TEXT',
            'year': 'INTEGER',
            'overview': 'TEXT',
            'tmdb_id': 'INTEGER',
            'genres': "TEXT DEFAULT '[]'",
            'notes': "TEXT DEFAULT ''",
            'watch_count': 'INTEGER NOT NULL DEFAULT 0',
            'last_watched_at': 'TEXT',
            'created_at': 'TEXT',
            'updated_at': 'TEXT',
        }.items():
            _add_column_if_missing(connection, 'watched', column, column_type)

        # Remove old duplicate rows before enforcing uniqueness.
        connection.execute('''
            DELETE FROM watched
            WHERE id NOT IN (
                SELECT MIN(id) FROM watched
                WHERE tmdb_id IS NOT NULL
                GROUP BY tmdb_id, type
            )
            AND tmdb_id IS NOT NULL
        ''')
        connection.execute('''
            DELETE FROM recommendation_history
            WHERE id NOT IN (
                SELECT MIN(id) FROM recommendation_history GROUP BY tmdb_id, type
            )
        ''')
        connection.execute('''
            DELETE FROM not_interested
            WHERE id NOT IN (
                SELECT MIN(id) FROM not_interested GROUP BY tmdb_id, type
            )
        ''')
        connection.execute('''
            DELETE FROM recommendation_feedback
            WHERE id NOT IN (
                SELECT MAX(id) FROM recommendation_feedback GROUP BY tmdb_id, type
            )
        ''')

        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_watched_tmdb_type
            ON watched(tmdb_id, type)
            WHERE tmdb_id IS NOT NULL
        ''')
        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_watched_title_type_year
            ON watched(lower(trim(title)), type, COALESCE(year, 0))
            WHERE tmdb_id IS NULL
        ''')
        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_recommendation_history_tmdb_type
            ON recommendation_history(tmdb_id, type)
        ''')
        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_not_interested_tmdb_type
            ON not_interested(tmdb_id, type)
        ''')
        connection.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_recommendation_feedback_tmdb_type
            ON recommendation_feedback(tmdb_id, type)
        ''')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_watched_type ON watched(type)')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_watched_year ON watched(year)')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_watched_last_watched ON watched(last_watched_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_history_watched_at ON watch_history(watched_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_history_tmdb_type ON watch_history(tmdb_id, type)')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_jobs_client_status ON recommendation_jobs(client_id, status, updated_at)')

        # Initialize lifetime stats from current library only on first creation.
        row = connection.execute('SELECT id FROM lifetime_statistics WHERE id = 1').fetchone()
        if row is None:
            watched_total = connection.execute('SELECT COUNT(*) c FROM watched').fetchone()['c']
            movies_total = connection.execute("SELECT COUNT(*) c FROM watched WHERE type='Movie'").fetchone()['c']
            series_total = connection.execute("SELECT COUNT(*) c FROM watched WHERE type='Series'").fetchone()['c']
            rec_total = connection.execute('SELECT COUNT(*) c FROM recommendation_history').fetchone()['c']
            connection.execute('''
                INSERT INTO lifetime_statistics(id, watched_total, movies_total, series_total, recommendations_total, ai_generated_total)
                VALUES (1, ?, ?, ?, ?, 0)
            ''', (watched_total, movies_total, series_total, rec_total))

        connection.execute('''
            INSERT OR IGNORE INTO display_statistics(id) VALUES (1)
        ''')

        # Normalize missing timestamps in old rows.
        now = utc_now()
        connection.execute('UPDATE watched SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL', (now,))
        connection.execute('UPDATE watched SET updated_at = COALESCE(updated_at, created_at, ?) WHERE updated_at IS NULL', (now,))
        connection.execute("UPDATE watched SET genres = '[]' WHERE genres IS NULL OR trim(genres) = ''")
        connection.execute("UPDATE watched SET notes = '' WHERE notes IS NULL")

        connection.commit()
    finally:
        connection.close()


# ============================================================
# LIBRARY
# ============================================================

def get_all():
    connection = get_connection()
    try:
        return connection.execute('''
            SELECT id,title,rating,type,poster,backdrop,year,overview,tmdb_id,
                   genres,notes,watch_count,last_watched_at,created_at,updated_at
            FROM watched
            ORDER BY COALESCE(last_watched_at, updated_at, created_at) DESC, id DESC
        ''').fetchall()
    finally:
        connection.close()


def _decode_row(row):
    if not row:
        return None
    item = dict(row)
    try:
        item['genres'] = json.loads(item.get('genres') or '[]')
    except (ValueError, TypeError):
        item['genres'] = []
    item['watch_count'] = int(item.get('watch_count') or 0)
    return item


def find_movie(title=None, media_type=None, tmdb_id=None):
    connection = get_connection()
    try:
        result = None
        if tmdb_id and media_type:
            result = connection.execute('SELECT * FROM watched WHERE tmdb_id=? AND type=? LIMIT 1', (tmdb_id, media_type)).fetchone()
        if result is None and title and media_type:
            result = connection.execute('''
                SELECT * FROM watched
                WHERE lower(trim(title))=lower(trim(?)) AND type=?
                ORDER BY id DESC LIMIT 1
            ''', (title, media_type)).fetchone()
        return result
    finally:
        connection.close()


def movie_exists(title=None, media_type=None, tmdb_id=None):
    return find_movie(title=title, media_type=media_type, tmdb_id=tmdb_id) is not None


def add_movie(title, rating, media_type, poster=None, backdrop=None, year=None,
              overview=None, tmdb_id=None, genres=None, notes=None):
    title = (title or '').strip()
    rating = max(0, min(10, float(rating or 0)))
    if media_type not in ('Movie', 'Series'):
        raise ValueError('Invalid media type')

    connection = get_connection()
    now = utc_now()
    genres_json = json.dumps(genres or [], ensure_ascii=False)
    try:
        connection.execute('BEGIN IMMEDIATE')
        existing = None
        if tmdb_id:
            existing = connection.execute('SELECT * FROM watched WHERE tmdb_id=? AND type=? LIMIT 1', (tmdb_id, media_type)).fetchone()
        if existing is None:
            if year is not None:
                existing = connection.execute('''
                    SELECT * FROM watched
                    WHERE lower(trim(title))=lower(trim(?)) AND type=? AND COALESCE(year,0)=COALESCE(?,0)
                    LIMIT 1
                ''', (title, media_type, year)).fetchone()
            else:
                existing = connection.execute('''
                    SELECT * FROM watched
                    WHERE lower(trim(title))=lower(trim(?)) AND type=? AND tmdb_id IS NULL
                    LIMIT 1
                ''', (title, media_type)).fetchone()

        if existing:
            connection.execute('''
                UPDATE watched SET title=?,rating=?,type=?,poster=?,backdrop=?,year=?,overview=?,tmdb_id=?,genres=?,notes=?,updated_at=?
                WHERE id=?
            ''', (
                title or existing['title'], rating, media_type,
                poster if poster else existing['poster'],
                backdrop if backdrop else existing['backdrop'],
                year if year is not None else existing['year'],
                overview if overview is not None and overview != '' else existing['overview'],
                tmdb_id if tmdb_id else existing['tmdb_id'],
                genres_json if genres else existing['genres'],
                notes if notes is not None else existing['notes'],
                now, existing['id'],
            ))
            connection.commit()
            return existing['id']

        try:
            cursor = connection.execute('''
                INSERT INTO watched(title,rating,type,poster,backdrop,year,overview,tmdb_id,genres,notes,watch_count,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
            ''', (title, rating, media_type, poster, backdrop, year, overview or '', tmdb_id, genres_json, notes or '', now, now))
        except sqlite3.IntegrityError:
            # Race-safe fallback when another worker inserted the same title/ID.
            connection.rollback()
            return add_movie(title, rating, media_type, poster, backdrop, year, overview, tmdb_id, genres, notes)

        connection.execute('''
            UPDATE lifetime_statistics SET watched_total=watched_total+1,
                movies_total=movies_total+?, series_total=series_total+?, updated_at=CURRENT_TIMESTAMP WHERE id=1
        ''', (1 if media_type == 'Movie' else 0, 1 if media_type == 'Series' else 0))
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_movie(movie_id, title, rating, year=None, overview=None, notes=None):
    connection = get_connection()
    try:
        connection.execute('''
            UPDATE watched
            SET title=?, rating=?, year=?, overview=?, notes=?, updated_at=?
            WHERE id=?
        ''', ((title or '').strip(), max(0, min(10, float(rating or 0))), year, overview or '', notes or '', utc_now(), movie_id))
        connection.commit()
        row = connection.execute('SELECT * FROM watched WHERE id=?', (movie_id,)).fetchone()
        return _decode_row(row)
    finally:
        connection.close()


def delete_movie(movie_id):
    connection = get_connection()
    try:
        connection.execute('DELETE FROM watched WHERE id=?', (movie_id,))
        connection.commit()
    finally:
        connection.close()


def record_watch_event(movie_id=None, tmdb_id=None, media_type=None, title=None, rating=None, event='watched'):
    connection = get_connection()
    try:
        connection.execute('BEGIN IMMEDIATE')
        row = None
        if movie_id:
            row = connection.execute('SELECT * FROM watched WHERE id=?', (movie_id,)).fetchone()
        if row is not None:
            watched_at = utc_now()
            connection.execute('''
                UPDATE watched SET watch_count=COALESCE(watch_count,0)+1,last_watched_at=?,updated_at=? WHERE id=?
            ''', (watched_at, watched_at, row['id']))
            connection.execute('''
                INSERT INTO watch_history(watched_id,tmdb_id,type,title,rating,event,watched_at)
                VALUES(?,?,?,?,?,?,?)
            ''', (row['id'], row['tmdb_id'], row['type'], row['title'], row['rating'], event, watched_at))
            connection.commit()
            return _decode_row(connection.execute('SELECT * FROM watched WHERE id=?', (row['id'],)).fetchone())

        if not media_type or not title:
            connection.rollback()
            return None
        watched_at = utc_now()
        connection.execute('''
            INSERT INTO watch_history(watched_id,tmdb_id,type,title,rating,event,watched_at)
            VALUES(NULL,?,?,?,?,?,?)
        ''', (tmdb_id, media_type, title, rating, event, watched_at))
        connection.commit()
        return None
    finally:
        connection.close()


def get_watch_history(limit=100):
    connection = get_connection()
    try:
        rows = connection.execute('''
            SELECT id,watched_id,tmdb_id,type,title,rating,event,watched_at
            FROM watch_history ORDER BY watched_at DESC LIMIT ?
        ''', (max(1, min(500, int(limit))),)).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


# ============================================================
# RECOMMENDATION HISTORY / FEEDBACK
# ============================================================

def add_recommendation_history(tmdb_id, media_type, title):
    if not tmdb_id:
        return
    connection = get_connection()
    try:
        connection.execute('''INSERT OR IGNORE INTO recommendation_history(tmdb_id,type,title) VALUES(?,?,?)''', (tmdb_id, media_type, title))
        connection.commit()
    finally:
        connection.close()


def get_recent_recommendation_ids(media_type, limit=50):
    connection = get_connection()
    try:
        rows = connection.execute('''
            SELECT tmdb_id FROM recommendation_history WHERE type=? ORDER BY created_at DESC LIMIT ?
        ''', (media_type, limit)).fetchall()
        return [row['tmdb_id'] for row in rows]
    finally:
        connection.close()


def add_not_interested(tmdb_id, media_type, title):
    if not tmdb_id:
        return
    connection = get_connection()
    try:
        connection.execute('''INSERT OR IGNORE INTO not_interested(tmdb_id,type,title) VALUES(?,?,?)''', (tmdb_id, media_type, title))
        connection.commit()
    finally:
        connection.close()


def get_not_interested_ids(media_type):
    connection = get_connection()
    try:
        rows = connection.execute('SELECT tmdb_id FROM not_interested WHERE type=?', (media_type,)).fetchall()
        return [row['tmdb_id'] for row in rows]
    finally:
        connection.close()


def set_recommendation_feedback(tmdb_id, media_type, title, feedback):
    if feedback not in ('like', 'later', 'dislike', 'already_watched'):
        raise ValueError('Invalid feedback')
    connection = get_connection()
    try:
        now = utc_now()
        connection.execute('''
            INSERT INTO recommendation_feedback(tmdb_id,type,title,feedback,created_at,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(tmdb_id,type) DO UPDATE SET title=excluded.title,feedback=excluded.feedback,updated_at=excluded.updated_at
        ''', (tmdb_id, media_type, title, feedback, now, now))
        if feedback == 'dislike':
            connection.execute('INSERT OR IGNORE INTO not_interested(tmdb_id,type,title) VALUES(?,?,?)', (tmdb_id, media_type, title))
        connection.commit()
    finally:
        connection.close()


def get_recommendation_feedback(media_type=None, feedback=None, limit=100):
    connection = get_connection()
    try:
        query = 'SELECT * FROM recommendation_feedback'
        values = []
        clauses = []
        if media_type:
            clauses.append('type=?'); values.append(media_type)
        if feedback:
            clauses.append('feedback=?'); values.append(feedback)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY updated_at DESC LIMIT ?'
        values.append(max(1, min(500, int(limit))))
        return [dict(row) for row in connection.execute(query, values).fetchall()]
    finally:
        connection.close()


# ============================================================
# RECOMMENDATION JOBS
# ============================================================

def create_recommendation_job(job_id, client_id, status='queued', tmdb_data=None):
    now = utc_now()
    connection = get_connection()
    try:
        # Keep at most one live job per browser client.
        connection.execute('''
            UPDATE recommendation_jobs
            SET status='error', error='Superseded by a newer recommendation request', updated_at=?
            WHERE client_id=? AND status IN ('queued','loading')
        ''', (now, client_id))
        connection.execute('''
            INSERT INTO recommendation_jobs(job_id,client_id,status,data_json,tmdb_json,error,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
        ''', (job_id, client_id, status, None, json.dumps(tmdb_data or {}, ensure_ascii=False), None, now, now))
        connection.commit()
    finally:
        connection.close()


def get_active_recommendation_job(client_id):
    connection = get_connection()
    try:
        row = connection.execute('''
            SELECT * FROM recommendation_jobs
            WHERE client_id=? AND status IN ('queued','loading')
            ORDER BY created_at DESC LIMIT 1
        ''', (client_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_recommendation_job(job_id):
    connection = get_connection()
    try:
        row = connection.execute('SELECT * FROM recommendation_jobs WHERE job_id=? LIMIT 1', (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ('data_json', 'tmdb_json'):
            raw = item.get(key)
            try:
                item[key.replace('_json', '')] = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                item[key.replace('_json', '')] = None
        return item
    finally:
        connection.close()


def is_recommendation_job_active(job_id):
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT status FROM recommendation_jobs WHERE job_id=? LIMIT 1",
            (job_id,),
        ).fetchone()
        return bool(row and row["status"] in ("queued", "loading"))
    finally:
        connection.close()


def update_recommendation_job(job_id, status=None, data=None, tmdb_data=None, error=None):
    connection = get_connection()
    try:
        current = connection.execute('SELECT * FROM recommendation_jobs WHERE job_id=?', (job_id,)).fetchone()
        if not current:
            return
        next_status = status or current['status']
        connection.execute('''
            UPDATE recommendation_jobs SET status=?, data_json=?, tmdb_json=?, error=?, updated_at=? WHERE job_id=?
        ''', (
            next_status,
            json.dumps(data, ensure_ascii=False) if data is not None else current['data_json'],
            json.dumps(tmdb_data, ensure_ascii=False) if tmdb_data is not None else current['tmdb_json'],
            error,
            utc_now(),
            job_id,
        ))
        connection.commit()
    finally:
        connection.close()


# ============================================================
# STATISTICS
# ============================================================

def _ensure_display_statistics_row(connection):
    connection.execute('INSERT OR IGNORE INTO display_statistics(id) VALUES(1)')


def get_display_statistics():
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        row = connection.execute('SELECT ai_movies,ai_series,tmdb_movies,tmdb_series,trending_movies,trending_series,updated_at FROM display_statistics WHERE id=1').fetchone()
        if not row:
            return {'ai_movies':0,'ai_series':0,'tmdb_movies':0,'tmdb_series':0,'trending_movies':0,'trending_series':0,'ai_generated':0,'recommendations':0,'updated_at':None}
        values = {key: int(row[key] or 0) if key != 'updated_at' else row[key] for key in row.keys()}
        values['ai_generated'] = values['ai_movies'] + values['ai_series']
        values['recommendations'] = values['ai_generated'] + values['tmdb_movies'] + values['tmdb_series'] + values['trending_movies'] + values['trending_series']
        return values
    finally:
        connection.close()


def set_recommendation_statistics(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0):
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        connection.execute('''UPDATE display_statistics SET ai_movies=?,ai_series=?,tmdb_movies=?,tmdb_series=?,updated_at=CURRENT_TIMESTAMP WHERE id=1''', (max(0,int(ai_movies)),max(0,int(ai_series)),max(0,int(tmdb_movies)),max(0,int(tmdb_series))))
        connection.commit()
    finally:
        connection.close()


def set_trending_statistics(trending_movies=0, trending_series=0):
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        connection.execute('''UPDATE display_statistics SET trending_movies=?,trending_series=?,updated_at=CURRENT_TIMESTAMP WHERE id=1''', (max(0,int(trending_movies)),max(0,int(trending_series))))
        connection.commit()
    finally:
        connection.close()


def set_display_statistics(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0, trending_movies=0, trending_series=0):
    connection = get_connection()
    try:
        _ensure_display_statistics_row(connection)
        connection.execute('''
            UPDATE display_statistics SET ai_movies=?,ai_series=?,tmdb_movies=?,tmdb_series=?,trending_movies=?,trending_series=?,updated_at=CURRENT_TIMESTAMP WHERE id=1
        ''', (max(0,int(ai_movies)),max(0,int(ai_series)),max(0,int(tmdb_movies)),max(0,int(tmdb_series)),max(0,int(trending_movies)),max(0,int(trending_series))))
        connection.commit()
    finally:
        connection.close()


def get_current_statistics():
    connection = get_connection()
    try:
        totals = connection.execute('''
            SELECT COUNT(*) total,
                   SUM(CASE WHEN type='Movie' THEN 1 ELSE 0 END) movies,
                   SUM(CASE WHEN type='Series' THEN 1 ELSE 0 END) series,
                   COALESCE(AVG(rating),0) avg_rating,
                   COALESCE(MAX(rating),0) max_rating,
                   COALESCE(MIN(rating),0) min_rating,
                   COALESCE(SUM(COALESCE(watch_count,0)),0) plays
            FROM watched
        ''').fetchone()
        years = [dict(row) for row in connection.execute('''
            SELECT COALESCE(year,0) year, COUNT(*) count FROM watched GROUP BY COALESCE(year,0) ORDER BY count DESC, year DESC LIMIT 12
        ''').fetchall()]
        genre_counts = {}
        for row in connection.execute('SELECT genres FROM watched').fetchall():
            try:
                genres = json.loads(row['genres'] or '[]')
            except (ValueError, TypeError):
                genres = []
            for genre in genres:
                if genre:
                    genre_counts[genre] = genre_counts.get(genre, 0) + 1
        genres = [{'genre': k, 'count': v} for k, v in sorted(genre_counts.items(), key=lambda item: (-item[1], item[0]))[:12]]
        recent = [dict(row) for row in connection.execute('''
            SELECT id,title,type,rating,year,poster,last_watched_at,watch_count FROM watched
            ORDER BY COALESCE(last_watched_at,updated_at,created_at) DESC,id DESC LIMIT 12
        ''').fetchall()]
        return {
            'total': int(totals['total'] or 0),
            'movies': int(totals['movies'] or 0),
            'series': int(totals['series'] or 0),
            'avg_rating': round(float(totals['avg_rating'] or 0), 2),
            'max_rating': round(float(totals['max_rating'] or 0), 1),
            'min_rating': round(float(totals['min_rating'] or 0), 1) if totals['total'] else 0,
            'plays': int(totals['plays'] or 0),
            'years': years,
            'genres': genres,
            'recent': recent,
        }
    finally:
        connection.close()


def get_lifetime_statistics():
    connection = get_connection()
    try:
        row = connection.execute('''
            SELECT watched_total,movies_total,series_total,recommendations_total,ai_generated_total,updated_at
            FROM lifetime_statistics WHERE id=1 LIMIT 1
        ''').fetchone()
        if not row:
            return {'watched':0,'movies':0,'series':0,'recommendations':0,'ai_generated':0,'updated_at':None}
        return {
            'watched': max(0,int(row['watched_total'] or 0)),
            'movies': max(0,int(row['movies_total'] or 0)),
            'series': max(0,int(row['series_total'] or 0)),
            'recommendations': max(0,int(row['recommendations_total'] or 0)),
            'ai_generated': max(0,int(row['ai_generated_total'] or 0)),
            'updated_at': row['updated_at'],
        }
    finally:
        connection.close()


def increment_lifetime_recommendations(ai_movies=0, ai_series=0, tmdb_movies=0, tmdb_series=0):
    ai_movies = max(0,int(ai_movies or 0)); ai_series=max(0,int(ai_series or 0)); tmdb_movies=max(0,int(tmdb_movies or 0)); tmdb_series=max(0,int(tmdb_series or 0))
    inc = ai_movies + ai_series + tmdb_movies + tmdb_series
    if not inc:
        return get_lifetime_statistics()
    connection = get_connection()
    try:
        connection.execute('''UPDATE lifetime_statistics SET recommendations_total=recommendations_total+?,ai_generated_total=ai_generated_total+?,updated_at=CURRENT_TIMESTAMP WHERE id=1''', (inc, ai_movies+ai_series))
        connection.commit()
    finally:
        connection.close()
    return get_lifetime_statistics()


def increment_lifetime_trending(media_type, tmdb_ids):
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    connection = get_connection(); added = 0
    try:
        for tmdb_id in tmdb_ids or []:
            try: tmdb_id = int(tmdb_id)
            except (TypeError, ValueError): continue
            cursor = connection.execute('INSERT OR IGNORE INTO lifetime_trending_seen(day,type,tmdb_id) VALUES(?,?,?)', (day, media_type, tmdb_id))
            if cursor.rowcount: added += 1
        if added:
            connection.execute('UPDATE lifetime_statistics SET recommendations_total=recommendations_total+?,updated_at=CURRENT_TIMESTAMP WHERE id=1', (added,))
        connection.commit()
    finally:
        connection.close()
    return get_lifetime_statistics()


init_database()

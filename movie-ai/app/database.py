import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path('/data')
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'movies.db'


def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=15000')
    return connection


def _column_names(connection, table):
    return {r['name'] for r in connection.execute(f'PRAGMA table_info({table})').fetchall()}


def _add_column(connection, table, name, definition):
    if name not in _column_names(connection, table):
        connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def init_database():
    c = get_connection()
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS watched (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        rating REAL NOT NULL CHECK(rating >= 0 AND rating <= 10),
        type TEXT NOT NULL CHECK(type IN ('Movie','Series')),
        poster TEXT, backdrop TEXT, year INTEGER, overview TEXT, tmdb_id INTEGER,
        genres TEXT DEFAULT '[]', cast TEXT DEFAULT '[]', director TEXT DEFAULT '',
        creators TEXT DEFAULT '[]', keywords TEXT DEFAULT '[]', runtime INTEGER,
        status TEXT DEFAULT '', added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    for n,d in {
        'genres':"TEXT DEFAULT '[]'", 'cast':"TEXT DEFAULT '[]'", 'director':"TEXT DEFAULT ''",
        'creators':"TEXT DEFAULT '[]'", 'keywords':"TEXT DEFAULT '[]'", 'runtime':'INTEGER',
        'status':"TEXT DEFAULT ''", 'added_at':'DATETIME', 'updated_at':'DATETIME'
    }.items(): _add_column(c,'watched',n,d)

    c.execute('''CREATE TABLE IF NOT EXISTS recommendation_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('Movie','Series')), title TEXT NOT NULL,
        job_id TEXT, source TEXT DEFAULT 'unknown', match_score REAL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tmdb_id,type)
    )''')
    for n,d in {'job_id':'TEXT','source':"TEXT DEFAULT 'unknown'",'match_score':'REAL'}.items(): _add_column(c,'recommendation_history',n,d)

    c.execute('''CREATE TABLE IF NOT EXISTS recommendation_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('Movie','Series')), title TEXT NOT NULL,
        event TEXT NOT NULL CHECK(event IN ('shown','clicked','watched','liked','disliked','not_interested','dismissed','deep_dive')),
        job_id TEXT, metadata TEXT DEFAULT '{}', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_rec_events_title ON recommendation_events(tmdb_id,type,created_at DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_rec_history_recent ON recommendation_history(type,created_at DESC)')
    c.execute('''CREATE TABLE IF NOT EXISTS not_interested (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('Movie','Series')), title TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(tmdb_id,type)
    )''')

    # Repair legacy duplicates before enforcing uniqueness.
    c.execute('''DELETE FROM recommendation_history WHERE id NOT IN (SELECT MAX(id) FROM recommendation_history GROUP BY tmdb_id,type)''')
    c.execute('''DELETE FROM not_interested WHERE id NOT IN (SELECT MAX(id) FROM not_interested GROUP BY tmdb_id,type)''')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_rec_history_title ON recommendation_history(tmdb_id,type)')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_not_interested_title ON not_interested(tmdb_id,type)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_watched_type_rating ON watched(type,rating DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_watched_tmdb ON watched(tmdb_id,type)')


    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        media_type TEXT NOT NULL CHECK(media_type IN ('Movie','Series')), title TEXT NOT NULL,
        year INTEGER, poster TEXT, backdrop TEXT, overview TEXT, vote_average REAL DEFAULT 0,
        priority INTEGER DEFAULT 0, added_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tmdb_id,media_type)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS tmdb_cache (
        cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tmdb_cache_expiry ON tmdb_cache(expires_at)')

    c.execute('''CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS series_progress (
        tmdb_id INTEGER PRIMARY KEY, season INTEGER DEFAULT 1, episode INTEGER DEFAULT 1,
        progress REAL DEFAULT 0, status TEXT DEFAULT 'Not started', updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS lifetime_statistics (
        id INTEGER PRIMARY KEY CHECK(id=1), watched_total INTEGER NOT NULL DEFAULT 0,
        movies_total INTEGER NOT NULL DEFAULT 0, series_total INTEGER NOT NULL DEFAULT 0,
        recommendations_total INTEGER NOT NULL DEFAULT 0, ai_generated_total INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lifetime_trending_seen (
        day TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('Movie','Series')), tmdb_id INTEGER NOT NULL,
        PRIMARY KEY(day,type,tmdb_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS display_statistics (
        id INTEGER PRIMARY KEY CHECK(id=1), ai_movies INTEGER DEFAULT 0, ai_series INTEGER DEFAULT 0,
        tmdb_movies INTEGER DEFAULT 0, tmdb_series INTEGER DEFAULT 0, trending_movies INTEGER DEFAULT 0,
        trending_series INTEGER DEFAULT 0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('INSERT OR IGNORE INTO display_statistics(id) VALUES(1)')

    row = c.execute('SELECT id FROM lifetime_statistics WHERE id=1').fetchone()
    if not row:
        wm = c.execute("SELECT COUNT(*) n FROM watched WHERE type='Movie'").fetchone()['n']
        ws = c.execute("SELECT COUNT(*) n FROM watched WHERE type='Series'").fetchone()['n']
        rh = c.execute('SELECT COUNT(*) n FROM recommendation_history').fetchone()['n']
        c.execute('INSERT INTO lifetime_statistics(id,watched_total,movies_total,series_total,recommendations_total,ai_generated_total) VALUES(1,?,?,?,?,?)',(wm+ws,wm,ws,rh,0))
    else:
        # Backfill only empty timestamps on upgraded databases.
        c.execute("UPDATE watched SET added_at=COALESCE(added_at,CURRENT_TIMESTAMP), updated_at=COALESCE(updated_at,CURRENT_TIMESTAMP)")
    c.commit(); c.close()


def _json(value):
    import json
    try: return json.loads(value or '[]')
    except Exception: return []


def _dump(value):
    import json
    if isinstance(value, str):
        try: json.loads(value); return value
        except Exception: return json.dumps([value], ensure_ascii=False)
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def get_all():
    c=get_connection(); rows=c.execute('SELECT * FROM watched ORDER BY id DESC').fetchall(); c.close(); return rows


def find_movie(title=None, media_type=None, tmdb_id=None):
    c=get_connection(); r=None
    if tmdb_id and media_type: r=c.execute('SELECT * FROM watched WHERE tmdb_id=? AND type=? LIMIT 1',(tmdb_id,media_type)).fetchone()
    if r is None and title and media_type: r=c.execute('SELECT * FROM watched WHERE LOWER(TRIM(title))=LOWER(TRIM(?)) AND type=? LIMIT 1',(title,media_type)).fetchone()
    c.close(); return r


def movie_exists(title=None, media_type=None, tmdb_id=None): return find_movie(title,media_type,tmdb_id) is not None


def add_movie(title,rating,media_type,poster=None,backdrop=None,year=None,overview=None,tmdb_id=None,genres=None,cast=None,director=None,creators=None,keywords=None,runtime=None,status=None):
    if media_type not in ('Movie','Series'): raise ValueError('Invalid media type')
    c=get_connection(); existing=None
    if tmdb_id: existing=c.execute('SELECT * FROM watched WHERE tmdb_id=? AND type=? LIMIT 1',(tmdb_id,media_type)).fetchone()
    if existing is None: existing=c.execute('SELECT * FROM watched WHERE LOWER(TRIM(title))=LOWER(TRIM(?)) AND type=? LIMIT 1',(title,media_type)).fetchone()
    vals=(title,rating,media_type,poster,backdrop,year,overview,tmdb_id,_dump(genres),_dump(cast),director or '',_dump(creators),_dump(keywords),runtime,status or '')
    if existing:
        genres_v = _dump(genres) if genres is not None else existing['genres']
        cast_v = _dump(cast) if cast is not None else existing['cast']
        creators_v = _dump(creators) if creators is not None else existing['creators']
        keywords_v = _dump(keywords) if keywords is not None else existing['keywords']
        director_v = director if director is not None else existing['director']
        status_v = status if status is not None else existing['status']
        c.execute('''UPDATE watched SET title=?,rating=?,type=?,poster=COALESCE(?,poster),backdrop=COALESCE(?,backdrop),year=COALESCE(?,year),overview=COALESCE(?,overview),tmdb_id=COALESCE(?,tmdb_id),genres=?,cast=?,director=?,creators=?,keywords=?,runtime=COALESCE(?,runtime),status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''', (title,rating,media_type,poster,backdrop,year,overview,tmdb_id,genres_v,cast_v,director_v,creators_v,keywords_v,runtime,status_v,existing['id']))
        new_id=existing['id']
    else:
        cur=c.execute('''INSERT INTO watched(title,rating,type,poster,backdrop,year,overview,tmdb_id,genres,cast,director,creators,keywords,runtime,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',vals)
        new_id=cur.lastrowid
        c.execute('''UPDATE lifetime_statistics SET watched_total=watched_total+1,movies_total=movies_total+?,series_total=series_total+?,updated_at=CURRENT_TIMESTAMP WHERE id=1''',(1 if media_type=='Movie' else 0,1 if media_type=='Series' else 0))
    c.commit(); c.close(); return new_id


def delete_movie(movie_id):
    c=get_connection(); c.execute('DELETE FROM watched WHERE id=?',(movie_id,)); c.commit(); c.close()


def add_recommendation_history(tmdb_id,media_type,title,job_id=None,source='unknown',match_score=None):
    if not tmdb_id:return
    c=get_connection(); c.execute('''INSERT INTO recommendation_history(tmdb_id,type,title,job_id,source,match_score) VALUES(?,?,?,?,?,?) ON CONFLICT(tmdb_id,type) DO UPDATE SET job_id=excluded.job_id,source=excluded.source,match_score=excluded.match_score,created_at=CURRENT_TIMESTAMP''',(tmdb_id,media_type,title,job_id,source,match_score)); c.commit(); c.close()


def get_recent_recommendation_ids(media_type,limit=50):
    c=get_connection(); rows=c.execute('SELECT tmdb_id FROM recommendation_history WHERE type=? ORDER BY created_at DESC LIMIT ?',(media_type,limit)).fetchall(); c.close(); return [r['tmdb_id'] for r in rows]


def add_recommendation_event(tmdb_id,media_type,title,event,job_id=None,metadata=None):
    if not tmdb_id:return
    c=get_connection(); c.execute('INSERT INTO recommendation_events(tmdb_id,type,title,event,job_id,metadata) VALUES(?,?,?,?,?,?)',(tmdb_id,media_type,title,event,job_id,_dump(metadata or {}))); c.commit(); c.close()


def get_recommendation_events(limit=500):
    c=get_connection(); rows=c.execute('SELECT * FROM recommendation_events ORDER BY created_at DESC LIMIT ?',(limit,)).fetchall(); c.close(); return rows


def add_not_interested(tmdb_id,media_type,title):
    if not tmdb_id:return
    c=get_connection(); c.execute('INSERT INTO not_interested(tmdb_id,type,title) VALUES(?,?,?) ON CONFLICT(tmdb_id,type) DO NOTHING',(tmdb_id,media_type,title)); c.commit(); c.close()
    add_recommendation_event(tmdb_id,media_type,title,'not_interested')


def get_not_interested_ids(media_type):
    c=get_connection(); rows=c.execute('SELECT tmdb_id FROM not_interested WHERE type=?',(media_type,)).fetchall(); c.close(); return [r['tmdb_id'] for r in rows]


def remove_not_interested(tmdb_id,media_type):
    c=get_connection(); c.execute('DELETE FROM not_interested WHERE tmdb_id=? AND type=?',(tmdb_id,media_type)); c.commit(); c.close()


def get_not_interested(media_type=None):
    c=get_connection();
    rows=c.execute('SELECT * FROM not_interested'+(' WHERE type=?' if media_type else '')+' ORDER BY created_at DESC',((media_type,) if media_type else ())).fetchall(); c.close(); return rows


def upsert_watchlist(item):
    c=get_connection(); c.execute('''INSERT INTO watchlist(tmdb_id,media_type,title,year,poster,backdrop,overview,vote_average,priority) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tmdb_id,media_type) DO UPDATE SET title=excluded.title,year=excluded.year,poster=excluded.poster,backdrop=excluded.backdrop,overview=excluded.overview,vote_average=excluded.vote_average,priority=excluded.priority,updated_at=CURRENT_TIMESTAMP''',(item['tmdb_id'],item['media_type'],item.get('title',''),item.get('year'),item.get('poster'),item.get('backdrop'),item.get('overview',''),item.get('vote_average',0),item.get('priority',0))); c.commit(); c.close()


def remove_watchlist(tmdb_id,media_type):
    c=get_connection(); c.execute('DELETE FROM watchlist WHERE tmdb_id=? AND media_type=?',(tmdb_id,media_type)); c.commit(); c.close()


def is_watchlisted(tmdb_id,media_type):
    c=get_connection(); r=c.execute('SELECT 1 FROM watchlist WHERE tmdb_id=? AND media_type=?',(tmdb_id,media_type)).fetchone(); c.close(); return bool(r)


def get_watchlist(media_type=None):
    c=get_connection();
    q='SELECT * FROM watchlist'; args=()
    if media_type: q+=' WHERE media_type=?'; args=(media_type,)
    q+=' ORDER BY priority DESC,added_at DESC'; rows=c.execute(q,args).fetchall(); c.close(); return rows


def get_watchlist_count():
    c=get_connection(); n=c.execute('SELECT COUNT(*) n FROM watchlist').fetchone()['n']; c.close(); return int(n)


def cache_get(key):
    c=get_connection(); r=c.execute('SELECT payload FROM tmdb_cache WHERE cache_key=? AND expires_at>CURRENT_TIMESTAMP',(key,)).fetchone(); c.close()
    if not r:return None
    import json
    try:return json.loads(r['payload'])
    except Exception:return None


def cache_set(key,payload,ttl=3600):
    import json
    c=get_connection(); c.execute("INSERT INTO tmdb_cache(cache_key,payload,expires_at) VALUES(?,?,datetime('now',?)) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at,created_at=CURRENT_TIMESTAMP",(key,json.dumps(payload,ensure_ascii=False),f'+{int(ttl)} seconds')); c.commit(); c.close()


def cache_clear(prefix=None):
    c=get_connection();
    if prefix:c.execute('DELETE FROM tmdb_cache WHERE cache_key LIKE ?',(prefix+'%',))
    else:c.execute('DELETE FROM tmdb_cache')
    c.commit(); c.close()


def set_setting(key,value):
    import json
    v=json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value
    c=get_connection(); c.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP',(key,v)); c.commit(); c.close()


def get_setting(key,default=None):
    import json
    c=get_connection(); r=c.execute('SELECT value FROM app_settings WHERE key=?',(key,)).fetchone(); c.close()
    if not r:return default
    try:return json.loads(r['value'])
    except Exception:return r['value']


def set_series_progress(tmdb_id,season=1,episode=1,progress=0,status='Watching'):
    c=get_connection(); c.execute('INSERT INTO series_progress(tmdb_id,season,episode,progress,status) VALUES(?,?,?,?,?) ON CONFLICT(tmdb_id) DO UPDATE SET season=excluded.season,episode=excluded.episode,progress=excluded.progress,status=excluded.status,updated_at=CURRENT_TIMESTAMP',(tmdb_id,season,episode,progress,status)); c.commit(); c.close()


def get_series_progress(tmdb_id=None):
    c=get_connection();
    if tmdb_id:r=c.execute('SELECT * FROM series_progress WHERE tmdb_id=?',(tmdb_id,)).fetchone(); c.close(); return r
    rows=c.execute('SELECT * FROM series_progress ORDER BY updated_at DESC').fetchall(); c.close(); return rows


def get_display_statistics():
    c=get_connection(); r=c.execute('SELECT * FROM display_statistics WHERE id=1').fetchone(); c.close()
    vals={k:max(0,int(r[k] or 0)) for k in ('ai_movies','ai_series','tmdb_movies','tmdb_series','trending_movies','trending_series')}
    vals.update(ai_generated=vals['ai_movies']+vals['ai_series'],recommendations=sum(vals.values()),updated_at=r['updated_at'] if r else None); return vals


def set_recommendation_statistics(ai_movies=0,ai_series=0,tmdb_movies=0,tmdb_series=0):
    c=get_connection(); c.execute('UPDATE display_statistics SET ai_movies=?,ai_series=?,tmdb_movies=?,tmdb_series=?,updated_at=CURRENT_TIMESTAMP WHERE id=1',(max(0,int(ai_movies)),max(0,int(ai_series)),max(0,int(tmdb_movies)),max(0,int(tmdb_series)))); c.commit(); c.close()


def set_trending_statistics(trending_movies=0,trending_series=0):
    c=get_connection(); c.execute('UPDATE display_statistics SET trending_movies=?,trending_series=?,updated_at=CURRENT_TIMESTAMP WHERE id=1',(max(0,int(trending_movies)),max(0,int(trending_series)))); c.commit(); c.close()


def set_display_statistics(ai_movies=0,ai_series=0,tmdb_movies=0,tmdb_series=0,trending_movies=0,trending_series=0):
    c=get_connection(); c.execute('UPDATE display_statistics SET ai_movies=?,ai_series=?,tmdb_movies=?,tmdb_series=?,trending_movies=?,trending_series=?,updated_at=CURRENT_TIMESTAMP WHERE id=1',(max(0,int(ai_movies)),max(0,int(ai_series)),max(0,int(tmdb_movies)),max(0,int(tmdb_series)),max(0,int(trending_movies)),max(0,int(trending_series)))); c.commit(); c.close()


def get_lifetime_statistics():
    c=get_connection(); r=c.execute('SELECT * FROM lifetime_statistics WHERE id=1').fetchone(); c.close()
    if not r:return {'watched':0,'movies':0,'series':0,'recommendations':0,'ai_generated':0,'updated_at':None}
    return {'watched':int(r['watched_total']),'movies':int(r['movies_total']),'series':int(r['series_total']),'recommendations':int(r['recommendations_total']),'ai_generated':int(r['ai_generated_total']),'updated_at':r['updated_at']}


def increment_lifetime_recommendations(ai_movies=0,ai_series=0,tmdb_movies=0,tmdb_series=0):
    total=int(ai_movies or 0)+int(ai_series or 0)+int(tmdb_movies or 0)+int(tmdb_series or 0); ai=int(ai_movies or 0)+int(ai_series or 0)
    c=get_connection(); c.execute('UPDATE lifetime_statistics SET recommendations_total=recommendations_total+?,ai_generated_total=ai_generated_total+?,updated_at=CURRENT_TIMESTAMP WHERE id=1',(total,ai)); c.commit(); c.close()


def increment_lifetime_trending(media_type,tmdb_ids):
    day=datetime.now(timezone.utc).date().isoformat(); c=get_connection();
    for tid in tmdb_ids or []:
        if tid:c.execute('INSERT OR IGNORE INTO lifetime_trending_seen(day,type,tmdb_id) VALUES(?,?,?)',(day,media_type,int(tid)))
    c.commit(); c.close()


def get_analytics():
    c=get_connection()
    total=c.execute('SELECT COUNT(*) n FROM watched').fetchone()['n']; avg=c.execute('SELECT AVG(rating) n FROM watched').fetchone()['n'] or 0
    genres={}
    for row in c.execute('SELECT genres,rating FROM watched').fetchall():
        for raw in _json(row['genres']):
            name = raw.get('name') if isinstance(raw, dict) else str(raw or '').strip()
            if not name:
                continue
            genres[name] = genres.get(name,[]) + [float(row['rating'])]
    genre_stats=sorted([{'genre':g,'count':len(v),'avg_rating':round(sum(v)/len(v),2)} for g,v in genres.items()],key=lambda x:(x['count'],x['avg_rating']),reverse=True)
    ratings=[dict(r) for r in c.execute('SELECT rating,COUNT(*) count FROM watched GROUP BY rating ORDER BY rating').fetchall()]
    years=[dict(r) for r in c.execute("SELECT CASE WHEN year IS NULL THEN 'Unknown' ELSE CAST((year/10)*10 AS TEXT) END decade,COUNT(*) count FROM watched GROUP BY decade ORDER BY decade").fetchall()]
    c.close(); return {'total':int(total),'average_rating':round(float(avg),2),'watchlist':get_watchlist_count(),'not_interested':len(get_not_interested_ids('Movie'))+len(get_not_interested_ids('Series')),'genres':genre_stats[:15],'ratings':ratings,'decades':years}

from __future__ import annotations
import asyncio, json, os, sqlite3, re
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA = Path(os.getenv('XROB_DATA_DIR', '/config'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'xrob_assistant.db'
WWW = Path(__file__).resolve().parent / 'www'
TOKEN = os.getenv('XROB_TELEGRAM_TOKEN', '').strip()
ALLOWED_CHAT = os.getenv('XROB_TELEGRAM_CHAT_ID', '').strip()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_date TEXT, due_time TEXT, priority TEXT DEFAULT 'normal', status TEXT DEFAULT 'open', category TEXT DEFAULT 'general', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS weights (id INTEGER PRIMARY KEY, date TEXT NOT NULL, kg REAL NOT NULL, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS workouts (id INTEGER PRIMARY KEY, date TEXT NOT NULL, name TEXT NOT NULL, duration_min INTEGER DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS workout_plan (id INTEGER PRIMARY KEY, weekday TEXT NOT NULL, name TEXT NOT NULL, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS exercises (id INTEGER PRIMARY KEY, workout_id INTEGER NOT NULL, name TEXT NOT NULL, sets INTEGER DEFAULT 0, reps TEXT DEFAULT '', weight_kg REAL DEFAULT 0, FOREIGN KEY(workout_id) REFERENCES workouts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS treadmill (id INTEGER PRIMARY KEY, date TEXT NOT NULL, duration_min REAL DEFAULT 0, distance_km REAL DEFAULT 0, speed_kmh REAL DEFAULT 0, incline REAL DEFAULT 0, calories_kcal REAL DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_mad REAL DEFAULT 0, due_date TEXT NOT NULL, recurrence TEXT DEFAULT 'none', reminder_days INTEGER DEFAULT 3, status TEXT DEFAULT 'unpaid', paid_at TEXT, notes TEXT DEFAULT '', recurrence_day INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, name TEXT NOT NULL, expiry_date TEXT NOT NULL, reminder_days INTEGER DEFAULT 30, reference TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routines (id INTEGER PRIMARY KEY, name TEXT NOT NULL, interval_days INTEGER DEFAULT 1, last_done TEXT, next_due TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', routine_time TEXT DEFAULT '10:00', routine_late_time TEXT DEFAULT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routine_logs (id INTEGER PRIMARY KEY, routine_id INTEGER NOT NULL, completed_at TEXT NOT NULL, FOREIGN KEY(routine_id) REFERENCES routines(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL, reminder_date TEXT NOT NULL, UNIQUE(kind,item_id,reminder_date));
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS weights_no_duplicate_date_insert BEFORE INSERT ON weights WHEN EXISTS (SELECT 1 FROM weights WHERE date=NEW.date) BEGIN SELECT RAISE(ABORT, 'weight date already exists'); END;
CREATE TRIGGER IF NOT EXISTS weights_no_duplicate_date_update BEFORE UPDATE OF date ON weights WHEN EXISTS (SELECT 1 FROM weights WHERE date=NEW.date AND id!=NEW.id) BEGIN SELECT RAISE(ABORT, 'weight date already exists'); END;
'''

@contextmanager
def connect():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=15000')
    try:
        yield c
    except Exception:
        c.rollback()
        raise
    else:
        c.commit()
    finally:
        c.close()

def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        # Add the optional task time column to databases created by earlier versions.
        task_columns = {r['name'] for r in c.execute('PRAGMA table_info(tasks)').fetchall()}
        if 'due_time' not in task_columns:
            c.execute('ALTER TABLE tasks ADD COLUMN due_time TEXT')
        bill_columns = {r['name'] for r in c.execute('PRAGMA table_info(bills)').fetchall()}
        if 'recurrence_day' not in bill_columns:
            c.execute('ALTER TABLE bills ADD COLUMN recurrence_day INTEGER')
        routine_columns = {r['name'] for r in c.execute('PRAGMA table_info(routines)').fetchall()}
        if 'routine_time' not in routine_columns:
            c.execute("ALTER TABLE routines ADD COLUMN routine_time TEXT DEFAULT '10:00'")
        if 'routine_late_time' not in routine_columns:
            c.execute("ALTER TABLE routines ADD COLUMN routine_late_time TEXT DEFAULT NULL")
        # In 0.1.11, 12:00 was automatically populated for every routine.
        # Convert those generated defaults to NULL so late reminders are opt-in.
        # Preserve any other time that the user may have deliberately selected.
        late_opt_in_migration = c.execute("SELECT 1 FROM app_migrations WHERE name='routine_late_reminder_optional_v1'").fetchone()
        if not late_opt_in_migration:
            c.execute("UPDATE routines SET routine_late_time=NULL WHERE routine_late_time='12:00'")
            c.execute("INSERT INTO app_migrations(name) VALUES('routine_late_reminder_optional_v1')")
        # Starter routines are inserted only once.
        if c.execute('SELECT COUNT(*) FROM routines').fetchone()[0] == 0:
            today = date.today().isoformat()
            for name, days, cat in [('Nail care',15,'personal care'),('Haircut',30,'personal care'),('Shower',1,'hygiene')]:
                c.execute('INSERT INTO routines(name,interval_days,last_done,next_due,category,routine_time,routine_late_time) VALUES(?,?,?,?,?,?,?)', (name,days,None,today,cat,'10:00',None))
        # Add a daily Weight check once during this upgrade so the scheduled/late
        # reminder flow works immediately; users can later edit or delete it.
        weight_seed = c.execute("SELECT 1 FROM app_migrations WHERE name='seed_weight_check_routine_v1'").fetchone()
        if not weight_seed:
            has_weight = c.execute("SELECT 1 FROM routines WHERE lower(name) LIKE '%weight%' OR lower(name) LIKE '%weigh%' LIMIT 1").fetchone()
            if not has_weight:
                c.execute("INSERT INTO routines(name,interval_days,last_done,next_due,category,routine_time,routine_late_time) VALUES(?,1,NULL,?,'health','10:00',NULL)", ('Weight check', date.today().isoformat()))
            c.execute("INSERT INTO app_migrations(name) VALUES('seed_weight_check_routine_v1')")
        # Add a daily Treadmill check only once, unless the user already has a
        # routine whose name identifies treadmill activity. Logging a treadmill
        # session will automatically check this routine for that session's date.
        treadmill_seed = c.execute("SELECT 1 FROM app_migrations WHERE name='seed_treadmill_check_routine_v1'").fetchone()
        if not treadmill_seed:
            has_treadmill = c.execute("SELECT 1 FROM routines WHERE lower(name) LIKE '%treadmill%' LIMIT 1").fetchone()
            if not has_treadmill:
                c.execute("INSERT INTO routines(name,interval_days,last_done,next_due,category,routine_time,routine_late_time) VALUES(?,1,NULL,?,'fitness','10:00',NULL)", ('Treadmill check', date.today().isoformat()))
            c.execute("INSERT INTO app_migrations(name) VALUES('seed_treadmill_check_routine_v1')")

def rows(sql, params=()):
    with connect() as c: return [dict(r) for r in c.execute(sql, params).fetchall()]
def one(sql, params=()):
    with connect() as c:
        r=c.execute(sql,params).fetchone(); return dict(r) if r else None

class Payload(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)

TABLES = {
 'events': ('events', ['title','start_at','end_at','category','notes']),
 'tasks': ('tasks', ['title','due_date','due_time','priority','status','category','notes']),
 'weights': ('weights', ['date','kg','notes']),
 'workouts': ('workouts', ['date','name','notes']),
 'workout_plan': ('workout_plan', ['weekday','name','notes']),
 'treadmill': ('treadmill', ['date','duration_min','distance_km','speed_kmh','incline','calories_kcal','notes']),
 'bills': ('bills', ['name','amount_mad','due_date','recurrence','reminder_days','status','paid_at','notes']),
 'documents': ('documents', ['name','expiry_date','reminder_days','reference','notes']),
 'routines': ('routines', ['name','interval_days','last_done','next_due','category','notes','routine_time','routine_late_time']),
}

@asynccontextmanager
async def lifespan(app):
    init_db()
    background = []
    if TOKEN and ALLOWED_CHAT:
        background = [asyncio.create_task(telegram_poll()), asyncio.create_task(reminder_loop())]
    elif TOKEN:
        print('Telegram bot token is set but allowed chat ID is empty; bot disabled for safety.', flush=True)
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)

app = FastAPI(title='XROB Assistant API', version='0.2.0', lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=WWW), name='static')

@app.get('/')
def home(): return FileResponse(WWW/'index.html')
@app.get('/api/health')
def health():
    try:
        with connect() as c: c.execute('SELECT 1').fetchone()
        return {'status':'ok','app':'XROB Assistant','version':'0.2.0','database':'connected'}
    except Exception as exc:
        raise HTTPException(503, f'Database check failed: {exc}')

@app.get('/api/{resource}')
def list_resource(resource: str, limit: int = Query(250, ge=1, le=10000)):
    if resource == 'summary': return summary()
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,_=TABLES[resource]
    return rows(f'SELECT * FROM {table} ORDER BY id DESC LIMIT ?', (limit,))

def validate_clock_time(value: Any, label: str) -> str | None:
    value = str(value or '').strip()
    if not value:
        return None
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise HTTPException(400, f'{label} must use 24-hour HH:MM time (00:00–23:59).')
    return value

def validate_task_schedule(data: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    existing = existing or {}
    if 'due_time' in data:
        value = str(data.get('due_time') or '').strip()
        if value:
            if not data.get('due_date', existing.get('due_date')):
                raise HTTPException(400, 'Choose a due date before setting a reminder time.')
            data['due_time'] = validate_clock_time(value, 'Task reminder time')
        else:
            data['due_time'] = None

def validate_routine_schedule(data: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    existing = existing or {}
    if 'routine_time' in data:
        data['routine_time'] = validate_clock_time(data.get('routine_time'), 'Routine time')
    if 'routine_late_time' in data:
        data['routine_late_time'] = validate_clock_time(data.get('routine_late_time'), 'Routine late reminder time')
    routine_time = data.get('routine_time', existing.get('routine_time') or '10:00')
    late_time = data.get('routine_late_time', existing.get('routine_late_time'))
    if routine_time and late_time and late_time <= routine_time:
        raise HTTPException(400, 'Late reminder time must be later than the routine time.')


def validate_date_field(value: Any, label: str) -> str | None:
    if value in (None, ''):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(400, f'{label} must be a valid YYYY-MM-DD date.')

def validate_event_schedule(data: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    existing = existing or {}
    start = data.get('start_at', existing.get('start_at'))
    end = data.get('end_at', existing.get('end_at'))
    if not start:
        raise HTTPException(400, 'Event start date/time is required.')
    try:
        parsed_start = datetime.fromisoformat(str(start))
    except (TypeError, ValueError):
        raise HTTPException(400, 'Event start must be a valid date/time.')
    if end:
        try:
            parsed_end = datetime.fromisoformat(str(end))
        except (TypeError, ValueError):
            raise HTTPException(400, 'Event end must be a valid date/time.')
        if parsed_end <= parsed_start:
            raise HTTPException(400, 'Event end must be after its start.')

def is_weight_routine(routine: dict[str, Any]) -> bool:
    name = str(routine.get('name') or '').lower()
    return 'weight' in name or 'weigh' in name

def is_treadmill_routine(routine: dict[str, Any]) -> bool:
    name = str(routine.get('name') or '').lower()
    return 'treadmill' in name

def auto_complete_matching_routines(completed_on: date, matcher) -> int:
    """Check matching routines when the corresponding activity is logged.

    A log is stored against the activity's date. Backdated entries are retained
    without moving a routine's current schedule backwards. Same-day duplicate
    activity logs never create duplicate routine completion records.
    """
    day = completed_on.isoformat()
    completed_count = 0
    for routine in rows('SELECT * FROM routines'):
        if not matcher(routine):
            continue
        interval = max(1, int(routine.get('interval_days') or 1))
        next_due = (completed_on + timedelta(days=interval)).isoformat()
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute(
                'INSERT INTO routine_logs(routine_id,completed_at) SELECT ?,? WHERE NOT EXISTS '
                '(SELECT 1 FROM routine_logs WHERE routine_id=? AND completed_at=?)',
                (routine['id'], day, routine['id'], day)
            )
            current = c.execute('SELECT last_done FROM routines WHERE id=?', (routine['id'],)).fetchone()
            current_last = current['last_done'] if current else None
            if not current_last or day >= current_last:
                c.execute('UPDATE routines SET last_done=?,next_due=? WHERE id=?', (day, next_due, routine['id']))
            completed_count += 1
    return completed_count

def auto_complete_weight_routines(completed_on: date) -> int:
    return auto_complete_matching_routines(completed_on, is_weight_routine)

def auto_complete_treadmill_routines(completed_on: date) -> int:
    return auto_complete_matching_routines(completed_on, is_treadmill_routine)

def build_routine_reminders(now: datetime) -> list[tuple[str, int, str, str]]:
    """Send the normal reminder on its due day; optional late reminders start next day.

    Overdue routines remain due until completed. If a late time is configured,
    a follow-up is sent at that time on each subsequent day, independent of interval_days.
    """
    today = now.date()
    current_hm = now.strftime('%H:%M')
    reminders: list[tuple[str, int, str, str]] = []
    for routine in rows('SELECT * FROM routines WHERE next_due IS NOT NULL'):
        try:
            due = date.fromisoformat(routine['next_due'])
            if due > today:
                continue
            # A weight log itself satisfies a Weight/Weigh-in routine.
            if is_weight_routine(routine) and one('SELECT id FROM weights WHERE date=? LIMIT 1', (today.isoformat(),)):
                auto_complete_weight_routines(today)
                continue
            routine_time = validate_clock_time(routine.get('routine_time'), 'Routine time')
            late_time = validate_clock_time(routine.get('routine_late_time'), 'Routine late reminder time')
            name = routine['name']
            if due == today:
                # The regular reminder is on the scheduled day. The late reminder
                # is deliberately deferred until the following day.
                if routine_time and current_hm >= routine_time:
                    reminders.append(('routine-time', routine['id'], today.isoformat(), f"🔁 ROUTINE DUE\n{name}\nScheduled: {routine_time}\nWhen finished, reply: done {name}"))
            elif late_time and current_hm >= late_time:
                scheduled = f'\nScheduled: {routine_time}' if routine_time else ''
                days_late = (today - due).days
                reminders.append(('routine-late', routine['id'], today.isoformat(), f"⚠️ ROUTINE STILL OPEN\n{name}\nOverdue: {days_late} day{'s' if days_late != 1 else ''}{scheduled}\nReply when finished: done {name}"))
        except Exception as exc:
            print(f'Routine reminder skipped for {routine.get("name", routine.get("id"))}: {exc}', flush=True)
    return reminders

@app.post('/api/{resource}')
def create_resource(resource: str, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table, allowed=TABLES[resource]
    data={k:v for k,v in payload.data.items() if k in allowed}
    if resource == 'tasks':
        data.setdefault('priority','normal')
        data.setdefault('status','open')
        data.setdefault('category','general')
        data['due_date'] = validate_date_field(data.get('due_date'), 'Task due date')
        validate_task_schedule(data)
    if resource == 'events':
        validate_event_schedule(data)
    if resource in ('weights','workouts','treadmill') and 'date' in data:
        data['date'] = validate_date_field(data.get('date'), 'Record date')
    if resource == 'weights':
        try:
            kg = float(data.get('kg'))
            if not 20 <= kg <= 350: raise ValueError()
            data['kg'] = round(kg, 2)
        except (TypeError, ValueError):
            raise HTTPException(400, 'Weight must be between 20 and 350 kg.')
    if resource == 'bills':
        data.setdefault('recurrence','none')
        data.setdefault('reminder_days',3)
        data['due_date'] = validate_date_field(data.get('due_date'), 'Bill due date')
        try:
            amount = float(data.get('amount_mad', 0))
            reminder_raw = float(data.get('reminder_days', 3))
            reminder_days = int(reminder_raw)
            if amount < 0 or reminder_days < 0 or reminder_raw != reminder_days: raise ValueError()
            data['amount_mad'] = amount
            data['reminder_days'] = reminder_days
        except (TypeError, ValueError):
            raise HTTPException(400, 'Bill amount and reminder days must be non-negative numbers.')
    if resource == 'documents':
        data['expiry_date'] = validate_date_field(data.get('expiry_date'), 'Document expiry date')
        if not data.get('expiry_date'): raise HTTPException(400, 'Document expiry date is required.')
    if resource == 'routines':
        data.setdefault('routine_time', '10:00')
        data.setdefault('routine_late_time', None)
        data.setdefault('next_due', date.today().isoformat())
        data['next_due'] = validate_date_field(data.get('next_due'), 'Routine next due date')
        if data.get('last_done'): data['last_done'] = validate_date_field(data.get('last_done'), 'Routine last completed date')
        validate_routine_schedule(data)
    treadmill_day = None
    if resource == 'treadmill':
        try:
            treadmill_day = date.fromisoformat(str(data.get('date', '')))
            duration = float(data.get('duration_min', 0))
            if not 1 <= duration <= 1440: raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, 'Enter a valid treadmill date and duration (1–1440 minutes).')
        latest = one('SELECT kg FROM weights ORDER BY date DESC,id DESC LIMIT 1')
        body_weight = float(latest['kg']) if latest else 70.0
        data.update({'date': treadmill_day.isoformat(), 'speed_kmh': 5.0, 'incline': 0.0,
                     'distance_km': round(5.0 * duration / 60.0, 2),
                     'calories_kcal': round(3.5 * 3.5 * body_weight / 200.0 * duration)})
    if not data: raise HTTPException(400,'No valid fields supplied')
    if resource in ('events','tasks','workouts','bills','documents','routines','workout_plan') and not data.get('title' if resource in ('events','tasks') else 'name'):
        raise HTTPException(400,'A title/name is required')
    if resource == 'workouts' and not data.get('date'):
        raise HTTPException(400, 'Workout date is required.')
    if resource == 'workout_plan' and data.get('weekday') not in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'):
        raise HTTPException(400, 'Choose a valid weekday.')
    if resource == 'tasks' and data.get('priority') not in ('low','normal','high'):
        raise HTTPException(400, 'Choose a valid task priority.')
    if resource == 'tasks' and data.get('status') not in ('open','done'):
        raise HTTPException(400, 'Task status must be open or done.')
    if resource == 'bills' and not data.get('due_date'):
        raise HTTPException(400, 'Bill due date is required.')
    if resource == 'bills' and data.get('recurrence') not in ('none','monthly','quarterly','yearly'):
        raise HTTPException(400, 'Choose a valid bill recurrence.')
    if resource == 'routines':
        try:
            interval_raw = float(data.get('interval_days', 1))
            interval = int(interval_raw)
            if interval < 1 or interval_raw != interval: raise ValueError()
            data['interval_days'] = interval
        except (TypeError, ValueError):
            raise HTTPException(400, 'Routine interval must be at least one day.')
    keys=list(data); marks=','.join('?' for _ in keys)
    with connect() as c:
        if resource == 'weights':
            try:
                weight_day = date.fromisoformat(str(data.get('date', '')))
            except (TypeError, ValueError):
                raise HTTPException(400, 'Enter a valid weight date (YYYY-MM-DD).')
            # Acquire the SQLite write lock before checking so two near-simultaneous
            # submissions cannot create two records for the same calendar day.
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT kg FROM weights WHERE date=? ORDER BY id DESC LIMIT 1', (weight_day.isoformat(),)).fetchone()
            if existing:
                pretty = weight_day.strftime('%d/%m/%Y')
                raise HTTPException(409, f'Weight already recorded for {pretty} ({float(existing["kg"]):.2f} kg). No new entry was added.')
        cur=c.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({marks})", [data[k] for k in keys])
        new_id=cur.lastrowid
    if resource == 'weights':
        auto_complete_weight_routines(weight_day)
    elif resource == 'treadmill':
        auto_complete_treadmill_routines(treadmill_day)
    return one(f'SELECT * FROM {table} WHERE id=?',(new_id,))

@app.patch('/api/{resource}/{item_id}')
def update_resource(resource: str, item_id: int, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,allowed=TABLES[resource]; data={k:v for k,v in payload.data.items() if k in allowed}
    if resource in ('weights','workouts','treadmill') and 'date' in data:
        data['date'] = validate_date_field(data.get('date'), 'Record date')
    if resource == 'weights':
        if 'kg' in data:
            try:
                kg=float(data['kg'])
                if not 20 <= kg <= 350: raise ValueError()
                data['kg']=round(kg,2)
            except (TypeError,ValueError): raise HTTPException(400, 'Weight must be between 20 and 350 kg.')
        if 'date' in data:
            duplicate=one('SELECT id FROM weights WHERE date=? AND id!=?',(data['date'],item_id))
            if duplicate: raise HTTPException(409, 'A weight record already exists for this date.')
    if resource == 'bills':
        if 'due_date' in data: data['due_date'] = validate_date_field(data.get('due_date'), 'Bill due date')
        if 'recurrence' in data and data['recurrence'] not in ('none','monthly','quarterly','yearly'):
            raise HTTPException(400, 'Choose a valid bill recurrence.')
        if 'amount_mad' in data:
            try:
                amount = float(data['amount_mad'])
                if amount < 0: raise ValueError()
                data['amount_mad'] = amount
            except (TypeError, ValueError): raise HTTPException(400, 'Bill amount must be non-negative.')
    if resource == 'events':
        existing_event = one('SELECT * FROM events WHERE id=?',(item_id,))
        if not existing_event:
            raise HTTPException(404, 'Event not found')
        validate_event_schedule(data, existing_event)
    if resource == 'tasks':
        if 'priority' in data and data['priority'] not in ('low','normal','high'):
            raise HTTPException(400, 'Choose a valid task priority.')
        if 'status' in data and data['status'] not in ('open','done'):
            raise HTTPException(400, 'Task status must be open or done.')
        if 'due_date' in data:
            data['due_date'] = validate_date_field(data.get('due_date'), 'Task due date')
        existing_task = one('SELECT * FROM tasks WHERE id=?',(item_id,))
        if not existing_task:
            raise HTTPException(404, 'Task not found')
        if 'due_date' in data and not data.get('due_date') and existing_task.get('due_time') and 'due_time' not in data:
            data['due_time'] = None
        validate_task_schedule(data, existing_task)
    if resource == 'documents':
        if 'expiry_date' in data: data['expiry_date']=validate_date_field(data.get('expiry_date'),'Document expiry date')
    if resource == 'routines':
        if 'next_due' in data: data['next_due']=validate_date_field(data.get('next_due'),'Routine next due date')
        if 'last_done' in data and data.get('last_done'): data['last_done']=validate_date_field(data.get('last_done'),'Routine last completed date')
        existing_routine = one('SELECT * FROM routines WHERE id=?',(item_id,))
        if not existing_routine:
            raise HTTPException(404, 'Routine not found')
        validate_routine_schedule(data, existing_routine)
    if not data: raise HTTPException(400,'No valid fields supplied')
    if resource == 'treadmill' and ('duration_min' in data or 'date' in data):
        existing_tread = one('SELECT * FROM treadmill WHERE id=?', (item_id,))
        if not existing_tread: raise HTTPException(404, 'Treadmill session not found')
        try:
            duration = float(data.get('duration_min', existing_tread['duration_min']))
            if not 1 <= duration <= 1440: raise ValueError()
        except (TypeError,ValueError): raise HTTPException(400, 'Duration must be between 1 and 1440 minutes.')
        latest = one('SELECT kg FROM weights ORDER BY date DESC,id DESC LIMIT 1')
        body_weight = float(latest['kg']) if latest else 70.0
        data.update({'duration_min':duration,'speed_kmh':5.0,'incline':0.0,'distance_km':round(5.0*duration/60.0,2),'calories_kcal':round(3.5*3.5*body_weight/200.0*duration)})
    if resource == 'routines' and 'interval_days' in data:
        try:
            interval_raw=float(data['interval_days'])
            interval=int(interval_raw)
            if interval_raw != interval: raise ValueError()
        except (TypeError,ValueError):
            raise HTTPException(400,'Repeat interval must be a positive number of days.')
        existing=one('SELECT last_done FROM routines WHERE id=?',(item_id,))
        reference=date.fromisoformat(existing['last_done']) if existing and existing.get('last_done') else date.today()
        data['interval_days']=interval
        data['next_due']=(reference+timedelta(days=interval)).isoformat()
    with connect() as c:
        cur=c.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in data)} WHERE id=?", [*data.values(),item_id])
        if cur.rowcount == 0: raise HTTPException(404,'Record not found')
    return one(f'SELECT * FROM {table} WHERE id=?',(item_id,))

@app.delete('/api/{resource}/{item_id}')
def delete_resource(resource: str, item_id: int):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,_=TABLES[resource]
    with connect() as c:
        cur=c.execute(f'DELETE FROM {table} WHERE id=?',(item_id,))
        if cur.rowcount == 0: raise HTTPException(404,'Record not found')
        reminder_kinds={'tasks':['task','task-time'],'bills':['bill'],'routines':['routine-time','routine-late']}
        for kind in reminder_kinds.get(resource,[]):
            c.execute('DELETE FROM notification_log WHERE kind=? AND item_id=?',(kind,item_id))
    return {'deleted':True,'id':item_id}

# Legacy exercises table is retained to protect existing records; no UI currently uses it.

@app.post('/api/routines/{routine_id}/complete')
def complete_routine(routine_id:int):
    r=one('SELECT * FROM routines WHERE id=?',(routine_id,))
    if not r: raise HTTPException(404,'Routine not found')
    today=date.today(); nxt=(today+timedelta(days=max(1,int(r['interval_days'])))).isoformat()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO routine_logs(routine_id,completed_at) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM routine_logs WHERE routine_id=? AND completed_at=?)',(routine_id,today.isoformat(),routine_id,today.isoformat()))
        latest = c.execute('SELECT MAX(completed_at) FROM routine_logs WHERE routine_id=?',(routine_id,)).fetchone()[0] or today.isoformat()
        latest_date = date.fromisoformat(latest)
        next_due = (latest_date+timedelta(days=max(1,int(r['interval_days'])))).isoformat()
        c.execute('UPDATE routines SET last_done=?,next_due=? WHERE id=?',(latest,next_due,routine_id))
    return one('SELECT * FROM routines WHERE id=?',(routine_id,))

@app.patch('/api/routines/{routine_id}/last-record')
def edit_routine_last_record(routine_id: int, payload: Payload):
    r=one('SELECT * FROM routines WHERE id=?',(routine_id,))
    if not r: raise HTTPException(404,'Routine not found')
    raw=payload.data.get('completed_at')
    try:
        completed=date.fromisoformat(str(raw)).isoformat()
    except (TypeError,ValueError):
        raise HTTPException(400,'Enter a valid completion date (YYYY-MM-DD).')
    interval=max(1,int(r['interval_days'] or 1))
    with connect() as c:
        latest=c.execute('SELECT id FROM routine_logs WHERE routine_id=? ORDER BY completed_at DESC,id DESC LIMIT 1',(routine_id,)).fetchone()
        if latest:
            c.execute('UPDATE routine_logs SET completed_at=? WHERE id=?',(completed,latest['id']))
        else:
            c.execute('INSERT INTO routine_logs(routine_id,completed_at) VALUES(?,?)',(routine_id,completed))
        latest_after=c.execute('SELECT MAX(completed_at) AS completed_at FROM routine_logs WHERE routine_id=?',(routine_id,)).fetchone()['completed_at']
        latest_after=latest_after or completed
        next_due=(date.fromisoformat(latest_after)+timedelta(days=interval)).isoformat()
        c.execute('UPDATE routines SET last_done=?,next_due=? WHERE id=?',(latest_after,next_due,routine_id))
    return one('SELECT * FROM routines WHERE id=?',(routine_id,))


def next_recurrence_date(due: date, recurrence: str, anchor_day: int | None = None) -> date | None:
    anchor = max(1, min(31, int(anchor_day or due.day)))
    if recurrence == 'monthly':
        year = due.year + (1 if due.month == 12 else 0)
        month = 1 if due.month == 12 else due.month + 1
        # Clamp to the last day of the target month (Jan 31 -> Feb 28/29).
        import calendar
        return date(year, month, min(anchor, calendar.monthrange(year, month)[1]))
    if recurrence == 'quarterly':
        month_index = due.month - 1 + 3
        year = due.year + month_index // 12
        month = month_index % 12 + 1
        import calendar
        return date(year, month, min(anchor, calendar.monthrange(year, month)[1]))
    if recurrence == 'yearly':
        year = due.year + 1
        import calendar
        return date(year, due.month, min(anchor, calendar.monthrange(year, due.month)[1]))
    return None


def mark_bill_paid(bill_id: int) -> dict:
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bill_row = c.execute('SELECT * FROM bills WHERE id=?', (bill_id,)).fetchone()
        if not bill_row:
            raise HTTPException(404, 'Bill not found')
        bill = dict(bill_row)
        if bill.get('status') == 'paid':
            return bill
        now = datetime.now().isoformat(timespec='minutes')
        due_date = date.fromisoformat(bill['due_date'])
        anchor_day = int(bill.get('recurrence_day') or due_date.day)
        recurrence = bill.get('recurrence') or 'none'
        next_due = next_recurrence_date(due_date, recurrence, anchor_day)
        # If payment is late, roll to the first cycle strictly after today.
        while next_due and next_due <= date.today():
            next_due = next_recurrence_date(next_due, recurrence, anchor_day)
        c.execute("UPDATE bills SET status='paid',paid_at=? WHERE id=? AND status!='paid'", (now, bill_id))
        if next_due:
            c.execute('INSERT INTO bills(name,amount_mad,due_date,recurrence,reminder_days,status,notes,recurrence_day) VALUES(?,?,?,?,?,?,?,?)',
                      (bill['name'], bill['amount_mad'], next_due.isoformat(), recurrence, bill['reminder_days'], 'unpaid', bill['notes'], anchor_day))
    return one('SELECT * FROM bills WHERE id=?', (bill_id,))


@app.post('/api/bills/{bill_id}/pay')
def pay_bill(bill_id:int):
    return mark_bill_paid(bill_id)

def count_expiring_documents(today: date | None = None) -> int:
    today = today or date.today()
    count = 0
    for document in rows('SELECT expiry_date,reminder_days FROM documents'):
        try:
            expiry = date.fromisoformat(document['expiry_date'])
            lead = max(0, int(document.get('reminder_days') if document.get('reminder_days') is not None else 30))
            if expiry <= today + timedelta(days=lead): count += 1
        except (TypeError, ValueError):
            continue
    return count

@app.get('/api/summary')
def summary():
    today=date.today().isoformat()
    next_bill = one("SELECT name,due_date,amount_mad FROM bills WHERE status!='paid' ORDER BY CASE WHEN due_date>=? THEN 0 ELSE 1 END, CASE WHEN due_date>=? THEN due_date END ASC, CASE WHEN due_date<? THEN due_date END DESC, id ASC LIMIT 1", (today, today, today))
    days_to_next_bill = (date.fromisoformat(next_bill['due_date']) - date.today()).days if next_bill else None
    return {
      'events_today':len(rows('SELECT id FROM events WHERE substr(start_at,1,10)=?',(today,))),
      'routines_due_today':one('SELECT COUNT(*) n FROM routines WHERE next_due<=?',(today,))['n'],
      'open_tasks':one("SELECT COUNT(*) n FROM tasks WHERE status!='done'")['n'],
      'latest_weight':one('SELECT kg,date FROM weights ORDER BY date DESC,id DESC LIMIT 1'),
      'unpaid_bills':one("SELECT COUNT(*) n FROM bills WHERE status!='paid'")['n'],
      'unpaid_total_mad':one("SELECT COALESCE(SUM(amount_mad),0) n FROM bills WHERE status!='paid'")['n'],
      'upcoming_bill':({**next_bill, 'days_left': days_to_next_bill} if next_bill else None),
      'expiring_documents':count_expiring_documents(),
      'next_routine':one('SELECT * FROM routines ORDER BY next_due LIMIT 1'),
      'recent_treadmill':one('SELECT * FROM treadmill ORDER BY date DESC,id DESC LIMIT 1')
    }

@app.get('/api/ha/state')
def ha_state():
    s=summary()
    return {'weight_kg':s['latest_weight']['kg'] if s['latest_weight'] else None,'unpaid_bills':s['unpaid_bills'],'unpaid_total_mad':s['unpaid_total_mad'],'expiring_documents':s['expiring_documents'],'open_tasks':s['open_tasks'],'events_today':s['events_today'],'routines_due_today':s['routines_due_today'],'next_routine':s['next_routine']['name'] if s['next_routine'] else None}

async def reminder_loop():
    """Send due reminders once at their scheduled task time or once daily for date-only items."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                now = datetime.now()
                today = now.date()
                reminders = []
                for b in rows("SELECT * FROM bills WHERE status!='paid'"):
                    try: remind_on = date.fromisoformat(b['due_date']) - timedelta(days=int(b.get('reminder_days') or 3))
                    except Exception: continue
                    if remind_on <= today:
                        reminders.append(('bill', b['id'], today.isoformat(), f"💳 BILL REMINDER\n{b['name']} · {b['amount_mad']:.2f} MAD\nDue: {date.fromisoformat(b['due_date']).strftime('%d/%m/%y')}\nAfter paying, reply: paid {b['name']}"))
                for t in rows("SELECT * FROM tasks WHERE status!='done' AND due_date IS NOT NULL"):
                    try:
                        due_day = date.fromisoformat(t['due_date'])
                        if t.get('due_time'):
                            due_at = datetime.combine(due_day, datetime.strptime(t['due_time'], '%H:%M').time())
                            if due_at <= now:
                                reminder_key = due_at.strftime('%Y-%m-%dT%H:%M')
                                reminders.append(('task-time', t['id'], reminder_key, f"⏰ TASK REMINDER\n{t['title']}\n{due_day.strftime('%d/%m/%y')} · {t['due_time']}\nWhen finished, reply: task done {t['title']}"))
                        elif due_day <= today:
                            reminders.append(('task', t['id'], today.isoformat(), f"📌 TASK OVERDUE\n{t['title']}\nDue: {due_day.strftime('%d/%m/%y')}\nWhen finished, reply: task done {t['title']}"))
                    except Exception:
                        continue
                reminders.extend(build_routine_reminders(now))
                for kind, item_id, reminder_date, message in reminders:
                    already_sent = one('SELECT id FROM notification_log WHERE kind=? AND item_id=? AND reminder_date=?', (kind, item_id, reminder_date))
                    if already_sent:
                        continue
                    sent = await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': ALLOWED_CHAT, 'text': message, 'disable_web_page_preview': True})
                    sent.raise_for_status()
                    with connect() as c:
                        c.execute('INSERT OR IGNORE INTO notification_log(kind,item_id,reminder_date) VALUES(?,?,?)', (kind, item_id, reminder_date))
            except Exception as e:
                print(f'Reminder scheduler error: {e}', flush=True)
            await asyncio.sleep(15)

async def telegram_poll():
    """Telegram bot with a compact menu, command shortcuts and persistent update offset."""
    import httpx
    keyboard = {
        'keyboard': [
            [{'text': '📅 Today'}, {'text': '✅ Tasks'}],
            [{'text': '🔁 Routines'}, {'text': '💳 Bills'}],
            [{'text': '🏃 Fitness'}, {'text': '🗓 Week'}],
            [{'text': '⚖️ Add weight'}, {'text': '🏃 Add treadmill'}],
            [{'text': '❔ Help'}]
        ],
        'resize_keyboard': True,
        'is_persistent': True,
        'input_field_placeholder': 'Try: task Workout tomorrow at 18:00'
    }
    commands = [
        {'command': 'today', 'description': 'Today’s plan'},
        {'command': 'tasks', 'description': 'Open tasks'},
        {'command': 'routines', 'description': 'Due and upcoming routines'},
        {'command': 'bills', 'description': 'Unpaid bills and due dates'},
        {'command': 'fitness', 'description': 'Weight and activity summary'},
        {'command': 'week', 'description': 'Next 7 days'},
        {'command': 'done', 'description': 'Complete a routine'},
        {'command': 'taskdone', 'description': 'Complete a task'},
        {'command': 'weight', 'description': 'Log weight (guided)'},
        {'command': 'treadmill', 'description': 'Log treadmill session (guided)'},
        {'command': 'paid', 'description': 'Mark a bill paid'},
        {'command': 'cancel', 'description': 'Cancel a guided entry'},
        {'command': 'help', 'description': 'Show examples'}
    ]
    saved_offset = one("SELECT value FROM telegram_state WHERE key='update_offset'")
    try:
        offset = int(saved_offset['value']) if saved_offset else 0
    except (TypeError, ValueError):
        offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        try:
            await client.post(f'https://api.telegram.org/bot{TOKEN}/setMyCommands', json={'commands': commands})
        except Exception as exc:
            print(f'Telegram command-menu setup warning: {exc}', flush=True)
        while True:
            try:
                response = await client.get(
                    f'https://api.telegram.org/bot{TOKEN}/getUpdates',
                    params={'timeout': 30, 'offset': offset, 'allowed_updates': json.dumps(['message'])}
                )
                response.raise_for_status()
                data = response.json()
                if not data.get('ok'):
                    raise RuntimeError(data.get('description', 'Telegram getUpdates failed'))
                for update in data.get('result', []):
                    offset = int(update['update_id']) + 1
                    message = update.get('message', {})
                    chat = str(message.get('chat', {}).get('id', ''))
                    message_text = (message.get('text') or '').strip()
                    if not message_text or chat != ALLOWED_CHAT:
                        with connect() as c:
                            c.execute("INSERT INTO telegram_state(key,value) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                        continue
                    try:
                        reply = await telegram_command(message_text)
                    except Exception as exc:
                        print(f'Telegram command failed for update {update["update_id"]}: {exc}', flush=True)
                        reply = 'I could not complete that. Check the details and try again, or send /help.'
                    # Persist after applying the command but before replying. If Telegram's
                    # sendMessage endpoint is temporarily unavailable, an action is not replayed.
                    with connect() as c:
                        c.execute("INSERT INTO telegram_state(key,value) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                    sent = await client.post(
                        f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                        json={'chat_id': chat, 'text': reply, 'reply_markup': keyboard, 'disable_web_page_preview': True}
                    )
                    sent.raise_for_status()
            except Exception as exc:
                print(f'Telegram polling error: {exc}', flush=True)
                await asyncio.sleep(5)

def _telegram_parse_date(value: str | None) -> date | None:
    if not value:
        return None
    raw = value.strip().lower()
    today = date.today()
    if raw in ('today', 'today\'s'):
        return today
    if raw == 'yesterday':
        return today - timedelta(days=1)
    if raw == 'tomorrow':
        return today + timedelta(days=1)
    if raw in ('day after tomorrow', 'the day after tomorrow'):
        return today + timedelta(days=2)
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%d-%m-%y', '%Y-%m-%d'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError('Date not understood. Use tomorrow or DD/MM/YY.')

def _telegram_task_payload(text: str) -> tuple[str, str | None, str | None]:
    body = text.strip()
    body = re.sub(r'^(?:/task(?:@[a-z0-9_]+)?|task|add task|new task|remind me to|please remind me to|remind me|i want to|schedule)\s*', '', body, flags=re.I)
    body = body.replace('|', ' ')
    if re.search(r'\b(?:AM|PM)\b', body, flags=re.I):
        raise ValueError('Use 24-hour time, for example 18:00.')
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', body)
    due_time = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    if time_match:
        body = body[:time_match.start()] + ' ' + body[time_match.end():]
    date_match = re.search(r'(?i)\b(day after tomorrow|tomorrow|today|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b', body)
    due_date = None
    if date_match:
        due_date = _telegram_parse_date(date_match.group(1)).isoformat()
        body = body[:date_match.start()] + ' ' + body[date_match.end():]
    elif due_time:
        parsed_time = datetime.strptime(due_time, '%H:%M').time()
        due_day = date.today()
        if parsed_time <= datetime.now().time():
            due_day += timedelta(days=1)
        due_date = due_day.isoformat()
    # Remove only scheduling connectors, not words inside the task title (e.g. "go to gym").
    body = re.sub(r'(?i)^\s*(?:on|at|for|please)\b', ' ', body)
    body = re.sub(r'(?i)\b(?:on|at|for)\s*$', ' ', body)
    title = re.sub(r'\s+', ' ', body).strip(' .,:;-')
    if not title:
        raise ValueError('Add a task name. Example: task Workout tomorrow at 18:00.')
    return title, due_date, due_time

def _telegram_name_key(value: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower())).strip()


def _telegram_date_label(value: str | None, include_weekday: bool = False) -> str:
    if not value:
        return 'No date'
    parsed = date.fromisoformat(value)
    return parsed.strftime('%a %d/%m/%y' if include_weekday else '%d/%m/%y')


def _telegram_match_named_item(query: str, items: list[dict], field: str = 'name') -> tuple[dict | None, list[dict]]:
    wanted = _telegram_name_key(query)
    exact = [item for item in items if _telegram_name_key(item.get(field, '')) == wanted]
    matches = exact or [item for item in items if wanted and wanted in _telegram_name_key(item.get(field, ''))]
    return (matches[0] if len(matches) == 1 else None), matches



def _telegram_get_state() -> str | None:
    state = one("SELECT value FROM telegram_state WHERE key='conversation_state'")
    return state['value'] if state else None


def _telegram_set_state(value: str | None) -> None:
    with connect() as c:
        if value is None:
            c.execute("DELETE FROM telegram_state WHERE key='conversation_state'")
        else:
            c.execute("INSERT INTO telegram_state(key,value) VALUES('conversation_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))


def _telegram_save_weight(kg: float, day: date) -> str:
    if not 20 <= kg <= 350:
        raise ValueError('Weight must be between 20 and 350 kg.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT kg FROM weights WHERE date=? ORDER BY id DESC LIMIT 1', (day.isoformat(),)).fetchone()
        if existing:
            return f"Already saved for {day.strftime('%d/%m/%y')}: {float(existing['kg']):.2f} kg. No duplicate was added."
        c.execute('INSERT INTO weights(date,kg) VALUES(?,?)', (day.isoformat(), kg))
    auto_complete_weight_routines(day)
    return f'✅ Weight saved · {kg:.2f} kg · {day.strftime("%d/%m/%y")}.'


def _telegram_save_treadmill(duration: float, day: date) -> str:
    if not 1 <= duration <= 1440:
        raise ValueError('Duration must be between 1 and 1440 minutes.')
    latest = one('SELECT kg FROM weights ORDER BY date DESC,id DESC LIMIT 1')
    body_weight = float(latest['kg']) if latest else 70.0
    distance = round(5.0 * duration / 60.0, 2)
    calories = round(3.5 * 3.5 * body_weight / 200.0 * duration)
    with connect() as c:
        c.execute('INSERT INTO treadmill(date,duration_min,distance_km,speed_kmh,incline,calories_kcal) VALUES(?,?,?,?,?,?)', (day.isoformat(), duration, distance, 5.0, 0.0, calories))
    auto_complete_treadmill_routines(day)
    estimate_note = '' if latest else '\nCalories estimated using default 70 kg (no weight record).'
    return f'✅ Treadmill saved · {duration:g} min\n{day.strftime("%d/%m/%y")} · 5 km/h · 0% incline\n{distance:.2f} km · ~{calories} kcal{estimate_note}'


def _telegram_parse_weight(text: str) -> tuple[float, date]:
    raw = re.sub(r'(?i)^(?:/weight(?:@[a-z0-9_]+)?|weight|log weight)\s*', '', text.strip())
    raw = raw.lower().replace('kg', '').strip()
    date_match = re.search(r'(?i)(today|tomorrow|yesterday|day after tomorrow|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})', raw)
    day = _telegram_parse_date(date_match.group(1)) if date_match else date.today()
    if date_match:
        raw = raw[:date_match.start()] + ' ' + raw[date_match.end():]
    raw = raw.strip().replace(',', '.')
    kg = float(raw)
    if not 20 <= kg <= 350:
        raise ValueError()
    return kg, day


def _telegram_parse_treadmill(text: str) -> tuple[float, date]:
    raw = re.sub(r'(?i)^(?:/treadmill(?:@[a-z0-9_]+)?|treadmill|run|walk)\s*', '', text.strip()).lower().replace('minutes', 'min').replace('minute', 'min')
    date_match = re.search(r'(?i)(today|tomorrow|yesterday|day after tomorrow|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})', raw)
    day = _telegram_parse_date(date_match.group(1)) if date_match else date.today()
    if date_match:
        raw = raw[:date_match.start()] + ' ' + raw[date_match.end():]
    raw = re.sub(r'(?i)\b(?:treadmill|run|walk|session|log|add|duration|min)\b', ' ', raw)
    duration = float(raw.strip().replace(',', '.'))
    if not 1 <= duration <= 1440:
        raise ValueError()
    return duration, day

async def telegram_command(text: str) -> str:
    """Short, action-oriented Telegram interface for the XROB personal manager."""
    raw = text.strip()
    low = raw.lower().strip()
    today = date.today()
    today_iso = today.isoformat()

    # Guided input flows. They survive restarts and can be cancelled with /cancel.
    state = _telegram_get_state()
    if low in ('/cancel', 'cancel', 'stop'):
        _telegram_set_state(None)
        return 'Cancelled. Nothing was saved.'
    if state and (low.startswith('/') or low in ('📅 today','today','✅ tasks','tasks','💳 bills','bills','🔁 routines','routines','🏃 fitness','fitness','🗓 week','week','❔ help','help') or low.startswith(('weight ','log weight ','treadmill ','run ','walk ','task ','paid ','done '))):
        _telegram_set_state(None)
        state = None
    if low in ('⚖️ add weight', 'add weight', '/weight', 'weight'):
        _telegram_set_state('weight_value')
        return '⚖️ Weight entry\nSend your weight in kg, for example 82.4. You may add a date: 82.4 tomorrow.\n/cancel to stop.'
    if low in ('🏃 add treadmill', 'add treadmill', '/treadmill', 'treadmill'):
        _telegram_set_state('treadmill_duration')
        return '🏃 Treadmill entry\nHow many minutes? Example: 30. You may add a date: 30 tomorrow.\n/cancel to stop.'
    if state == 'weight_value' and not low.startswith('/'):
        try:
            kg, day = _telegram_parse_weight(raw)
            _telegram_set_state(None)
            return _telegram_save_weight(kg, day)
        except Exception:
            return 'I could not read that weight. Send a number from 20 to 350, e.g. 82.4 or 82.4 tomorrow. /cancel to stop.'
    if state == 'treadmill_duration' and not low.startswith('/'):
        try:
            duration, day = _telegram_parse_treadmill(raw)
            _telegram_set_state(None)
            return _telegram_save_treadmill(duration, day)
        except Exception:
            return 'I could not read that duration. Send minutes, e.g. 30 or 30 tomorrow. /cancel to stop.'

    if low in ('/start', 'start', '/menu', 'menu', 'hi', 'hello', '/help', 'help', '❔ help'):
        return (
            '👋 XROB Assistant\n'
            'Your daily plan, tasks, routines, bills and fitness.\n\n'
            'Use the buttons below, or send:\n'
            '• task Workout tomorrow at 18:00\n'
            '• done Shower\n'
            '• weight 89.5\n'
            '• treadmill 30\n'
            '• task done Buy groceries\n'
            '• paid Electricity'
        )

    if low in ('📅 today', '/today', 'today', 'what is today', "what's today", 'what do i have today?', 'agenda today'):
        events = rows('SELECT title,start_at FROM events WHERE substr(start_at,1,10)=? ORDER BY start_at', (today_iso,))
        tasks = rows("SELECT title,due_time FROM tasks WHERE status!='done' AND due_date=? ORDER BY CASE WHEN due_time IS NULL THEN 1 ELSE 0 END,due_time,title", (today_iso,))
        treadmill = one('SELECT COALESCE(SUM(duration_min),0) AS mins FROM treadmill WHERE date=?', (today_iso,))['mins']
        gym = rows('SELECT name FROM workouts WHERE date=? ORDER BY id', (today_iso,))
        bills_today = rows("SELECT name,amount_mad FROM bills WHERE status!='paid' AND due_date=? ORDER BY name", (today_iso,))
        routines = rows('SELECT name,next_due,routine_time FROM routines WHERE next_due<=? ORDER BY next_due,COALESCE(routine_time,\'23:59\'),name', (today_iso,))
        lines = [f'📅 TODAY · {today.strftime("%a %d/%m/%y")}', f'{len(tasks)} tasks · {len(routines)} routines · {len(events)} events']
        for event in events:
            lines.append(f"{event['start_at'][11:16]} · {event['title']}")
        for task in tasks:
            lines.append(f"{task.get('due_time') or 'Any time'} · {task['title']}")
        if routines:
            lines.append('')
            lines.append('ROUTINES')
            lines.extend((f"{routine.get('routine_time') or 'Any time'} · {routine['name']} · OVERDUE {(today-date.fromisoformat(routine['next_due'])).days}d" if routine['next_due'] < today_iso else f"{routine.get('routine_time') or 'Any time'} · {routine['name']}") for routine in routines)
        if bills_today:
            lines.append('')
            lines.extend(f"💳 Due today · {bill['name']} · {float(bill['amount_mad']):.2f} MAD" for bill in bills_today)
        activity = []
        if treadmill:
            activity.append(f'Treadmill {int(treadmill)} min')
        if gym:
            activity.append('Gym: ' + ', '.join(item['name'] for item in gym))
        if activity:
            lines.append('')
            lines.append('ACTIVITY · ' + ' · '.join(activity))
        if not (events or tasks or routines or bills_today or treadmill or gym):
            lines.append('No plans today. Enjoy the free time!')
        return '\n'.join(lines)

    if low in ('✅ tasks', '/tasks', 'tasks', 'my tasks', 'show tasks', 'list tasks', 'to do', 'todo'):
        tasks = rows("SELECT id,title,due_date,due_time,priority FROM tasks WHERE status!='done' ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,due_date,CASE WHEN due_time IS NULL THEN 1 ELSE 0 END,due_time,id LIMIT 12")
        if not tasks:
            return '✅ All clear — no open tasks.'
        lines = ['✅ OPEN TASKS']
        for task in tasks:
            due = _telegram_date_label(task.get('due_date'))
            if task.get('due_date') and task['due_date'] < today_iso:
                due = 'OVERDUE · ' + due
            elif task.get('due_date') == today_iso:
                due = 'TODAY'
            time_label = task.get('due_time') or 'Any time'
            lines.append(f"• {task['title']} · {due} · {time_label}")
        lines.append('')
        lines.append('Finish one: task done TASK NAME')
        return '\n'.join(lines)

    if low in ('💳 bills', '/bills', 'bills', 'my bills', 'unpaid bills'):
        bills = rows("SELECT id,name,amount_mad,due_date FROM bills WHERE status!='paid' ORDER BY due_date,id LIMIT 10")
        if not bills:
            return '✅ No unpaid bills.'
        lines = ['💳 UNPAID BILLS']
        for bill in bills:
            due = date.fromisoformat(bill['due_date'])
            days = (due - today).days
            if days < 0:
                label = f'overdue {abs(days)}d'
            elif days == 0:
                label = 'due today'
            else:
                label = f'in {days}d'
            lines.append(f"• {bill['name']} · {float(bill['amount_mad']):.2f} MAD · {label} ({due.strftime('%d/%m/%y')})")
        lines.append('')
        lines.append('After paying: paid BILL NAME')
        return '\n'.join(lines)

    if low in ('🔁 routines', '🧴 personal care', 'care', 'personal care', '/care', 'routines', 'my routines', '/routines'):
        routines = rows("SELECT id,name,next_due,interval_days,routine_time,routine_late_time,last_done FROM routines ORDER BY CASE WHEN next_due<=? THEN 0 ELSE 1 END,next_due,COALESCE(routine_time,'23:59'),name LIMIT 15", (today_iso,))
        if not routines:
            return 'No routines yet. Add them in XROB Assistant.'
        due_items = [r for r in routines if r.get('next_due') and r['next_due'] <= today_iso]
        upcoming = [r for r in routines if r not in due_items]
        lines = [f'🔁 ROUTINES · {len(due_items)} due']
        for routine in due_items:
            due = date.fromisoformat(routine['next_due'])
            status = 'DUE TODAY' if due == today else f'OVERDUE {(today-due).days}d'
            lines.append(f"• {routine['name']} · {routine.get('routine_time') or 'Any time'} · {status}")
        if upcoming:
            lines.append('')
            lines.append('UP NEXT')
            for routine in upcoming[:5]:
                lines.append(f"• {routine['name']} · {_telegram_date_label(routine.get('next_due'))} · {routine.get('routine_time') or 'Any time'}")
        lines.append('')
        lines.append('Complete: done ROUTINE NAME')
        return '\n'.join(lines)

    if low in ('🏃 fitness', '/fitness', 'fitness', 'activity', 'my fitness', 'workouts'):
        weight = one('SELECT kg,date FROM weights ORDER BY date DESC,id DESC LIMIT 1')
        treadmill = one('SELECT COALESCE(SUM(duration_min),0) AS mins,COALESCE(SUM(distance_km),0) AS km,COALESCE(SUM(calories_kcal),0) AS kcal FROM treadmill WHERE date=?', (today_iso,))
        gym = rows('SELECT name FROM workouts WHERE date=? ORDER BY id', (today_iso,))
        lines = ['🏃 FITNESS']
        lines.append(f"Weight · {float(weight['kg']):.2f} kg ({_telegram_date_label(weight['date'])})" if weight else 'Weight · no records yet')
        lines.append(f"Treadmill today · {int(treadmill['mins'] or 0)} min · {float(treadmill['km'] or 0):.2f} km · {int(treadmill['kcal'] or 0)} kcal" if treadmill['mins'] else 'Treadmill today · no session logged')
        lines.append('Gym today · ' + (', '.join(item['name'] for item in gym) if gym else 'no session logged'))
        lines.append('')
        lines.append('Quick log: weight 89.5 · treadmill 30')
        lines.append('Or tap Add weight / Add treadmill for guided entry.')
        return '\n'.join(lines)

    if low in ('📆 next 7 days', '🗓 week', '/week', 'week', 'next', 'next 7 days', 'agenda', 'this week'):
        end = (today + timedelta(days=7)).isoformat()
        items: list[tuple[str, str]] = []
        for event in rows('SELECT title,start_at FROM events WHERE substr(start_at,1,10)>=? AND substr(start_at,1,10)<=?', (today_iso, end)):
            items.append((event['start_at'], f"{event['start_at'][8:10]}/{event['start_at'][5:7]} {event['start_at'][11:16]} · {event['title']}"))
        for task in rows("SELECT title,due_date,due_time FROM tasks WHERE status!='done' AND due_date>=? AND due_date<=?", (today_iso, end)):
            clock = task.get('due_time') or '23:59'
            items.append((task['due_date']+'T'+clock, f"{_telegram_date_label(task['due_date'])} {task.get('due_time') or 'Any time'} · {task['title']}"))
        for bill in rows("SELECT name,due_date FROM bills WHERE status!='paid' AND due_date>=? AND due_date<=?", (today_iso, end)):
            items.append((bill['due_date']+'T23:59', f"{_telegram_date_label(bill['due_date'])} · Bill: {bill['name']}"))
        for routine in rows('SELECT name,next_due,routine_time FROM routines WHERE next_due>=? AND next_due<=?', (today_iso, end)):
            clock = routine.get('routine_time') or '23:59'
            items.append((routine['next_due']+'T'+clock, f"{_telegram_date_label(routine['next_due'])} {routine.get('routine_time') or 'Any time'} · {routine['name']}"))
        items.sort(key=lambda item: item[0])
        if not items:
            return '🗓 Nothing scheduled in the next 7 days.'
        return '🗓 NEXT 7 DAYS\n' + '\n'.join('• ' + item[1] for item in items[:30])

    # Direct quick logs; guided entry is available from the menu or /weight and /treadmill.
    if re.match(r'(?i)^/(?:weight|logweight)(?:@[a-z0-9_]+)?\s+', raw) or low.startswith('/weight ') or low.startswith('/logweight ') or low.startswith('weight ') or low.startswith('log weight '):
        try:
            kg, day = _telegram_parse_weight(raw)
            return _telegram_save_weight(kg, day)
        except Exception:
            return 'Enter a weight from 20–350 kg, e.g. weight 89.5 or weight 89.5 17/11/26.'

    if re.match(r'(?i)^/treadmill(?:@[a-z0-9_]+)?\s+', raw) or low.startswith('/treadmill ') or low.startswith('treadmill ') or low.startswith('run ') or low.startswith('walk '):
        try:
            duration, day = _telegram_parse_treadmill(raw)
            return _telegram_save_treadmill(duration, day)
        except Exception:
            return 'Enter a duration in minutes, e.g. treadmill 30 or treadmill 30 17/11/26.'

    # Mark a bill as paid (tracking only; no payment is performed).
    paid_match = re.match(r'(?i)^(?:/paid(?:@[a-z0-9_]+)?|paid|bill paid)\s+(.+)$', raw)
    if paid_match:
        query = paid_match.group(1).strip()
        bills = rows("SELECT * FROM bills WHERE status!='paid'")
        matched, matches = _telegram_match_named_item(query, bills)
        if not matched:
            if len(matches) > 1:
                return 'Which bill? ' + ', '.join(item['name'] for item in matches[:5])
            return f"I couldn't find an unpaid bill called '{query}'. Send Bills to see the list."
        mark_bill_paid(matched['id'])
        next_text = f' Next bill due {_telegram_date_label(next_recurrence_date(date.fromisoformat(matched['due_date']), matched.get('recurrence') or 'none').isoformat())}.' if next_recurrence_date(date.fromisoformat(matched['due_date']), matched.get('recurrence') or 'none') else ''
        return f"✅ Marked paid: {matched['name']} · {float(matched['amount_mad']):.2f} MAD.{next_text}"

    # Bare /taskdone displays open items for convenient selection.
    if low in ('/taskdone','task done','finish task','complete task'):
        open_tasks=rows("SELECT id,title,due_date,due_time FROM tasks WHERE status!='done' ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,due_date,due_time,id LIMIT 12")
        if not open_tasks: return 'No open tasks to complete.'
        return 'Open tasks:\n'+'\n'.join(f"• {t['title']} · {_telegram_date_label(t.get('due_date'))} {t.get('due_time') or ''}\nComplete with: task done {t['title']}" for t in open_tasks)

    # Mark a task complete without confusing it with routine completion.
    task_done = re.match(r'(?i)^(?:/taskdone(?:@[a-z0-9_]+)?|task done|done task|finish task)\s+(.+)$', raw)
    if task_done:
        query = task_done.group(1).strip()
        tasks = rows("SELECT * FROM tasks WHERE status!='done'")
        matched, matches = _telegram_match_named_item(query, tasks, 'title')
        if not matched:
            if len(matches) > 1:
                return 'Which task? ' + ', '.join(item['title'] for item in matches[:5])
            return f"I couldn't find an open task called '{query}'. Send Tasks to see open tasks."
        with connect() as c:
            c.execute("UPDATE tasks SET status='done' WHERE id=?", (matched['id'],))
        return f"✅ Task completed: {matched['title']}"

    # Add a scheduled task. Times always use 24-hour HH:MM.
    task_request = (bool(re.match(r'(?i)^/task(?:@[a-z0-9_]+)?\s+', raw)) or low.startswith('/task ') or low.startswith('task ') or low.startswith('add task ') or
                    low.startswith('new task ') or low.startswith('remind me to ') or
                    low.startswith('please remind me to ') or low.startswith('i want to ') or
                    low.startswith('schedule '))
    bare_scheduled_task = bool(re.search(r'(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)', raw)) and low not in ('today', 'agenda', 'next 7 days') and not low.startswith('/event ')
    if task_request or bare_scheduled_task:
        try:
            title, due_date, due_time = _telegram_task_payload(raw)
            with connect() as c:
                c.execute('INSERT INTO tasks(title,due_date,due_time,category) VALUES(?,?,?,?)', (title, due_date, due_time, 'general'))
            if due_date and due_time:
                return f"✅ Reminder set · {title}\n{_telegram_date_label(due_date)} at {due_time}."
            if due_date:
                return f"✅ Task added · {title}\nDue {_telegram_date_label(due_date)}."
            return f"✅ Task added · {title}\nNo reminder set yet. Add a date/time in the app or resend with 'tomorrow at 18:00'."
        except ValueError as exc:
            return str(exc)

    # Complete a routine. A bare /done shows which routines need attention.
    done_command = re.match(r'(?i)^/done(?:@[a-z0-9_]+)?(?:\s+(.+))?$', raw)
    done_name = None
    if low in ('done', 'complete routine', 'mark routine done') or (done_command and not done_command.group(1)):
        due_now = rows("SELECT name FROM routines WHERE next_due<=? ORDER BY COALESCE(routine_time,'23:59'),name", (today_iso,))
        return 'Routines to check off:\n' + ('\n'.join('• ' + item['name'] for item in due_now) if due_now else 'None due today.') + '\n\nComplete one: done ROUTINE NAME'
    if done_command and done_command.group(1):
        done_name = done_command.group(1).strip()
    elif low.startswith('done ') or low.startswith('complete '):
        done_name = re.sub(r'^(?:done|complete)\s+', '', raw, flags=re.I).strip()
    else:
        mark_match = re.match(r'(?i)^mark\s+(.+?)\s+(?:as\s+)?done$', raw)
        if mark_match:
            done_name = mark_match.group(1).strip()
        else:
            did_match = re.match(r'(?i)^(?:i did|i completed|finished)\s+(.+)$', raw)
            if did_match:
                done_name = did_match.group(1).strip()
    if done_name:
        wanted = _telegram_name_key(done_name)
        wanted = re.sub(r'^(?:the|my)\s+', '', wanted)
        wanted = re.sub(r'\s+routine$', '', wanted)
        routines = rows('SELECT * FROM routines')
        matched, matches = _telegram_match_named_item(wanted, routines)
        if not matched:
            if len(matches) > 1:
                return 'Which routine? ' + ', '.join(item['name'] for item in matches[:5])
            return f"I couldn't find '{done_name}'. Send Routines to see your list."
        updated = complete_routine(matched['id'])
        next_due = _telegram_date_label(updated.get('next_due'))
        return f"✅ Routine done · {updated['name']}\nNext due: {next_due}."

    if low in ('/event', '/add_event'):
        return 'To add an event, use: /event Dentist|20/11/26 10:00'
    if re.match(r'(?i)^/event(?:@[a-z0-9_]+)?\s+', raw) or low.startswith('/event '):
        try:
            event_body = re.sub(r'(?i)^/event(?:@[a-z0-9_]+)?\s*', '', raw)
            title, when = event_body.split('|', 1)
            title = title.strip()
            _, due_day, due_time = _telegram_task_payload(f'event {title} {when}')
            if not due_day or not due_time:
                raise ValueError()
            start_at = f'{due_day}T{due_time}'
            with connect() as c:
                c.execute('INSERT INTO events(title,start_at,category) VALUES(?,?,?)', (title, start_at, 'personal'))
            return f"✅ Event added · {title}\n{_telegram_date_label(due_day)} at {due_time}."
        except Exception:
            return 'Use: /event Dentist|20/11/26 10:00'

    return ('I can help with your day. Use the buttons below, or send:\n'
            '• task Workout tomorrow at 18:00\n'
            '• done Shower\n'
            '• task done Buy groceries\n'
            '• weight 89.5\n'
            '• treadmill 30\n'
            '• paid Electricity')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8090)

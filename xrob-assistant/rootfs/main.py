from __future__ import annotations
import asyncio, json, os, sqlite3, re
from contextlib import asynccontextmanager
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
CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_mad REAL DEFAULT 0, due_date TEXT NOT NULL, recurrence TEXT DEFAULT 'none', reminder_days INTEGER DEFAULT 3, status TEXT DEFAULT 'unpaid', paid_at TEXT, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, name TEXT NOT NULL, expiry_date TEXT NOT NULL, reminder_days INTEGER DEFAULT 30, reference TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routines (id INTEGER PRIMARY KEY, name TEXT NOT NULL, interval_days INTEGER DEFAULT 1, last_done TEXT, next_due TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', routine_time TEXT DEFAULT '10:00', routine_late_time TEXT DEFAULT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routine_logs (id INTEGER PRIMARY KEY, routine_id INTEGER NOT NULL, completed_at TEXT NOT NULL, FOREIGN KEY(routine_id) REFERENCES routines(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL, reminder_date TEXT NOT NULL, UNIQUE(kind,item_id,reminder_date));
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''

def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        # Add the optional task time column to databases created by earlier versions.
        task_columns = {r['name'] for r in c.execute('PRAGMA table_info(tasks)').fetchall()}
        if 'due_time' not in task_columns:
            c.execute('ALTER TABLE tasks ADD COLUMN due_time TEXT')
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
    if TOKEN and ALLOWED_CHAT:
        asyncio.create_task(telegram_poll())
        asyncio.create_task(reminder_loop())
    elif TOKEN:
        print('Telegram bot token is set but allowed chat ID is empty; bot disabled for safety.', flush=True)
    yield

app = FastAPI(title='XROB Assistant API', version='0.1.13', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=WWW), name='static')

@app.get('/')
def home(): return FileResponse(WWW/'index.html')
@app.get('/api/health')
def health(): return {'status':'ok','app':'XROB Assistant','version':'0.1.13','database':'connected'}

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

def is_weight_routine(routine: dict[str, Any]) -> bool:
    name = str(routine.get('name') or '').lower()
    return 'weight' in name or 'weigh' in name

def auto_complete_weight_routines(completed_on: date) -> None:
    day = completed_on.isoformat()
    for routine in rows('SELECT * FROM routines'):
        if not is_weight_routine(routine):
            continue
        interval = max(1, int(routine.get('interval_days') or 1))
        next_due = (completed_on + timedelta(days=interval)).isoformat()
        with connect() as c:
            already_logged = c.execute('SELECT id FROM routine_logs WHERE routine_id=? AND completed_at=? LIMIT 1', (routine['id'], day)).fetchone()
            if not already_logged:
                c.execute('INSERT INTO routine_logs(routine_id,completed_at) VALUES(?,?)', (routine['id'], day))
            c.execute('UPDATE routines SET last_done=?,next_due=? WHERE id=?', (day, next_due, routine['id']))

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
                    reminders.append(('routine-time', routine['id'], today.isoformat(), f"It's time for your routine: {name} (scheduled for {routine_time}). Mark it Done in XROB Assistant when completed."))
            elif late_time and current_hm >= late_time:
                scheduled = f' It was scheduled for {routine_time}.' if routine_time else ''
                days_late = (today - due).days
                reminders.append(('routine-late', routine['id'], today.isoformat(), f"You still haven't checked off {name}.{scheduled} It was due {days_late} day{'s' if days_late != 1 else ''} ago. This follow-up reminder ({late_time}) will repeat daily until completed."))
        except Exception as exc:
            print(f'Routine reminder skipped for {routine.get("name", routine.get("id"))}: {exc}', flush=True)
    return reminders

@app.post('/api/{resource}')
def create_resource(resource: str, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table, allowed=TABLES[resource]
    data={k:v for k,v in payload.data.items() if k in allowed}
    if resource == 'tasks':
        validate_task_schedule(data)
    if resource == 'routines':
        data.setdefault('routine_time', '10:00')
        data.setdefault('routine_late_time', None)
        data.setdefault('next_due', date.today().isoformat())
        validate_routine_schedule(data)
    if resource == 'treadmill':
        try:
            duration = float(data.get('duration_min', 0))
            if duration <= 0: raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, 'Enter a valid treadmill duration.')
        latest = one('SELECT kg FROM weights ORDER BY date DESC,id DESC LIMIT 1')
        body_weight = float(latest['kg']) if latest else 70.0
        data.update({'speed_kmh': 5.0, 'incline': 0.0,
                     'distance_km': round(5.0 * duration / 60.0, 2),
                     'calories_kcal': round(3.5 * 3.5 * body_weight / 200.0 * duration)})
    if not data: raise HTTPException(400,'No valid fields supplied')
    if resource in ('events','tasks','workouts','bills','documents','routines') and not data.get('title' if resource in ('events','tasks') else 'name'):
        raise HTTPException(400,'A title/name is required')
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
    if resource == 'weights' and weight_day == date.today():
        auto_complete_weight_routines(weight_day)
    return one(f'SELECT * FROM {table} WHERE id=?',(new_id,))

@app.patch('/api/{resource}/{item_id}')
def update_resource(resource: str, item_id: int, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,allowed=TABLES[resource]; data={k:v for k,v in payload.data.items() if k in allowed}
    if resource == 'tasks':
        existing_task = one('SELECT * FROM tasks WHERE id=?',(item_id,))
        if not existing_task:
            raise HTTPException(404, 'Task not found')
        validate_task_schedule(data, existing_task)
    if resource == 'routines':
        existing_routine = one('SELECT * FROM routines WHERE id=?',(item_id,))
        if not existing_routine:
            raise HTTPException(404, 'Routine not found')
        validate_routine_schedule(data, existing_routine)
    if not data: raise HTTPException(400,'No valid fields supplied')
    if resource == 'routines' and 'interval_days' in data:
        try:
            interval=max(1,int(data['interval_days']))
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
    return {'deleted':True,'id':item_id}

@app.post('/api/workouts/{workout_id}/exercises')
def add_exercise(workout_id: int, payload: Payload):
    d=payload.data
    if not one('SELECT id FROM workouts WHERE id=?',(workout_id,)): raise HTTPException(404,'Workout not found')
    with connect() as c:
        cur=c.execute('INSERT INTO exercises(workout_id,name,sets,reps,weight_kg) VALUES(?,?,?,?,?)',(workout_id,d.get('name','Exercise'),d.get('sets',0),str(d.get('reps','')),d.get('weight_kg',0)))
    return one('SELECT * FROM exercises WHERE id=?',(cur.lastrowid,))
@app.get('/api/workouts/{workout_id}/exercises')
def workout_exercises(workout_id:int): return rows('SELECT * FROM exercises WHERE workout_id=?',(workout_id,))

@app.post('/api/routines/{routine_id}/complete')
def complete_routine(routine_id:int):
    r=one('SELECT * FROM routines WHERE id=?',(routine_id,))
    if not r: raise HTTPException(404,'Routine not found')
    today=date.today(); nxt=(today+timedelta(days=max(1,int(r['interval_days'])))).isoformat()
    with connect() as c:
        c.execute('INSERT INTO routine_logs(routine_id,completed_at) VALUES(?,?)',(routine_id,today.isoformat()))
        c.execute('UPDATE routines SET last_done=?,next_due=? WHERE id=?',(today.isoformat(),nxt,routine_id))
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

@app.post('/api/bills/{bill_id}/pay')
def pay_bill(bill_id:int):
    b=one('SELECT * FROM bills WHERE id=?',(bill_id,))
    if not b: raise HTTPException(404,'Bill not found')
    now=datetime.now().isoformat(timespec='minutes')
    with connect() as c: c.execute("UPDATE bills SET status='paid',paid_at=? WHERE id=?",(now,bill_id))
    return one('SELECT * FROM bills WHERE id=?',(bill_id,))

@app.get('/api/summary')
def summary():
    today=date.today().isoformat()
    next_bill = one("SELECT name,due_date,amount_mad FROM bills WHERE status!='paid' ORDER BY CASE WHEN due_date>=? THEN 0 ELSE 1 END, CASE WHEN due_date>=? THEN due_date END ASC, CASE WHEN due_date<? THEN due_date END DESC, id ASC LIMIT 1", (today, today, today))
    days_to_next_bill = (date.fromisoformat(next_bill['due_date']) - date.today()).days if next_bill else None
    return {
      'events_today':len(rows('SELECT id FROM events WHERE substr(start_at,1,10)=?',(today,))),
      'routines_due_today':one('SELECT COUNT(*) n FROM routines WHERE next_due=?',(today,))['n'],
      'open_tasks':one("SELECT COUNT(*) n FROM tasks WHERE status!='done'")['n'],
      'latest_weight':one('SELECT kg,date FROM weights ORDER BY date DESC,id DESC LIMIT 1'),
      'unpaid_bills':one("SELECT COUNT(*) n FROM bills WHERE status!='paid'")['n'],
      'unpaid_total_mad':one("SELECT COALESCE(SUM(amount_mad),0) n FROM bills WHERE status!='paid'")['n'],
      'upcoming_bill':({**next_bill, 'days_left': days_to_next_bill} if next_bill else None),
      'expiring_documents':len(rows('SELECT id FROM documents WHERE expiry_date<=?',((date.today()+timedelta(days=30)).isoformat(),))),
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
                        reminders.append(('bill', b['id'], today.isoformat(), f"Bill reminder: {b['name']} — {b['amount_mad']:.2f} MAD (due {b['due_date']})"))
                for t in rows("SELECT * FROM tasks WHERE status!='done' AND due_date IS NOT NULL"):
                    try:
                        due_day = date.fromisoformat(t['due_date'])
                        if t.get('due_time'):
                            due_at = datetime.combine(due_day, datetime.strptime(t['due_time'], '%H:%M').time())
                            if due_at <= now:
                                reminder_key = due_at.strftime('%Y-%m-%dT%H:%M')
                                reminders.append(('task-time', t['id'], reminder_key, f"It's time: {t['title']} (scheduled for {due_day.strftime('%d/%m/%y')} at {t['due_time']})"))
                        elif due_day <= today:
                            reminders.append(('task', t['id'], today.isoformat(), f"Task due: {t['title']} (due {due_day.strftime('%d/%m/%y')})"))
                    except Exception:
                        continue
                reminders.extend(build_routine_reminders(now))
                for kind, item_id, reminder_date, message in reminders:
                    with connect() as c:
                        cur = c.execute('INSERT OR IGNORE INTO notification_log(kind,item_id,reminder_date) VALUES(?,?,?)', (kind, item_id, reminder_date))
                        should_send = cur.rowcount > 0
                    if should_send:
                        await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': ALLOWED_CHAT, 'text': '🔔 XROB Assistant\n' + message})
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
            [{'text': '📆 Next 7 days'}, {'text': '❔ Help'}]
        ],
        'resize_keyboard': True,
        'is_persistent': True,
        'input_field_placeholder': 'Ask XROB or type: done Shower'
    }
    commands = [
        {'command': 'today', 'description': 'Show today’s schedule'},
        {'command': 'tasks', 'description': 'List open tasks'},
        {'command': 'routines', 'description': 'Show due and upcoming routines'},
        {'command': 'bills', 'description': 'Show unpaid bills'},
        {'command': 'week', 'description': 'Show the next 7 days'},
        {'command': 'done', 'description': 'Complete a routine: /done Shower'},
        {'command': 'weight', 'description': 'Log weight: /weight 89.5'},
        {'command': 'help', 'description': 'Show bot help'}
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
                        reply = 'Sorry, I could not complete that request. Please try again or send /help.'
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
    body = re.sub(r'^(?:/task|task|add task|remind me to|please remind me to|remind me|i want to|schedule)\s*', '', body, flags=re.I)
    body = body.replace('|', ' ')
    if re.search(r'\b(?:AM|PM)\b', body, flags=re.I):
        raise ValueError('Please use 24-hour time without AM/PM, for example 18:00.')
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
    body = re.sub(r'(?i)\b(on|at|for|please|me|to|remind|reminder)\b', ' ', body)
    title = re.sub(r'\s+', ' ', body).strip(' .,:;-')
    if not title:
        raise ValueError('Please include a task name, for example: remind me to workout tomorrow at 18:00.')
    return title, due_date, due_time

async def telegram_command(text: str) -> str:
    raw = text.strip()
    low = raw.lower().strip()
    today = date.today()
    if low in ('/start', 'start', '/help', 'help', '❔ help', 'hi', 'hello'):
        return ('Hi! I’m XROB Assistant.\n\nQuick buttons: Today, Tasks, Routines, Bills and Next 7 days.\n\nUseful messages:\n• done Shower — mark a routine complete\n• task Workout tomorrow at 18:00 — create a reminder\n• weight 89.5 — log today’s weight\n• help — show examples\n\nI handle these actions locally; no AI or cloud service is needed.')
    if low in ('📅 today', '/today', 'today', 'what is today', "what's today", 'what do i have today?', 'agenda today'):
        s = summary()
        events = rows('SELECT title,start_at FROM events WHERE substr(start_at,1,10)=? ORDER BY start_at', (today.isoformat(),))
        tasks = rows("SELECT title,due_time FROM tasks WHERE status!='done' AND due_date=? ORDER BY due_time", (today.isoformat(),))
        tread = one('SELECT COALESCE(SUM(duration_min),0) AS mins FROM treadmill WHERE date=?', (today.isoformat(),))['mins']
        gym = rows('SELECT name,duration_min FROM workouts WHERE date=? ORDER BY id', (today.isoformat(),))
        bills_today = rows("SELECT name,amount_mad FROM bills WHERE status!='paid' AND due_date=? ORDER BY name", (today.isoformat(),))
        care_today = rows('SELECT name FROM routines WHERE next_due<=? ORDER BY next_due', (today.isoformat(),))
        lines = [f"Today · {today.strftime('%A %d/%m/%y')}", f"{len(events)} events · {len(tasks)} scheduled tasks · {int(tread or 0)} treadmill minutes"]
        lines += [f"📅 {e['start_at'][11:16] if len(e['start_at'])>10 else ''} {e['title']}" for e in events]
        lines += [f"⏰ {t['due_time'] or 'Any time'} · {t['title']}" for t in tasks]
        if gym: lines += ['🏋️ Gym: ' + ', '.join(g['name'] + (f" ({g['duration_min']} min)" if g.get('duration_min') else '') for g in gym)]
        if bills_today: lines += [f"💳 Bill due: {b['name']} · {b['amount_mad']:.2f} MAD" for b in bills_today]
        if care_today: lines += ['🔁 Routines due: ' + ', '.join(r['name'] for r in care_today)]
        if not events and not tasks and not tread and not gym and not bills_today and not care_today: lines.append('Nothing scheduled yet. Enjoy the day!')
        return '\n'.join(lines)
    if low in ('✅ tasks', '/tasks', 'tasks', 'my tasks', 'show tasks', 'list tasks', 'to do', 'todo'):
        ts = rows("SELECT title,due_date,due_time,priority FROM tasks WHERE status!='done' ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,due_date,due_time LIMIT 20")
        return 'Your open tasks:\n' + '\n'.join('• ' + t['title'] + (f" — {datetime.strptime(t['due_date'],'%Y-%m-%d').strftime('%d/%m/%y')}" if t['due_date'] else '') + (f" at {t['due_time']}" if t.get('due_time') else '') for t in ts) if ts else 'You have no open tasks. Nice work!'
    if low in ('💳 bills', '/bills', 'bills', 'my bills', 'unpaid bills'):
        bs = rows("SELECT name,amount_mad,due_date FROM bills WHERE status!='paid' ORDER BY due_date LIMIT 20")
        return 'Your unpaid bills:\n' + '\n'.join(f"• {b['name']}: {b['amount_mad']:.2f} MAD · due {datetime.strptime(b['due_date'],'%Y-%m-%d').strftime('%d/%m/%y')}" for b in bs) if bs else 'No unpaid bills. Everything is clear!'
    if low in ('🔁 routines', '🧴 personal care', 'care', 'personal care', '/care', 'routines', 'my routines', '/routines'):
        routines = rows("SELECT id,name,next_due,interval_days,routine_time,routine_late_time FROM routines ORDER BY CASE WHEN next_due<=? THEN 0 ELSE 1 END,next_due,COALESCE(routine_time,'23:59') LIMIT 20", (today.isoformat(),))
        if not routines:
            return 'No routines set up yet. Add one in XROB Assistant.'
        lines = []
        for routine in routines:
            due = date.fromisoformat(routine['next_due']) if routine.get('next_due') else None
            if due == today:
                due_label = 'DUE TODAY'
            elif due and due < today:
                due_label = f'OVERDUE {(today-due).days}d'
            else:
                due_label = due.strftime('%d/%m/%y') if due else 'not scheduled'
            late = f" · follow-up {routine['routine_late_time']} next day" if routine.get('routine_late_time') else ''
            lines.append(f"• {routine['name']} — {due_label} · {routine.get('routine_time') or 'no time'}{late} · every {routine['interval_days']}d")
        return 'Routines — send “done NAME” when finished:\n' + '\n'.join(lines)
    if low in ('📆 next 7 days', '/week', 'week', 'next', 'next 7 days', 'agenda', 'this week'):
        end = (today + timedelta(days=7)).isoformat()
        items = []
        for e in rows('SELECT title,start_at FROM events WHERE substr(start_at,1,10)>=? AND substr(start_at,1,10)<=?', (today.isoformat(), end)):
            items.append((e['start_at'], f"📅 {e['start_at'][0:10][8:10]}/{e['start_at'][0:10][5:7]} {e['start_at'][11:16]} · {e['title']}"))
        for t in rows("SELECT title,due_date,due_time FROM tasks WHERE status!='done' AND due_date>=? AND due_date<=?", (today.isoformat(), end)):
            when = t['due_date'] + ('T' + t['due_time'] if t.get('due_time') else 'T23:59')
            items.append((when, f"⏰ {datetime.strptime(t['due_date'],'%Y-%m-%d').strftime('%d/%m')} {t.get('due_time') or ''} · {t['title']}"))
        for b in rows("SELECT name,due_date FROM bills WHERE status!='paid' AND due_date>=? AND due_date<=?", (today.isoformat(), end)):
            items.append((b['due_date']+'T23:59', f"💳 {datetime.strptime(b['due_date'],'%Y-%m-%d').strftime('%d/%m')} · {b['name']}"))
        for r in rows('SELECT name,next_due,routine_time FROM routines WHERE next_due>=? AND next_due<=?', (today.isoformat(), end)):
            routine_hm = r.get('routine_time') or '23:59'
            items.append((r['next_due']+'T'+routine_hm, f"🔁 {datetime.strptime(r['next_due'],'%Y-%m-%d').strftime('%d/%m')} {routine_hm} · {r['name']}"))
        items.sort(key=lambda x:x[0])
        return 'Next 7 days:\n' + '\n'.join(x[1] for x in items) if items else 'Nothing scheduled in the next 7 days.'
    if low.startswith('/weight ') or low.startswith('weight '):
        try:
            kg = float(raw.split()[-1].replace(',', '.'))
            if not 20 <= kg <= 350: raise ValueError()
        except Exception:
            return 'Please send a valid weight, for example: weight 89,5'
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT kg FROM weights WHERE date=? ORDER BY id DESC LIMIT 1', (today.isoformat(),)).fetchone()
            if existing:
                return f"You already recorded {float(existing['kg']):.2f} kg today. I did not add a duplicate. Edit today's weight in the app if it needs correction."
            c.execute('INSERT INTO weights(date,kg) VALUES(?,?)', (today.isoformat(), kg))
        auto_complete_weight_routines(today)
        return f'Weight saved: {kg:.2f} kg for {today.strftime("%d/%m/%y")}.'
    if low.startswith('/event '):
        try:
            title, when = raw[7:].split('|', 1)
            title = title.strip()
            task_title, due_day, due_time = _telegram_task_payload(f'event {title} {when}')
            if not due_day or not due_time: raise ValueError()
            start_at = f'{due_day}T{due_time}'
            with connect() as c: c.execute('INSERT INTO events(title,start_at,category) VALUES(?,?,?)', (title, start_at, 'personal'))
            return f'Event added: {title} on {datetime.strptime(due_day,"%Y-%m-%d").strftime("%d/%m/%y")} at {due_time}.'
        except Exception:
            return 'To add an event, send: /event Dentist|17/11/26 10:00'
    done_name = None
    done_command = re.match(r'(?i)^/done(?:@[a-z0-9_]+)?(?:\s+(.+))?$', raw)
    if low in ('done', 'complete routine', 'mark routine done') or (done_command and not done_command.group(1)):
        due_now = rows("SELECT name FROM routines WHERE next_due<=? ORDER BY COALESCE(routine_time,'23:59')", (today.isoformat(),))
        names = ', '.join(r['name'] for r in due_now) or 'none due today'
        return f"To check off a routine, send: done Shower\nDue or overdue routines: {names}"
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
        wanted = re.sub(r'[^a-z0-9]+', ' ', done_name.lower()).strip()
        wanted = re.sub(r'^(?:the|my)\s+', '', wanted)
        wanted = re.sub(r'\s+routine$', '', wanted)
        if wanted in ('my nails', 'nails', 'nail care routine'):
            wanted = 'nail'
        all_routines = rows('SELECT * FROM routines')
        exact = [r for r in all_routines if re.sub(r'[^a-z0-9]+', ' ', r['name'].lower()).strip() == wanted]
        matches = exact or [r for r in all_routines if wanted in re.sub(r'[^a-z0-9]+', ' ', r['name'].lower()).strip()]
        if not matches:
            available = ', '.join(r['name'] for r in all_routines) or 'none configured'
            return f"I couldn't find '{done_name}'. Your routines: {available}. Try: done Shower"
        if len(matches) > 1:
            return 'I found more than one match. Please use the full routine name: ' + ', '.join(r['name'] for r in matches)
        updated = complete_routine(matches[0]['id'])
        next_due = datetime.strptime(updated['next_due'], '%Y-%m-%d').strftime('%d/%m/%y')
        return f"✅ {updated['name']} marked done. Next due: {next_due} (every {updated['interval_days']} days)."

    if low in ('/naildone', 'naildone', 'nails done', 'i did my nails'):
        r = one("SELECT id FROM routines WHERE lower(name) LIKE '%nail%' LIMIT 1")
        if not r: return 'I could not find a nail-care routine. Add it in Routines first.'
        complete_routine(r['id'])
        return 'Nail routine marked as done. I updated the next reminder date.'
    task_request = (low.startswith('/task ') or low.startswith('task ') or low.startswith('add task ') or
                    low.startswith('remind me to ') or low.startswith('please remind me to ') or
                    low.startswith('i want to ') or low.startswith('schedule '))
    bare_scheduled_task = bool(re.search(r'(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)', raw)) and low not in ('today', 'agenda', 'next 7 days')
    if task_request or bare_scheduled_task:
        try:
            title, due_date, due_time = _telegram_task_payload(raw)
            with connect() as c:
                c.execute('INSERT INTO tasks(title,due_date,due_time,category) VALUES(?,?,?,?)', (title, due_date, due_time, 'general'))
            if due_date and due_time:
                return f"Done — I'll remind you about '{title}' on {datetime.strptime(due_date,'%Y-%m-%d').strftime('%d/%m/%y')} at {due_time}."
            if due_date:
                return f"Task added: '{title}' for {datetime.strptime(due_date,'%Y-%m-%d').strftime('%d/%m/%y')}."
            return f"Task added: '{title}'. Add a date and time whenever you want a reminder."
        except ValueError as exc:
            return str(exc)
    return ('I can help with your schedule. Try Today, Tasks, Routines, Bills, or Next 7 days. '
            'To finish a routine, send: done Shower. To add a reminder: task Workout tomorrow at 18:00. '
            'To log weight: weight 89.5. Send /help for examples.')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8090)

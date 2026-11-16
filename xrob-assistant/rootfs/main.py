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
CREATE TABLE IF NOT EXISTS routines (id INTEGER PRIMARY KEY, name TEXT NOT NULL, interval_days INTEGER DEFAULT 1, last_done TEXT, next_due TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routine_logs (id INTEGER PRIMARY KEY, routine_id INTEGER NOT NULL, completed_at TEXT NOT NULL, FOREIGN KEY(routine_id) REFERENCES routines(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL, reminder_date TEXT NOT NULL, UNIQUE(kind,item_id,reminder_date));
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
        # Starter routines are inserted only once.
        if c.execute('SELECT COUNT(*) FROM routines').fetchone()[0] == 0:
            today = date.today().isoformat()
            for name, days, cat in [('Nail care',15,'personal care'),('Haircut',30,'personal care'),('Shower',1,'hygiene')]:
                c.execute('INSERT INTO routines(name,interval_days,last_done,next_due,category) VALUES(?,?,?,?,?)', (name,days,None,today,cat))

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
 'routines': ('routines', ['name','interval_days','last_done','next_due','category','notes']),
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

app = FastAPI(title='XROB Assistant API', version='0.1.10', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=WWW), name='static')

@app.get('/')
def home(): return FileResponse(WWW/'index.html')
@app.get('/api/health')
def health(): return {'status':'ok','app':'XROB Assistant','version':'0.1.10','database':'connected'}

@app.get('/api/{resource}')
def list_resource(resource: str, limit: int = Query(250, ge=1, le=10000)):
    if resource == 'summary': return summary()
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,_=TABLES[resource]
    return rows(f'SELECT * FROM {table} ORDER BY id DESC LIMIT ?', (limit,))

def validate_task_schedule(data: dict[str, Any]) -> None:
    if 'due_time' in data:
        value = str(data.get('due_time') or '').strip()
        if value:
            if not data.get('due_date'):
                raise HTTPException(400, 'Choose a due date before setting a reminder time.')
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                raise HTTPException(400, 'Reminder time must use HH:MM (24-hour time).')
            data['due_time'] = value
        else:
            data['due_time'] = None

@app.post('/api/{resource}')
def create_resource(resource: str, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table, allowed=TABLES[resource]
    data={k:v for k,v in payload.data.items() if k in allowed}
    if resource == 'tasks':
        validate_task_schedule(data)
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
    return one(f'SELECT * FROM {table} WHERE id=?',(new_id,))

@app.patch('/api/{resource}/{item_id}')
def update_resource(resource: str, item_id: int, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,allowed=TABLES[resource]; data={k:v for k,v in payload.data.items() if k in allowed}
    if resource == 'tasks':
        validate_task_schedule(data)
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
    return {
      'events_today':len(rows('SELECT id FROM events WHERE substr(start_at,1,10)=?',(today,))),
      'open_tasks':one("SELECT COUNT(*) n FROM tasks WHERE status!='done'")['n'],
      'latest_weight':one('SELECT kg,date FROM weights ORDER BY date DESC,id DESC LIMIT 1'),
      'unpaid_bills':one("SELECT COUNT(*) n FROM bills WHERE status!='paid'")['n'],
      'unpaid_total_mad':one("SELECT COALESCE(SUM(amount_mad),0) n FROM bills WHERE status!='paid'")['n'],
      'expiring_documents':len(rows('SELECT id FROM documents WHERE expiry_date<=?',((date.today()+timedelta(days=30)).isoformat(),))),
      'next_routine':one('SELECT * FROM routines ORDER BY next_due LIMIT 1'),
      'recent_treadmill':one('SELECT * FROM treadmill ORDER BY date DESC,id DESC LIMIT 1')
    }

@app.get('/api/ha/state')
def ha_state():
    s=summary()
    return {'weight_kg':s['latest_weight']['kg'] if s['latest_weight'] else None,'unpaid_bills':s['unpaid_bills'],'unpaid_total_mad':s['unpaid_total_mad'],'expiring_documents':s['expiring_documents'],'open_tasks':s['open_tasks'],'events_today':s['events_today'],'next_routine':s['next_routine']['name'] if s['next_routine'] else None}

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
                for r in rows('SELECT * FROM routines WHERE next_due IS NOT NULL'):
                    try:
                        due = date.fromisoformat(r['next_due'])
                        if due <= today:
                            reminders.append(('routine', r['id'], today.isoformat(), f"Personal care reminder: {r['name']} is due"))
                    except Exception:
                        continue
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
    """Simple Telegram assistant with quick-reply buttons and friendly plain-text commands."""
    import httpx
    offset = 0
    keyboard = {
        'keyboard': [
            [{'text': '📅 Today'}, {'text': '✅ Tasks'}],
            [{'text': '💳 Bills'}, {'text': '🧴 Personal care'}],
            [{'text': '📆 Next 7 days'}, {'text': '❔ Help'}]
        ],
        'resize_keyboard': True,
        'is_persistent': True
    }
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                r = await client.get(f'https://api.telegram.org/bot{TOKEN}/getUpdates', params={'timeout': 30, 'offset': offset})
                data = r.json()
                for upd in data.get('result', []):
                    offset = upd['update_id'] + 1
                    msg = upd.get('message', {})
                    chat = str(msg.get('chat', {}).get('id', ''))
                    message_text = (msg.get('text') or '').strip()
                    if not message_text:
                        continue
                    if ALLOWED_CHAT and chat != ALLOWED_CHAT:
                        continue
                    reply = await telegram_command(message_text)
                    payload = {'chat_id': chat, 'text': reply}
                    if message_text.lower() in ('/start', 'start', '/help', 'help', '❔ help'):
                        payload['reply_markup'] = keyboard
                    await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json=payload)
            except Exception as e:
                print(f'Telegram polling error: {e}', flush=True)
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
        return ('Hi! I’m XROB Assistant.\n\nUse the buttons below, or just type: today, tasks, bills, personal care, or next 7 days.\n\nTo schedule a reminder, send: remind me to workout tomorrow at 18:00\nTo log weight, send: weight 89,5\nTo add a task, send: task Buy groceries tomorrow\n\nI understand these simple requests locally; no AI or cloud assistant is used.')
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
        if care_today: lines += ['🧴 Personal care due: ' + ', '.join(r['name'] for r in care_today)]
        if not events and not tasks and not tread and not gym and not bills_today and not care_today: lines.append('Nothing scheduled yet. Enjoy the day!')
        return '\n'.join(lines)
    if low in ('✅ tasks', '/tasks', 'tasks', 'my tasks', 'show tasks', 'list tasks', 'to do', 'todo'):
        ts = rows("SELECT title,due_date,due_time,priority FROM tasks WHERE status!='done' ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,due_date,due_time LIMIT 20")
        return 'Your open tasks:\n' + '\n'.join('• ' + t['title'] + (f" — {datetime.strptime(t['due_date'],'%Y-%m-%d').strftime('%d/%m/%y')}" if t['due_date'] else '') + (f" at {t['due_time']}" if t.get('due_time') else '') for t in ts) if ts else 'You have no open tasks. Nice work!'
    if low in ('💳 bills', '/bills', 'bills', 'my bills', 'unpaid bills'):
        bs = rows("SELECT name,amount_mad,due_date FROM bills WHERE status!='paid' ORDER BY due_date LIMIT 20")
        return 'Your unpaid bills:\n' + '\n'.join(f"• {b['name']}: {b['amount_mad']:.2f} MAD · due {datetime.strptime(b['due_date'],'%Y-%m-%d').strftime('%d/%m/%y')}" for b in bs) if bs else 'No unpaid bills. Everything is clear!'
    if low in ('🧴 personal care', 'care', 'personal care', '/care', 'routines', 'my routines'):
        routines = rows('SELECT name,next_due,interval_days FROM routines ORDER BY next_due LIMIT 20')
        return 'Upcoming personal care:\n' + '\n'.join(f"• {r['name']} — {datetime.strptime(r['next_due'],'%Y-%m-%d').strftime('%d/%m/%y') if r.get('next_due') else 'not scheduled'} (every {r['interval_days']} days)" for r in routines) if routines else 'No personal-care routines set up yet.'
    if low in ('📆 next 7 days', 'next', 'next 7 days', 'agenda', 'week', 'this week'):
        end = (today + timedelta(days=7)).isoformat()
        items = []
        for e in rows('SELECT title,start_at FROM events WHERE substr(start_at,1,10)>=? AND substr(start_at,1,10)<=?', (today.isoformat(), end)):
            items.append((e['start_at'], f"📅 {e['start_at'][0:10][8:10]}/{e['start_at'][0:10][5:7]} {e['start_at'][11:16]} · {e['title']}"))
        for t in rows("SELECT title,due_date,due_time FROM tasks WHERE status!='done' AND due_date>=? AND due_date<=?", (today.isoformat(), end)):
            when = t['due_date'] + ('T' + t['due_time'] if t.get('due_time') else 'T23:59')
            items.append((when, f"⏰ {datetime.strptime(t['due_date'],'%Y-%m-%d').strftime('%d/%m')} {t.get('due_time') or ''} · {t['title']}"))
        for b in rows("SELECT name,due_date FROM bills WHERE status!='paid' AND due_date>=? AND due_date<=?", (today.isoformat(), end)):
            items.append((b['due_date']+'T23:59', f"💳 {datetime.strptime(b['due_date'],'%Y-%m-%d').strftime('%d/%m')} · {b['name']}"))
        for r in rows('SELECT name,next_due FROM routines WHERE next_due>=? AND next_due<=?', (today.isoformat(), end)):
            items.append((r['next_due']+'T08:00', f"🧴 {datetime.strptime(r['next_due'],'%Y-%m-%d').strftime('%d/%m')} · {r['name']}"))
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
    if low in ('/naildone', 'naildone', 'nails done', 'i did my nails'):
        r = one("SELECT id FROM routines WHERE lower(name) LIKE '%nail%' LIMIT 1")
        if not r: return 'I could not find a nail-care routine. Add it in Personal care first.'
        complete_routine(r['id'])
        return 'Nail care marked as done. I updated the next reminder date.'
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
    return ('I can help with your schedule. Try today, tasks, bills, personal care, next 7 days, '
            'weight 89,5, or remind me to workout tomorrow at 18:00. Send /help for the quick menu.')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8090)

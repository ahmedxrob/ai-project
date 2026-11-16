from __future__ import annotations
import asyncio, json, os, sqlite3
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
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_date TEXT, priority TEXT DEFAULT 'normal', status TEXT DEFAULT 'open', category TEXT DEFAULT 'general', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
 'tasks': ('tasks', ['title','due_date','priority','status','category','notes']),
 'weights': ('weights', ['date','kg','notes']),
 'workouts': ('workouts', ['date','name','duration_min','notes']),
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

app = FastAPI(title='XROB Assistant API', version='0.1.5', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=WWW), name='static')

@app.get('/')
def home(): return FileResponse(WWW/'index.html')
@app.get('/api/health')
def health(): return {'status':'ok','app':'XROB Assistant','version':'0.1.3','database':'connected'}

@app.get('/api/{resource}')
def list_resource(resource: str, limit: int = Query(250, ge=1, le=1000)):
    if resource == 'summary': return summary()
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,_=TABLES[resource]
    return rows(f'SELECT * FROM {table} ORDER BY id DESC LIMIT ?', (limit,))

@app.post('/api/{resource}')
def create_resource(resource: str, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table, allowed=TABLES[resource]
    data={k:v for k,v in payload.data.items() if k in allowed}
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
        cur=c.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({marks})", [data[k] for k in keys])
        new_id=cur.lastrowid
    return one(f'SELECT * FROM {table} WHERE id=?',(new_id,))

@app.patch('/api/{resource}/{item_id}')
def update_resource(resource: str, item_id: int, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,allowed=TABLES[resource]; data={k:v for k,v in payload.data.items() if k in allowed}
    if not data: raise HTTPException(400,'No valid fields supplied')
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
    """Send each due-date reminder once per day to the configured Telegram chat."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                today=date.today()
                reminders=[]
                for b in rows("SELECT * FROM bills WHERE status!='paid'"):
                    try: remind_on=date.fromisoformat(b['due_date'])-timedelta(days=int(b.get('reminder_days') or 3))
                    except Exception: continue
                    if remind_on <= today: reminders.append(('bill',b['id'],today.isoformat(),f"Bill reminder: {b['name']} — {b['amount_mad']:.2f} MAD (due {b['due_date']})"))
                for d in rows('SELECT * FROM documents'):
                    try: due=date.fromisoformat(d['expiry_date'])-timedelta(days=int(d['reminder_days'] or 30))
                    except Exception: continue
                    if due <= today and date.fromisoformat(d['expiry_date']) >= today:
                        reminders.append(('document',d['id'],today.isoformat(),f"Document expiry approaching: {d['name']} expires {d['expiry_date']}"))
                    elif date.fromisoformat(d['expiry_date']) < today:
                        reminders.append(('document-expired',d['id'],today.isoformat(),f"DOCUMENT EXPIRED: {d['name']} expired {d['expiry_date']}"))
                for t in rows("SELECT * FROM tasks WHERE status!='done' AND due_date IS NOT NULL"):
                    if t['due_date'] <= today.isoformat(): reminders.append(('task',t['id'],today.isoformat(),f"Task due: {t['title']} (due {t['due_date']})"))
                for r in rows('SELECT * FROM routines WHERE next_due IS NOT NULL'):
                    if r['next_due'] <= today.isoformat(): reminders.append(('routine',r['id'],today.isoformat(),f"Routine reminder: {r['name']} is due"))
                for kind,item_id,day,message in reminders:
                    with connect() as c:
                        cur=c.execute('INSERT OR IGNORE INTO notification_log(kind,item_id,reminder_date) VALUES(?,?,?)',(kind,item_id,day))
                        should_send=cur.rowcount>0
                    if should_send:
                        await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':ALLOWED_CHAT,'text':'🔔 XROB Assistant\n'+message})
            except Exception as e:
                print(f'Reminder scheduler error: {e}',flush=True)
            await asyncio.sleep(60)

async def telegram_poll():
    """Minimal Telegram long-polling bot. No AI; commands use explicit parsing."""
    import httpx
    offset=0
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                r=await client.get(f'https://api.telegram.org/bot{TOKEN}/getUpdates',params={'timeout':30,'offset':offset})
                data=r.json()
                for upd in data.get('result',[]):
                    offset=upd['update_id']+1
                    msg=upd.get('message',{}); chat=str(msg.get('chat',{}).get('id','')); text=(msg.get('text') or '').strip()
                    if not text: continue
                    if ALLOWED_CHAT and chat != ALLOWED_CHAT: continue
                    reply=await telegram_command(text)
                    await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':chat,'text':reply})
            except Exception as e:
                print(f'Telegram polling error: {e}',flush=True); await asyncio.sleep(5)

async def telegram_command(text:str)->str:
    low=text.lower()
    if low in ('/start','/help','help'):
        return ('XROB Assistant commands:\n/today – today summary\n/tasks – open tasks\n/bills – unpaid bills\n/weight 89.5 – record weight\n/event Meeting|2026-11-17 10:00 – add event\n/task Buy groceries – add task\n/naildone – complete nail care\n\nYou can also send: weight 89.5')
    if low.startswith('/weight ') or low.startswith('weight '):
        try: kg=float(text.split()[-1])
        except: return 'Please send a number, e.g. /weight 89.5'
        with connect() as c: c.execute('INSERT INTO weights(date,kg) VALUES(?,?)',(date.today().isoformat(),kg))
        return f'Weight saved: {kg:.1f} kg.'
    if low.startswith('/event '):
        try:
            title,when=text[7:].split('|',1); when=when.strip().replace(' ','T')
            with connect() as c: c.execute('INSERT INTO events(title,start_at,category) VALUES(?,?,?)',(title.strip(),when,'personal'))
            return f'Event added: {title.strip()} at {when}'
        except Exception: return 'Use /event Title|YYYY-MM-DD HH:MM'
    if low.startswith('/task '):
        title=text[6:].strip()
        if not title:return 'Please provide a task title.'
        with connect() as c:c.execute('INSERT INTO tasks(title) VALUES(?)',(title,))
        return f'Task added: {title}'
    if low in ('/today','today'):
        s=summary(); return f"Today: {s['events_today']} events, {s['open_tasks']} open tasks. Weight: {s['latest_weight']['kg'] if s['latest_weight'] else 'not recorded'} kg."
    if low in ('/tasks','tasks'):
        ts=rows("SELECT title,due_date FROM tasks WHERE status!='done' ORDER BY due_date LIMIT 15")
        return 'Open tasks:\n'+'\n'.join('• '+t['title']+((' — '+t['due_date']) if t['due_date'] else '') for t in ts) if ts else 'No open tasks.'
    if low in ('/bills','bills'):
        bs=rows("SELECT name,amount_mad,due_date FROM bills WHERE status!='paid' ORDER BY due_date")
        return 'Unpaid bills:\n'+'\n'.join(f"• {b['name']}: {b['amount_mad']:.2f} MAD — {b['due_date']}" for b in bs) if bs else 'No unpaid bills.'
    if low in ('/naildone','naildone'):
        r=one("SELECT id FROM routines WHERE lower(name) LIKE '%nail%' LIMIT 1")
        if not r:return 'No nail-care routine found.'
        complete_routine(r['id']); return 'Nail care completed. Next reminder date updated.'
    return 'I did not understand that. Send /help to see available commands.'

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8099)

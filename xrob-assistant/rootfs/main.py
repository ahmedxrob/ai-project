from __future__ import annotations
import asyncio, json, os, sqlite3, re, math, random
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, date, timedelta, time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
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
CREATE TABLE IF NOT EXISTS routines (id INTEGER PRIMARY KEY, name TEXT NOT NULL, interval_days INTEGER DEFAULT 1, last_done TEXT, next_due TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', routine_time TEXT DEFAULT '10:00', routine_late_time TEXT DEFAULT NULL, routine_rollover_after_late INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routine_logs (id INTEGER PRIMARY KEY, routine_id INTEGER NOT NULL, completed_at TEXT NOT NULL, FOREIGN KEY(routine_id) REFERENCES routines(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL, reminder_date TEXT NOT NULL, UNIQUE(kind,item_id,reminder_date));
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_inbox (update_id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, reply TEXT NOT NULL, sent_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS recipes (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, meal_type TEXT NOT NULL DEFAULT 'any', cuisine TEXT DEFAULT '',
 prep_min INTEGER DEFAULT 0, cook_min INTEGER DEFAULT 0, servings REAL DEFAULT 2, difficulty TEXT DEFAULT 'easy',
 ingredients_json TEXT NOT NULL DEFAULT '[]', instructions TEXT DEFAULT '', dietary_tags TEXT DEFAULT '',
 calories_kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, source_url TEXT DEFAULT '', notes TEXT DEFAULT '',
 favorite INTEGER DEFAULT 0, rating INTEGER DEFAULT 0, eating_interval_days INTEGER NOT NULL DEFAULT 1, batch_plates INTEGER NOT NULL DEFAULT 7, is_active INTEGER NOT NULL DEFAULT 1,
 meal_source TEXT NOT NULL DEFAULT 'home', plates_ready INTEGER NOT NULL DEFAULT 0, next_routine_date TEXT,
 excluded_weekdays_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recipes_meal_type ON recipes(meal_type);
CREATE TABLE IF NOT EXISTS meal_plans (
 id INTEGER PRIMARY KEY, plan_date TEXT NOT NULL, meal_type TEXT NOT NULL, recipe_id INTEGER,
 servings REAL NOT NULL DEFAULT 2, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(plan_date, meal_type), FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_meal_plans_date ON meal_plans(plan_date);
CREATE TABLE IF NOT EXISTS shopping_items (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, quantity REAL DEFAULT 1, unit TEXT DEFAULT '',
 category TEXT DEFAULT 'Other', checked INTEGER DEFAULT 0, source TEXT DEFAULT 'manual', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shopping_checked ON shopping_items(checked, category, name);
CREATE TABLE IF NOT EXISTS meal_batches (
 id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL, start_date TEXT NOT NULL, plates_total INTEGER NOT NULL, plates_scheduled INTEGER NOT NULL DEFAULT 0,
 eating_interval_days INTEGER NOT NULL DEFAULT 1, last_scheduled_date TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meal_batches_active ON meal_batches(recipe_id, plates_scheduled, plates_total);
CREATE TABLE IF NOT EXISTS daily_meals (
 id INTEGER PRIMARY KEY, plan_date TEXT NOT NULL UNIQUE, recipe_id INTEGER NOT NULL, batch_id INTEGER, status TEXT NOT NULL DEFAULT 'pending',
 notified_date TEXT, suggestion_notified_date TEXT, checkin_notified_date TEXT, confirmed_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT, FOREIGN KEY(batch_id) REFERENCES meal_batches(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_meals_date ON daily_meals(plan_date, status);
CREATE TABLE IF NOT EXISTS meal_cook_reminders (id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(recipe_id,due_date), FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_meal_cook_reminders_date ON meal_cook_reminders(due_date,status);
CREATE TABLE IF NOT EXISTS meal_schedule_skips (plan_date TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_meal_schedule_skips_date ON meal_schedule_skips(plan_date);
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
        recipe_columns = {r['name'] for r in c.execute('PRAGMA table_info(recipes)').fetchall()}
        if 'eating_interval_days' not in recipe_columns:
            c.execute('ALTER TABLE recipes ADD COLUMN eating_interval_days INTEGER NOT NULL DEFAULT 1')
        if 'batch_plates' not in recipe_columns:
            c.execute('ALTER TABLE recipes ADD COLUMN batch_plates INTEGER NOT NULL DEFAULT 7')
        if 'is_active' not in recipe_columns:
            c.execute('ALTER TABLE recipes ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1')
        had_plates_ready = 'plates_ready' in recipe_columns
        if 'meal_source' not in recipe_columns:
            c.execute("ALTER TABLE recipes ADD COLUMN meal_source TEXT NOT NULL DEFAULT 'home'")
        if 'plates_ready' not in recipe_columns:
            c.execute('ALTER TABLE recipes ADD COLUMN plates_ready INTEGER NOT NULL DEFAULT 0')
        if 'next_routine_date' not in recipe_columns:
            c.execute('ALTER TABLE recipes ADD COLUMN next_routine_date TEXT')
        if 'excluded_weekdays_json' not in recipe_columns:
            c.execute("ALTER TABLE recipes ADD COLUMN excluded_weekdays_json TEXT NOT NULL DEFAULT '[]'")
        # Meal Manager 0.4.0 adds a single-plate-per-day schedule. The older
        # meal_plans table is retained so historical 0.3.0 data is not lost.
        c.execute('''CREATE TABLE IF NOT EXISTS meal_batches (
            id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL, start_date TEXT NOT NULL,
            plates_total INTEGER NOT NULL, plates_scheduled INTEGER NOT NULL DEFAULT 0,
            eating_interval_days INTEGER NOT NULL DEFAULT 1, last_scheduled_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_meal_batches_active ON meal_batches(recipe_id, plates_scheduled, plates_total)')
        c.execute('''CREATE TABLE IF NOT EXISTS daily_meals (
            id INTEGER PRIMARY KEY, plan_date TEXT NOT NULL UNIQUE, recipe_id INTEGER NOT NULL, batch_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending', notified_date TEXT, confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT,
            FOREIGN KEY(batch_id) REFERENCES meal_batches(id) ON DELETE SET NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_daily_meals_date ON daily_meals(plan_date, status)')
        daily_meal_columns = {r['name'] for r in c.execute('PRAGMA table_info(daily_meals)').fetchall()}
        if 'suggestion_notified_date' not in daily_meal_columns:
            c.execute('ALTER TABLE daily_meals ADD COLUMN suggestion_notified_date TEXT')
        if 'checkin_notified_date' not in daily_meal_columns:
            c.execute('ALTER TABLE daily_meals ADD COLUMN checkin_notified_date TEXT')
        c.execute('''CREATE TABLE IF NOT EXISTS meal_cook_reminders (
            id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL, due_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(recipe_id,due_date), FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_meal_cook_reminders_date ON meal_cook_reminders(due_date,status)')
        c.execute('CREATE TABLE IF NOT EXISTS meal_schedule_skips (plan_date TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_meal_schedule_skips_date ON meal_schedule_skips(plan_date)')
        if not had_plates_ready:
            c.execute('''UPDATE recipes SET plates_ready=MAX(batch_plates,
                (SELECT COUNT(*) FROM daily_meals dm WHERE dm.recipe_id=recipes.id AND dm.status='pending'))
                WHERE is_active=1''')
        c.execute('''UPDATE recipes SET next_routine_date=COALESCE(
            (SELECT date(MAX(dm.plan_date), '+' || recipes.eating_interval_days || ' day')
             FROM daily_meals dm WHERE dm.recipe_id=recipes.id), date('now'))
            WHERE next_routine_date IS NULL''')
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
        if 'routine_rollover_after_late' not in routine_columns:
            c.execute('ALTER TABLE routines ADD COLUMN routine_rollover_after_late INTEGER NOT NULL DEFAULT 0')
        # In 0.1.11, 12:00 was automatically populated for every routine.
        # Convert those generated defaults to NULL so late reminders are opt-in.
        # Preserve any other time that the user may have deliberately selected.
        late_opt_in_migration = c.execute("SELECT 1 FROM app_migrations WHERE name='routine_late_reminder_optional_v1'").fetchone()
        if not late_opt_in_migration:
            c.execute("UPDATE routines SET routine_late_time=NULL WHERE routine_late_time='12:00'")
            c.execute("INSERT INTO app_migrations(name) VALUES('routine_late_reminder_optional_v1')")
        # Preserve the intended behavior for existing routines that already
        # have a late reminder: enable next-day rollover once during migration.
        rollover_migration = c.execute("SELECT 1 FROM app_migrations WHERE name='routine_rollover_after_late_v1'").fetchone()
        if not rollover_migration:
            c.execute("UPDATE routines SET routine_rollover_after_late=1 WHERE routine_late_time IS NOT NULL AND TRIM(routine_late_time)!=''")
            c.execute("INSERT INTO app_migrations(name) VALUES('routine_rollover_after_late_v1')")
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
 'routines': ('routines', ['name','interval_days','last_done','next_due','category','notes','routine_time','routine_late_time','routine_rollover_after_late']),
 'recipes': ('recipes', ['name','calories_kcal','ingredients_json','eating_interval_days','batch_plates','meal_source','plates_ready','excluded_weekdays_json']),
 'meal_cook_reminders': ('meal_cook_reminders', ['recipe_id','due_date','status']),
 'meal_plans': ('meal_plans', ['plan_date','meal_type','recipe_id','servings','notes']),
 'daily_meals': ('daily_meals', ['plan_date','recipe_id','batch_id','status','notified_date','confirmed_at']),
 'shopping_items': ('shopping_items', ['name','checked','source']),
}

@asynccontextmanager
async def lifespan(app):
    init_db()
    try:
        maintain_meal_schedule()
    except Exception as exc:
        print(f'Initial meal schedule maintenance warning: {exc}', flush=True)
    background = [asyncio.create_task(meal_schedule_loop())]
    if TOKEN and ALLOWED_CHAT:
        background.extend([asyncio.create_task(telegram_poll()), asyncio.create_task(reminder_loop())])
    elif TOKEN:
        print('Telegram bot token is set but allowed chat ID is empty; bot disabled for safety.', flush=True)
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)

app = FastAPI(title='XROB Assistant API', version='0.4.9', lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=WWW), name='static')

@app.get('/')
def home(): return FileResponse(WWW/'index.html')
@app.get('/api/health')
def health():
    try:
        with connect() as c: c.execute('SELECT 1').fetchone()
        return {'status':'ok','app':'XROB Assistant','version':'0.4.9','database':'connected'}
    except Exception as exc:
        raise HTTPException(503, f'Database check failed: {exc}')

@app.post('/api/daily-meals/restore')
def restore_daily_meal_day(payload: Payload):
    try:
        plan_day = date.fromisoformat(str(payload.data.get('plan_date') or ''))
    except (TypeError, ValueError):
        raise HTTPException(400, 'Choose a valid date.')
    if plan_day < date.today():
        raise HTTPException(400, 'Past dates are read-only in the weekly meal plan.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM meal_schedule_skips WHERE plan_date=?', (plan_day.isoformat(),))
    maintain_meal_schedule()
    return {'plan_date': plan_day.isoformat(), 'restored': True}


@app.get('/api/meal-schedule-skips')
def list_meal_schedule_skips(start_date: str | None = None, end_date: str | None = None):
    try:
        start = date.fromisoformat(start_date) if start_date else date.today()
        end = date.fromisoformat(end_date) if end_date else start + timedelta(days=90)
        if end < start or (end - start).days > 365:
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, 'Choose a valid date range up to 365 days.')
    return rows('SELECT plan_date FROM meal_schedule_skips WHERE plan_date BETWEEN ? AND ? ORDER BY plan_date', (start.isoformat(), end.isoformat()))


@app.get('/api/{resource}')
def list_resource(resource: str, limit: int = Query(250, ge=1, le=10000)):
    if resource == 'summary': return summary()
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,_=TABLES[resource]
    if resource == 'meal_plans':
        return rows('''SELECT mp.*, r.name AS recipe_name, r.meal_type AS recipe_category, r.prep_min, r.cook_min
                       FROM meal_plans mp LEFT JOIN recipes r ON r.id=mp.recipe_id
                       ORDER BY mp.plan_date, CASE mp.meal_type WHEN 'breakfast' THEN 1 WHEN 'lunch' THEN 2 WHEN 'dinner' THEN 3 ELSE 4 END, mp.id LIMIT ?''', (limit,))
    if resource == 'daily_meals':
        return rows('''SELECT dm.*, r.name AS recipe_name, r.calories_kcal, r.ingredients_json,
                              r.eating_interval_days, r.batch_plates, r.meal_source, r.plates_ready
                       FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id
                       ORDER BY dm.plan_date, dm.id LIMIT ?''', (limit,))
    if resource == 'meal_cook_reminders':
        return rows('''SELECT mcr.*,r.name AS recipe_name,r.meal_source FROM meal_cook_reminders mcr
                       JOIN recipes r ON r.id=mcr.recipe_id WHERE mcr.status='open'
                       ORDER BY mcr.due_date,r.name COLLATE NOCASE LIMIT ?''', (limit,))
    if resource == 'recipes':
        result = rows('''SELECT id,name,calories_kcal,eating_interval_days,batch_plates,meal_source,plates_ready,next_routine_date,excluded_weekdays_json,ingredients_json,created_at
                        FROM recipes WHERE is_active=1 ORDER BY meal_source,name COLLATE NOCASE LIMIT ?''', (limit,))
        for item in result:
            item['ingredients'] = _recipe_ingredient_names(item.pop('ingredients_json', '[]'))
            item['excluded_weekdays'] = _decode_excluded_weekdays(item.pop('excluded_weekdays_json', '[]'))
        return result
    if resource == 'shopping_items':
        return rows('SELECT id,name,checked,source FROM shopping_items ORDER BY checked, name COLLATE NOCASE, id DESC LIMIT ?', (limit,))
    return rows(f'SELECT * FROM {table} ORDER BY id DESC LIMIT ?', (limit,))

MEAL_TYPES = ('breakfast', 'lunch', 'dinner', 'snack')
WEEKDAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
WEEKDAY_LOOKUP = {name.casefold(): index for index, name in enumerate(WEEKDAY_NAMES)}

def _decode_excluded_weekdays(raw: Any) -> list[int]:
    """Return normalized Monday=0..Sunday=6 weekday numbers from stored JSON."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    if not isinstance(values, list):
        return []
    result = set()
    for value in values:
        if isinstance(value, str) and value.strip().casefold() in WEEKDAY_LOOKUP:
            result.add(WEEKDAY_LOOKUP[value.strip().casefold()])
            continue
        try:
            day = int(value)
            if str(value).strip() == str(day) and 0 <= day <= 6:
                result.add(day)
        except (TypeError, ValueError):
            continue
    return sorted(result)

def _normalize_excluded_weekdays(raw: Any) -> str:
    """Validate user-supplied weekday names or indices and return canonical JSON."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError('Choose valid weekdays to exclude.')
    if values in (None, ''):
        values = []
    if not isinstance(values, list):
        raise ValueError('Choose valid weekdays to exclude.')
    result = set()
    for value in values:
        if isinstance(value, str) and value.strip().casefold() in WEEKDAY_LOOKUP:
            result.add(WEEKDAY_LOOKUP[value.strip().casefold()])
            continue
        try:
            day = int(value)
            if str(value).strip() != str(day) or not 0 <= day <= 6:
                raise ValueError()
            result.add(day)
        except (TypeError, ValueError):
            raise ValueError('Choose valid weekdays to exclude.')
    if len(result) == 7:
        raise ValueError('A meal must be available on at least one day of the week.')
    return json.dumps(sorted(result))

MEAL_CATEGORIES = {'produce': 'Produce', 'fruit': 'Produce', 'vegetable': 'Produce', 'meat': 'Meat & seafood', 'fish': 'Meat & seafood', 'seafood': 'Meat & seafood', 'dairy': 'Dairy & eggs', 'egg': 'Dairy & eggs', 'eggs': 'Dairy & eggs', 'bread': 'Bakery', 'grain': 'Grains & pasta', 'pasta': 'Grains & pasta', 'rice': 'Grains & pasta', 'spice': 'Spices & seasonings', 'oil': 'Oils & condiments'}

INGREDIENT_AISLES = {
    'Produce': ('tomato','onion','garlic','potato','carrot','cucumber','lettuce','spinach','pepper','zucchini','courgette','eggplant','aubergine','broccoli','cauliflower','lemon','lime','orange','apple','banana','parsley','cilantro','coriander leaves','mint','ginger','mushroom','avocado'),
    'Meat & seafood': ('chicken','beef','lamb','meat','turkey','fish','salmon','tuna','shrimp','prawn','sausage','mince','bacon'),
    'Dairy & eggs': ('milk','cheese','yogurt','yoghurt','butter','cream','egg','mozzarella','feta'),
    'Bakery': ('bread','pita','baguette','bun','croissant','tortilla'),
    'Grains & pasta': ('rice','pasta','noodle','spaghetti','couscous','flour','oat','quinoa','semolina'),
    'Pantry & canned': ('chickpea','lentil','bean','canned','can of','tomato paste','stock','broth','sugar','honey'),
    'Spices & seasonings': ('salt','cumin','paprika','turmeric','cinnamon','peppercorn','chili powder','chilli powder','saffron','oregano','thyme','curry','seasoning'),
    'Oils & condiments': ('oil','vinegar','mayonnaise','mustard','soy sauce','ketchup','harissa','sauce')
}

def _ingredient_category(name: str, fallback: str = 'Other') -> str:
    low = str(name or '').lower()
    # Protect common spice names from the produce keyword "pepper".
    if any(term in low for term in ('black pepper', 'white pepper', 'peppercorn', 'pepper powder')):
        return 'Spices & seasonings'
    if 'bell pepper' in low or 'sweet pepper' in low:
        return 'Produce'
    for aisle, keywords in INGREDIENT_AISLES.items():
        # Accept common plural spellings (onion/onions, tomato/tomatoes,
        # chickpea/chickpeas) so aisle grouping stays useful for natural entries.
        if any(re.search(rf'\b{re.escape(word)}(?:s|es)?\b', low) for word in keywords):
            return aisle
    return fallback or 'Other'


def _decode_ingredients(raw: Any) -> list[dict]:
    try:
        value = json.loads(raw or '[]') if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []

def _clean_ingredient_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get('name', '')
    text = str(value or '').strip().lstrip('-•').strip()
    if not text:
        return ''
    try:
        parsed = _parse_ingredient_line(text)
        text = parsed.get('name', text) if parsed else text
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return re.sub(r'\s+', ' ', text).strip(' ,;')[:120]


def _recipe_ingredient_names(raw: Any) -> list[str]:
    names = []
    for item in _decode_ingredients(raw):
        name = _clean_ingredient_name(item)
        if name and _ingredient_key(name) not in {_ingredient_key(x) for x in names}:
            names.append(name)
    return names


def _parse_quantity(value: str) -> float:
    value = str(value or '').strip().replace(',', '.')
    fractions = {'½': 0.5, '⅓': 1/3, '⅔': 2/3, '¼': 0.25, '¾': 0.75, '⅛': 0.125}
    if value in fractions:
        return fractions[value]
    if '/' in value:
        a, b = value.split('/', 1)
        return float(a) / float(b)
    return float(value)


def _parse_ingredient_line(line: str) -> dict | None:
    text = str(line or '').strip().lstrip('-•').strip()
    if not text:
        return None
    pattern = r'^(?P<qty>\d+(?:[.,]\d+)?|\d+\/\d+|[½⅓⅔¼¾⅛])\s*(?P<unit>kg|g|mg|l|ml|cl|tbsp|tbsps|tablespoons?|tsp|tsps|teaspoons?|cups?|pcs?|pieces?|cloves?|slices?|cans?|packs?|pinch(?:es)?|bunch(?:es)?)?\s+(?P<name>.+)$'
    match = re.match(pattern, text, re.I)
    if match:
        qty = _parse_quantity(match.group('qty'))
        unit = (match.group('unit') or '').lower()
        name = match.group('name').strip()
    else:
        qty, unit, name = 1.0, '', text
    if qty <= 0 or not name:
        raise ValueError(f'Invalid ingredient: {text}')
    unit_map = {'tbsps':'tbsp','tablespoon':'tbsp','tablespoons':'tbsp','tsps':'tsp','teaspoon':'tsp','teaspoons':'tsp','piece':'pcs','pc':'pcs','clove':'cloves','slice':'slices','can':'cans','pack':'packs','bunch':'bunches','pinch':'pinches'}
    unit = unit_map.get(unit, unit)
    category = _ingredient_category(name)
    return {'name': name, 'quantity': round(qty, 3), 'unit': unit, 'category': category}


def _normalize_unit(quantity: float, unit: str) -> tuple[float, str]:
    unit = (unit or '').strip().lower()
    if unit == 'kg': return quantity * 1000, 'g'
    if unit in ('l', 'cl'): return quantity * (1000 if unit == 'l' else 10), 'ml'
    if unit == 'mg': return quantity / 1000, 'g'
    if unit in ('piece', 'pc'): return quantity, 'pcs'
    return quantity, unit


def _ingredient_key(name: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[\W_]+', ' ', str(name or '').lower(), flags=re.UNICODE)).strip()


def _suggest_meals(days: int, meal_types: list[str], start_date: date, dietary: str = '', exclude: str = '', max_prep: int | None = None) -> list[dict]:
    recipes = rows('SELECT * FROM recipes WHERE is_active=1 ORDER BY name COLLATE NOCASE')
    if not recipes:
        return []
    meal_types = [x for x in meal_types if x in MEAL_TYPES]
    if not meal_types:
        raise ValueError('Choose at least one meal type.')
    excluded = {_ingredient_key(x) for x in re.split(r'[,;\n]+', exclude) if _ingredient_key(x)}
    diet_terms = {_ingredient_key(x) for x in re.split(r'[,;]+', dietary) if _ingredient_key(x)}
    recent_rows = rows('SELECT recipe_id, meal_type, COUNT(*) AS uses FROM meal_plans WHERE plan_date>=? GROUP BY recipe_id, meal_type', ((start_date - timedelta(days=14)).isoformat(),))
    recent = {(int(x['recipe_id']), x['meal_type']): int(x['uses']) for x in recent_rows if x['recipe_id'] is not None}
    result = []
    used_in_plan: set[int] = set()
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        for meal_type in meal_types:
            candidates = []
            for recipe in recipes:
                category = (recipe.get('meal_type') or 'any').lower()
                if category not in (meal_type, 'any', 'all'):
                    continue
                prep_total = int(recipe.get('prep_min') or 0) + int(recipe.get('cook_min') or 0)
                if max_prep and prep_total > max_prep:
                    continue
                ingredients = _decode_ingredients(recipe.get('ingredients_json'))
                if excluded and any(any(term in _ingredient_key(i.get('name', '')) for term in excluded) for i in ingredients):
                    continue
                tags = {_ingredient_key(x) for x in re.split(r'[,;]+', recipe.get('dietary_tags') or '') if _ingredient_key(x)}
                if diet_terms and not diet_terms.issubset(tags):
                    continue
                used = int(recipe['id']) in used_in_plan
                recent_uses = recent.get((int(recipe['id']), meal_type), 0)
                exact_match = 0 if category == meal_type else 1
                candidates.append(((used, recent_uses, exact_match, -int(recipe.get('favorite') or 0), prep_total, str(recipe['name']).lower()), recipe))
            if not candidates:
                continue
            candidates.sort(key=lambda pair: pair[0])
            # Randomize equally-ranked candidates so repeated plans don't always favor
            # the same alphabetically-first recipe when other signals are equal.
            best_key = candidates[0][0][:5]
            tied = [r for key, r in candidates if key[:5] == best_key]
            chosen = random.choice(tied)
            used_in_plan.add(int(chosen['id']))
            result.append({'plan_date': day.isoformat(), 'meal_type': meal_type, 'recipe_id': int(chosen['id']),
                           'recipe_name': chosen['name'], 'servings': float(chosen.get('servings') or 2),
                           'reason': ('Matches your ' + meal_type + ' preference') if chosen.get('meal_type') == meal_type else 'Flexible recipe; fits this meal slot'})
    return result


@app.post('/api/meals/suggest')
def suggest_meals(payload: Payload):
    data = payload.data
    try:
        days_raw = data.get('days', 1)
        days = int(days_raw)
        if float(days_raw) != days or not 1 <= days <= 30: raise ValueError()
        start = date.fromisoformat(str(data.get('start_date') or date.today().isoformat()))
        max_prep_raw = data.get('max_prep')
        max_prep = int(max_prep_raw) if max_prep_raw not in (None, '', 0, '0') else None
        if max_prep is not None and (float(max_prep_raw) != max_prep or not 1 <= max_prep <= 1440): raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, 'Choose 1–30 days, a valid start date and a valid maximum preparation time.')
    meal_types = data.get('meal_types') or ['lunch', 'dinner']
    if not isinstance(meal_types, list) or any(x not in MEAL_TYPES for x in meal_types):
        raise HTTPException(400, 'Choose valid meal types.')
    meal_types = list(dict.fromkeys(meal_types))
    try:
        suggestions = _suggest_meals(days, meal_types, start, str(data.get('dietary') or ''), str(data.get('exclude') or ''), max_prep)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    expected = days * len(meal_types)
    return {'suggestions': suggestions, 'requested': expected, 'suggested': len(suggestions), 'missing': expected - len(suggestions)}


@app.post('/api/meals/apply')
def apply_meal_suggestions(payload: Payload):
    data = payload.data
    plan = data.get('plan') or []
    replace_value = data.get('replace_existing', False)
    replace_existing = replace_value is True or str(replace_value).strip().lower() in ('1', 'true', 'yes', 'on')
    if not isinstance(plan, list) or not plan:
        raise HTTPException(400, 'There are no meal suggestions to save.')
    clean = []
    seen_slots = set()
    for item in plan:
        if not isinstance(item, dict): continue
        try:
            day = date.fromisoformat(str(item.get('plan_date')))
            meal_type = str(item.get('meal_type'))
            recipe_id = int(item.get('recipe_id'))
            servings = float(item.get('servings', 2))
            if meal_type not in MEAL_TYPES or not 0.25 <= servings <= 100: raise ValueError()
            if (day.isoformat(), meal_type) in seen_slots: raise ValueError()
            seen_slots.add((day.isoformat(), meal_type))
            if not one('SELECT id FROM recipes WHERE id=?', (recipe_id,)): raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, 'A suggested meal contains an invalid date, category, recipe or serving count.')
        clean.append((day.isoformat(), meal_type, recipe_id, servings, str(item.get('notes') or '')[:1000]))
    if not clean: raise HTTPException(400, 'No valid meal suggestions were supplied.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for day, meal_type, recipe_id, servings, notes in clean:
            existing = c.execute('SELECT id FROM meal_plans WHERE plan_date=? AND meal_type=?', (day, meal_type)).fetchone()
            if existing and not replace_existing:
                raise HTTPException(409, f'A {meal_type} is already planned for {day}. Choose replace existing to overwrite it.')
            c.execute('''INSERT INTO meal_plans(plan_date,meal_type,recipe_id,servings,notes) VALUES(?,?,?,?,?)
                         ON CONFLICT(plan_date,meal_type) DO UPDATE SET recipe_id=excluded.recipe_id, servings=excluded.servings, notes=excluded.notes''',
                      (day, meal_type, recipe_id, servings, notes))
    return {'saved': len(clean)}


def _ensure_cook_reminder(c: sqlite3.Connection, recipe_id: int, due_date: date) -> None:
    c.execute("INSERT OR IGNORE INTO meal_cook_reminders(recipe_id,due_date,status) VALUES(?,?,'open')",
              (recipe_id, due_date.isoformat()))


def _recalculate_recipe_due(c: sqlite3.Connection, recipe_id: int) -> None:
    row = c.execute('SELECT eating_interval_days FROM recipes WHERE id=?', (recipe_id,)).fetchone()
    if not row:
        return
    latest = c.execute('SELECT MAX(plan_date) AS last_date FROM daily_meals WHERE recipe_id=?', (recipe_id,)).fetchone()
    next_due = (date.fromisoformat(latest['last_date']) + timedelta(days=max(1, int(row['eating_interval_days'] or 1)))).isoformat() if latest and latest['last_date'] else date.today().isoformat()
    c.execute('UPDATE recipes SET next_routine_date=? WHERE id=?', (next_due, recipe_id))


def _sync_home_stock(c: sqlite3.Connection, recipe_id: int, plates_ready: int) -> None:
    recipe = c.execute('SELECT meal_source,eating_interval_days FROM recipes WHERE id=?', (recipe_id,)).fetchone()
    if not recipe:
        raise ValueError('Meal not found.')
    if recipe['meal_source'] == 'outside':
        plates_ready = 0
    c.execute('UPDATE recipes SET plates_ready=? WHERE id=?', (plates_ready, recipe_id))
    if recipe['meal_source'] == 'outside':
        c.execute("UPDATE meal_cook_reminders SET status='done' WHERE recipe_id=? AND status='open'", (recipe_id,))
        return
    # A stock reduction cannot leave more promised plates than are physically ready.
    pending = c.execute("SELECT id FROM daily_meals WHERE recipe_id=? AND status='pending' ORDER BY plan_date DESC,id DESC", (recipe_id,)).fetchall()
    removed_any = False
    for row in pending[max(0, plates_ready):]:
        c.execute("DELETE FROM daily_meals WHERE id=?", (int(row['id']),))
        removed_any = True
    if removed_any:
        _recalculate_recipe_due(c, recipe_id)
    if plates_ready > 0:
        c.execute("UPDATE meal_cook_reminders SET status='done' WHERE recipe_id=? AND status='open'", (recipe_id,))
        _recalculate_recipe_due(c, recipe_id)
    else:
        _ensure_cook_reminder(c, recipe_id, date.today() + timedelta(days=1))


def _shift_pending_meal_sequence(c: sqlite3.Connection, moving_rows: list[dict], base_delta: int, first_target: date) -> dict[int, date]:
    """Shift an ordered pending sequence forward without violating closures or explicit day-offs."""
    ordered = sorted((dict(row) for row in moving_rows), key=lambda row: (row['plan_date'], int(row['id'])))
    if not ordered:
        return {}
    moving_ids = {int(row['id']) for row in ordered}
    marks = ','.join('?' for _ in moving_ids)
    fixed_dates = {row['plan_date'] for row in c.execute(
        f"SELECT plan_date FROM daily_meals WHERE id NOT IN ({marks})", tuple(moving_ids)).fetchall()}
    skipped_dates = {row['plan_date'] for row in c.execute('SELECT plan_date FROM meal_schedule_skips').fetchall()}
    excluded_by_recipe: dict[int, set[int]] = {}
    for recipe_id in {int(row['recipe_id']) for row in ordered}:
        recipe = c.execute('SELECT excluded_weekdays_json FROM recipes WHERE id=?', (recipe_id,)).fetchone()
        excluded_by_recipe[recipe_id] = set(_decode_excluded_weekdays(recipe['excluded_weekdays_json'] if recipe else '[]'))

    destinations: dict[int, date] = {}
    previous_target: date | None = None
    for index, row in enumerate(ordered):
        original = date.fromisoformat(row['plan_date'])
        candidate = max(original + timedelta(days=base_delta), first_target)
        if previous_target is not None:
            candidate = max(candidate, previous_target + timedelta(days=1))
        excluded = excluded_by_recipe.get(int(row['recipe_id']), set())
        for _ in range(3660):
            iso = candidate.isoformat()
            if candidate.weekday() not in excluded and iso not in skipped_dates and iso not in fixed_dates:
                break
            candidate += timedelta(days=1)
        else:
            raise ValueError('Could not find an available date for the pending meal sequence. Review weekday exclusions and skipped days.')
        destinations[int(row['id'])] = candidate
        previous_target = candidate

    # Update in reverse chronological order so the UNIQUE(plan_date) constraint
    # never collides with a later row that has not yet moved out of its old date.
    for row in reversed(ordered):
        meal_id = int(row['id'])
        c.execute('''UPDATE daily_meals SET plan_date=?,notified_date=NULL,
            suggestion_notified_date=NULL,checkin_notified_date=NULL WHERE id=? AND status='pending' ''',
            (destinations[meal_id].isoformat(), meal_id))
    for recipe_id in excluded_by_recipe:
        _recalculate_recipe_due(c, recipe_id)
    return destinations


def _generate_daily_meals(days: int, start: date) -> dict:
    if not 1 <= int(days) <= 90:
        raise ValueError('Choose between 1 and 90 days.')
    if start < date.today():
        raise ValueError('The meal plan cannot start in the past. Choose today or a future date.')
    created = []
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        recipes = c.execute("SELECT id FROM recipes WHERE is_active=1").fetchall()
        if not recipes:
            return {'created': 0, 'meals': [], 'message': 'Add at least one meal first.'}
        skipped_dates = {row['plan_date'] for row in c.execute('SELECT plan_date FROM meal_schedule_skips WHERE plan_date>=?', (start.isoformat(),)).fetchall()}
        for offset in range(int(days)):
            day = start + timedelta(days=offset)
            day_iso = day.isoformat()
            if day_iso in skipped_dates or c.execute('SELECT 1 FROM daily_meals WHERE plan_date=?', (day_iso,)).fetchone():
                continue
            candidates = c.execute('''SELECT r.*,COUNT(dm.id) AS reserved_plates
                FROM recipes r LEFT JOIN daily_meals dm ON dm.recipe_id=r.id AND dm.status='pending'
                WHERE r.is_active=1 AND COALESCE(r.next_routine_date,date('now'))<=?
                  GROUP BY r.id
                HAVING r.meal_source='outside' OR r.plates_ready>COUNT(dm.id)
                ORDER BY COALESCE(r.next_routine_date,date('now')),
                         CASE r.meal_source WHEN 'home' THEN 0 ELSE 1 END,
                         r.name COLLATE NOCASE,r.id''', (day_iso,)).fetchall()
            eligible = next((candidate for candidate in candidates
                             if day.weekday() not in _decode_excluded_weekdays(candidate['excluded_weekdays_json'])), None)
            if not eligible:
                continue
            cur = c.execute("INSERT INTO daily_meals(plan_date,recipe_id,batch_id,status) VALUES(?,?,NULL,'pending')",
                            (day_iso, int(eligible['id'])))
            next_due = day + timedelta(days=max(1, int(eligible['eating_interval_days'] or 1)))
            c.execute('UPDATE recipes SET next_routine_date=? WHERE id=?', (next_due.isoformat(), int(eligible['id'])))
            created.append({'id': int(cur.lastrowid), 'plan_date': day_iso, 'recipe_id': int(eligible['id']),
                            'recipe_name': eligible['name'], 'calories_kcal': eligible['calories_kcal'], 'status': 'pending'})
        meals = [dict(r) for r in c.execute('''SELECT dm.*,r.name AS recipe_name,r.calories_kcal,r.ingredients_json,r.meal_source,r.plates_ready
            FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id
            WHERE dm.plan_date BETWEEN ? AND ? ORDER BY dm.plan_date''',
            (start.isoformat(), (start+timedelta(days=days-1)).isoformat())).fetchall()]
    for meal in meals:
        meal['ingredients'] = _recipe_ingredient_names(meal.pop('ingredients_json', '[]'))
    return {'created': len(created), 'meals': meals, 'message': f'{len(created)} daily plate(s) planned.'}


def _roll_forward_unconfirmed_meals(today: date) -> int:
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        overdue = c.execute("SELECT id,plan_date,recipe_id FROM daily_meals WHERE status='pending' AND plan_date<? ORDER BY plan_date,id LIMIT 1", (today.isoformat(),)).fetchone()
        if not overdue:
            return 0
        old_day = date.fromisoformat(overdue['plan_date'])
        delta = (today - old_day).days
        later = c.execute("SELECT id,plan_date,recipe_id FROM daily_meals WHERE plan_date>? AND status='pending' ORDER BY plan_date,id", (old_day.isoformat(),)).fetchall()
        moving = [dict(overdue)] + [dict(row) for row in later]
        _shift_pending_meal_sequence(c, moving, delta, today)
        return 1

def maintain_meal_schedule() -> dict:
    today = date.today()
    rolled = _roll_forward_unconfirmed_meals(today)
    result = _generate_daily_meals(45, today)
    result['rolled_forward'] = rolled
    return result


async def meal_schedule_loop():
    while True:
        try:
            result = maintain_meal_schedule()
            if result.get('created') or result.get('rolled_forward'):
                print(f"Meal schedule updated: {result.get('created', 0)} new plate(s), {result.get('rolled_forward', 0)} overdue plate(s) rolled forward.", flush=True)
        except Exception as exc:
            print(f'Meal schedule maintenance error: {exc}', flush=True)
        await asyncio.sleep(300)


def _confirm_daily_meal(meal_id: int, eaten: bool) -> dict:
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        meal = c.execute('SELECT * FROM daily_meals WHERE id=?', (meal_id,)).fetchone()
        if not meal:
            raise ValueError('Meal not found.')
        if meal['status'] != 'pending':
            return {**dict(meal), 'shift_days': 0}
        recipe_id = int(meal['recipe_id'])
        if eaten:
            c.execute("UPDATE daily_meals SET status='eaten',confirmed_at=?,notified_date=?,checkin_notified_date=? WHERE id=?",
                      (datetime.now().isoformat(timespec='seconds'), date.today().isoformat(), date.today().isoformat(), meal_id))
            recipe = c.execute('SELECT meal_source,plates_ready FROM recipes WHERE id=?', (recipe_id,)).fetchone()
            if recipe and recipe['meal_source'] == 'home':
                remaining = max(0, int(recipe['plates_ready'] or 0) - 1)
                c.execute('UPDATE recipes SET plates_ready=? WHERE id=?', (remaining, recipe_id))
                pending_left = c.execute("SELECT COUNT(*) FROM daily_meals WHERE recipe_id=? AND status='pending'", (recipe_id,)).fetchone()[0]
                if remaining == 0 and pending_left == 0:
                    _ensure_cook_reminder(c, recipe_id, date.today() + timedelta(days=1))
                elif remaining > 0:
                    c.execute("UPDATE meal_cook_reminders SET status='done' WHERE recipe_id=? AND status='open'", (recipe_id,))
        else:
            old_day = date.fromisoformat(meal['plan_date'])
            destination = max(old_day + timedelta(days=1), date.today() + timedelta(days=1))
            delta = (destination - old_day).days
            later = c.execute("SELECT id,plan_date,recipe_id FROM daily_meals WHERE plan_date>? AND status='pending' ORDER BY plan_date,id",
                              (old_day.isoformat(),)).fetchall()
            moving = [dict(meal)] + [dict(row) for row in later]
            destinations = _shift_pending_meal_sequence(c, moving, delta, destination)
            delta = (destinations[int(meal_id)] - old_day).days
        if eaten:
            _recalculate_recipe_due(c, recipe_id)
        updated = c.execute('SELECT * FROM daily_meals WHERE id=?', (meal_id,)).fetchone()
        return {**dict(updated), 'shift_days': 0 if eaten else delta}


@app.post('/api/recipes/{recipe_id}/stock')
def update_recipe_stock(recipe_id: int, payload: Payload):
    try:
        raw = payload.data.get('plates_ready')
        value = int(raw)
        if str(raw).strip() != str(value) or not 0 <= value <= 1000:
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, 'Ready plates must be a whole number from 0 to 1000.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        recipe = c.execute('SELECT id,meal_source FROM recipes WHERE id=? AND is_active=1', (recipe_id,)).fetchone()
        if not recipe:
            raise HTTPException(404, 'Meal not found.')
        if recipe['meal_source'] != 'home':
            raise HTTPException(400, 'Only Home meals have ready-plate stock.')
        _sync_home_stock(c, recipe_id, value)
    maintain_meal_schedule()
    return one('SELECT id,name,meal_source,plates_ready,next_routine_date FROM recipes WHERE id=?', (recipe_id,))


def _select_daily_meal(recipe_id: int, plan_day: date) -> dict:
    """Manually choose one active meal for a date without consuming stock until eaten."""
    # First roll any overdue pending plate forward so a late scheduler tick cannot
    # collide with the manually chosen date's UNIQUE daily_meals slot.
    _roll_forward_unconfirmed_meals(date.today())
    if plan_day < date.today():
        raise ValueError('A meal can only be selected for today or a future date.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT id,recipe_id,status FROM daily_meals WHERE plan_date=?', (plan_day.isoformat(),)).fetchone()
        if existing:
            raise ValueError('A plate is already scheduled for this date. Remove or move it before choosing another.')
        recipe = c.execute("SELECT id,name,meal_source,plates_ready,eating_interval_days,calories_kcal FROM recipes WHERE id=? AND is_active=1", (recipe_id,)).fetchone()
        if not recipe:
            raise ValueError('This meal is no longer available. Refresh the Meals page and choose another.')
        recipe_full = c.execute('SELECT excluded_weekdays_json FROM recipes WHERE id=?', (recipe_id,)).fetchone()
        if plan_day.weekday() in _decode_excluded_weekdays(recipe_full['excluded_weekdays_json'] if recipe_full else '[]'):
            raise ValueError(f"{recipe['name']} is not available on {WEEKDAY_NAMES[plan_day.weekday()]}s. Choose another meal.")
        if recipe['meal_source'] == 'home':
            reserved = int(c.execute("SELECT COUNT(*) FROM daily_meals WHERE recipe_id=? AND status='pending'", (recipe_id,)).fetchone()[0])
            ready = max(0, int(recipe['plates_ready'] or 0))
            if ready <= reserved:
                raise ValueError(f"{recipe['name']} has no unreserved plates ready. Update its stock first.")
        cursor = c.execute("INSERT INTO daily_meals(plan_date,recipe_id,batch_id,status) VALUES(?,?,NULL,'pending')",
                           (plan_day.isoformat(), recipe_id))
        c.execute('DELETE FROM meal_schedule_skips WHERE plan_date=?', (plan_day.isoformat(),))
        _recalculate_recipe_due(c, recipe_id)
        result = dict(recipe)
        result.update({'id': int(cursor.lastrowid), 'plan_date': plan_day.isoformat(), 'status': 'pending'})
    return result


@app.post('/api/daily-meals/select')
def select_daily_meal(payload: Payload):
    try:
        recipe_id = int(payload.data.get('recipe_id'))
        raw_day = str(payload.data.get('plan_date') or date.today().isoformat())
        plan_day = date.fromisoformat(raw_day)
        result = _select_daily_meal(recipe_id, plan_day)
        maintain_meal_schedule()
        result['message'] = 'Meal selected for ' + plan_day.isoformat() + '.'
        return result
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc) or 'Choose a valid meal.')


def _assign_or_skip_daily_meal(plan_day: date, recipe_id: int | None) -> dict:
    """Assign/replace one pending plate, or deliberately leave a day without a plate."""
    _roll_forward_unconfirmed_meals(date.today())
    if plan_day < date.today():
        raise ValueError('Past dates are read-only in the weekly meal plan.')
    day_iso = plan_day.isoformat()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT id,recipe_id,status FROM daily_meals WHERE plan_date=?', (day_iso,)).fetchone()
        if current and current['status'] != 'pending':
            raise ValueError('This plate is already confirmed as eaten and cannot be changed.')
        old_recipe_id = int(current['recipe_id']) if current else None
        if recipe_id is None:
            if current:
                c.execute("DELETE FROM daily_meals WHERE id=? AND status='pending'", (int(current['id']),))
            c.execute('INSERT OR IGNORE INTO meal_schedule_skips(plan_date) VALUES(?)', (day_iso,))
            if old_recipe_id:
                _recalculate_recipe_due(c, old_recipe_id)
            result = {'plan_date': day_iso, 'skipped': True, 'recipe_id': None}
        else:
            recipe = c.execute('SELECT id,name,meal_source,plates_ready,excluded_weekdays_json FROM recipes WHERE id=? AND is_active=1', (recipe_id,)).fetchone()
            if not recipe:
                raise ValueError('This meal option is no longer available. Refresh the page and try again.')
            if plan_day.weekday() in _decode_excluded_weekdays(recipe['excluded_weekdays_json']):
                raise ValueError(f"{recipe['name']} is excluded on {WEEKDAY_NAMES[plan_day.weekday()]}s.")
            if recipe['meal_source'] == 'home':
                reserved = c.execute("SELECT COUNT(*) FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date!=?", (recipe_id, day_iso)).fetchone()[0]
                if int(recipe['plates_ready'] or 0) <= int(reserved):
                    raise ValueError(f"{recipe['name']} has no unreserved plates ready. Update its stock first.")
            if current:
                c.execute("UPDATE daily_meals SET recipe_id=?,batch_id=NULL,notified_date=NULL,suggestion_notified_date=NULL,checkin_notified_date=NULL WHERE id=? AND status='pending'", (recipe_id, int(current['id'])))
                meal_id = int(current['id'])
            else:
                cursor = c.execute("INSERT INTO daily_meals(plan_date,recipe_id,batch_id,status) VALUES(?,?,NULL,'pending')", (day_iso, recipe_id))
                meal_id = int(cursor.lastrowid)
            c.execute('DELETE FROM meal_schedule_skips WHERE plan_date=?', (day_iso,))
            if old_recipe_id and old_recipe_id != recipe_id:
                _recalculate_recipe_due(c, old_recipe_id)
            _recalculate_recipe_due(c, recipe_id)
            result = {'id': meal_id, 'plan_date': day_iso, 'recipe_id': recipe_id, 'recipe_name': recipe['name'], 'meal_source': recipe['meal_source'], 'status': 'pending', 'skipped': False}
    maintain_meal_schedule()
    return result


@app.post('/api/daily-meals/assign')
def assign_daily_meal(payload: Payload):
    data = payload.data
    try:
        plan_day = date.fromisoformat(str(data.get('plan_date') or ''))
        raw_recipe_id = data.get('recipe_id')
        recipe_id = None if raw_recipe_id in (None, '', 0, '0') else int(raw_recipe_id)
        return _assign_or_skip_daily_meal(plan_day, recipe_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc) or 'Choose a valid date and meal.')


@app.post('/api/daily-meals/generate')
def generate_daily_meals(payload: Payload):
    data = payload.data
    try:
        days_raw = data.get('days', 7)
        days = int(days_raw)
        if float(days_raw) != days:
            raise ValueError('Choose a whole number of days.')
        start = date.fromisoformat(str(data.get('start_date') or date.today().isoformat()))
        return _generate_daily_meals(days, start)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc) or 'Enter a valid meal planning range.')


@app.post('/api/daily-meals/{meal_id}/confirm')
def confirm_daily_meal(meal_id: int, payload: Payload):
    decision = str(payload.data.get('decision') or '').lower()
    if decision not in ('yes', 'no', 'eaten', 'not_today'):
        raise HTTPException(400, 'Decision must be yes or no.')
    try:
        return _confirm_daily_meal(meal_id, decision in ('yes', 'eaten'))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post('/api/meals/shopping-list')
def add_meal_ingredients_to_shopping(payload: Payload):
    data = payload.data
    start = validate_date_field(data.get('start_date') or date.today().isoformat(), 'Start date')
    end = validate_date_field(data.get('end_date') or (date.today()+timedelta(days=30)).isoformat(), 'End date')
    if not start or not end or end < start or (date.fromisoformat(end)-date.fromisoformat(start)).days > 89:
        raise HTTPException(400, 'Choose a valid shopping-list date range (maximum 90 days).')
    meal_rows = rows('''SELECT r.ingredients_json FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id
                        WHERE dm.plan_date BETWEEN ? AND ? AND dm.status='pending' ''', (start, end))
    names = {}
    for meal in meal_rows:
        for name in _recipe_ingredient_names(meal.get('ingredients_json')):
            names.setdefault(_ingredient_key(name), name)
    if not names:
        raise HTTPException(400, 'No ingredients found in the planned meals for this date range.')
    added = 0
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = {_ingredient_key(r['name']) for r in c.execute('SELECT name FROM shopping_items WHERE checked=0').fetchall()}
        for key, name in names.items():
            if key in existing:
                continue
            c.execute("INSERT INTO shopping_items(name,quantity,unit,category,checked,source) VALUES(?,1,'','',0,'meal plan')", (name,))
            existing.add(key)
            added += 1
    return {'items_processed': len(names), 'new_items': added, 'meals': len(meal_rows), 'idempotent': True}


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


def normalize_routine_rollover(data: dict[str, Any], existing: dict[str, Any] | None = None) -> None:
    """Allow automatic next-day rollover only when a late reminder is configured."""
    existing = existing or {}
    late_time = data.get('routine_late_time', existing.get('routine_late_time'))
    raw = data.get('routine_rollover_after_late', existing.get('routine_rollover_after_late', 0))
    enabled = str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
    data['routine_rollover_after_late'] = 1 if late_time and enabled else 0


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
                rollover_note = '\nStill incomplete after this alert? It will move to tomorrow automatically.' if int(routine.get('routine_rollover_after_late') or 0) else ''
                reminders.append(('routine-late', routine['id'], today.isoformat(), f"⚠️ ROUTINE STILL OPEN\n{name}\nOverdue: {days_late} day{'s' if days_late != 1 else ''}{scheduled}{rollover_note}\nReply when finished: done {name}"))
        except Exception as exc:
            print(f'Routine reminder skipped for {routine.get("name", routine.get("id"))}: {exc}', flush=True)
    return reminders


def roll_forward_late_routine(routine_id: int, reminder_day: date) -> bool:
    """Move an uncompleted opted-in routine to tomorrow after its late alert is delivered."""
    tomorrow = (reminder_day + timedelta(days=1)).isoformat()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT next_due,routine_late_time,routine_rollover_after_late FROM routines WHERE id=?', (routine_id,)).fetchone()
        if not current or not current['routine_late_time'] or not int(current['routine_rollover_after_late'] or 0):
            return False
        try:
            due = date.fromisoformat(current['next_due']) if current['next_due'] else None
        except (TypeError, ValueError):
            return False
        # A completion from the website or Telegram changes next_due. Only roll
        # forward if it is still overdue; never overwrite a newer completion.
        if due is None or due >= reminder_day:
            return False
        cur = c.execute('UPDATE routines SET next_due=? WHERE id=? AND next_due=?', (tomorrow, routine_id, current['next_due']))
        return cur.rowcount == 1

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
        normalize_routine_rollover(data)
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
    if resource == 'recipes':
        data.setdefault('meal_source', 'home')
        try:
            data['excluded_weekdays_json'] = _normalize_excluded_weekdays(data.get('excluded_weekdays_json', []))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if data['meal_source'] not in ('home','outside'):
            raise HTTPException(400, 'Meal type must be home or outside.')
        data.setdefault('plates_ready', data.get('batch_plates', 1) if data['meal_source']=='home' else 0)
        try:
            raw_stock = float(data['plates_ready'])
            stock = int(raw_stock)
            if not math.isfinite(raw_stock) or raw_stock != stock or not 0 <= stock <= 1000:
                raise ValueError()
            data['plates_ready'] = stock
        except (TypeError, ValueError):
            raise HTTPException(400, 'Ready plates must be a whole number from 0 to 1000.')
        if data['meal_source']=='outside': data['plates_ready']=0
        data.setdefault('next_routine_date', date.today().isoformat())
        name = str(data.get('name') or '').strip()
        if not name:
            raise HTTPException(400, 'Meal name is required.')
        data['name'] = name[:160]
        raw_kcal = data.get('calories_kcal')
        if raw_kcal in ('', None):
            data['calories_kcal'] = None
        else:
            try:
                kcal = float(raw_kcal)
                if not math.isfinite(kcal) or not 0 <= kcal <= 10000:
                    raise ValueError()
                data['calories_kcal'] = kcal
            except (TypeError, ValueError):
                raise HTTPException(400, 'Calories per plate must be between 0 and 10000 kcal.')
        try:
            interval_raw = float(data.get('eating_interval_days', 1))
            interval = int(interval_raw)
            plates_raw = float(data.get('batch_plates', 7))
            plates = int(plates_raw)
            if interval_raw != interval or not 1 <= interval <= 90:
                raise ValueError()
            if plates_raw != plates or not 1 <= plates <= 1000:
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, 'Eating interval must be 1–90 whole days and batch plates must be 1–1000.')
        data['eating_interval_days'] = interval
        data['batch_plates'] = plates
        try:
            raw_ingredients = data.get('ingredients_json', '[]')
            ingredients = json.loads(raw_ingredients) if isinstance(raw_ingredients, str) else raw_ingredients
            if not isinstance(ingredients, list):
                raise ValueError()
            clean = []
            seen = set()
            for ingredient in ingredients:
                name_part = _clean_ingredient_name(ingredient)
                key = _ingredient_key(name_part)
                if name_part and key not in seen:
                    clean.append(name_part)
                    seen.add(key)
            if not clean and data['meal_source'] == 'home':
                raise ValueError()
            data['ingredients_json'] = json.dumps(clean, ensure_ascii=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise HTTPException(400, 'Home meals need at least one valid ingredient name. Ingredients are optional for Outside meals.')
    if resource == 'meal_plans':
        data['plan_date'] = validate_date_field(data.get('plan_date'), 'Meal date')
        if not data.get('plan_date'): raise HTTPException(400, 'Meal date is required.')
        if data.get('meal_type') not in MEAL_TYPES: raise HTTPException(400, 'Choose breakfast, lunch, dinner or snack.')
        try:
            recipe_id = int(data.get('recipe_id'))
            servings = float(data.get('servings', 2))
            if not one('SELECT id FROM recipes WHERE id=?', (recipe_id,)) or not 0.25 <= servings <= 100: raise ValueError()
            data['recipe_id'] = recipe_id; data['servings'] = servings
        except (TypeError, ValueError): raise HTTPException(400, 'Choose a saved recipe and a valid serving count.')
    if resource == 'shopping_items':
        data['name'] = str(data.get('name') or '').strip()[:160]
        if not data['name']:
            raise HTTPException(400, 'Shopping item name is required.')
        data['checked'] = 1 if str(data.get('checked', 0)).lower() in ('1','true','yes','on') else 0
        data.setdefault('source', 'manual')
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
    result = one(f'SELECT * FROM {table} WHERE id=?',(new_id,))
    if resource == 'recipes' and result:
        if result.get('meal_source') == 'home' and int(result.get('plates_ready') or 0) == 0:
            with connect() as c: _ensure_cook_reminder(c, int(new_id), date.today()+timedelta(days=1))
        maintain_meal_schedule()
        result = {k: result.get(k) for k in ('id','name','calories_kcal','eating_interval_days','batch_plates','meal_source','plates_ready','next_routine_date','created_at')}
        result['excluded_weekdays'] = _decode_excluded_weekdays(one('SELECT excluded_weekdays_json FROM recipes WHERE id=?',(new_id,))['excluded_weekdays_json'])
        result['ingredients'] = _recipe_ingredient_names(one('SELECT ingredients_json FROM recipes WHERE id=?',(new_id,))['ingredients_json'])
    return result

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
        normalize_routine_rollover(data, existing_routine)
    if resource == 'recipes':
        existing_recipe = one('SELECT * FROM recipes WHERE id=?', (item_id,))
        if not existing_recipe:
            raise HTTPException(404, 'Meal not found.')
        if 'meal_source' in data and data['meal_source'] not in ('home','outside'):
            raise HTTPException(400, 'Meal type must be home or outside.')
        if 'excluded_weekdays_json' in data:
            try:
                data['excluded_weekdays_json'] = _normalize_excluded_weekdays(data['excluded_weekdays_json'])
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if data.get('meal_source') == 'outside':
            data['plates_ready'] = 0
        if 'plates_ready' in data:
            try:
                raw_stock=float(data['plates_ready']); stock=int(raw_stock)
                if raw_stock != stock or not 0 <= stock <= 1000: raise ValueError()
                data['plates_ready']=stock
            except (TypeError,ValueError): raise HTTPException(400, 'Ready plates must be a whole number from 0 to 1000.')
        if 'name' in data:
            data['name'] = str(data['name'] or '').strip()[:160]
            if not data['name']:
                raise HTTPException(400, 'Meal name is required.')
        if 'calories_kcal' in data:
            raw_kcal = data['calories_kcal']
            if raw_kcal in ('', None):
                data['calories_kcal'] = None
            else:
                try:
                    kcal = float(raw_kcal)
                    if not math.isfinite(kcal) or not 0 <= kcal <= 10000:
                        raise ValueError()
                    data['calories_kcal'] = kcal
                except (TypeError, ValueError):
                    raise HTTPException(400, 'Calories per plate must be between 0 and 10000 kcal.')
        for field, default, low, high in (('eating_interval_days', 1, 1, 90), ('batch_plates', 7, 1, 1000)):
            if field in data:
                try:
                    raw_value = float(data[field])
                    value = int(raw_value)
                    if raw_value != value or not low <= value <= high:
                        raise ValueError()
                    data[field] = value
                except (TypeError, ValueError):
                    raise HTTPException(400, f'{field.replace("_", " ").title()} must be a whole number from {low} to {high}.')
        if 'ingredients_json' in data:
            try:
                raw_ingredients = data['ingredients_json']
                ingredients = json.loads(raw_ingredients) if isinstance(raw_ingredients, str) else raw_ingredients
                if not isinstance(ingredients, list):
                    raise ValueError()
                clean = []
                seen = set()
                for ingredient in ingredients:
                    name_part = _clean_ingredient_name(ingredient)
                    key = _ingredient_key(name_part)
                    if name_part and key not in seen:
                        clean.append(name_part)
                        seen.add(key)
                effective_source = data.get('meal_source', existing_recipe.get('meal_source', 'home'))
                if not clean and effective_source == 'home':
                    raise ValueError()
                data['ingredients_json'] = json.dumps(clean, ensure_ascii=False)
            except (TypeError, ValueError, json.JSONDecodeError):
                raise HTTPException(400, 'Home meals need at least one valid ingredient name. Ingredients are optional for Outside meals.')
    if resource == 'meal_plans':
        if 'plan_date' in data: data['plan_date'] = validate_date_field(data['plan_date'], 'Meal date')
        if 'meal_type' in data and data['meal_type'] not in MEAL_TYPES: raise HTTPException(400, 'Choose breakfast, lunch, dinner or snack.')
        if 'recipe_id' in data:
            try:
                data['recipe_id'] = int(data['recipe_id'])
                if not one('SELECT id FROM recipes WHERE id=?', (data['recipe_id'],)): raise ValueError()
            except (TypeError, ValueError): raise HTTPException(400, 'Choose a saved recipe.')
        if 'servings' in data:
            try:
                data['servings'] = float(data['servings'])
                if not 0.25 <= data['servings'] <= 100: raise ValueError()
            except (TypeError, ValueError): raise HTTPException(400, 'Servings must be between 0.25 and 100.')
    if resource == 'shopping_items':
        if 'name' in data:
            data['name'] = str(data['name'] or '').strip()[:160]
            if not data['name']:
                raise HTTPException(400, 'Shopping item name is required.')
        if 'checked' in data:
            data['checked'] = 1 if str(data['checked']).lower() in ('1','true','yes','on') else 0
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
        if resource == 'recipes' and 'excluded_weekdays_json' in data:
            excluded = set(_decode_excluded_weekdays(data['excluded_weekdays_json']))
            pending = c.execute("SELECT id,plan_date FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>=?", (item_id, date.today().isoformat())).fetchall()
            for scheduled in pending:
                try:
                    if date.fromisoformat(scheduled['plan_date']).weekday() in excluded:
                        c.execute("DELETE FROM daily_meals WHERE id=? AND status='pending'", (int(scheduled['id']),))
                except ValueError:
                    continue
            if pending:
                _recalculate_recipe_due(c, item_id)
    result = one(f'SELECT * FROM {table} WHERE id=?',(item_id,))
    if resource == 'recipes' and result:
        if 'meal_source' in data or 'plates_ready' in data:
            with connect() as c:
                current = c.execute('SELECT meal_source,plates_ready FROM recipes WHERE id=?', (item_id,)).fetchone()
                if current and current['meal_source']=='home': _sync_home_stock(c, item_id, int(current['plates_ready'] or 0))
                elif current: c.execute("UPDATE meal_cook_reminders SET status='done' WHERE recipe_id=? AND status='open'", (item_id,))
        maintain_meal_schedule()
        result = {k: result.get(k) for k in ('id','name','calories_kcal','eating_interval_days','batch_plates','meal_source','plates_ready','next_routine_date','created_at')}
        result['excluded_weekdays'] = _decode_excluded_weekdays(one('SELECT excluded_weekdays_json FROM recipes WHERE id=?',(item_id,))['excluded_weekdays_json'])
        ingredients_row = one('SELECT ingredients_json FROM recipes WHERE id=?',(item_id,))
        result['ingredients'] = _recipe_ingredient_names(ingredients_row['ingredients_json'] if ingredients_row else '[]')
    return result

def _remove_recipe_option(item_id: int) -> dict:
    """Remove a meal option while preserving confirmed meal history."""
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        recipe = c.execute('SELECT id,name,is_active FROM recipes WHERE id=?', (item_id,)).fetchone()
        if not recipe or not int(recipe['is_active'] or 0):
            raise ValueError('Meal option not found.')
        c.execute("DELETE FROM daily_meals WHERE recipe_id=? AND status='pending'", (item_id,))
        c.execute("UPDATE meal_cook_reminders SET status='cancelled' WHERE recipe_id=? AND status='open'", (item_id,))
        has_history = c.execute("SELECT 1 FROM daily_meals WHERE recipe_id=? AND status='eaten' LIMIT 1", (item_id,)).fetchone()
        has_legacy_plan = c.execute('SELECT 1 FROM meal_plans WHERE recipe_id=? LIMIT 1', (item_id,)).fetchone()
        history_preserved = bool(has_history or has_legacy_plan)
        if history_preserved:
            c.execute('UPDATE recipes SET is_active=0,plates_ready=0 WHERE id=?', (item_id,))
        else:
            c.execute('DELETE FROM recipes WHERE id=?', (item_id,))
    maintain_meal_schedule()
    return {
        'deleted': True,
        'id': item_id,
        'name': recipe['name'],
        'history_preserved': history_preserved,
        'message': 'Meal option removed. Confirmed eating history was kept.' if history_preserved else 'Meal option deleted.'
    }


@app.delete('/api/{resource}/{item_id}')
def delete_resource(resource: str, item_id: int):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,_=TABLES[resource]
    if resource == 'recipes':
        try:
            return _remove_recipe_option(item_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
    refresh_meal_schedule = resource == 'daily_meals'
    with connect() as c:
        if resource == 'daily_meals':
            meal = c.execute('SELECT id,batch_id,status,plan_date,recipe_id FROM daily_meals WHERE id=?', (item_id,)).fetchone()
            if not meal:
                raise HTTPException(404, 'Plate not found.')
            if meal['status'] != 'pending':
                raise HTTPException(409, 'A confirmed eaten plate is part of your history and cannot be removed.')
            cur = c.execute("DELETE FROM daily_meals WHERE id=? AND status='pending'", (item_id,))
            # A manual removal means the user wants this date left open; persist
            # that choice so the automatic scheduler does not immediately refill it.
            c.execute('INSERT OR IGNORE INTO meal_schedule_skips(plan_date) VALUES(?)', (meal['plan_date'],))
            # Removing a pending plate is a schedule cancellation, not an eaten
            # plate: keep kitchen stock unchanged and preserve confirmed history.
            if meal['batch_id'] is not None:
                batch_id = int(meal['batch_id'])
                c.execute("""UPDATE meal_batches
                             SET plates_scheduled=MAX(0,plates_scheduled-1),
                                 last_scheduled_date=(SELECT MAX(plan_date) FROM daily_meals WHERE batch_id=?)
                             WHERE id=?""", (batch_id, batch_id))
            recipe = c.execute('SELECT eating_interval_days,next_routine_date FROM recipes WHERE id=?', (int(meal['recipe_id']),)).fetchone()
            if recipe:
                min_next = (date.fromisoformat(meal['plan_date']) + timedelta(days=max(1,int(recipe['eating_interval_days'] or 1)))).isoformat()
                current_next = recipe['next_routine_date'] or min_next
                if current_next <= meal['plan_date']:
                    c.execute('UPDATE recipes SET next_routine_date=? WHERE id=?', (min_next, int(meal['recipe_id'])))
        else:
            cur=c.execute(f'DELETE FROM {table} WHERE id=?',(item_id,))
        if cur.rowcount == 0: raise HTTPException(404,'Record not found')
        reminder_kinds={'tasks':['task','task-time'],'bills':['bill'],'routines':['routine-time','routine-late']}
        for kind in reminder_kinds.get(resource,[]):
            c.execute('DELETE FROM notification_log WHERE kind=? AND item_id=?',(kind,item_id))
    if refresh_meal_schedule:
        maintain_meal_schedule()
    result = {'deleted':True,'id':item_id}
    if refresh_meal_schedule:
        result['stock_changed'] = False
    return result

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
      'recent_treadmill':one('SELECT * FROM treadmill ORDER BY date DESC,id DESC LIMIT 1'),
      'today_meal':one('''SELECT dm.id,dm.plan_date,dm.status,r.name AS recipe_name,r.calories_kcal
                         FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id WHERE dm.plan_date=? LIMIT 1''',(today,)),
      'next_meal':one('''SELECT dm.id,dm.plan_date,dm.status,r.name AS recipe_name,r.calories_kcal
                        FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id
                        WHERE dm.status='pending' AND dm.plan_date>=? ORDER BY dm.plan_date LIMIT 1''',(today,))
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
                # When a home-meal batch runs out, notify the user on the due
                # date with a direct shortcut for entering the newly cooked stock.
                for cook in rows("""SELECT mcr.id,mcr.due_date,r.name
                                  FROM meal_cook_reminders mcr
                                  JOIN recipes r ON r.id=mcr.recipe_id
                                  WHERE mcr.status='open' AND mcr.due_date<=?
                                  ORDER BY mcr.due_date,mcr.id""", (today.isoformat(),)):
                    reminders.append(('meal-cook', cook['id'], cook['due_date'],
                                      f"🍳 HOME MEAL BATCH TO PREPARE\n{cook['name']}\n\nYour ready plates have run out. After cooking, update your stock with:\n/stock {cook['name']}|3"))
                # Meal messages follow the user's local Home Assistant timezone (TZ in run.sh).
                # 14:00 announces the plate; 22:00 asks whether it was eaten.
                today_meal = one("SELECT dm.id,dm.plan_date,dm.status,dm.suggestion_notified_date,dm.checkin_notified_date, r.name AS recipe_name,r.calories_kcal FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id WHERE dm.plan_date=? LIMIT 1", (today.isoformat(),))
                if today_meal and today_meal['status'] == 'pending':
                    kcal = f"\n{float(today_meal['calories_kcal']):g} kcal per plate" if today_meal.get('calories_kcal') is not None else ''
                    # Do not send a stale 14:00 announcement after 22:00 if the app was offline all afternoon.
                    if time(14, 0) <= now.time() < time(22, 0) and today_meal.get('suggestion_notified_date') != today.isoformat():
                        message = (f"🍽️ TODAY'S SUGGESTED PLATE\n\n{today_meal['recipe_name']}{kcal}\n\n"
                                   "This is your suggested plate for today. I will check with you at 22:00.")
                        sent = await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': ALLOWED_CHAT, 'text': message, 'disable_web_page_preview': True})
                        sent.raise_for_status()
                        with connect() as c:
                            c.execute("UPDATE daily_meals SET suggestion_notified_date=? WHERE id=? AND status='pending'", (today.isoformat(), int(today_meal['id'])))
                    if now.time() >= time(22, 0) and today_meal.get('checkin_notified_date') != today.isoformat():
                        message = (f"🌙 22:00 MEAL CHECK-IN\n\nDid you eat today's suggested plate?\n"
                                   f"{today_meal['recipe_name']}{kcal}\n\n"
                                   "Reply YES if you ate it, or NO to move this plate and every later plate to the next day.")
                        sent = await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': ALLOWED_CHAT, 'text': message, 'disable_web_page_preview': True})
                        sent.raise_for_status()
                        with connect() as c:
                            c.execute("UPDATE daily_meals SET checkin_notified_date=? WHERE id=? AND status='pending'", (today.isoformat(), int(today_meal['id'])))
                reminders.extend(build_routine_reminders(now))
                for kind, item_id, reminder_date, message in reminders:
                    already_sent = one('SELECT id FROM notification_log WHERE kind=? AND item_id=? AND reminder_date=?', (kind, item_id, reminder_date))
                    if already_sent:
                        if kind == 'routine-late':
                            roll_forward_late_routine(int(item_id), date.fromisoformat(reminder_date))
                        continue
                    sent = await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', json={'chat_id': ALLOWED_CHAT, 'text': message, 'disable_web_page_preview': True})
                    sent.raise_for_status()
                    with connect() as c:
                        c.execute('INSERT OR IGNORE INTO notification_log(kind,item_id,reminder_date) VALUES(?,?,?)', (kind, item_id, reminder_date))
                    if kind == 'routine-late':
                        roll_forward_late_routine(int(item_id), date.fromisoformat(reminder_date))
            except Exception as e:
                print(f'Reminder scheduler error: {e}', flush=True)
            await asyncio.sleep(15)

async def telegram_poll():
    """Poll Telegram with a guided menu and a durable reply queue.

    Incoming actions are executed once. Their reply is stored before sending;
    if Telegram is temporarily unavailable, the stored reply is retried without
    executing the user's command a second time.
    """
    import httpx
    keyboard = {
        'keyboard': [
            [{'text': '📅 Today'}, {'text': '✅ Tasks'}],
            [{'text': '🔁 Routines'}, {'text': '💳 Bills'}],
            [{'text': '🍽 Today plate'}, {'text': '🍽 Choose meal'}],
            [{'text': '🗓 7-day meals'}, {'text': '🛒 Shopping list'}],
            [{'text': '🏠 Home meals'}, {'text': '🍴 Outside meals'}],
            [{'text': '➕ Add meal'}, {'text': '🔢 Update plates'}],
            [{'text': '🗑 Remove meal'}, {'text': '➕ Add grocery'}],
            [{'text': '✅ Ate it'}, {'text': '⏭ Not today'}],
            [{'text': '⚖️ Add weight'}, {'text': '🏃 Add treadmill'}],
            [{'text': '🗓 Week'}, {'text': '❔ Help'}],
            [{'text': '⬅️ Back'}]
        ],
        'resize_keyboard': True,
        'is_persistent': True,
        'input_field_placeholder': 'Choose a button or type /help'
    }
    commands = [
        {'command': 'start', 'description': 'Open the assistant menu'},
        {'command': 'today', 'description': 'Today’s plan'},
        {'command': 'tasks', 'description': 'Show open tasks'},
        {'command': 'routines', 'description': 'Show due and upcoming routines'},
        {'command': 'bills', 'description': 'Show unpaid bills'},
        {'command': 'fitness', 'description': 'Show weight and activity'},
        {'command': 'week', 'description': 'Show the next 7 days'},
        {'command': 'done', 'description': 'Complete a routine: done Shower'},
        {'command': 'taskdone', 'description': 'Complete a task: task done Study'},
        {'command': 'weight', 'description': 'Start guided weight entry'},
        {'command': 'treadmill', 'description': 'Start guided treadmill entry'},
        {'command': 'paid', 'description': 'Mark a bill paid: paid Electricity'},
        {'command': 'meals', 'description': 'Show today’s plate or choose one'},
        {'command': 'choosemeal', 'description': 'Choose a meal for today'},
        {'command': 'back', 'description': 'Return to the main menu'},
        {'command': 'mealplan', 'description': 'Show the next 7 days of plates'},
        {'command': 'yes', 'description': 'Confirm today’s plate was eaten'},
        {'command': 'no', 'description': 'Move today’s plate to tomorrow'},
        {'command': 'shopping', 'description': 'Show your shopping list'},
        {'command': 'addrecipe', 'description': 'Add a meal with guided questions'},
        {'command': 'homemeals', 'description': 'List home meals and ready plates'},
        {'command': 'outsidemeals', 'description': 'List outside meal options'},
        {'command': 'stock', 'description': 'Update stock: /stock Tagine|3'},
        {'command': 'deletemeal', 'description': 'Remove a meal with confirmation'},
        {'command': 'additem', 'description': 'Add an item to the shopping list'},
        {'command': 'bought', 'description': 'Mark a shopping item bought'},
        {'command': 'cancel', 'description': 'Cancel the current guided form'},
        {'command': 'help', 'description': 'Show step-by-step examples'}
    ]
    saved_offset = one("SELECT value FROM telegram_state WHERE key='update_offset'")
    try:
        offset = int(saved_offset['value']) if saved_offset else 0
    except (TypeError, ValueError):
        offset = 0

    async with httpx.AsyncClient(timeout=40) as client:
        try:
            setup = await client.post(f'https://api.telegram.org/bot{TOKEN}/setMyCommands', json={'commands': commands})
            setup.raise_for_status()
            if not setup.json().get('ok'):
                print('Telegram command-menu setup warning: ' + str(setup.json().get('description', 'unknown error')), flush=True)
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
                    update_id = int(update['update_id'])
                    message = update.get('message') or {}
                    chat = str((message.get('chat') or {}).get('id', ''))
                    message_text = (message.get('text') or '').strip()

                    # Ignore unsupported updates and unauthorized chats safely.
                    if not message_text or chat != ALLOWED_CHAT:
                        offset = update_id + 1
                        with connect() as c:
                            c.execute("INSERT INTO telegram_state(key,value) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                        continue

                    queued = one('SELECT reply,sent_at FROM telegram_inbox WHERE update_id=?', (update_id,))
                    if queued and queued.get('sent_at'):
                        offset = update_id + 1
                        with connect() as c:
                            c.execute("INSERT INTO telegram_state(key,value) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                        continue

                    if queued:
                        reply = queued['reply']
                    else:
                        try:
                            reply = await telegram_command(message_text)
                        except Exception as exc:
                            print(f'Telegram command failed for update {update_id}: {exc}', flush=True)
                            reply = 'I could not complete that request. Your data was not intentionally changed by this error. Check your answer and try /help or /cancel.'
                        with connect() as c:
                            c.execute('INSERT OR IGNORE INTO telegram_inbox(update_id,chat_id,reply) VALUES(?,?,?)', (update_id, chat, reply))
                            c.execute('DELETE FROM telegram_inbox WHERE update_id < (SELECT COALESCE(MAX(update_id),0)-2000 FROM telegram_inbox) AND sent_at IS NOT NULL')

                    reply_keyboard = _telegram_context_keyboard(keyboard)
                    sent = await client.post(
                        f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                        json={'chat_id': chat, 'text': reply, 'reply_markup': reply_keyboard, 'disable_web_page_preview': True}
                    )
                    sent.raise_for_status()
                    sent_data = sent.json()
                    if not sent_data.get('ok'):
                        raise RuntimeError(sent_data.get('description', 'Telegram sendMessage failed'))

                    # Persist the reply as delivered and advance the offset only
                    # after Telegram confirms delivery. A failed send reuses the
                    # queued reply instead of repeating the command's side effect.
                    offset = update_id + 1
                    with connect() as c:
                        c.execute('UPDATE telegram_inbox SET sent_at=CURRENT_TIMESTAMP WHERE update_id=?', (update_id,))
                        c.execute("INSERT INTO telegram_state(key,value) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
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


def _telegram_get_flow() -> dict | None:
    raw = _telegram_get_state()
    if not raw or not raw.startswith('{'):
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) and value.get('flow') else None
    except ValueError:
        return None


def _telegram_set_flow(flow: str, step: str, data: dict | None = None) -> None:
    _telegram_set_state(json.dumps({'flow': flow, 'step': step, 'data': data or {}}, ensure_ascii=False))


def _telegram_context_keyboard(default_keyboard: dict) -> dict:
    """Show only the choices relevant to the active guided step."""
    flow = _telegram_get_flow()
    if not flow:
        return default_keyboard
    name, step = flow.get('flow'), flow.get('step')
    rows: list[list[dict[str, str]]] = []
    placeholder = 'Enter your answer'
    if name == 'choose_today_meal' and step == 'name':
        choices = flow.get('data', {}).get('choices', [])
        buttons = [{'text': str(value)[:60]} for value in choices if str(value).strip() and len(str(value)) <= 60]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([{'text': '⬅️ Back'}])
        placeholder = 'Choose a meal for today'
    elif name == 'recipe' and step == 'source':
        rows = [[{'text': '🏠 HOME'}, {'text': '🍴 OUTSIDE'}], [{'text': '/cancel'}]]
        placeholder = 'Choose Home or Outside'
    elif name == 'recipe' and step == 'confirm':
        rows = [[{'text': 'YES'}, {'text': 'NO'}], [{'text': '/cancel'}]]
        placeholder = 'Confirm your meal'
    elif name == 'recipe' and step == 'calories':
        rows = [[{'text': 'skip'}], [{'text': '/cancel'}]]
        placeholder = 'Enter kcal per plate or choose skip'
    elif name == 'recipe' and step == 'interval':
        rows = [[{'text': '1 day'}, {'text': '2 days'}], [{'text': '3 days'}, {'text': '7 days'}], [{'text': '/cancel'}]]
        placeholder = 'Choose a frequency or enter days'
    elif name == 'recipe' and step == 'plates':
        rows = [[{'text': '0'}, {'text': '1'}, {'text': '2'}], [{'text': '3'}, {'text': '5'}, {'text': '7'}], [{'text': '/cancel'}]]
        placeholder = 'Choose a plate count or type one'
    elif name == 'recipe' and step == 'ingredients' and flow.get('data', {}).get('meal_source') == 'outside':
        rows = [[{'text': 'skip'}], [{'text': '/cancel'}]]
        placeholder = 'Add ingredients or choose skip'
    elif name == 'delete_recipe' and step == 'delete_confirm':
        rows = [[{'text': 'YES'}, {'text': 'NO'}], [{'text': '/cancel'}]]
        placeholder = 'Confirm meal removal'
    elif name in ('stock', 'delete_recipe') and step == 'name':
        choices = flow.get('data', {}).get('choices', [])
        buttons = [{'text': str(value)[:60]} for value in choices if str(value).strip() and len(str(value)) <= 60]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([{'text': '/cancel'}])
        placeholder = 'Tap a meal or type its name'
    else:
        rows = [[{'text': '/cancel'}]]
        placeholder = 'Type your answer or /cancel'
    return {'keyboard': rows, 'resize_keyboard': True, 'one_time_keyboard': True,
            'is_persistent': False, 'input_field_placeholder': placeholder}


def _telegram_save_recipe(data: dict) -> int:
    ingredients = []
    for raw in data.get('ingredients', []):
        name = _clean_ingredient_name(raw)
        if name and _ingredient_key(name) not in {_ingredient_key(x) for x in ingredients}:
            ingredients.append(name)
    source = data.get('meal_source','home') if data.get('meal_source','home') in ('home','outside') else 'home'
    if not ingredients and source == 'home':
        raise ValueError('Home meals need at least one ingredient.')
    excluded_weekdays_json = _normalize_excluded_weekdays(data.get('excluded_weekdays', []))
    with connect() as c:
        plates = int(data.get('plates_ready', data.get('batch_plates', 1))) if source == 'home' else 0
        cur = c.execute('''INSERT INTO recipes(name,calories_kcal,ingredients_json,eating_interval_days,batch_plates,meal_source,plates_ready,next_routine_date,excluded_weekdays_json)
                           VALUES(?,?,?,?,?,?,?,?,?)''',
                        (data['name'], data.get('calories_kcal'), json.dumps(ingredients, ensure_ascii=False),
                         int(data.get('eating_interval_days', 1)), max(1, plates or 1), source, plates, date.today().isoformat(), excluded_weekdays_json))
        recipe_id = int(cur.lastrowid)
        if source == 'home' and plates == 0:
            _ensure_cook_reminder(c, recipe_id, date.today() + timedelta(days=1))
    # Telegram-created meals enter the same automatic schedule immediately.
    maintain_meal_schedule()
    return recipe_id


def _telegram_meals_by_source(source: str) -> str:
    meals = rows("SELECT id,name,calories_kcal,eating_interval_days,plates_ready,next_routine_date,excluded_weekdays_json FROM recipes WHERE is_active=1 AND meal_source=? ORDER BY name COLLATE NOCASE", (source,))
    if not meals:
        return 'No ' + ('home' if source == 'home' else 'outside') + ' meals yet. Use /addrecipe to add one.'
    title = '🏠 HOME MEALS' if source == 'home' else '🍴 OUTSIDE MEALS'
    lines = [title]
    for meal in meals:
        stock = f" · {int(meal['plates_ready'] or 0)} plates ready" if source == 'home' else ''
        due = f" · next { _telegram_date_label(meal.get('next_routine_date')) }" if meal.get('next_routine_date') else ''
        excluded = _decode_excluded_weekdays(meal.get('excluded_weekdays_json', '[]'))
        unavailable = f" · not scheduled: {', '.join(WEEKDAY_NAMES[x] for x in excluded)}" if excluded else ''
        lines.append(f"• {meal['name']}{stock} · every {int(meal['eating_interval_days'] or 1)} day(s){due}{unavailable}")
    return '\n'.join(lines)


def _telegram_update_meal_stock(name: str, plates: int) -> str:
    recipe = one("SELECT id,name,meal_source FROM recipes WHERE is_active=1 AND meal_source='home' AND lower(name)=lower(?)", (name.strip(),))
    if not recipe:
        matches = rows("SELECT id,name,meal_source FROM recipes WHERE is_active=1 AND meal_source='home' AND lower(name) LIKE ?", (f"%{name.strip().lower()}%",))
        if len(matches) != 1:
            return ('I could not identify that home meal. Available: ' + ', '.join(x['name'] for x in matches[:8])) if matches else f"No home meal matches '{name}'."
        recipe = matches[0]
    if not 0 <= int(plates) <= 1000:
        raise ValueError('Enter a number from 0 to 1000.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        _sync_home_stock(c, int(recipe['id']), int(plates))
    maintain_meal_schedule()
    if plates == 0:
        return f"🍳 {recipe['name']} marked as out of stock. A cooking reminder is set for tomorrow."
    return f"✅ {recipe['name']} · {plates} plate(s) ready. The meal routine has been updated."


def _telegram_save_shopping_item(line: str) -> str:
    name = _clean_ingredient_name(line)
    if not name:
        raise ValueError('Enter an item name.')
    with connect() as c:
        existing = c.execute('SELECT id,name FROM shopping_items WHERE lower(trim(name))=lower(trim(?)) AND checked=0 LIMIT 1', (name,)).fetchone()
        if existing:
            return f"ℹ️ Already on your shopping list · {existing['name']}"
        c.execute("INSERT INTO shopping_items(name,quantity,unit,category,source) VALUES(?,1,'','','Telegram')", (name,))
    return f"✅ Added to shopping list · {name}"


def _telegram_available_today_meals() -> list[dict]:
    """Meals selectable today, excluding closed weekdays and reserved-out Home stock."""
    today = date.today()
    candidates = rows("""SELECT r.id,r.name,r.meal_source,r.plates_ready,r.calories_kcal,r.excluded_weekdays_json,
        COALESCE((SELECT COUNT(*) FROM daily_meals dm WHERE dm.recipe_id=r.id AND dm.status='pending' AND dm.plan_date>=?),0) AS reserved_plates
        FROM recipes r WHERE r.is_active=1
          AND (r.meal_source='outside' OR r.plates_ready>COALESCE((SELECT COUNT(*) FROM daily_meals dm WHERE dm.recipe_id=r.id AND dm.status='pending' AND dm.plan_date>=?),0))
        ORDER BY CASE r.meal_source WHEN 'home' THEN 0 ELSE 1 END,r.name COLLATE NOCASE""", (today.isoformat(), today.isoformat()))
    return [meal for meal in candidates if today.weekday() not in _decode_excluded_weekdays(meal.get('excluded_weekdays_json', '[]'))]



def _telegram_meal_text(start: date, days: int) -> str:
    end = start + timedelta(days=days-1)
    meals = rows('''SELECT dm.id,dm.plan_date,dm.status,r.name AS recipe_name,r.calories_kcal
                    FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id
                    WHERE dm.plan_date BETWEEN ? AND ? ORDER BY dm.plan_date''',
                 (start.isoformat(), end.isoformat()))
    if not meals:
        return f'🍽 No meal entries in the next {days} day(s). Use Today plate to choose a meal manually, or /addrecipe to save another option.'
    labels = []
    for meal in meals:
        status = '✅ Eaten' if meal['status'] == 'eaten' else '⏳ Waiting for confirmation'
        kcal = f" · {float(meal['calories_kcal']):g} kcal/plate" if meal.get('calories_kcal') is not None else ''
        labels.append(f"{_telegram_date_label(meal['plan_date'], True)}\n• {meal['recipe_name']}{kcal}\n  {status}")
    return '🍽 DAILY PLATES\n\n' + '\n\n'.join(labels)


def _telegram_shopping_text() -> str:
    items = rows('SELECT id,name,checked FROM shopping_items ORDER BY checked,name COLLATE NOCASE LIMIT 60')
    if not items:
        return '🛒 Your shopping list is empty. Use /additem to add an item.'
    pending = [x for x in items if not int(x['checked'])]
    bought = [x for x in items if int(x['checked'])]
    lines = [f'🛒 SHOPPING LIST · {len(pending)} to buy']
    lines.extend(f"□ {item['name']} · bought {item['name']}" for item in pending)
    if bought:
        lines.append(f'\n✅ Purchased: {len(bought)}')
        lines.extend(f"✓ {x['name']}" for x in bought[:8])
    return '\n'.join(lines)


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
    low = re.sub(r'(?m)^(\/[a-z0-9_]+)@[a-z0-9_]+', r'\1', low)
    today = date.today()
    today_iso = today.isoformat()

    # Guided input flows. They survive restarts and can be cancelled with /cancel.
    state = _telegram_get_state()
    conversation_active = bool(state)
    flow_state = _telegram_get_flow()
    if low in ('/cancel', 'cancel', 'stop'):
        _telegram_set_state(None)
        return 'Cancelled. Nothing was saved.'
    if low in ('⬅️ back', 'back', '/back'):
        _telegram_set_state(None)
        return '↩️ Back to the main menu. Choose an action from the menu below, or send /help for examples.'
    meal_menu_commands = (
        '🍽 today plate','🍽 today meals','today plate','today meals','meals','/meals',
        '🗓 meal plan','🗓 7-day meals','🗓 upcoming meals','upcoming meals','meal plan','/mealplan',
        '✅ ate it','ate it','i ate it','⏭ not today','not today','yes','y','/yes','/eaten','no','n','/no','/skip',
        '🛒 shopping list','shopping list','shopping','/shopping','➕ add recipe','add recipe','➕ add meal','add meal','/addrecipe',
        '🏠 home meals','home meals','/homemeals','🍴 outside meals','outside meals','/outsidemeals',
        '🔢 update plates','update plates','/stock','🗑 remove meal','remove meal','delete meal','/deletemeal','➕ add grocery','add grocery','/additem',
        '🍽 choose meal','choose meal','/choosemeal','⬅️ back','back','/back'
    )
    meal_interrupt_prefixes = ('bought ', 'got ', 'suggest meals', 'plan meals', '/bought', '/suggestmeals', 'add item ', 'add grocery ')
    guided_confirming = bool(flow_state and flow_state.get('step') in ('confirm', 'delete_confirm'))
    guided_confirmation_reply = guided_confirming and low in ('yes', 'y', 'save', 'confirm', 'no', 'n', 'remove', 'delete', '/yes', '/no', '/save', '/confirm')
    guided_yes_no_reply = bool(conversation_active and low in ('yes', 'y', 'no', 'n'))
    flow_interrupted = (
        (low.startswith('/') and not guided_confirmation_reply)
        or low.startswith(('weight ', 'treadmill ', 'task ', 'paid ', 'done ', *meal_interrupt_prefixes))
        or (low in meal_menu_commands and not guided_yes_no_reply and not guided_confirmation_reply)
    )
    meal_checkin_inputs = ('✅ ate it', 'ate it', 'i ate it', 'yes', 'y', '/yes', '/eaten', '⏭ not today', 'not today', 'no', 'n', '/no', '/skip')
    if conversation_active and not guided_confirming and low in meal_checkin_inputs:
        return 'You are in the middle of a guided entry. Finish that step or send /cancel first; then use YES or NO for your meal check-in.'

    if flow_state and flow_state.get('flow') == 'choose_today_meal' and not flow_interrupted:
        if flow_state.get('step') == 'name':
            query = raw.strip()
            available = _telegram_available_today_meals()
            matched, matches = _telegram_match_named_item(query, available)
            if not matched:
                if matches:
                    return 'I found more than one match. Please tap or type the full meal name:\n' + '\n'.join('• ' + item['name'] for item in matches[:8])
                return 'Please choose one of the meal buttons below, or use ⬅️ Back to return to the main menu.'
            try:
                selected = _select_daily_meal(int(matched['id']), today)
                _telegram_set_state(None)
                maintain_meal_schedule()
                kcal = f" · {float(selected['calories_kcal']):g} kcal/plate" if selected.get('calories_kcal') is not None else ''
                return f"✅ Today's plate selected\n\n🍽 {selected['name']}{kcal}\n\nI will ask at 22:00 whether you ate it. You can change your choice from the Meals page if needed."
            except ValueError as exc:
                refreshed = _telegram_available_today_meals()
                _telegram_set_flow('choose_today_meal','name',{'choices':[x['name'] for x in refreshed[:16]]})
                return str(exc) + ('\n\nChoose another available meal below.' if refreshed else '\n\nNo meal is available now. Update Home stock or add an Outside meal, then try again.')

    if flow_state and flow_state.get('flow') == 'stock' and not flow_interrupted:
        step = flow_state.get('step')
        data = flow_state.setdefault('data', {})
        if step == 'name':
            if raw.strip().lower() in ('yes', 'y', 'no', 'n') or len(raw.strip()) < 2:
                return 'Please send the full HOME meal name, for example Chicken tagine.\n/cancel to stop.'
            home_meals = rows("SELECT id,name,plates_ready FROM recipes WHERE is_active=1 AND meal_source='home' ORDER BY name COLLATE NOCASE")
            matched, matches = _telegram_match_named_item(raw.strip(), home_meals)
            if not matched:
                if matches:
                    return 'I found more than one matching meal. Please tap or type its full name:\n' + '\n'.join('• ' + x['name'] for x in matches[:8])
                return 'I could not find that Home meal. Please choose one from the buttons or type its exact name.\n/cancel to stop.'
            data['name'] = matched['name']
            _telegram_set_flow('stock','plates',data)
            return f"🔢 UPDATE READY PLATES · 2/2\n\nHow many plates of {matched['name']} are ready now? Send a whole number from 0 to 1000.\nExample: 3"
        if step == 'plates':
            try:
                qty = int(raw)
                reply = _telegram_update_meal_stock(data['name'], qty)
                _telegram_set_state(None)
                return reply + '\n\nWhat next? Use /meals, /addrecipe or /help.'
            except (TypeError,ValueError):
                return 'Please send a whole number from 0 to 1000.\nExample: 3\n/cancel to stop.'

    if flow_state and flow_state.get('flow') == 'delete_recipe' and not flow_interrupted:
        step = flow_state.get('step')
        data = flow_state.setdefault('data', {})
        if step == 'name':
            query = raw.strip()
            if len(query) < 2:
                return 'Send the name of the meal you want to remove, or /cancel to stop.'
            meals = rows('SELECT id,name FROM recipes WHERE is_active=1 ORDER BY name COLLATE NOCASE')
            matched, matches = _telegram_match_named_item(query, meals)
            if not matched:
                if matches:
                    return 'I found several matches. Please send the full meal name:\n' + '\n'.join('• ' + item['name'] for item in matches[:8])
                return f"I could not find a saved meal matching '{query}'. Send /meals to see the available options."
            data.update({'id': int(matched['id']), 'name': matched['name']})
            _telegram_set_flow('delete_recipe', 'delete_confirm', data)
            return (f"🗑 REMOVE MEAL\n\n{matched['name']}\n\n"
                    'Remove this meal option? Pending plates will be cancelled. Confirmed eaten history will be kept.\n'
                    'Reply YES to remove or NO to keep it.')
        if step == 'delete_confirm':
            if low in ('yes', 'y', 'remove', 'delete', '/yes', '/confirm'):
                try:
                    result = _remove_recipe_option(int(data['id']))
                except (KeyError, TypeError, ValueError) as exc:
                    _telegram_set_state(None)
                    return f"I could not remove that meal: {exc}\nUse /meals to review your options."
                _telegram_set_state(None)
                history = ' Your confirmed eating history was kept.' if result['history_preserved'] else ''
                return f"✅ Removed meal option · {result['name']}.{history}\nUse /addrecipe to add another meal."
            if low in ('no', 'n', 'cancel', '/no'):
                _telegram_set_state(None)
                return 'No changes made. Your meal option is still available.'
            return 'Please reply YES to remove this meal or NO to keep it. Send /cancel to stop.'

    if flow_state and flow_state.get('flow') == 'recipe' and not flow_interrupted:
        step = flow_state.get('step')
        data = flow_state.setdefault('data', {})
        answer = raw.strip()
        try:
            if step == 'source':
                source = answer.lower().strip()
                if source in ('home','🏠 home','house'): source='home'
                elif source in ('outside','🍴 outside','restaurant','out'): source='outside'
                else: raise ValueError()
                data['meal_source']=source
                _telegram_set_flow('recipe','name',data)
                return ('🍽️ ADD A HOME MEAL · 2/7\n\n' if source=='home' else '🍴 ADD AN OUTSIDE MEAL · 2/6\n\n') + 'What is the meal name?\nExample: Chicken tagine'
            if step == 'name':
                if answer.lower() in ('yes', 'y', 'no', 'n', 'skip') or len(answer) < 2 or len(answer) > 160: raise ValueError('Enter the meal name (2–160 characters). YES/NO is only used when I show the final review.')
                data['name'] = answer
                _telegram_set_flow('recipe', 'calories', data)
                return 'Step 3 · Calories\n\nHow many kcal are in one plate? Send a number or type skip.\nExample: 650'
            if step == 'calories':
                cleaned = re.sub(r'\s*kcal\s*$', '', answer, flags=re.I).replace(',', '.')
                data['calories_kcal'] = None if cleaned.lower() in ('skip','-','none') else float(cleaned)
                if data['calories_kcal'] is not None and not 0 <= data['calories_kcal'] <= 10000: raise ValueError()
                _telegram_set_flow('recipe', 'interval', data)
                return 'Step 4 · Eating frequency\n\nHow often do you usually eat this meal?\nSend a number of days, for example 1, 2 or 3. You can also write “every 3 days”.'
            if step == 'interval':
                match = re.fullmatch(r'(?:every\s+)?(\d+)(?:\s+days?)?', answer, flags=re.I)
                value = int(match.group(1)) if match else int(answer)
                if not 1 <= value <= 90: raise ValueError()
                data['eating_interval_days'] = value
                if data.get('meal_source') == 'outside':
                    data['plates_ready']=0
                    data['batch_plates']=1
                    _telegram_set_flow('recipe','ingredients',data)
                    return 'Step 5 · Ingredients (optional)\n\nSend ingredients separated by commas, or type skip.\nExample: bread, chicken, lettuce'
                _telegram_set_flow('recipe', 'plates', data)
                return 'Step 5 · Ready plates\n\nHow many plates are ready in your kitchen now?\nSend a whole number from 0 to 1000. Example: 3'
            if step == 'plates':
                value = int(answer)
                if not 0 <= value <= 1000: raise ValueError()
                data['batch_plates'] = max(1, value)
                data['plates_ready'] = value
                _telegram_set_flow('recipe', 'ingredients', data)
                return 'Step 6 · Ingredients\n\nList ingredients separated by commas or new lines.\nExample: chicken, onions, tomatoes, olive oil'
            if step == 'ingredients':
                if answer.lower() in ('yes', 'y', 'no', 'n'):
                    raise ValueError('Please list ingredients, or type skip if this is an Outside meal. YES/NO is used at the final review.')
                parts = [] if answer.strip().lower() in ('skip','-','none') else [_clean_ingredient_name(x) for x in re.split(r'[,;\n]+', answer) if x.strip()]
                parts = list(dict.fromkeys(x for x in parts if x))
                if not parts and data.get('meal_source','home') == 'home': raise ValueError('Home meals need at least one ingredient.')
                data['ingredients'] = parts
                _telegram_set_flow('recipe','excluded_days',data)
                return ('Step 7 · Closed days\n\n' if data.get('meal_source') == 'home' else 'Step 6 · Closed days\n\n') + 'Which days should this meal NOT be scheduled? Send full weekday names separated by commas (example: Sunday, Wednesday), or type none.\n/cancel to stop.'
            if step == 'excluded_days':
                normalized = _normalize_excluded_weekdays([] if answer.lower() in ('none','no','skip','-') else re.split(r'[,;]+', answer))
                data['excluded_weekdays'] = json.loads(normalized)
                _telegram_set_flow('recipe','confirm',data)
                source_label = 'Home meal' if data.get('meal_source') == 'home' else 'Outside meal'
                kcal_label = f"{data['calories_kcal']:g} kcal/plate" if data.get('calories_kcal') is not None else 'Calories not set'
                stock_label = f"\nReady plates: {int(data.get('plates_ready',0))}" if data.get('meal_source') == 'home' else ''
                ingredients = data.get('ingredients', [])
                ingredient_label = ', '.join(ingredients) if ingredients else 'Not added'
                excluded = data['excluded_weekdays']
                excluded_label = f"\nNot scheduled: {', '.join(WEEKDAY_NAMES[x] for x in excluded)}" if excluded else '\nNot scheduled: No weekday restrictions'
                return (f"🔎 REVIEW YOUR MEAL\n\nType: {source_label}\nName: {data['name']}\nCalories: {kcal_label}\nFrequency: every {data['eating_interval_days']} day(s){stock_label}\nIngredients: {ingredient_label}{excluded_label}\n\n"
                        'Save this meal? Reply YES to save or NO to cancel.\n/cancel also cancels the form.')
            if step == 'confirm':
                if low in ('yes','y','save','confirm','/yes','/save','/confirm'):
                    recipe_id = _telegram_save_recipe(data)
                    _telegram_set_state(None)
                    return (f"✅ Meal saved · {data['name']}\n\nYour meal has been added to the automatic routine.\nUse /meals to view today’s plate or /stock to update ready plates.")
                if low in ('no','n','cancel','/no'):
                    _telegram_set_state(None)
                    return 'No problem. The meal was not saved. Send /addrecipe whenever you want to try again.'
                return 'Please reply YES to save this meal or NO to cancel. You can also send /cancel.'
        except (TypeError, ValueError, ZeroDivisionError):
            prompts = {
                'source':'Choose one option: HOME or OUTSIDE.',
                'name':'Send a meal name between 2 and 160 characters.',
                'calories':'Send a kcal number (for example 650) or type skip.',
                'interval':'Send a whole number from 1 to 90 days, for example 2 or every 3 days.',
                'plates':'Send a whole number from 0 to 1000 plates, for example 3.',
                'ingredients':('Send ingredients separated by commas, or type skip.' if data.get('meal_source') == 'outside' else 'Home meals need at least one ingredient. Send a comma-separated list.'),
                'excluded_days':'Send full weekday names separated by commas (example: Sunday, Wednesday), or type none.'
            }
            return prompts.get(step, 'I could not use that answer. Please try again, or send /cancel.')

    if flow_state and flow_state.get('flow') == 'shopping' and not flow_interrupted:
        if low in ('yes', 'y', 'no', 'n'):
            return 'Please send the shopping item name, for example tomatoes. YES/NO is only used when I ask you to confirm a meal.'
        try:
            reply = _telegram_save_shopping_item(raw)
            _telegram_set_state(None)
            return reply
        except (TypeError, ValueError):
            return 'I could not read that item. Send a simple item name, for example rice or tomatoes. Send /cancel to stop.'
    if state and low not in ('yes', 'y', 'no', 'n') and (low.startswith('/') or low in ('📅 today','today','✅ tasks','tasks','💳 bills','bills','🔁 routines','routines','🏃 fitness','fitness','🗓 week','week','❔ help','help') or low in meal_menu_commands or low.startswith(('weight ','log weight ','treadmill ','run ','walk ','task ','paid ','done ', *meal_interrupt_prefixes))):
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

    if low in ('🗑 remove meal', 'remove meal', 'delete meal', '/deletemeal'):
        meals = rows('SELECT id,name FROM recipes WHERE is_active=1 ORDER BY name COLLATE NOCASE')
        if not meals:
            return 'There are no saved meal options to remove. Use /addrecipe to add one.'
        _telegram_set_flow('delete_recipe', 'name', {'choices': [meal['name'] for meal in meals[:12]]})
        preview = '\n'.join('• ' + meal['name'] for meal in meals[:12])
        more = f'\n…and {len(meals)-12} more' if len(meals) > 12 else ''
        return '🗑 REMOVE A MEAL\n\nWhich saved meal do you want to remove? Send its name.\n\n' + preview + more + '\n\n/cancel to stop.'
    if low in ('➕ add recipe','add recipe','➕ add meal','add meal','/addrecipe','/recipe'):
        _telegram_set_flow('recipe','source',{})
        return '➕ ADD A MEAL\nIs this a home meal or an outside meal? Reply HOME or OUTSIDE.\nSend /cancel to stop.'
    if low in ('🔢 update plates','update plates','/stock'):
        home_meals = rows("SELECT name,plates_ready FROM recipes WHERE is_active=1 AND meal_source='home' ORDER BY name COLLATE NOCASE")
        _telegram_set_flow('stock','name',{'choices': [meal['name'] for meal in home_meals[:12]]})
        choices = '\n'.join(f"• {meal['name']} · {int(meal['plates_ready'] or 0)} ready" for meal in home_meals[:10])
        if not choices:
            return 'There are no Home meals yet. Use /addrecipe and choose HOME first.'
        return '🔢 UPDATE READY PLATES\n\nWhich Home meal do you want to update? Send its name.\n\n' + choices + '\n\n/cancel to stop.'
    stock_match = re.match(r'(?i)^/stock(?:@[a-z0-9_]+)?\s+(.+?)\s*[|,]\s*(\d+)\s*$', raw)
    if stock_match:
        return _telegram_update_meal_stock(stock_match.group(1), int(stock_match.group(2)))
    if low in ('🏠 home meals','home meals','/homemeals'):
        return _telegram_meals_by_source('home')
    if low in ('🍴 outside meals','outside meals','/outsidemeals'):
        return _telegram_meals_by_source('outside')
    if low in ('➕ add grocery','add grocery','/additem','add item'):
        _telegram_set_flow('shopping','item',{})
        return '🛒 ADD SHOPPING ITEM\nSend the item name, for example rice, tomatoes or olive oil.\n/cancel to stop.'
    add_item_match = re.match(r'(?i)^(?:/additem(?:@[a-z0-9_]+)?|add item|add grocery)\s+(.+)$', raw)
    if add_item_match:
        try: return _telegram_save_shopping_item(add_item_match.group(1))
        except (TypeError, ValueError): return 'Use: add item rice'
    if low in ('🍽 today plate','🍽 today meals','today meals','today plate','meals','/meals','what should i eat today','what am i eating today','🍽 choose meal','choose meal','/choosemeal'):
        today_meal = one("SELECT dm.id,dm.plan_date,dm.status,r.name AS recipe_name,r.calories_kcal FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id WHERE dm.plan_date=?", (today_iso,))
        if today_meal:
            return _telegram_meal_text(today,1)
        available = _telegram_available_today_meals()
        if not available:
            _telegram_set_state(None)
            return '🍽️ There is no meal ready to choose right now. Add an Outside meal with /addrecipe, or update the ready plates of a Home meal with /stock.\n\nUse ⬅️ Back to return to the main menu.'
        _telegram_set_flow('choose_today_meal','name',{'choices':[meal['name'] for meal in available[:16]]})
        options='\n'.join(f"• {meal['name']} · {'Home' if meal['meal_source']=='home' else 'Outside'}" for meal in available[:12])
        more=f"\n…and {len(available)-12} more" if len(available)>12 else ''
        return '🍽️ WHAT WOULD YOU LIKE TO EAT TODAY?\n\nChoose a meal below to schedule it for today.\n\n' + options + more + '\n\nTap ⬅️ Back to return to the main menu.'
    if low in ('🗓 meal plan','🗓 upcoming meals','upcoming meals','meal plan','/mealplan','my meal plan'):
        return _telegram_meal_text(today,7)
    if low in ('🛒 shopping list','shopping list','shopping','/shopping','/groceries'):
        return _telegram_shopping_text()
    # The pending plate confirmation is intentionally a simple YES / NO reply.
    if not conversation_active and low in ('✅ ate it','ate it','yes','y','/yes','/eaten','i ate it','⏭ not today','not today','no','n','/no','/skip'):
        pending = one("SELECT id,plan_date FROM daily_meals WHERE status='pending' AND plan_date<=? ORDER BY plan_date,id LIMIT 1", (today_iso,))
        if not pending:
            return 'There is no plate waiting for confirmation.'
        eaten = low in ('✅ ate it','ate it','yes','y','/yes','/eaten','i ate it')
        try:
            updated = _confirm_daily_meal(int(pending['id']), eaten)
        except ValueError as exc:
            return str(exc)
        if eaten:
            return '✅ Confirmed! Enjoy. Your next planned plate stays on schedule.'
        delay = int(updated.get('shift_days') or 1)
        return f"↪️ No problem. This plate moved to {_telegram_date_label(updated['plan_date'], True)}. Every later plate moved by {delay} day(s) too."

    if low.startswith('/suggestmeals') or low in ('suggest meals', 'plan meals'):
        maintain_meal_schedule()
        return _telegram_meal_text(today, 7) + '\n\nYour meal schedule is now maintained automatically; no manual generation is needed.'
    bought_match = re.match(r'(?i)^(?:/bought(?:@[a-z0-9_]+)?|bought|got)\s+(.+)$', raw)
    if bought_match:
        query = bought_match.group(1).strip()
        items = rows('SELECT * FROM shopping_items WHERE checked=0')
        matched, matches = _telegram_match_named_item(query, items)
        if not matched:
            return 'Which item? ' + ', '.join(x['name'] for x in matches[:6]) if matches else f"I couldn't find '{query}' on the shopping list."
        with connect() as c: c.execute('UPDATE shopping_items SET checked=1 WHERE id=?', (matched['id'],))
        return f"✅ Marked purchased · {matched['name']}"
    if low in ('/start', 'start', '/menu', 'menu', 'hi', 'hello', '/help', 'help', '❔ help'):
        return (
            '👋 WELCOME TO XROB ASSISTANT\n\n'
            'I can help you manage your day, routines, fitness, bills and meals. Use the menu buttons below or type a command.\n\n'
            '🍽️ MEALS\n'
            '• /meals — today’s plate, or choose a meal if none is scheduled\n'
            '• /choosemeal — choose a meal for today manually\n'
            '• /homemeals — home meals and ready plates\n'
            '• /outsidemeals — outside meal options\n'
            '• /addrecipe — add a meal with guided questions (YES to save, NO to cancel)\n'
            '• /stock — choose a home meal, then enter how many plates are ready\n'
            '• /deletemeal — remove a saved meal after a YES/NO confirmation\n'
            '• /shopping — show groceries · /additem — add one · bought tomatoes — mark bought\n\n'
            '✅ CONFIRM TODAY’S PLATE\n'
            'At 14:00 I send your suggested plate. At 22:00, reply YES if you ate it or NO to move it and later plates forward.\n\n'
            '🏃 FITNESS\n'
            '• /weight then send 82.4 (or 82.4 tomorrow)\n'
            '• /treadmill then send 30 (minutes)\n\n'
            '📅 PLANNER & ROUTINES\n'
            '• task Study tomorrow at 18:00\n'
            '• task done Study\n'
            '• done Shower — complete a routine\n'
            '• paid Electricity — mark a bill paid\n'
            '• /today · /tasks · /routines · /bills · /week\n\n'
            'Use ⬅️ Back to return to the main menu, or /cancel to stop the current guided form. I ask one question at a time and show a summary before saving a new meal.'
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
        routines = rows("SELECT id,name,next_due,interval_days,routine_time,routine_late_time,routine_rollover_after_late,last_done FROM routines ORDER BY CASE WHEN next_due<=? THEN 0 ELSE 1 END,next_due,COALESCE(routine_time,'23:59'),name LIMIT 15", (today_iso,))
        if not routines:
            return 'No routines yet. Add them in XROB Assistant.'
        due_items = [r for r in routines if r.get('next_due') and r['next_due'] <= today_iso]
        upcoming = [r for r in routines if r not in due_items]
        lines = [f'🔁 ROUTINES · {len(due_items)} due']
        for routine in due_items:
            due = date.fromisoformat(routine['next_due'])
            status = 'DUE TODAY' if due == today else f'OVERDUE {(today-due).days}d'
            late_behavior = ' · moves to tomorrow after late alert' if routine.get('routine_late_time') and int(routine.get('routine_rollover_after_late') or 0) else ''
            lines.append(f"• {routine['name']} · {routine.get('routine_time') or 'Any time'} · {status}{late_behavior}")
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
        for meal in rows('''SELECT dm.plan_date,r.name AS recipe_name FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id WHERE dm.plan_date>=? AND dm.plan_date<=?''', (today_iso, end)):
            items.append((meal['plan_date']+'T12:00', f"{_telegram_date_label(meal['plan_date'])} · 🍽 {meal['recipe_name']}"))
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
            '• paid Electricity\n'
            '• /meals · /mealplan (upcoming plates)\n'
            '• Reply YES to confirm a plate or NO to move it and all later plates one day.\n'
            '• /shopping · bought tomatoes · /addrecipe · /additem\n'
            '• Add a meal through the short guided questions; use /cancel to stop.')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8090)

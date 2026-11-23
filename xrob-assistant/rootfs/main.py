from __future__ import annotations
import asyncio, json, os, sqlite3, re, math, random, importlib.util, hashlib, unicodedata, shutil
from contextlib import asynccontextmanager, contextmanager
import threading
import time as clock
from datetime import datetime, date, timedelta, time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_VERSION = '1.0.2'
APP_RELEASE_CHANNEL = 'stable'
DATA = Path(os.getenv('XROB_DATA_DIR', '/config'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'xrob_assistant.db'
WWW = Path(__file__).resolve().parent / 'www'
TOKEN = os.getenv('XROB_TELEGRAM_TOKEN', '').strip()
ALLOWED_CHAT = os.getenv('XROB_TELEGRAM_CHAT_ID', '').strip()
MIKASA_PERSONA_SOURCE = 'bundled'
MIKASA_GITHUB_RAW_URL = 'https://raw.githubusercontent.com/ahmedxrob/ai-project/main/xrob-assistant/rootfs/mikasa_persona.py'
MIKASA_GITHUB_RAW_HOST = 'raw.githubusercontent.com'
MIKASA_UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_MIKASA_UPDATE_LOCK = asyncio.Lock()
_DATABASE_BACKUP_LOCK = threading.Lock()
_MANUAL_BACKUP_LOCK = threading.Lock()
_LAST_MANUAL_BACKUP_AT = 0.0
MANUAL_BACKUP_COOLDOWN_SECONDS = 60
DATABASE_BACKUP_RETENTION = 14

_MIKASA_REQUIRED_API = (
    'PERSONA_VERSION', 'TELEGRAM_PROFILE_DESCRIPTION', 'TELEGRAM_PROFILE_SHORT_DESCRIPTION',
    'start_message', 'unclear_message', 'morning_opening', 'morning_closing', 'playful_message',
    'social_reply', 'command_prefix', 'help_intro', 'help_boundary', 'reminder_task',
    'reminder_routine', 'routine_moved_to_tomorrow', 'routine_already_tomorrow', 'event_countdown_reply', 'event_not_found_reply', 'reminder_bill', 'reminder_bill_early', 'reminder_cook',
    'meal_suggestion', 'meal_checkin', 'meal_confirmed', 'meal_unconfirmed', 'meal_undo_unavailable', 'meal_moved', 'task_created',
    'fallback_reply', 'unsupported_message', 'processing_error',
    'task_completed', 'bill_paid', 'today_empty', 'today_open_count', 'routine_completed', 'app_status_reply',
    'agenda_item_line', 'agenda_conversation_line', 'today_agenda_reply', 'tomorrow_agenda_reply', 'morning_report', 'ensure_voice', 'command_reply',
)


def _load_persona_file(path: Path, module_name: str):
    """Load and validate a persona by absolute file path, not sys.path lookup."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Mikasa voice file is missing: {path}')
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'could not create a Python loader for Mikasa voice file: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [name for name in _MIKASA_REQUIRED_API if not hasattr(module, name)]
    if missing:
        raise ImportError('incomplete Mikasa voice pack; missing: ' + ', '.join(missing))
    return module


def _persona_version_order(value: Any) -> tuple[int, int, int, int]:
    """Return a comparable four-part version; unknown legacy labels sort first."""
    match = re.search(r'(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)', str(value or ''))
    if not match:
        return (0, 0, 0, 0)
    parts = tuple(int(part) for part in match.group(1).split('.'))
    return (parts + (0, 0, 0, 0))[:4]


def _load_mikasa_persona_module():
    """Load the newest valid persona from /config or the bundled application.

    A stale /config/mikasa_persona.py must not silently shadow a newer voice
    shipped with an app update. A same-version or newer config override remains
    preferred, so user edits are still supported.
    """
    global MIKASA_PERSONA_SOURCE
    override = DATA / 'mikasa_persona.py'
    app_dir = Path(__file__).resolve().parent
    bundled_candidates = (
        app_dir / 'mikasa_persona.py',
        app_dir / 'rootfs' / 'mikasa_persona.py',
        Path('/rootfs/mikasa_persona.py'),
    )
    bundled_path = next((candidate for candidate in bundled_candidates if candidate.is_file()), None)
    bundled_module = None
    override_module = None

    if bundled_path is not None:
        try:
            bundled_module = _load_persona_file(bundled_path, 'xrob_mikasa_persona_bundled')
        except Exception as exc:
            print(f'Bundled Mikasa voice could not be loaded: {exc}', flush=True)

    if override.is_file() and override.resolve() not in {p.resolve() for p in bundled_candidates if p.exists()}:
        try:
            override_module = _load_persona_file(override, 'xrob_mikasa_persona_override')
        except Exception as exc:
            print(f'Mikasa voice override could not be loaded; using bundled voice: {exc}', flush=True)

    if override_module is not None and bundled_module is not None:
        override_version = getattr(override_module, 'PERSONA_VERSION', 'unknown')
        bundled_version = getattr(bundled_module, 'PERSONA_VERSION', 'unknown')
        if _persona_version_order(override_version) >= _persona_version_order(bundled_version):
            MIKASA_PERSONA_SOURCE = 'config override'
            print(f'Loaded Mikasa voice pack {override_version} from /config/mikasa_persona.py', flush=True)
            return override_module
        print(
            f'Ignoring stale Mikasa config voice {override_version}; bundled version '
            f'{bundled_version} is newer.',
            flush=True,
        )

    if bundled_module is not None:
        MIKASA_PERSONA_SOURCE = 'bundled'
        print(f'Loaded bundled Mikasa voice pack {getattr(bundled_module, "PERSONA_VERSION", "unknown")} from {bundled_path}', flush=True)
        return bundled_module

    if override_module is not None:
        MIKASA_PERSONA_SOURCE = 'config override'
        print(f'Loaded Mikasa voice pack {getattr(override_module, "PERSONA_VERSION", "unknown")} from /config/mikasa_persona.py', flush=True)
        return override_module

    searched = ', '.join(str(path) for path in bundled_candidates)
    raise FileNotFoundError(
        'No valid Mikasa voice file was found. Expected a complete persona beside '
        f'main.py, in a rootfs directory, or at {override}. Checked: {searched}'
    )



mikasa_persona = _load_mikasa_persona_module()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_date TEXT, due_time TEXT, priority TEXT DEFAULT 'normal', status TEXT DEFAULT 'open', completed_at TEXT, category TEXT DEFAULT 'general', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS weights (id INTEGER PRIMARY KEY, date TEXT NOT NULL, kg REAL NOT NULL, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS workouts (id INTEGER PRIMARY KEY, date TEXT NOT NULL, name TEXT NOT NULL, duration_min INTEGER DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS workout_plan (id INTEGER PRIMARY KEY, weekday TEXT NOT NULL, name TEXT NOT NULL, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS exercises (id INTEGER PRIMARY KEY, workout_id INTEGER NOT NULL, name TEXT NOT NULL, sets INTEGER DEFAULT 0, reps TEXT DEFAULT '', weight_kg REAL DEFAULT 0, FOREIGN KEY(workout_id) REFERENCES workouts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS treadmill (id INTEGER PRIMARY KEY, date TEXT NOT NULL, duration_min REAL DEFAULT 0, distance_km REAL DEFAULT 0, speed_kmh REAL DEFAULT 0, incline REAL DEFAULT 0, calories_kcal REAL DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_mad REAL DEFAULT 0, due_date TEXT NOT NULL, recurrence TEXT DEFAULT 'none', reminder_days INTEGER DEFAULT 3, status TEXT DEFAULT 'unpaid', paid_at TEXT, notes TEXT DEFAULT '', recurrence_day INTEGER, series_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, name TEXT NOT NULL, expiry_date TEXT NOT NULL, reminder_days INTEGER DEFAULT 30, reference TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routines (id INTEGER PRIMARY KEY, name TEXT NOT NULL, interval_days INTEGER DEFAULT 1, last_done TEXT, next_due TEXT, category TEXT DEFAULT 'personal', notes TEXT DEFAULT '', routine_time TEXT DEFAULT '10:00', routine_late_time TEXT DEFAULT NULL, routine_rollover_after_late INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS routine_logs (id INTEGER PRIMARY KEY, routine_id INTEGER NOT NULL, completed_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', FOREIGN KEY(routine_id) REFERENCES routines(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL, reminder_date TEXT NOT NULL, UNIQUE(kind,item_id,reminder_date));
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_inbox (update_id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, reply TEXT NOT NULL, sent_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS mikasa_learning_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 fingerprint TEXT NOT NULL UNIQUE,
 sample_text TEXT NOT NULL,
 normalized_text TEXT NOT NULL,
 occurrences INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','learned','ignored')),
 teaching_reply TEXT NOT NULL DEFAULT '',
 intent_note TEXT NOT NULL DEFAULT '',
 first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mikasa_learning_status_seen ON mikasa_learning_log(status,last_seen_at DESC);
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
 id INTEGER PRIMARY KEY, plan_date TEXT NOT NULL UNIQUE, recipe_id INTEGER NOT NULL, batch_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', assignment_source TEXT NOT NULL DEFAULT 'automatic',
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
    # WAL allows dashboard reads to continue while reminder/Telegram writes are
    # committed. A generous busy timeout prevents short concurrent writes from
    # surfacing as user-facing database errors.
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    c.execute('PRAGMA synchronous=NORMAL')
    c.execute('PRAGMA temp_store=MEMORY')
    c.execute('PRAGMA cache_size=-4000')
    try:
        yield c
    except Exception:
        c.rollback()
        raise
    else:
        c.commit()
    finally:
        c.close()

def _migrate_legacy_bill_series(c: sqlite3.Connection) -> None:
    """Give each bill an identity and safely connect obvious legacy recurrence rows.

    Before 0.4.16, each paid recurrence generated a new independent bill row.
    The old rows did not store their relationship. We reconnect only unambiguous
    adjacent occurrences whose name, amount, recurrence, anchor day and notes
    match exactly, and where the earlier occurrence is already paid.
    """
    marker = c.execute("SELECT 1 FROM app_migrations WHERE name='bill_series_identity_v1'").fetchone()
    if marker:
        # Rows created by a previous version or imported manually may still have
        # a NULL series id. Keep them individually addressable rather than merging.
        c.execute('UPDATE bills SET series_id=id WHERE series_id IS NULL')
        return

    records = [dict(row) for row in c.execute('SELECT * FROM bills ORDER BY due_date,id').fetchall()]
    for bill in records:
        if bill.get('series_id') is None:
            bill['series_id'] = int(bill['id'])
            c.execute('UPDATE bills SET series_id=? WHERE id=?', (int(bill['id']), int(bill['id'])))

    grouped: dict[tuple, list[dict]] = {}
    for bill in records:
        recurrence = (bill.get('recurrence') or 'none').lower()
        if recurrence not in ('monthly', 'quarterly', 'yearly'):
            continue
        try:
            due = date.fromisoformat(str(bill['due_date']))
            anchor = int(bill.get('recurrence_day') or due.day)
            amount = round(float(bill.get('amount_mad') or 0), 2)
        except (TypeError, ValueError):
            continue
        signature = (
            str(bill.get('name') or '').strip().casefold(), amount, recurrence,
            anchor, str(bill.get('notes') or '').strip(),
        )
        grouped.setdefault(signature, []).append(bill)

    for signature_rows in grouped.values():
        signature_rows.sort(key=lambda item: (item['due_date'], int(item['id'])))
        for index, current in enumerate(signature_rows):
            if current.get('status') != 'paid':
                continue
            try:
                due = date.fromisoformat(str(current['due_date']))
                recurrence = (current.get('recurrence') or 'none').lower()
                anchor = int(current.get('recurrence_day') or due.day)
                expected = next_recurrence_date(due, recurrence, anchor)
                paid_at = current.get('paid_at')
                try:
                    paid_day = datetime.fromisoformat(str(paid_at)).date() if paid_at else due
                except (TypeError, ValueError):
                    paid_day = due
                # Legacy payment handling skipped cycles that had already passed
                # when the user paid late. Reproduce that exact next-due rule.
                while expected and expected <= paid_day:
                    expected = next_recurrence_date(expected, recurrence, anchor)
            except (TypeError, ValueError):
                continue
            if expected is None:
                continue
            candidates = [candidate for candidate in signature_rows[index + 1:]
                          if candidate['due_date'] == expected.isoformat()
                          and int(candidate.get('series_id') or candidate['id']) == int(candidate['id'])]
            # Multiple matches are ambiguous, so leave them as separate bills.
            if len(candidates) != 1:
                continue
            candidate = candidates[0]
            root_id = int(current.get('series_id') or current['id'])
            c.execute('UPDATE bills SET series_id=? WHERE id=?', (root_id, int(candidate['id'])))
            candidate['series_id'] = root_id

    c.execute("INSERT INTO app_migrations(name) VALUES('bill_series_identity_v1')")



def _database_backup_files() -> list[Path]:
    backup_dir = DB.parent / 'backups'
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob('xrob_assistant-*.db'), key=lambda path: path.stat().st_mtime, reverse=True)


def _create_database_backup(*, force: bool = False) -> Path | None:
    """Create a consistent SQLite snapshot using the online backup API.

    Automatic startup snapshots are limited to one per local day. Manual
    snapshots are always new. The newest 14 snapshots are retained.
    """
    if not DB.is_file() or DB.stat().st_size <= 0:
        return None
    backup_dir = DB.parent / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    with _DATABASE_BACKUP_LOCK:
        existing = _database_backup_files()
        today_key = datetime.now().astimezone().strftime('%Y%m%d')
        todays = next((item for item in existing if item.name.startswith(f'xrob_assistant-{today_key}-')), None)
        if todays and not force:
            # Refresh today's snapshot when the live database has changed since
            # the previous one; a restart must never reuse a stale pre-migration copy.
            try:
                database_mtime = DB.stat().st_mtime
                wal_path = Path(str(DB) + '-wal')
                if wal_path.is_file():
                    database_mtime = max(database_mtime, wal_path.stat().st_mtime)
                if todays.stat().st_mtime >= database_mtime:
                    return todays
            except OSError:
                pass

        stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
        target = backup_dir / f'xrob_assistant-{stamp}.db'
        temporary = backup_dir / f'.{target.name}.tmp'
        source = sqlite3.connect(str(DB), timeout=30)
        destination = sqlite3.connect(str(temporary), timeout=30)
        try:
            source.execute('PRAGMA busy_timeout=30000')
            source.backup(destination, pages=256, sleep=0.05)
            check = destination.execute('PRAGMA quick_check').fetchone()
            if not check or str(check[0]).lower() != 'ok':
                raise RuntimeError('The database backup did not pass SQLite quick_check.')
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.replace(temporary, target)

        # Keep a bounded history. If pruning fails, retain the new good backup
        # and log a warning rather than failing the user's main operation.
        try:
            for stale in _database_backup_files()[DATABASE_BACKUP_RETENTION:]:
                stale.unlink(missing_ok=True)
        except OSError as exc:
            print(f'Database backup retention warning: {exc}', flush=True)
        return target


def _latest_database_backup() -> Path | None:
    files = _database_backup_files()
    return files[0] if files else None


def _enable_database_wal() -> None:
    connection = sqlite3.connect(str(DB), timeout=30)
    try:
        connection.execute('PRAGMA busy_timeout=30000')
        mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()
        if not mode or str(mode[0]).lower() != 'wal':
            print('SQLite WAL mode is unavailable; continuing with the database default.', flush=True)
    finally:
        connection.close()

def init_db():
    # Snapshot existing user data before schema migrations. A failed backup is
    # non-fatal, but is always visible in the add-on log.
    try:
        if DB.is_file() and DB.stat().st_size > 0:
            _create_database_backup(force=False)
        _enable_database_wal()
    except Exception as exc:
        print(f'Pre-startup database backup/WAL warning: {exc}', flush=True)
    with connect() as c:
        c.executescript(SCHEMA)
        # Preserve task completion dates going forward. Older databases keep their
        # existing status values; we do not guess when historic tasks were done.
        task_columns = {r['name'] for r in c.execute('PRAGMA table_info(tasks)').fetchall()}
        if 'completed_at' not in task_columns:
            c.execute('ALTER TABLE tasks ADD COLUMN completed_at TEXT')
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
            status TEXT NOT NULL DEFAULT 'pending', assignment_source TEXT NOT NULL DEFAULT 'automatic', notified_date TEXT, confirmed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT,
            FOREIGN KEY(batch_id) REFERENCES meal_batches(id) ON DELETE SET NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_daily_meals_date ON daily_meals(plan_date, status)')
        daily_meal_columns = {r['name'] for r in c.execute('PRAGMA table_info(daily_meals)').fetchall()}
        if 'suggestion_notified_date' not in daily_meal_columns:
            c.execute('ALTER TABLE daily_meals ADD COLUMN suggestion_notified_date TEXT')
        if 'checkin_notified_date' not in daily_meal_columns:
            c.execute('ALTER TABLE daily_meals ADD COLUMN checkin_notified_date TEXT')
        if 'assignment_source' not in daily_meal_columns:
            c.execute("ALTER TABLE daily_meals ADD COLUMN assignment_source TEXT NOT NULL DEFAULT 'unknown'")
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
        if 'series_id' not in bill_columns:
            c.execute('ALTER TABLE bills ADD COLUMN series_id INTEGER')
        _migrate_legacy_bill_series(c)
        c.execute('CREATE INDEX IF NOT EXISTS idx_bills_series_status_date ON bills(series_id,status,due_date,id)')
        routine_columns = {r['name'] for r in c.execute('PRAGMA table_info(routines)').fetchall()}
        if 'routine_time' not in routine_columns:
            c.execute("ALTER TABLE routines ADD COLUMN routine_time TEXT DEFAULT '10:00'")
        if 'routine_late_time' not in routine_columns:
            c.execute("ALTER TABLE routines ADD COLUMN routine_late_time TEXT DEFAULT NULL")
        if 'routine_rollover_after_late' not in routine_columns:
            c.execute('ALTER TABLE routines ADD COLUMN routine_rollover_after_late INTEGER NOT NULL DEFAULT 0')
        routine_log_columns = {r['name'] for r in c.execute('PRAGMA table_info(routine_logs)').fetchall()}
        if 'source' not in routine_log_columns:
            c.execute("ALTER TABLE routine_logs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        # Old versions did not record why a routine was completed. Recover the
        # activity-linked rows so deleting an old weight/session can also undo
        # the matching automatic routine check. Other historic checks stay manual.
        activity_log_migration = c.execute("SELECT 1 FROM app_migrations WHERE name='routine_activity_log_sources_v1'").fetchone()
        if not activity_log_migration:
            c.execute('''UPDATE routine_logs SET source='weight'
                         WHERE EXISTS (SELECT 1 FROM routines r
                                       WHERE r.id=routine_logs.routine_id
                                         AND (lower(r.name) LIKE '%weight%' OR lower(r.name) LIKE '%weigh%'))
                           AND EXISTS (SELECT 1 FROM weights w WHERE w.date=routine_logs.completed_at)''')
            c.execute('''UPDATE routine_logs SET source='treadmill'
                         WHERE EXISTS (SELECT 1 FROM routines r
                                       WHERE r.id=routine_logs.routine_id AND lower(r.name) LIKE '%treadmill%')
                           AND EXISTS (SELECT 1 FROM treadmill t WHERE t.date=routine_logs.completed_at)''')
            c.execute("INSERT INTO app_migrations(name) VALUES('routine_activity_log_sources_v1')")
        # Keep one completion row per routine and day, including concurrent
        # Telegram, website and activity-triggered updates.
        c.execute('''DELETE FROM routine_logs
                     WHERE id NOT IN (SELECT MIN(id) FROM routine_logs GROUP BY routine_id,completed_at)''')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_logs_unique_day ON routine_logs(routine_id,completed_at)')
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
    cleared = _clear_resolved_mikasa_learning(str(getattr(mikasa_persona, 'PERSONA_VERSION', 'unknown')))
    if cleared:
        print(f'Cleared {cleared} unresolved Mikasa message(s) after persona update.', flush=True)
    try:
        maintain_meal_schedule()
    except Exception as exc:
        print(f'Initial meal schedule maintenance warning: {exc}', flush=True)
    background = [asyncio.create_task(meal_schedule_loop()), asyncio.create_task(mikasa_update_loop())]
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

app = FastAPI(title='XROB Assistant API', version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=WWW), name='static')


@app.middleware('http')
async def add_security_and_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
    )
    if request.url.path == '/' or request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def home():
    return FileResponse(WWW/'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/api/health')
def health():
    try:
        with connect() as c:
            c.execute('SELECT 1').fetchone()
        return {
            'status': 'ok', 'app': 'XROB Assistant', 'version': APP_VERSION,
            'release_channel': APP_RELEASE_CHANNEL, 'database': 'connected',
            'sqlite_version': sqlite3.sqlite_version,
            'mikasa_persona_version': getattr(mikasa_persona, 'PERSONA_VERSION', 'unknown'),
            'mikasa_persona_source': MIKASA_PERSONA_SOURCE,
        }
    except Exception:
        raise HTTPException(503, 'Database check failed. See the add-on log for details.')


@app.get('/api/system/diagnostics')
def system_diagnostics():
    """Safe operational diagnostics; never returns bot tokens or private records."""
    database_ok = False
    integrity = 'not checked'
    try:
        with connect() as c:
            result = c.execute('PRAGMA quick_check').fetchone()
            integrity = str(result[0]) if result else 'unknown'
            database_ok = integrity.lower() == 'ok'
    except Exception:
        integrity = 'failed — inspect add-on logs'
    backup = _latest_database_backup()
    try:
        db_size = DB.stat().st_size if DB.exists() else 0
    except OSError:
        db_size = 0
    try:
        free_bytes = shutil.disk_usage(DB.parent).free
    except OSError:
        free_bytes = None
    return {
        'app_version': APP_VERSION,
        'release_channel': APP_RELEASE_CHANNEL,
        'persona_version': str(getattr(mikasa_persona, 'PERSONA_VERSION', 'unknown')),
        'persona_source': MIKASA_PERSONA_SOURCE,
        'database_status': 'healthy' if database_ok else 'needs_attention',
        'database_integrity': integrity,
        'database_size_bytes': db_size,
        'sqlite_version': sqlite3.sqlite_version,
        'backup_count': len(_database_backup_files()),
        'latest_backup': backup.name if backup else None,
        'latest_backup_modified': datetime.fromtimestamp(backup.stat().st_mtime).astimezone().isoformat(timespec='minutes') if backup else None,
        'free_disk_bytes': free_bytes,
        'telegram_configured': bool(TOKEN and ALLOWED_CHAT),
    }


@app.post('/api/system/backup')
def create_database_backup():
    global _LAST_MANUAL_BACKUP_AT
    try:
        with _MANUAL_BACKUP_LOCK:
            now = clock.monotonic()
            elapsed = now - _LAST_MANUAL_BACKUP_AT
            if _LAST_MANUAL_BACKUP_AT and elapsed < MANUAL_BACKUP_COOLDOWN_SECONDS:
                retry_after = max(1, int(MANUAL_BACKUP_COOLDOWN_SECONDS - elapsed))
                raise HTTPException(429, f'Please wait {retry_after} seconds before creating another manual backup.')
            backup = _create_database_backup(force=True)
            if backup is None:
                raise HTTPException(409, 'The database has not been created yet.')
            _LAST_MANUAL_BACKUP_AT = now
        return {'created': True, 'filename': backup.name, 'size_bytes': backup.stat().st_size}
    except HTTPException:
        raise
    except Exception:
        print('Manual database backup failed.', flush=True)
        raise HTTPException(500, 'Database backup failed. See the add-on log for details.')


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
    rebuild_start = date.today() if plan_day == date.today() else date.today() + timedelta(days=1)
    rebuilt = _rebuild_automatic_meal_schedule(rebuild_start)
    return {'plan_date': plan_day.isoformat(), 'restored': True, **rebuilt}


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


TELEGRAM_PREFERENCE_DEFAULTS = {
    'morning_report_enabled': '0',
    'morning_report_time': '07:30',
    'playful_messages_enabled': '0',
    'playful_frequency': 'few',
    'mikasa_learning_enabled': '1',
    'mikasa_auto_update_enabled': '0',
}


def _telegram_setting_get(key: str, default: str = '') -> str:
    row = one('SELECT value FROM telegram_state WHERE key=?', (f'setting:{key}',))
    return str(row['value']) if row else default


def _telegram_setting_set(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO telegram_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (f'setting:{key}', str(value)))


def get_telegram_preferences() -> dict[str, Any]:
    values = {key: _telegram_setting_get(key, default)
              for key, default in TELEGRAM_PREFERENCE_DEFAULTS.items()}
    return {
        'morning_report_enabled': values['morning_report_enabled'] == '1',
        'morning_report_time': values['morning_report_time'],
        'playful_messages_enabled': values['playful_messages_enabled'] == '1',
        'playful_frequency': values['playful_frequency'],
        'mikasa_learning_enabled': values['mikasa_learning_enabled'] == '1',
        'mikasa_auto_update_enabled': values['mikasa_auto_update_enabled'] == '1',
        'telegram_configured': bool(TOKEN and ALLOWED_CHAT),
        'timezone': os.getenv('TZ', 'Local add-on time'),
    }


@app.get('/api/telegram-settings')
def read_telegram_settings():
    return get_telegram_preferences()


@app.post('/api/telegram-settings')
def save_telegram_settings(payload: Payload):
    data = payload.data
    current = get_telegram_preferences()
    updates: dict[str, str] = {}
    for field, key in (
        ('morning_report_enabled', 'morning_report_enabled'),
        ('playful_messages_enabled', 'playful_messages_enabled'),
        ('mikasa_learning_enabled', 'mikasa_learning_enabled'),
        ('mikasa_auto_update_enabled', 'mikasa_auto_update_enabled'),
    ):
        if field in data:
            if not isinstance(data[field], bool):
                raise HTTPException(400, f'{field} must be true or false.')
            updates[key] = '1' if data[field] else '0'
    if 'morning_report_time' in data:
        value = str(data['morning_report_time'] or '').strip()
        try:
            parsed = datetime.strptime(value, '%H:%M')
            if parsed.strftime('%H:%M') != value:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Choose a valid morning report time in 24-hour HH:MM format.')
        updates['morning_report_time'] = value
    if 'playful_frequency' in data:
        value = str(data['playful_frequency'] or '').strip().lower()
        if value not in ('few', 'daily'):
            raise HTTPException(400, 'Choose either a few times per week or daily.')
        updates['playful_frequency'] = value
    for key, value in updates.items():
        _telegram_setting_set(key, value)
    return get_telegram_preferences()


# Mikasa voice-only GitHub updater --------------------------------------------
def _mikasa_version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r'(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)', str(value or ''))
    if not match:
        raise ValueError('The persona file does not declare a numeric version.')
    return tuple(int(part) for part in match.group(1).split('.'))


def _mikasa_update_status() -> dict[str, Any]:
    return {
        'current_version': str(getattr(mikasa_persona, 'PERSONA_VERSION', 'unknown')),
        'source': MIKASA_PERSONA_SOURCE,
        'auto_update_enabled': _telegram_setting_get('mikasa_auto_update_enabled', '0') == '1',
        'last_checked': _telegram_setting_get('mikasa_update_last_checked', ''),
        'last_result': _telegram_setting_get('mikasa_update_last_result', ''),
        'latest_version': _telegram_setting_get('mikasa_update_latest_version', ''),
    }


async def _download_mikasa_persona_source() -> str:
    """Fetch only the fixed project-owned raw GitHub file over HTTPS."""
    parsed = urlparse(MIKASA_GITHUB_RAW_URL)
    if parsed.scheme != 'https' or parsed.hostname != MIKASA_GITHUB_RAW_HOST:
        raise RuntimeError('Mikasa update source is not the trusted HTTPS GitHub raw host.')
    import httpx
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        response = await client.get(MIKASA_GITHUB_RAW_URL, headers={'Accept': 'text/plain', 'Cache-Control': 'no-cache'})
        response.raise_for_status()
        final_url = urlparse(str(response.url))
        if final_url.scheme != 'https' or final_url.hostname != MIKASA_GITHUB_RAW_HOST:
            raise RuntimeError('GitHub redirected the persona update outside the trusted raw host.')
        if len(response.content) > 512 * 1024:
            raise RuntimeError('The downloaded Mikasa file is unexpectedly large; update cancelled.')
        return response.content.decode('utf-8')


async def _check_and_install_mikasa_update() -> dict[str, Any]:
    """Check GitHub, validate a persona, back up the override, then hot-load it."""
    global mikasa_persona, MIKASA_PERSONA_SOURCE
    async with _MIKASA_UPDATE_LOCK:
        checked_at = datetime.now().astimezone().isoformat(timespec='seconds')
        try:
            source = await _download_mikasa_persona_source()
            temp_path = DATA / '.mikasa_persona.download.py'
            temp_path.write_text(source, encoding='utf-8')
            try:
                candidate = _load_persona_file(temp_path, 'xrob_mikasa_persona_remote_candidate')
                remote_version = str(getattr(candidate, 'PERSONA_VERSION', '')).strip()
                current_version = str(getattr(mikasa_persona, 'PERSONA_VERSION', 'unknown'))
                if _mikasa_version_tuple(remote_version) <= _mikasa_version_tuple(current_version):
                    temp_path.unlink(missing_ok=True)
                    _telegram_setting_set('mikasa_update_last_checked', checked_at)
                    _telegram_setting_set('mikasa_update_latest_version', remote_version)
                    _telegram_setting_set('mikasa_update_last_result', f'Already up to date · {current_version}')
                    return {'updated': False, 'message': f'Mikasa is already up to date ({current_version}).', **_mikasa_update_status()}

                override = DATA / 'mikasa_persona.py'
                backup_path = None
                if override.exists():
                    backup_dir = DATA / 'backups'
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                    backup_path = backup_dir / f'mikasa_persona-{current_version}-{stamp}.py'
                    shutil.copy2(override, backup_path)
                elif Path(getattr(mikasa_persona, '__xrob_source_path__', '')).is_file():
                    backup_dir = DATA / 'backups'
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                    backup_path = backup_dir / f'mikasa_persona-bundled-{current_version}-{stamp}.py'
                    shutil.copy2(Path(mikasa_persona.__xrob_source_path__), backup_path)

                # Atomic replacement: the active file is never partially written.
                os.replace(temp_path, override)
                try:
                    loaded = _load_persona_file(override, 'xrob_mikasa_persona_override')
                except Exception:
                    if backup_path and backup_path.exists():
                        shutil.copy2(backup_path, override)
                    else:
                        override.unlink(missing_ok=True)
                    raise
                mikasa_persona = loaded
                MIKASA_PERSONA_SOURCE = 'config override'
                _telegram_setting_set('mikasa_update_last_checked', checked_at)
                _telegram_setting_set('mikasa_update_latest_version', remote_version)
                cleared_messages = _clear_resolved_mikasa_learning(remote_version)
                _telegram_setting_set('mikasa_update_last_result', f'Updated to {remote_version}')
                if cleared_messages:
                    _telegram_setting_set('mikasa_update_last_result', f'Updated to {remote_version} · cleared {cleared_messages} resolved message(s)')
                return {
                    'updated': True,
                    'cleared_missed_messages': cleared_messages,
                    'message': f'Mikasa updated to {remote_version}. Her new voice is active now. Cleared {cleared_messages} resolved missed-message entries.',
                    'backup': str(backup_path) if backup_path else '',
                    **_mikasa_update_status(),
                }
            finally:
                temp_path.unlink(missing_ok=True)
        except Exception as exc:
            _telegram_setting_set('mikasa_update_last_checked', checked_at)
            _telegram_setting_set('mikasa_update_last_result', f'Update failed · {str(exc)[:240]}')
            raise HTTPException(502, f'Mikasa update failed safely: {exc}')


@app.get('/api/mikasa/update-status')
def read_mikasa_update_status():
    return _mikasa_update_status()


@app.post('/api/mikasa/update')
async def install_mikasa_update():
    return await _check_and_install_mikasa_update()


async def mikasa_update_loop():
    """Check once per day when the user has explicitly enabled auto-updates."""
    while True:
        try:
            if _telegram_setting_get('mikasa_auto_update_enabled', '0') == '1':
                last = _telegram_setting_get('mikasa_update_last_checked', '')
                try:
                    checked = datetime.fromisoformat(last) if last else None
                except ValueError:
                    checked = None
                if checked is None or (datetime.now().astimezone() - checked.astimezone()).total_seconds() >= MIKASA_UPDATE_CHECK_INTERVAL_SECONDS:
                    await _check_and_install_mikasa_update()
        except Exception as exc:
            print(f'Mikasa automatic update warning: {exc}', flush=True)
        await asyncio.sleep(15 * 60)


# Mikasa Learning Inbox -------------------------------------------------------
# Unknown messages are captured only from the configured Telegram chat, after
# the command engine reaches its explicit generic fallback. They remain local
# in the existing SQLite database until reviewed, taught, ignored or deleted.
MIKASA_UNKNOWN_REPLY_PREFIX = 'I can help with your day. Use the buttons below, or send:'

MIKASA_LEARNING_MAX_TEXT = 1200

# This report is intentionally explicit: it documents actual deterministic
# routes, social intents and attachment limitations in this release. Examples
# are representative phrases, not a promise to understand arbitrary wording.
MIKASA_CAPABILITY_CATALOG = [
    ('Daily planning', ['/today', 'today please', "what's on today", 'tomorrow plan', '/tomorrow', '/week', 'next 7 days', 'what needs my attention', 'what did I finish today']),
    ('Tasks', ['task Study tomorrow at 18:00', 'task done Study', '/tasks', 'what are my tasks', 'edit task Study | due_date=tomorrow', 'delete task Study']),
    ('Events and appointments', ['/event Dentist|25/11/26 10:00', 'add event Dentist|25/11/26 10:00', '/agenda', 'show my agenda', 'how many days left for exams']),
    ('Routines', ['/routines', 'what are my routines', 'done Shower', 'add routine Stretching every 2 days at 08:00', 'edit routine Shower | interval_days=2']),
    ('Fitness and weight', ['weight 82.4', 'treadmill 30', 'treadmill 30 yesterday', 'log workout Upper body', '/fitness', '/fitnessweek', 'what is my current weight']),
    ('Meals and cooking', ['/meals', '/choosemeal', '/mealplan', "what's for dinner", 'YES', 'NO', 'undo eaten', '/undo_eaten', '/addrecipe', '/stock Chicken|3', '/homemeals', '/outsidemeals', 'edit recipe Chicken | plates_ready=4']),
    ('Shopping', ['/shopping', '/additem', 'bought tomatoes', 'edit shopping tomatoes | checked=yes']),
    ('Bills and documents', ['/bills', 'what do I owe', 'paid Electricity', 'add bill Internet|299|30/11/26|monthly', '/documents', 'add document Passport|31/12/27']),
    ('App awareness', ['/status', '/about', '/features', 'app status', 'my progress', 'how am I doing', 'how are my meals', 'how does meal scheduling work']),
    ('Social conversation', ['hello / good morning', 'thank you', 'I love you', 'I need a hug', 'motivate me', 'I am stressed', 'I passed my exam', 'are you a real person']),
    ('Settings and help', ['/help', '/about', '/features', 'how do I edit records', 'what can you not do', 'cancel', 'back']),
]

MIKASA_ATTACHMENT_CAPABILITIES = [
    ('Text and captions', 'Parsed and routed through the normal Mikasa command/social handler.'),
    ('Photos and images', 'Acknowledged; image contents are not analysed in this local release.'),
    ('Voice notes and audio', 'Acknowledged; audio is not transcribed in this local release.'),
    ('Videos and animations', 'Acknowledged; video contents are not analysed in this local release.'),
    ('Documents and files', 'Acknowledged; file contents are not opened or extracted from Telegram.'),
    ('Stickers, locations, contacts and other supported Telegram message types', 'Acknowledged; Mikasa asks what you want her to do with the item.'),
]


def _normalize_mikasa_learning_text(value: str) -> str:
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = re.sub(r'\s+', ' ', text.strip().casefold())
    return text.strip(' .!?؛،')[:MIKASA_LEARNING_MAX_TEXT]


def _record_mikasa_unhandled_message(raw: str) -> int | None:
    # Save or group a message only when Mikasa reaches the unknown fallback.
    if _telegram_setting_get('mikasa_learning_enabled', '1') != '1':
        return None
    sample = str(raw or '').strip()
    if not sample:
        return None
    sample = sample[:MIKASA_LEARNING_MAX_TEXT]
    normalized = _normalize_mikasa_learning_text(sample)
    if len(normalized) < 2:
        return None
    fingerprint = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    with connect() as c:
        c.execute('''
            INSERT INTO mikasa_learning_log(fingerprint,sample_text,normalized_text)
            VALUES(?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                occurrences=MIN(mikasa_learning_log.occurrences+1,999999),
                last_seen_at=CURRENT_TIMESTAMP,
                sample_text=CASE WHEN length(excluded.sample_text)>length(mikasa_learning_log.sample_text)
                                 THEN excluded.sample_text ELSE mikasa_learning_log.sample_text END
        ''', (fingerprint, sample, normalized))
        row = c.execute('SELECT id FROM mikasa_learning_log WHERE fingerprint=?', (fingerprint,)).fetchone()
        # Keep the inbox bounded. Taught phrases are never purged automatically.
        c.execute('''DELETE FROM mikasa_learning_log
                     WHERE status IN ('new','ignored') AND id NOT IN (
                       SELECT id FROM mikasa_learning_log WHERE status IN ('new','ignored')
                       ORDER BY last_seen_at DESC,id DESC LIMIT 500
                     )''')
    return int(row['id']) if row else None


def _mikasa_learned_reply(raw: str) -> str | None:
    # Use a taught exact phrase only after the command engine gets first refusal.
    normalized = _normalize_mikasa_learning_text(raw)
    if len(normalized) < 2:
        return None
    row = one('''SELECT teaching_reply FROM mikasa_learning_log
                 WHERE status='learned' AND normalized_text=? AND teaching_reply<>''
                 ORDER BY updated_at DESC,id DESC LIMIT 1''', (normalized,))
    return str(row['teaching_reply']).strip() if row and str(row['teaching_reply']).strip() else None


def _mikasa_learning_counts() -> dict[str, int]:
    result = {'new': 0, 'learned': 0, 'ignored': 0}
    with connect() as c:
        for row in c.execute('SELECT status,COUNT(*) AS total FROM mikasa_learning_log GROUP BY status'):
            result[str(row['status'])] = int(row['total'])
    return result


def _clear_resolved_mikasa_learning(persona_version: str) -> int:
    """Clear unresolved messages whenever a genuinely new persona version is activated.

    Learned and ignored records are retained; only the open missed-message inbox is
    cleared because those patterns were supplied for the persona improvement.
    """
    version = str(persona_version or 'unknown').strip()
    previous = _telegram_setting_get('mikasa_learning_persona_version', '')
    if not version or version == previous:
        return 0
    with connect() as c:
        cur = c.execute("DELETE FROM mikasa_learning_log WHERE status='new'")
        deleted = int(cur.rowcount or 0)
        c.execute("INSERT INTO telegram_state(key,value) VALUES('setting:mikasa_learning_persona_version',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))
    return deleted


@app.get('/api/mikasa/learning')
def list_mikasa_learning(status: str = 'open', limit: int = Query(100, ge=1, le=500)):
    allowed = {'open', 'new', 'learned', 'ignored', 'all'}
    selected = str(status or 'open').lower()
    if selected not in allowed:
        raise HTTPException(400, 'Choose open, new, learned, ignored or all.')
    where = "status='new'" if selected == 'open' else ('1=1' if selected == 'all' else 'status=?')
    params: tuple[Any, ...] = () if selected in ('open', 'all') else (selected,)
    items = rows(f'''SELECT id,sample_text,normalized_text,occurrences,status,teaching_reply,intent_note,
                            first_seen_at,last_seen_at,updated_at
                     FROM mikasa_learning_log WHERE {where}
                     ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'learned' THEN 1 ELSE 2 END,
                              last_seen_at DESC,id DESC LIMIT ?''', (*params, limit))
    counts = _mikasa_learning_counts()
    return {'items': items, 'counts': counts, 'total': sum(counts.values()), 'capture_enabled': _telegram_setting_get('mikasa_learning_enabled', '1') == '1'}


@app.get('/api/mikasa/learning/export.txt')
def export_mikasa_missed_messages():
    """Download the missed-message inbox as plain text for a future persona update."""
    items = rows('''SELECT sample_text,occurrences,first_seen_at,last_seen_at
                       FROM mikasa_learning_log WHERE status='new'
                       ORDER BY first_seen_at,id''')
    lines = [
        'MIKASA — MISSED TELEGRAM MESSAGES',
        'Copy or send this text log with the XROB project when requesting persona improvements.',
        '',
    ]
    if not items:
        lines.append('No missed text messages have been recorded.')
    else:
        for index, item in enumerate(items, 1):
            occurrences = max(1, int(item.get('occurrences') or 1))
            lines.append(f'{index}. Seen {occurrences} time' + ('s' if occurrences != 1 else ''))
            lines.append(f"   First seen: {item.get('first_seen_at') or 'unknown'}")
            lines.append(f"   Last seen:  {item.get('last_seen_at') or 'unknown'}")
            lines.append('   Message:')
            lines.extend('   ' + part for part in str(item.get('sample_text') or '').splitlines())
            lines.append('')
    return PlainTextResponse(
        '\n'.join(lines).rstrip() + '\n',
        headers={'Content-Disposition': 'attachment; filename="mikasa-missed-messages.txt"'},
    )


@app.get('/api/mikasa/coverage.txt')
def export_mikasa_coverage_report():
    """Export a versioned map of supported intents, known limitations and missed text."""
    counts = _mikasa_learning_counts()
    missed = rows("""SELECT sample_text,occurrences,first_seen_at,last_seen_at
                    FROM mikasa_learning_log WHERE status='new'
                    ORDER BY last_seen_at DESC,id DESC LIMIT 500""")
    learned = rows("""SELECT sample_text,occurrences,updated_at FROM mikasa_learning_log
                     WHERE status='learned' ORDER BY updated_at DESC,id DESC LIMIT 200""")
    ignored = rows("""SELECT sample_text,occurrences,updated_at FROM mikasa_learning_log
                     WHERE status='ignored' ORDER BY updated_at DESC,id DESC LIMIT 200""")
    lines = [
        'XROB ASSISTANT — MIKASA MESSAGE COVERAGE REPORT',
        f'App version: {APP_VERSION} ({APP_RELEASE_CHANNEL})',
        f'Persona version: {getattr(mikasa_persona, "PERSONA_VERSION", "unknown")}',
        f'Generated: {datetime.now().astimezone().isoformat(timespec="seconds")}',
        '',
        'HOW TO READ THIS REPORT',
        'The examples below are verified command/social patterns and representative natural-language aliases.',
        'They are not an exhaustive list of every sentence Mikasa can interpret.',
        'XROB uses deterministic local handlers; it does not run a general-purpose cloud AI model.',
        '',
        'SUPPORTED APP INTENTS AND EXAMPLES',
    ]
    for name, examples in MIKASA_CAPABILITY_CATALOG:
        lines.append(f'[{name}]')
        lines.extend(f'  - {example}' for example in examples)
        lines.append('')
    lines.extend(['TELEGRAM ATTACHMENT HANDLING'])
    for name, description in MIKASA_ATTACHMENT_CAPABILITIES:
        lines.append(f'- {name}: {description}')
    lines.extend([
        '',
        'LOCAL MESSAGE COVERAGE COUNTS',
        f'- Unresolved messages needing review: {counts.get("new", 0)}',
        f'- Learned exact phrases: {counts.get("learned", 0)}',
        f'- Ignored phrases: {counts.get("ignored", 0)}',
        '',
        'UNRESOLVED MESSAGES TO IMPROVE',
    ])
    if missed:
        for index, item in enumerate(missed, 1):
            lines.append(f'{index}. Seen {max(1, int(item.get("occurrences") or 1))} time(s) · last {item.get("last_seen_at") or "unknown"}')
            lines.extend('   ' + part for part in str(item.get('sample_text') or '').splitlines())
    else:
        lines.append('None currently recorded.')
    lines.extend(['', 'ALREADY LEARNED EXACT PHRASES'])
    if learned:
        lines.extend(f'- {item.get("sample_text")} (seen {max(1, int(item.get("occurrences") or 1))} time(s))' for item in learned)
    else:
        lines.append('None.')
    lines.extend(['', 'INTENTIONALLY IGNORED PHRASES'])
    if ignored:
        lines.extend(f'- {item.get("sample_text")}' for item in ignored)
    else:
        lines.append('None.')
    lines.extend([
        '',
        'NOTES',
        '- Only text messages that reach Mikasa’s explicit unhandled fallback enter the unresolved list.',
        '- Messages with captions are handled as text. Media without captions may be acknowledged but not analysed.',
        '- User data and this report are stored locally in the XROB add-on configuration directory.',
    ])
    return PlainTextResponse(
        '\n'.join(lines).rstrip() + '\n',
        headers={'Content-Disposition': 'attachment; filename="mikasa-message-coverage-v1.0.2.txt"'},
    )


@app.patch('/api/mikasa/learning/{item_id}')
def update_mikasa_learning(item_id: int, payload: Payload):
    current = one('SELECT * FROM mikasa_learning_log WHERE id=?', (item_id,))
    if not current:
        raise HTTPException(404, 'Learning entry not found.')
    data = payload.data
    status = str(data.get('status', current['status']) or current['status']).strip().lower()
    if status not in ('new', 'learned', 'ignored'):
        raise HTTPException(400, 'Status must be new, learned or ignored.')
    reply = str(data.get('teaching_reply', current['teaching_reply']) or '').strip()
    note = str(data.get('intent_note', current['intent_note']) or '').strip()
    if len(reply) > 1200:
        raise HTTPException(400, 'Mikasa’s learned reply must be 1,200 characters or fewer.')
    if len(note) > 500:
        raise HTTPException(400, 'The learning note must be 500 characters or fewer.')
    if status == 'learned' and not reply:
        raise HTTPException(400, 'Write the reply Mikasa should use before teaching this phrase.')
    if status == 'learned':
        duplicate = one("SELECT id FROM mikasa_learning_log WHERE status='learned' AND normalized_text=? AND id<>?", (current['normalized_text'], item_id))
        if duplicate:
            raise HTTPException(409, 'This exact phrase is already taught in another entry.')
    with connect() as c:
        c.execute('''UPDATE mikasa_learning_log
                     SET status=?,teaching_reply=?,intent_note=?,updated_at=CURRENT_TIMESTAMP
                     WHERE id=?''', (status, reply, note, item_id))
    return one('''SELECT id,sample_text,normalized_text,occurrences,status,teaching_reply,intent_note,
                         first_seen_at,last_seen_at,updated_at
                  FROM mikasa_learning_log WHERE id=?''', (item_id,))


@app.delete('/api/mikasa/learning/{item_id}')
def delete_mikasa_learning(item_id: int):
    with connect() as c:
        cur = c.execute('DELETE FROM mikasa_learning_log WHERE id=?', (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, 'Learning entry not found.')
    return {'deleted': True, 'id': item_id}


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
    if resource == 'routines':
        return rows('''SELECT * FROM routines
                       ORDER BY CASE WHEN next_due<=? THEN 0 ELSE 1 END,
                                next_due,COALESCE(routine_time,'23:59'),id DESC LIMIT ?''',
                    (date.today().isoformat(), limit))
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
    """Set the next due date from the latest retained occurrence of this meal."""
    row = c.execute('SELECT eating_interval_days FROM recipes WHERE id=?', (recipe_id,)).fetchone()
    if not row:
        return
    latest = c.execute('SELECT MAX(plan_date) AS last_date FROM daily_meals WHERE recipe_id=?', (recipe_id,)).fetchone()
    next_due = (date.fromisoformat(latest['last_date']) + timedelta(days=max(1, int(row['eating_interval_days'] or 1)))).isoformat() if latest and latest['last_date'] else date.today().isoformat()
    c.execute('UPDATE recipes SET next_routine_date=? WHERE id=?', (next_due, recipe_id))


def _validate_recipe_interval_for_date(c: sqlite3.Connection, recipe_id: int, plan_day: date) -> None:
    """Reject a manual repeat that is earlier than the meal's saved minimum gap."""
    recipe = c.execute('SELECT name,eating_interval_days FROM recipes WHERE id=? AND is_active=1', (recipe_id,)).fetchone()
    if not recipe:
        raise ValueError('This meal is no longer available. Refresh the page and choose another.')
    interval = max(1, int(recipe['eating_interval_days'] or 1))
    previous = c.execute(
        "SELECT MAX(plan_date) AS last_date FROM daily_meals WHERE recipe_id=? AND plan_date<? AND status IN ('pending','eaten')",
        (recipe_id, plan_day.isoformat()),
    ).fetchone()
    if previous and previous['last_date']:
        earliest = date.fromisoformat(previous['last_date']) + timedelta(days=interval)
        if plan_day < earliest:
            raise ValueError(
                f"{recipe['name']} was last planned for {previous['last_date']}. "
                f"Its every-{interval}-day interval means it can be scheduled again from {earliest.isoformat()} or later."
            )
    # A later manual choice is just as important as an earlier occurrence.
    # Future automatic suggestions are intentionally ignored here because the
    # weekly replan will replace them; explicit user choices are never silently moved.
    following = c.execute(
        "SELECT MIN(plan_date) AS next_date FROM daily_meals WHERE recipe_id=? AND plan_date>? "
        "AND (status='eaten' OR (status='pending' AND assignment_source='manual'))",
        (recipe_id, plan_day.isoformat()),
    ).fetchone()
    if following and following['next_date']:
        next_day = date.fromisoformat(following['next_date'])
        latest_allowed = next_day - timedelta(days=interval)
        if plan_day > latest_allowed:
            earliest_after = next_day + timedelta(days=interval)
            raise ValueError(
                f"{recipe['name']} is already manually planned for {next_day.isoformat()}. "
                f"With its every-{interval}-day interval, choose a date on or before {latest_allowed.isoformat()}, "
                f"or on or after {earliest_after.isoformat()}. Change the later manual choice first if needed."
            )


def _rebase_recipe_schedule_after_manual(c: sqlite3.Connection, recipe_id: int, plan_day: date) -> int:
    """Restart this meal's repeat interval from an explicit user choice.

    Future automatic suggestions are disposable and are rebuilt from the latest
    retained occurrence plus the saved interval. Manual choices and eaten history
    are never moved or deleted.
    """
    removed = c.execute("DELETE FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>? AND assignment_source IN ('automatic','unknown')", (recipe_id, plan_day.isoformat())).rowcount
    _recalculate_recipe_due(c, recipe_id)
    return int(removed)


def _rebuild_automatic_meal_schedule(
    start: date,
    *,
    clear_skips: bool = False,
    replace_manual: bool = False,
) -> dict:
    """Rebuild future automatic plates after a user changes the weekly plan.

    Past records, eaten plates and future manual choices remain fixed. Normal
    weekly edits preserve day-off markers; the explicit Reset action may clear
    them and replace all pending choices.
    """
    today = date.today()
    start = max(start, today)
    horizon_end = today + timedelta(days=44)
    if start > horizon_end:
        return {'cleared_automatic': 0, 'created': 0, 'meals': [], 'start_date': start.isoformat()}

    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if replace_manual:
            cleared = c.execute(
                "DELETE FROM daily_meals WHERE status='pending' AND plan_date>=?",
                (start.isoformat(),),
            ).rowcount
        else:
            cleared = c.execute(
                "DELETE FROM daily_meals WHERE status='pending' AND plan_date>=? AND assignment_source IN ('automatic','unknown')",
                (start.isoformat(),),
            ).rowcount
        if clear_skips:
            c.execute('DELETE FROM meal_schedule_skips WHERE plan_date>=?', (start.isoformat(),))
        recipe_ids = [int(row['id']) for row in c.execute('SELECT id FROM recipes WHERE is_active=1').fetchall()]
        for recipe_id in recipe_ids:
            _recalculate_recipe_due(c, recipe_id)

    days = (horizon_end - start).days + 1
    generated = _generate_daily_meals(days, start)
    return {
        'start_date': start.isoformat(),
        'cleared_automatic': int(cleared),
        'created': int(generated.get('created', 0)),
        'meals': generated.get('meals', []),
    }


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


def _ensure_home_plate_available(c: sqlite3.Connection, recipe_id: int, plan_day: date) -> str | None:
    """Make one physical Home plate available, moving only an automatic later reservation.

    Manual selections are never silently displaced. The database transaction keeps the
    stock reservation transfer atomic with the new assignment.
    """
    recipe = c.execute("SELECT name,plates_ready,meal_source FROM recipes WHERE id=? AND is_active=1", (recipe_id,)).fetchone()
    if not recipe:
        raise ValueError('This meal is no longer available. Refresh the page and try again.')
    if recipe['meal_source'] != 'home':
        return None
    ready = max(0, int(recipe['plates_ready'] or 0))
    if ready <= 0:
        raise ValueError(f"{recipe['name']} has no ready plates. Update its stock first.")
    target_iso = plan_day.isoformat()
    reserved = int(c.execute("SELECT COUNT(*) FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>=? AND plan_date!=?",
                              (recipe_id, date.today().isoformat(), target_iso)).fetchone()[0])
    if ready > reserved:
        return None
    # Move only a later automatic or pre-0.4.10 unclassified reservation. A
    # user's explicit future choice remains protected from an invisible change.
    movable = c.execute("""SELECT id,plan_date FROM daily_meals
        WHERE recipe_id=? AND status='pending' AND assignment_source IN ('automatic','unknown') AND plan_date>?
        ORDER BY plan_date DESC,id DESC LIMIT 1""", (recipe_id, target_iso)).fetchone()
    if not movable:
        raise ValueError(f"{recipe['name']} has no unreserved plates ready. All existing reservations are protected manual choices; update stock or change a later day first.")
    c.execute("DELETE FROM daily_meals WHERE id=? AND status='pending' AND assignment_source IN ('automatic','unknown')", (int(movable['id']),))
    _recalculate_recipe_due(c, recipe_id)
    return str(movable['plan_date'])


def _shift_pending_meal_sequence(c: sqlite3.Connection, moving_rows: list[dict], base_delta: int, first_target: date) -> dict[int, date]:
    """Shift an ordered pending sequence forward without violating closures or explicit day-offs."""
    ordered = sorted((dict(row) for row in moving_rows), key=lambda row: (row['plan_date'], int(row['id'])))
    if not ordered:
        return {}
    moving_ids = {int(row['id']) for row in ordered}
    marks = ','.join('?' for _ in moving_ids)
    fixed_rows = [dict(row) for row in c.execute(
        f"SELECT id,plan_date,recipe_id FROM daily_meals WHERE id NOT IN ({marks})", tuple(moving_ids)).fetchall()]
    fixed_dates = {row['plan_date'] for row in fixed_rows}
    fixed_dates_by_recipe: dict[int, list[date]] = {}
    for fixed in fixed_rows:
        fixed_dates_by_recipe.setdefault(int(fixed['recipe_id']), []).append(date.fromisoformat(fixed['plan_date']))
    skipped_dates = {row['plan_date'] for row in c.execute('SELECT plan_date FROM meal_schedule_skips').fetchall()}
    excluded_by_recipe: dict[int, set[int]] = {}
    interval_by_recipe: dict[int, int] = {}
    for recipe_id in {int(row['recipe_id']) for row in ordered}:
        recipe = c.execute('SELECT excluded_weekdays_json,eating_interval_days FROM recipes WHERE id=?', (recipe_id,)).fetchone()
        excluded_by_recipe[recipe_id] = set(_decode_excluded_weekdays(recipe['excluded_weekdays_json'] if recipe else '[]'))
        interval_by_recipe[recipe_id] = max(1, int(recipe['eating_interval_days'] or 1)) if recipe else 1

    destinations: dict[int, date] = {}
    previous_target: date | None = None
    previous_target_by_recipe: dict[int, date] = {}
    for row in ordered:
        recipe_id = int(row['recipe_id'])
        interval = interval_by_recipe.get(recipe_id, 1)
        original = date.fromisoformat(row['plan_date'])
        candidate = max(original + timedelta(days=base_delta), first_target)
        if previous_target is not None:
            candidate = max(candidate, previous_target + timedelta(days=1))
        if recipe_id in previous_target_by_recipe:
            candidate = max(candidate, previous_target_by_recipe[recipe_id] + timedelta(days=interval))
        excluded = excluded_by_recipe.get(recipe_id, set())
        fixed_for_recipe = fixed_dates_by_recipe.get(recipe_id, [])
        for _ in range(3660):
            iso = candidate.isoformat()
            if candidate.weekday() in excluded or iso in skipped_dates or iso in fixed_dates:
                candidate += timedelta(days=1)
                continue
            # A fixed historical/future occurrence is also an interval boundary.
            # If this move lands too close on either side, place it after that
            # occurrence plus the configured delay rather than compressing repeats.
            conflicts = [fixed_day for fixed_day in fixed_for_recipe
                         if abs((candidate - fixed_day).days) < interval]
            if conflicts:
                candidate = max(conflicts) + timedelta(days=interval)
                continue
            break
        else:
            raise ValueError('Could not find an available date for the pending meal sequence. Review weekday exclusions, skipped days and meal intervals.')
        destinations[int(row['id'])] = candidate
        previous_target = candidate
        previous_target_by_recipe[recipe_id] = candidate

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
            cur = c.execute("INSERT INTO daily_meals(plan_date,recipe_id,batch_id,status,assignment_source) VALUES(?,?,NULL,'pending','automatic')",
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


def _undo_daily_meal_confirmation(meal_id: int) -> dict:
    """Undo an accidental eaten confirmation without moving the planned plate."""
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        meal = c.execute(
            '''SELECT dm.*,r.meal_source,r.plates_ready FROM daily_meals dm
               JOIN recipes r ON r.id=dm.recipe_id WHERE dm.id=?''',
            (meal_id,),
        ).fetchone()
        if not meal:
            raise ValueError('Meal not found.')
        if meal['status'] != 'eaten':
            raise ValueError('This plate is not marked as eaten, so there is nothing to undo.')
        recipe_id = int(meal['recipe_id'])
        c.execute("""UPDATE daily_meals
                     SET status='pending',confirmed_at=NULL,notified_date=NULL,
                         suggestion_notified_date=NULL,checkin_notified_date=NULL
                     WHERE id=? AND status='eaten'""", (meal_id,))
        if meal['meal_source'] == 'home':
            c.execute('UPDATE recipes SET plates_ready=MIN(1000,COALESCE(plates_ready,0)+1) WHERE id=?', (recipe_id,))
            c.execute("UPDATE meal_cook_reminders SET status='done' WHERE recipe_id=? AND status='open'", (recipe_id,))
        _recalculate_recipe_due(c, recipe_id)
        updated = c.execute('SELECT * FROM daily_meals WHERE id=?', (meal_id,)).fetchone()
        return {**dict(updated), 'undone': True}


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
    _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
    return one('SELECT id,name,meal_source,plates_ready,next_routine_date FROM recipes WHERE id=?', (recipe_id,))


def _select_daily_meal(recipe_id: int, plan_day: date, replace_pending: bool = False) -> dict:
    """Choose a meal for a date and restart its repeat interval from that choice."""
    _roll_forward_unconfirmed_meals(date.today())
    if plan_day < date.today():
        raise ValueError('A meal can only be selected for today or a future date.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT id,recipe_id,status FROM daily_meals WHERE plan_date=?', (plan_day.isoformat(),)).fetchone()
        if existing:
            if existing['status'] != 'pending':
                raise ValueError('This plate is already confirmed as eaten and cannot be changed.')
            if not replace_pending:
                raise ValueError('A plate is already scheduled for this date. Change it from the weekly meal plan.')
        recipe = c.execute("SELECT id,name,meal_source,plates_ready,eating_interval_days,calories_kcal,excluded_weekdays_json FROM recipes WHERE id=? AND is_active=1", (recipe_id,)).fetchone()
        if not recipe:
            raise ValueError('This meal is no longer available. Refresh the Meals page and choose another.')
        if plan_day.weekday() in _decode_excluded_weekdays(recipe['excluded_weekdays_json']):
            raise ValueError(f"{recipe['name']} is not available on {WEEKDAY_NAMES[plan_day.weekday()]}s. Choose another meal.")
        _validate_recipe_interval_for_date(c, recipe_id, plan_day)

        old_recipe_id = int(existing['recipe_id']) if existing else None
        reallocated_from = _ensure_home_plate_available(c, recipe_id, plan_day) if recipe['meal_source'] == 'home' and old_recipe_id != recipe_id else None
        if existing:
            meal_id = int(existing['id'])
            c.execute("UPDATE daily_meals SET recipe_id=?,batch_id=NULL,assignment_source='manual',notified_date=NULL,suggestion_notified_date=NULL,checkin_notified_date=NULL WHERE id=? AND status='pending'", (recipe_id, meal_id))
        else:
            cursor = c.execute("INSERT INTO daily_meals(plan_date,recipe_id,batch_id,status,assignment_source) VALUES(?,?,NULL,'pending','manual')", (plan_day.isoformat(), recipe_id))
            meal_id = int(cursor.lastrowid)
        c.execute('DELETE FROM meal_schedule_skips WHERE plan_date=?', (plan_day.isoformat(),))
        if old_recipe_id and old_recipe_id != recipe_id:
            _recalculate_recipe_due(c, old_recipe_id)
        replanned = _rebase_recipe_schedule_after_manual(c, recipe_id, plan_day)
        result = dict(recipe)
        result.pop('excluded_weekdays_json', None)
        result.update({'id': meal_id, 'plan_date': plan_day.isoformat(), 'status': 'pending', 'assignment_source': 'manual', 'reallocated_from': reallocated_from, 'replanned_automatic': replanned})
    # Any weekly edit refreshes the whole upcoming plan, not only dates after
    # the edited card. Today is already decided; future manual choices remain fixed.
    rebuilt = _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
    result['replanned_schedule'] = rebuilt
    return result


@app.post('/api/daily-meals/select')
def select_daily_meal(payload: Payload):
    try:
        recipe_id = int(payload.data.get('recipe_id'))
        raw_day = str(payload.data.get('plan_date') or date.today().isoformat())
        plan_day = date.fromisoformat(raw_day)
        result = _select_daily_meal(recipe_id, plan_day)
        recipe_state = one('SELECT next_routine_date FROM recipes WHERE id=?', (recipe_id,))
        result['next_routine_date'] = recipe_state.get('next_routine_date') if recipe_state else None
        next_plan = one("SELECT MIN(plan_date) AS next_date FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>?", (recipe_id, plan_day.isoformat()))
        result['next_planned_date'] = next_plan.get('next_date') if next_plan else None
        result['message'] = 'Meal selected for ' + plan_day.isoformat() + '.'
        return result
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc) or 'Choose a valid meal.')


def _assign_or_skip_daily_meal(plan_day: date, recipe_id: int | None) -> dict:
    """Assign/replace one plate and rebuild all upcoming automatic suggestions."""
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
            _validate_recipe_interval_for_date(c, recipe_id, plan_day)
            reallocated_from = _ensure_home_plate_available(c, recipe_id, plan_day) if recipe['meal_source'] == 'home' and old_recipe_id != recipe_id else None
            if current:
                c.execute("UPDATE daily_meals SET recipe_id=?,batch_id=NULL,assignment_source='manual',notified_date=NULL,suggestion_notified_date=NULL,checkin_notified_date=NULL WHERE id=? AND status='pending'", (recipe_id, int(current['id'])))
                meal_id = int(current['id'])
            else:
                cursor = c.execute("INSERT INTO daily_meals(plan_date,recipe_id,batch_id,status,assignment_source) VALUES(?,?,NULL,'pending','manual')", (day_iso, recipe_id))
                meal_id = int(cursor.lastrowid)
            c.execute('DELETE FROM meal_schedule_skips WHERE plan_date=?', (day_iso,))
            if old_recipe_id and old_recipe_id != recipe_id:
                _recalculate_recipe_due(c, old_recipe_id)
            replanned = _rebase_recipe_schedule_after_manual(c, recipe_id, plan_day)
            result = {'id': meal_id, 'plan_date': day_iso, 'recipe_id': recipe_id, 'recipe_name': recipe['name'], 'meal_source': recipe['meal_source'], 'status': 'pending', 'skipped': False, 'reallocated_from': reallocated_from, 'replanned_automatic': replanned}
    # A week edit rebuilds every upcoming automatic suggestion from tomorrow.
    rebuilt = _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
    result['replanned_schedule'] = rebuilt
    result['replanned_automatic'] = int(result.get('replanned_automatic', 0) or 0) + int(rebuilt.get('cleared_automatic', 0) or 0)
    result['replanned_created'] = int(rebuilt.get('created', 0) or 0)
    if recipe_id is not None:
        recipe_state = one('SELECT next_routine_date FROM recipes WHERE id=?', (recipe_id,))
        result['next_routine_date'] = recipe_state.get('next_routine_date') if recipe_state else None
        next_plan = one("SELECT MIN(plan_date) AS next_date FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>?", (recipe_id, plan_day.isoformat()))
        result['next_planned_date'] = next_plan.get('next_date') if next_plan else None
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


@app.post('/api/daily-meals/reset-week')
def reset_weekly_meal_plan():
    """Rebuild future pending meals while keeping today's plate and history untouched."""
    today = date.today()
    start = today + timedelta(days=1)
    rolled_forward = _roll_forward_unconfirmed_meals(today)
    with connect() as c:
        skips_before = c.execute('SELECT COUNT(*) FROM meal_schedule_skips WHERE plan_date>=?', (start.isoformat(),)).fetchone()[0]
    rebuilt = _rebuild_automatic_meal_schedule(start, clear_skips=True, replace_manual=True)
    return {
        'reset': True,
        'start_date': start.isoformat(),
        'cleared_pending': int(rebuilt.get('cleared_automatic', 0)),
        'cleared_day_offs': int(skips_before),
        'rolled_forward': int(rolled_forward),
        'created': int(rebuilt.get('created', 0)),
        'meals': rebuilt.get('meals', []),
        'message': f"Upcoming plan refreshed from tomorrow. Today's plate, past history and confirmed eaten records were preserved; {int(rebuilt.get('created', 0))} future plate(s) generated."
    }


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
    decision = str(payload.data.get('decision') or '').strip().lower()
    if decision in ('undo', 'not_eaten', 'cancel_eaten'):
        try:
            return _undo_daily_meal_confirmation(meal_id)
        except ValueError as exc:
            status = 404 if str(exc) == 'Meal not found.' else 409
            raise HTTPException(status, str(exc))
    if decision not in ('yes', 'no', 'eaten', 'not_today'):
        raise HTTPException(400, 'Decision must be yes, no or undo.')
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
            source = 'weight' if matcher is is_weight_routine else 'treadmill'
            c.execute('INSERT OR IGNORE INTO routine_logs(routine_id,completed_at,source) VALUES(?,?,?)',
                      (routine['id'], day, source))
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
    """Send both routine and late reminders on the same due day.

    The routine time is the first notification. The late time is the follow-up
    later that same day, not the next calendar day. If a routine is overdue
    because the service was offline, it is treated as due today and catches up
    without skipping today's occurrence.
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
            if routine_time and current_hm >= routine_time:
                reminders.append(('routine-time', routine['id'], today.isoformat(),
                                  mikasa_persona.reminder_routine(name)))
            if late_time and current_hm >= late_time:
                days_late = max(0, (today - due).days)
                late_label = f" · {days_late}d overdue" if days_late else ''
                reminders.append(('routine-late', routine['id'], today.isoformat(),
                                  mikasa_persona.reminder_routine(name, late_label)))
        except Exception as exc:
            print(f'Routine reminder skipped for {routine.get("name", routine.get("id"))}: {exc}', flush=True)
    return reminders


def roll_forward_late_routine(routine_id: int, reminder_day: date) -> bool:
    """Move an uncompleted opted-in routine to tomorrow after today's late alert."""
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
        # forward if it is still due today or earlier; never overwrite a newer
        # completion or a future schedule.
        if due is None or due > reminder_day:
            return False
        cur = c.execute('UPDATE routines SET next_due=? WHERE id=? AND next_due=?', (tomorrow, routine_id, current['next_due']))
        return cur.rowcount == 1


def normalize_overdue_routine_dates(current_day: date) -> int:
    """Rebase opted-in overdue routines to today before issuing today's reminders.

    This handles a service restart or missed midnight boundary without leaving
    a routine parked on yesterday (or an older date). The late alert still moves
    an unfinished routine to tomorrow after it is successfully delivered.
    """
    day = current_day.isoformat()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        cur = c.execute('''UPDATE routines SET next_due=?
                           WHERE next_due IS NOT NULL AND next_due<?
                             AND routine_late_time IS NOT NULL
                             AND TRIM(routine_late_time)!=''
                             AND routine_rollover_after_late=1''', (day, day))
        return cur.rowcount


def _recalculate_routine_after_activity_removal(c: sqlite3.Connection, routine_id: int) -> None:
    """Rebuild a routine's last/next dates after an auto-completing activity is removed."""
    routine = c.execute('SELECT interval_days FROM routines WHERE id=?', (routine_id,)).fetchone()
    if not routine:
        return
    latest = c.execute('SELECT MAX(completed_at) FROM routine_logs WHERE routine_id=?', (routine_id,)).fetchone()[0]
    interval = max(1, int(routine['interval_days'] or 1))
    next_due = (date.fromisoformat(latest) + timedelta(days=interval)).isoformat() if latest else date.today().isoformat()
    c.execute('UPDATE routines SET last_done=?,next_due=? WHERE id=?', (latest, next_due, routine_id))


def _undo_activity_routine_completion(c: sqlite3.Connection, source: str, activity_day: str) -> int:
    """Remove only activity-generated checks and keep manually completed routines."""
    if source == 'weight':
        matcher = "(lower(name) LIKE '%weight%' OR lower(name) LIKE '%weigh%')"
    elif source == 'treadmill':
        matcher = "lower(name) LIKE '%treadmill%'"
    else:
        return 0
    linked = c.execute(f'''SELECT rl.routine_id FROM routine_logs rl
                           JOIN routines r ON r.id=rl.routine_id
                           WHERE rl.completed_at=? AND rl.source=? AND {matcher}''',
                       (activity_day, source)).fetchall()
    c.execute(f'''DELETE FROM routine_logs WHERE completed_at=? AND source=?
                  AND routine_id IN (SELECT id FROM routines WHERE {matcher})''', (activity_day, source))
    for row in linked:
        _recalculate_routine_after_activity_removal(c, int(row['routine_id']))
    return len(linked)

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
        data['name'] = str(data.get('name') or '').strip()[:160]
        if not data['name']:
            raise HTTPException(400, 'Bill name is required.')
        data.setdefault('recurrence','none')
        data.setdefault('reminder_days',3)
        data['due_date'] = validate_date_field(data.get('due_date'), 'Bill due date')
        if not data['due_date']:
            raise HTTPException(400, 'Bill due date is required.')
        try:
            amount = float(data.get('amount_mad', 0))
            reminder_raw = float(data.get('reminder_days', 3))
            reminder_days = int(reminder_raw)
            if not math.isfinite(amount) or not math.isfinite(reminder_raw) or amount < 0 or reminder_days < 0 or reminder_raw != reminder_days: raise ValueError()
            data['amount_mad'] = amount
            data['reminder_days'] = reminder_days
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, 'Bill amount and reminder days must be finite, non-negative numbers.')
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
            try:
                kg = float(data.get('kg'))
                if not math.isfinite(kg) or not 20 <= kg <= 350:
                    raise ValueError()
                data['kg'] = round(kg, 2)
            except (TypeError, ValueError):
                raise HTTPException(400, 'Weight must be between 20 and 350 kg.')
            # Acquire the SQLite write lock before checking so two near-simultaneous
            # submissions cannot create two records for the same calendar day.
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT kg FROM weights WHERE date=? ORDER BY id DESC LIMIT 1', (weight_day.isoformat(),)).fetchone()
            if existing:
                pretty = weight_day.strftime('%d/%m/%Y')
                raise HTTPException(409, f'Weight already recorded for {pretty} ({float(existing["kg"]):.2f} kg). No new entry was added.')
        cur=c.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({marks})", [data[k] for k in keys])
        new_id=cur.lastrowid
        if resource == 'bills':
            c.execute('UPDATE bills SET series_id=? WHERE id=?', (int(new_id), int(new_id)))
    if resource == 'weights':
        auto_complete_weight_routines(weight_day)
    elif resource == 'treadmill':
        auto_complete_treadmill_routines(treadmill_day)
    result = one(f'SELECT * FROM {table} WHERE id=?',(new_id,))
    if resource == 'recipes' and result:
        if result.get('meal_source') == 'home' and int(result.get('plates_ready') or 0) == 0:
            with connect() as c: _ensure_cook_reminder(c, int(new_id), date.today()+timedelta(days=1))
        # A newly added option should be considered in the existing upcoming plan,
        # not wait until the currently pre-generated horizon has run out.
        _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
        result = one('SELECT * FROM recipes WHERE id=?', (new_id,))
        result = {k: result.get(k) for k in ('id','name','calories_kcal','eating_interval_days','batch_plates','meal_source','plates_ready','next_routine_date','created_at')}
        result['excluded_weekdays'] = _decode_excluded_weekdays(one('SELECT excluded_weekdays_json FROM recipes WHERE id=?',(new_id,))['excluded_weekdays_json'])
        result['ingredients'] = _recipe_ingredient_names(one('SELECT ingredients_json FROM recipes WHERE id=?',(new_id,))['ingredients_json'])
    return result

@app.patch('/api/{resource}/{item_id}')
def update_resource(resource: str, item_id: int, payload: Payload):
    if resource not in TABLES: raise HTTPException(404,'Unknown resource')
    table,allowed=TABLES[resource]; data={k:v for k,v in payload.data.items() if k in allowed}
    rebuild_recipe_future = False
    if resource in ('weights','workouts','treadmill') and 'date' in data:
        data['date'] = validate_date_field(data.get('date'), 'Record date')
    previous_activity_day = None
    if resource in ('weights', 'treadmill') and 'date' in data:
        previous = one(f'SELECT date FROM {table} WHERE id=?', (item_id,))
        if not previous:
            raise HTTPException(404, 'Record not found')
        previous_activity_day = previous['date']
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
    existing_bill = None
    if resource == 'bills':
        existing_bill = one('SELECT * FROM bills WHERE id=?', (item_id,))
        if not existing_bill:
            raise HTTPException(404, 'Bill not found')
        if 'name' in data:
            data['name'] = str(data.get('name') or '').strip()[:160]
            if not data['name']:
                raise HTTPException(400, 'Bill name is required.')
        if 'due_date' in data:
            data['due_date'] = validate_date_field(data.get('due_date'), 'Bill due date')
            if not data['due_date']:
                raise HTTPException(400, 'Bill due date is required.')
            if data['due_date'] != existing_bill['due_date']:
                # A deliberate due-date edit also changes the recurrence anchor.
                data['recurrence_day'] = date.fromisoformat(data['due_date']).day
        if 'recurrence' in data and data['recurrence'] not in ('none','monthly','quarterly','yearly'):
            raise HTTPException(400, 'Choose a valid bill recurrence.')
        if 'status' in data and data['status'] not in ('unpaid', 'paid'):
            raise HTTPException(400, 'Bill status must be unpaid or paid.')
        if 'amount_mad' in data:
            try:
                amount = float(data['amount_mad'])
                if not math.isfinite(amount) or amount < 0: raise ValueError()
                data['amount_mad'] = amount
            except (TypeError, ValueError): raise HTTPException(400, 'Bill amount must be a finite, non-negative number.')
        if 'reminder_days' in data:
            try:
                raw_days = float(data['reminder_days'])
                days = int(raw_days)
                if not math.isfinite(raw_days) or days < 0 or raw_days != days: raise ValueError()
                data['reminder_days'] = days
            except (TypeError, ValueError, OverflowError): raise HTTPException(400, 'Reminder days must be a non-negative whole number.')
        if 'paid_at' in data:
            raw_paid_at = str(data.get('paid_at') or '').strip()
            if not raw_paid_at:
                data['paid_at'] = None
            else:
                try:
                    paid_at = datetime.fromisoformat(raw_paid_at.replace('Z', '+00:00'))
                    data['paid_at'] = paid_at.isoformat(timespec='minutes')
                except (TypeError, ValueError):
                    raise HTTPException(400, 'Payment date must be a valid date and time (YYYY-MM-DDTHH:MM).')
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
        if data.get('status') == 'done' and existing_task.get('status') != 'done':
            data['completed_at'] = date.today().isoformat()
        elif data.get('status') == 'open':
            data['completed_at'] = None
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
        if 'eating_interval_days' in data and int(data['eating_interval_days']) != int(existing_recipe.get('eating_interval_days') or 1):
            rebuild_recipe_future = True
        if 'excluded_weekdays_json' in data:
            old_days = _decode_excluded_weekdays(existing_recipe.get('excluded_weekdays_json', '[]'))
            new_days = _decode_excluded_weekdays(data['excluded_weekdays_json'])
            if old_days != new_days:
                rebuild_recipe_future = True
        if 'meal_source' in data and data['meal_source'] != existing_recipe.get('meal_source'):
            rebuild_recipe_future = True
        if 'plates_ready' in data and int(data['plates_ready']) != int(existing_recipe.get('plates_ready') or 0):
            rebuild_recipe_future = True
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
        if resource == 'bills' and existing_bill:
            due_date_changed = data.get('due_date', existing_bill['due_date']) != existing_bill['due_date']
            status_changed_to_unpaid = data.get('status', existing_bill['status']) == 'unpaid' and existing_bill['status'] != 'unpaid'
            if due_date_changed or status_changed_to_unpaid:
                c.execute("DELETE FROM notification_log WHERE kind IN ('bill-3day','bill-4h','bill') AND item_id=?", (item_id,))
        if previous_activity_day and data.get('date') != previous_activity_day:
            if resource == 'weights':
                _undo_activity_routine_completion(c, 'weight', previous_activity_day)
            elif resource == 'treadmill':
                remaining = c.execute('SELECT COUNT(*) FROM treadmill WHERE date=?', (previous_activity_day,)).fetchone()[0]
                if not remaining:
                    _undo_activity_routine_completion(c, 'treadmill', previous_activity_day)
        if resource == 'recipes':
            if 'excluded_weekdays_json' in data:
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
            if 'eating_interval_days' in data and int(data['eating_interval_days']) != int(existing_recipe.get('eating_interval_days') or 1):
                # Keep today's plate and explicit manual choices. Rebuild only future
                # automatic suggestions using the newly saved repeat interval.
                c.execute("DELETE FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>? AND assignment_source IN ('automatic','unknown')", (item_id, date.today().isoformat()))
                _recalculate_recipe_due(c, item_id)
    if previous_activity_day and data.get('date') and data.get('date') != previous_activity_day:
        new_activity_day = date.fromisoformat(data['date'])
        if resource == 'weights':
            auto_complete_weight_routines(new_activity_day)
        elif resource == 'treadmill':
            auto_complete_treadmill_routines(new_activity_day)
    result = one(f'SELECT * FROM {table} WHERE id=?',(item_id,))
    if resource == 'recipes' and result:
        if 'meal_source' in data or 'plates_ready' in data:
            with connect() as c:
                current = c.execute('SELECT meal_source,plates_ready FROM recipes WHERE id=?', (item_id,)).fetchone()
                if current and current['meal_source']=='home': _sync_home_stock(c, item_id, int(current['plates_ready'] or 0))
                elif current: c.execute("UPDATE meal_cook_reminders SET status='done' WHERE recipe_id=? AND status='open'", (item_id,))
        if rebuild_recipe_future:
            _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
        else:
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
    _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
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
        activity_day = None
        if resource in ('weights', 'treadmill'):
            activity = c.execute(f'SELECT date FROM {table} WHERE id=?', (item_id,)).fetchone()
            if not activity:
                raise HTTPException(404, 'Record not found')
            activity_day = activity['date']
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
        if resource == 'weights':
            _undo_activity_routine_completion(c, 'weight', activity_day)
        elif resource == 'treadmill':
            remaining = c.execute('SELECT COUNT(*) FROM treadmill WHERE date=?', (activity_day,)).fetchone()[0]
            if not remaining:
                _undo_activity_routine_completion(c, 'treadmill', activity_day)
        reminder_kinds={'tasks':['task','task-time'],'bills':['bill','bill-3day','bill-4h'],'routines':['routine-time','routine-late']}
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
        c.execute('''INSERT INTO routine_logs(routine_id,completed_at,source) VALUES(?,?, 'manual')
                     ON CONFLICT(routine_id,completed_at) DO UPDATE SET source='manual' ''',
                  (routine_id,today.isoformat()))
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
        if latest and c.execute('SELECT 1 FROM routine_logs WHERE routine_id=? AND completed_at=? AND id!=?',
                                 (routine_id, completed, latest['id'])).fetchone():
            raise HTTPException(409, 'This routine already has a completion record for that date.')
        if latest:
            c.execute("UPDATE routine_logs SET completed_at=?,source='manual' WHERE id=?",(completed,latest['id']))
        else:
            c.execute("INSERT INTO routine_logs(routine_id,completed_at,source) VALUES(?,?,'manual')",(routine_id,completed))
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


def active_bill_occurrences() -> list[dict]:
    '''Return one actionable unpaid occurrence per logical bill series.

    If legacy/manual data contains multiple unpaid rows in one series, the
    earliest due occurrence is the one shown and reminded first. The others
    remain stored and are visible in the bill's review/history details.
    '''
    by_series: dict[int, dict] = {}
    for bill in rows("SELECT * FROM bills WHERE status!='paid'"):
        series_id = int(bill.get('series_id') or bill['id'])
        previous = by_series.get(series_id)
        if previous is None or (bill['due_date'], int(bill['id'])) < (previous['due_date'], int(previous['id'])):
            by_series[series_id] = bill
    return sorted(by_series.values(), key=lambda bill: (bill['due_date'], int(bill['id'])))


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
        series_id = int(bill.get('series_id') or bill_id)
        next_due = next_recurrence_date(due_date, recurrence, anchor_day)
        # If payment is late, roll to the first cycle strictly after today.
        while next_due and next_due <= date.today():
            next_due = next_recurrence_date(next_due, recurrence, anchor_day)
        c.execute("UPDATE bills SET status='paid',paid_at=?,series_id=? WHERE id=? AND status!='paid'",
                  (now, series_id, bill_id))
        if next_due:
            # Never create a second open installment for the same logical bill.
            existing_open = c.execute(
                "SELECT id FROM bills WHERE series_id=? AND status!='paid' ORDER BY due_date,id LIMIT 1",
                (series_id,),
            ).fetchone()
            if not existing_open:
                c.execute("INSERT INTO bills (name,amount_mad,due_date,recurrence,reminder_days,status,notes,recurrence_day,series_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (bill['name'], bill['amount_mad'], next_due.isoformat(), recurrence,
                     bill['reminder_days'], 'unpaid', bill['notes'], anchor_day, series_id))
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

def build_today_agenda(day: date | None = None) -> dict[str, Any]:
    """Build the single source-of-truth agenda shared by the app, Telegram and Home Assistant.

    Open items that are overdue remain visible until they are completed or paid;
    date-specific records (events, meals and activity logs) appear only on their
    actual date. This prevents each client from inventing its own definition of
    what belongs in today's view.
    """
    day = day or date.today()
    day_key = day.isoformat()
    items: list[dict[str, Any]] = []

    def add(kind: str, item_id: int | None, title: str, detail: str,
            status: str, *, item_date: str | None = None, item_time: str | None = None) -> None:
        items.append({
            'type': kind, 'id': item_id, 'title': str(title or ''),
            'detail': str(detail or ''), 'status': status,
            'date': item_date or day_key, 'time': item_time,
        })

    def parse_agenda_date(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value or ''))
        except (TypeError, ValueError):
            return None

    # Events scheduled for this date.
    for event in rows('SELECT id,title,start_at,category FROM events WHERE substr(start_at,1,10)=? ORDER BY start_at,id', (day_key,)):
        start_at = str(event.get('start_at') or '')
        event_time = start_at[11:16] if len(start_at) >= 16 and start_at[10:11] == 'T' else None
        category = f" · {event['category']}" if event.get('category') else ''
        add('event', int(event['id']), event['title'], f'Event{category}', 'scheduled', item_time=event_time)

    # Open tasks due today or earlier stay in view until marked complete.
    for task in rows("SELECT id,title,due_date,due_time,priority FROM tasks WHERE status!='done' AND due_date IS NOT NULL AND due_date<=? ORDER BY due_date,due_time,id", (day_key,)):
        due = parse_agenda_date(task.get('due_date'))
        if due is None:
            continue
        late_days = (day - due).days
        state = 'overdue' if late_days > 0 else 'due'
        timing = f'Overdue {late_days}d' if late_days else 'Due today'
        priority = str(task.get('priority') or 'normal').title()
        add('task', int(task['id']), task['title'], f'{timing} · {priority} priority', state,
            item_date=task['due_date'], item_time=task.get('due_time'))

    # Routines include any still-open due date, even when it predates today.
    for routine in rows('''SELECT id,name,next_due,routine_time,interval_days
                           FROM routines WHERE next_due IS NOT NULL AND next_due<=?
                           ORDER BY next_due,COALESCE(routine_time,'23:59'),name,id''', (day_key,)):
        due = parse_agenda_date(routine.get('next_due'))
        if due is None:
            continue
        late_days = (day - due).days
        state = 'overdue' if late_days > 0 else 'due'
        timing = f'Overdue {late_days}d' if late_days else 'Due today'
        interval = max(1, int(routine.get('interval_days') or 1))
        add('routine', int(routine['id']), routine['name'],
            f'Routine · {timing} · every {interval}d', state,
            item_date=routine['next_due'], item_time=routine.get('routine_time'))

    # Bills enter the agenda when their 3-day Telegram reminder becomes due.
    # Keep one active occurrence per logical series, matching the Bills page.
    for bill in active_bill_occurrences():
        try:
            due = date.fromisoformat(str(bill.get('due_date') or ''))
        except (TypeError, ValueError):
            continue
        days_left = (due - day).days
        if days_left > 3:
            continue
        amount = float(bill.get('amount_mad') or 0)
        if days_left < 0:
            state, timing = 'overdue', f'Overdue {abs(days_left)}d'
        elif days_left == 0:
            state, timing = 'due', 'Due today'
        else:
            state, timing = 'upcoming', f'Due in {days_left}d'
        add('bill', int(bill['id']), bill['name'], f'{amount:.2f} MAD · {timing}', state,
            item_date=bill['due_date'])

    # Documents appear as soon as their configured reminder window begins.
    # Expired documents remain visible until their expiry date is corrected.
    for document in rows('SELECT id,name,expiry_date,reminder_days FROM documents ORDER BY expiry_date,id'):
        try:
            expiry = date.fromisoformat(str(document.get('expiry_date') or ''))
            lead = max(0, int(document.get('reminder_days') if document.get('reminder_days') is not None else 30))
        except (TypeError, ValueError):
            continue
        days_left = (expiry - day).days
        if days_left > lead:
            continue
        if days_left < 0:
            state, timing = 'overdue', f'Expired {abs(days_left)}d ago'
        elif days_left == 0:
            state, timing = 'due', 'Expires today'
        else:
            state, timing = 'upcoming', f'Expires in {days_left}d'
        add('document', int(document['id']), document['name'], timing, state,
            item_date=document['expiry_date'])

    # Today's meal is pending until confirmed, then remains as completed history.
    meal = one('''SELECT dm.id,dm.plan_date,dm.status,r.name AS recipe_name,r.calories_kcal
                  FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id
                  WHERE dm.plan_date=?''', (day_key,))
    if meal:
        eaten = meal.get('status') == 'eaten'
        kcal = f" · {float(meal['calories_kcal']):g} kcal" if meal.get('calories_kcal') is not None else ''
        add('meal', int(meal['id']), meal['recipe_name'],
            ('Meal · confirmed' if eaten else 'Meal · waiting for your check-in') + kcal,
            'completed' if eaten else 'pending')

    # An open cook reminder remains visible while the home-meal batch is empty.
    for cook in rows('''SELECT mcr.id,mcr.due_date,r.name AS recipe_name
                        FROM meal_cook_reminders mcr JOIN recipes r ON r.id=mcr.recipe_id
                        WHERE mcr.status='open' AND mcr.due_date<=?
                        ORDER BY mcr.due_date,r.name''', (day_key,)):
        due = parse_agenda_date(cook.get('due_date'))
        if due is None:
            continue
        late_days = (day - due).days
        state = 'overdue' if late_days > 0 else 'due'
        timing = f'Overdue {late_days}d' if late_days else 'Due today'
        add('cook', int(cook['id']), f"Prepare {cook['recipe_name']}",
            f'Home meal stock · {timing}', state, item_date=cook['due_date'])

    # A planned workout and its matching logged session are one agenda item,
    # not two separate obligations. Unmatched sessions remain visible as logs.
    weekday = WEEKDAY_NAMES[day.weekday()]
    planned_workouts = rows('SELECT id,name,notes FROM workout_plan WHERE weekday=? ORDER BY id', (weekday,))
    logged_workouts = rows('SELECT id,name,duration_min FROM workouts WHERE date=? ORDER BY id', (day_key,))
    consumed_workout_ids: set[int] = set()
    def workout_key(value: Any) -> str:
        return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()
    for planned in planned_workouts:
        match = next((workout for workout in logged_workouts
                      if workout_key(workout.get('name')) == workout_key(planned.get('name'))
                      and int(workout['id']) not in consumed_workout_ids), None)
        if match:
            consumed_workout_ids.add(int(match['id']))
            duration = f" · {int(match.get('duration_min') or 0)} min" if int(match.get('duration_min') or 0) > 0 else ''
            add('workout', int(match['id']), planned['name'], f'Planned · completed{duration}', 'completed')
        else:
            detail = 'Workout plan' + (f" · {planned['notes']}" if planned.get('notes') else '')
            add('workout-plan', int(planned['id']), planned['name'], detail, 'scheduled')

    # Tasks completed today are kept in the daily recap. Old records with no
    # completion timestamp remain unchanged rather than being assigned a guess.
    for task in rows('''SELECT id,title,due_date,completed_at FROM tasks
                       WHERE status='done' AND substr(completed_at,1,10)=?
                       ORDER BY completed_at,id''', (day_key,)):
        detail = 'Task completed'
        if task.get('due_date'):
            detail += f" · Due {task['due_date']}"
        completed_time = str(task.get('completed_at') or '')
        time_label = completed_time[11:16] if len(completed_time) >= 16 else None
        add('task-done', int(task['id']), task['title'], detail, 'completed', item_time=time_label)

    # Completions and fitness entries are visible as done, not mixed with open actions.
    for log in rows('''SELECT rl.id,r.name,rl.source FROM routine_logs rl
                       JOIN routines r ON r.id=rl.routine_id WHERE rl.completed_at=?
                       ORDER BY r.name,rl.id''', (day_key,)):
        source = str(log.get('source') or 'manual').replace('_', ' ').title()
        add('routine-done', int(log['id']), log['name'], f'Routine completed · {source}', 'completed')

    for workout in logged_workouts:
        if int(workout['id']) in consumed_workout_ids:
            continue
        duration = f" · {int(workout.get('duration_min') or 0)} min" if int(workout.get('duration_min') or 0) > 0 else ''
        add('workout', int(workout['id']), workout['name'], f'Gym logged{duration}', 'completed')

    treadmill = one('''SELECT COUNT(*) AS sessions,COALESCE(SUM(duration_min),0) AS minutes
                       FROM treadmill WHERE date=?''', (day_key,)) or {}
    if int(treadmill.get('sessions') or 0):
        minutes = round(float(treadmill.get('minutes') or 0))
        sessions = int(treadmill['sessions'])
        add('treadmill', None, f'Treadmill · {minutes} min',
            f'{sessions} session' + ('' if sessions == 1 else 's') + ' logged', 'completed')

    weight = one('SELECT id,kg FROM weights WHERE date=? ORDER BY id DESC LIMIT 1', (day_key,))
    if weight:
        add('weight', int(weight['id']), f"Weight · {float(weight['kg']):g} kg",
            'Weigh-in logged', 'completed')

    status_order = {'overdue': 0, 'due': 1, 'pending': 2, 'scheduled': 3, 'upcoming': 4, 'completed': 5}
    items.sort(key=lambda x: (status_order.get(x['status'], 6), x.get('time') or '99:99', x['title'].casefold(), int(x.get('id') or 0)))
    open_count = sum(1 for entry in items if entry['status'] in ('overdue', 'due', 'pending'))
    scheduled_count = sum(1 for entry in items if entry['status'] in ('scheduled', 'upcoming'))
    completed_count = sum(1 for entry in items if entry['status'] == 'completed')
    return {
        'date': day_key, 'count': len(items), 'open_count': open_count,
        'scheduled_count': scheduled_count, 'completed_count': completed_count,
        'items': items,
    }


@app.get('/api/agenda/today')
def today_agenda_endpoint():
    return build_today_agenda()


@app.get('/api/summary')
def summary():
    today_date = date.today()
    today = today_date.isoformat()
    agenda = build_today_agenda(today_date)
    open_bills = active_bill_occurrences()
    future_bills = sorted((b for b in open_bills if b['due_date'] >= today), key=lambda b: (b['due_date'], int(b['id'])))
    overdue_bills = sorted((b for b in open_bills if b['due_date'] < today), key=lambda b: (b['due_date'], int(b['id'])), reverse=True)
    next_bill_row = (future_bills or overdue_bills)
    next_bill = ({'name': next_bill_row[0]['name'], 'due_date': next_bill_row[0]['due_date'],
                  'amount_mad': next_bill_row[0]['amount_mad']} if next_bill_row else None)
    days_to_next_bill = (date.fromisoformat(next_bill['due_date']) - today_date).days if next_bill else None
    return {
      'today': today,
      'today_agenda': agenda,
      'today_agenda_count': agenda['count'],
      'today_open_items': agenda['open_count'],
      'events_today':len(rows('SELECT id FROM events WHERE substr(start_at,1,10)=?',(today,))),
      'routines_due_today':one('SELECT COUNT(*) n FROM routines WHERE next_due<=?',(today,))['n'],
      'open_tasks':one("SELECT COUNT(*) n FROM tasks WHERE status!='done'")['n'],
      'latest_weight':one('SELECT kg,date FROM weights ORDER BY date DESC,id DESC LIMIT 1'),
      'unpaid_bills':len(open_bills),
      'unpaid_total_mad':sum(float(b.get('amount_mad') or 0) for b in open_bills),
      'upcoming_bill':({**next_bill, 'days_left': days_to_next_bill} if next_bill else None),
      'expiring_documents':count_expiring_documents(today_date),
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
    return {
        'weight_kg': s['latest_weight']['kg'] if s['latest_weight'] else None,
        'unpaid_bills': s['unpaid_bills'],
        'unpaid_total_mad': s['unpaid_total_mad'],
        'expiring_documents': s['expiring_documents'],
        'open_tasks': s['open_tasks'],
        'events_today': s['events_today'],
        'routines_due_today': s['routines_due_today'],
        'next_routine': s['next_routine']['name'] if s['next_routine'] else None,
        'today_agenda_count': s['today_agenda_count'],
        'today_open_items': s['today_open_items'],
        'today_agenda_date': s['today_agenda']['date'],
        'today_agenda': s['today_agenda']['items'],
    }
def _telegram_response_data(response, action: str) -> dict:
    """Return a confirmed Telegram API payload, not merely an HTTP-success response."""
    response.raise_for_status()
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f'Telegram {action} returned an unreadable response.') from exc
    if not isinstance(data, dict) or data.get('ok') is not True:
        description = data.get('description', 'Telegram did not confirm the request.') if isinstance(data, dict) else 'Telegram returned an invalid response.'
        raise RuntimeError(f'Telegram {action} failed: {description}')
    return data


def build_bill_reminders(now: datetime | None = None) -> list[tuple[str, int, str, str]]:
    """Build strict bill reminders: one 3-day notice, then repeat every four hours from T-1 day until paid."""
    now = now or datetime.now()
    today = now.date()
    reminders: list[tuple[str, int, str, str]] = []
    for bill in active_bill_occurrences():
        try:
            due_day = date.fromisoformat(bill['due_date'])
        except (TypeError, ValueError):
            continue
        days_left = (due_day - today).days
        amount = float(bill.get('amount_mad') or 0)
        due_label = due_day.strftime('%d/%m/%y')
        if 1 < days_left <= 3:
            # Key the first reminder to the bill's due date so a scheduler restart
            # or repeated polling cannot send the same 3-day notice again.
            reminder_key = due_day.isoformat()
            if one("SELECT id FROM notification_log WHERE kind='bill-3day' AND item_id=? AND reminder_date=?",
                   (int(bill['id']), reminder_key)):
                continue
            message = mikasa_persona.reminder_bill_early(bill['name'], f'{amount:.2f}', days_left, due_label)
            reminders.append(('bill-3day', int(bill['id']), reminder_key, message))
        elif days_left <= 1:
            # Use the persisted send timestamp, not a wall-clock bucket, so the
            # interval remains at least four hours even across app restarts.
            last = one("SELECT reminder_date FROM notification_log WHERE kind='bill-4h' AND item_id=? ORDER BY id DESC LIMIT 1",
                       (int(bill['id']),))
            last_sent = None
            if last and last.get('reminder_date'):
                try:
                    last_sent = datetime.fromisoformat(last['reminder_date'])
                except (TypeError, ValueError):
                    last_sent = None
            if last_sent is not None and now - last_sent < timedelta(hours=4):
                continue
            reminder_key = now.isoformat(timespec='seconds')
            if days_left == 1:
                due_status = 'Due tomorrow'
            elif days_left == 0:
                due_status = 'Due today'
            else:
                due_status = f'Overdue by {abs(days_left)} days'
            message = mikasa_persona.reminder_bill(bill['name'], f'{amount:.2f}', due_status, due_label)
            reminders.append(('bill-4h', int(bill['id']), reminder_key, message))
    return reminders


def build_morning_report(day: date | None = None) -> str:
    """Build the scheduled briefing using Mikasa's standalone presentation layer."""
    day = day or date.today()
    return mikasa_persona.morning_report(build_today_agenda(day), day)


def _mikasa_playful_time(day: date, frequency: str) -> time | None:
    """Choose a repeatable local-time slot so the 15-second poll cannot reroll it."""
    rng = random.Random(f'mikasa-playful:{day.isoformat()}:{frequency}')
    if frequency == 'few' and rng.random() >= (3 / 7):
        return None
    minute_of_day = rng.randint(11 * 60, 20 * 60 + 30)
    return time(minute_of_day // 60, minute_of_day % 60)


def mikasa_morning_report_due(now: datetime, preferences: dict[str, Any] | None = None) -> bool:
    preferences = preferences or get_telegram_preferences()
    if not preferences.get('morning_report_enabled'):
        return False
    if _telegram_setting_get('morning_report_last_sent') == now.date().isoformat():
        return False
    try:
        send_at = datetime.strptime(str(preferences.get('morning_report_time') or '07:30'), '%H:%M').time()
    except ValueError:
        send_at = time(7, 30)
    return now.time() >= send_at


def mikasa_playful_message_due(now: datetime, preferences: dict[str, Any] | None = None) -> bool:
    preferences = preferences or get_telegram_preferences()
    if not preferences.get('playful_messages_enabled'):
        return False
    if _telegram_setting_get('playful_messages_last_sent') == now.date().isoformat():
        return False
    send_at = _mikasa_playful_time(now.date(), str(preferences.get('playful_frequency') or 'few'))
    # Keep these spontaneous check-ins inside a daytime/evening window.
    return bool(send_at and send_at <= now.time() <= time(21, 30))


def build_mikasa_playful_message(day: date | None = None) -> str:
    day = day or date.today()
    agenda = build_today_agenda(day)
    active = [entry for entry in agenda.get('items', [])
              if entry.get('status') in ('overdue', 'due', 'pending')
              and entry.get('type') in ('task', 'routine', 'cook', 'workout-plan')]
    target = str(active[0].get('title') or 'your next task').strip() if active else None
    return mikasa_persona.playful_message(target=target, day=day)


async def _send_telegram_text(client, message: str, *, chat_id: str | None = None,
                             reply_markup: dict | None = None, context: str = 'notification') -> None:
    """Single outbound Telegram gateway; persona voice is enforced before delivery."""
    payload = {
        'chat_id': chat_id or ALLOWED_CHAT,
        'text': mikasa_persona.ensure_voice(message, context=context),
        'disable_web_page_preview': True,
    }
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    sent = await client.post(
        f'https://api.telegram.org/bot{TOKEN}/sendMessage',
        json=payload,
    )
    _telegram_response_data(sent, 'sendMessage')


async def _send_scheduled_mikasa_messages(client, now: datetime) -> None:
    """Send opt-in daily messages once per local date, after Telegram confirms delivery."""
    preferences = get_telegram_preferences()
    if mikasa_morning_report_due(now, preferences):
        try:
            await _send_telegram_text(client, build_morning_report(now.date()))
            _telegram_setting_set('morning_report_last_sent', now.date().isoformat())
        except Exception as exc:
            print(f'Mikasa morning report warning: {exc}', flush=True)
    if mikasa_playful_message_due(now, preferences):
        try:
            await _send_telegram_text(client, build_mikasa_playful_message(now.date()))
            _telegram_setting_set('playful_messages_last_sent', now.date().isoformat())
        except Exception as exc:
            print(f'Mikasa playful message warning: {exc}', flush=True)


async def reminder_loop():
    """Send Telegram reminders, including staged bill notices and four-hour urgent bill follow-ups."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                now = datetime.now()
                today = now.date()
                await _send_scheduled_mikasa_messages(client, now)
                normalize_overdue_routine_dates(today)
                reminders = []
                reminders.extend(build_bill_reminders(now))
                for t in rows("SELECT * FROM tasks WHERE status!='done' AND due_date IS NOT NULL"):
                    try:
                        due_day = date.fromisoformat(t['due_date'])
                        if t.get('due_time'):
                            due_at = datetime.combine(due_day, datetime.strptime(t['due_time'], '%H:%M').time())
                            if due_at <= now:
                                reminder_key = due_at.strftime('%Y-%m-%dT%H:%M')
                                reminders.append(('task-time', t['id'], reminder_key, mikasa_persona.reminder_task(t['title'], at_time='now')))
                        elif due_day <= today:
                            reminders.append(('task', t['id'], today.isoformat(), mikasa_persona.reminder_task(t['title'], due_day.strftime('%d/%m'))))
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
                                      mikasa_persona.reminder_cook(cook['name'])))
                # Meal messages follow the user's local Home Assistant timezone (TZ in run.sh).
                # 14:00 announces the plate; 22:00 asks whether it was eaten.
                today_meal = one("SELECT dm.id,dm.plan_date,dm.status,dm.suggestion_notified_date,dm.checkin_notified_date, r.name AS recipe_name,r.calories_kcal FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id WHERE dm.plan_date=? LIMIT 1", (today.isoformat(),))
                if today_meal and today_meal['status'] == 'pending':
                    kcal = f"\n{float(today_meal['calories_kcal']):g} kcal per plate" if today_meal.get('calories_kcal') is not None else ''
                    # Do not send a stale 14:00 announcement after 22:00 if the app was offline all afternoon.
                    if time(14, 0) <= now.time() < time(22, 0) and today_meal.get('suggestion_notified_date') != today.isoformat():
                        message = (mikasa_persona.meal_suggestion(today_meal['recipe_name'], kcal))
                        await _send_telegram_text(client, message, context='notification')
                        with connect() as c:
                            c.execute("UPDATE daily_meals SET suggestion_notified_date=? WHERE id=? AND status='pending'", (today.isoformat(), int(today_meal['id'])))
                    if now.time() >= time(22, 0) and today_meal.get('checkin_notified_date') != today.isoformat():
                        message = (mikasa_persona.meal_checkin(today_meal['recipe_name'], kcal))
                        await _send_telegram_text(client, message, context='notification')
                        with connect() as c:
                            c.execute("UPDATE daily_meals SET checkin_notified_date=? WHERE id=? AND status='pending'", (today.isoformat(), int(today_meal['id'])))
                reminders.extend(build_routine_reminders(now))
                for kind, item_id, reminder_date, message in reminders:
                    already_sent = one('SELECT id FROM notification_log WHERE kind=? AND item_id=? AND reminder_date=?', (kind, item_id, reminder_date))
                    if already_sent:
                        if kind == 'routine-late':
                            roll_forward_late_routine(int(item_id), date.fromisoformat(reminder_date))
                        continue
                    await _send_telegram_text(client, message, context='notification')
                    with connect() as c:
                        c.execute('INSERT OR IGNORE INTO notification_log(kind,item_id,reminder_date) VALUES(?,?,?)', (kind, item_id, reminder_date))
                    if kind in ('routine-time', 'routine-late'):
                        _telegram_record_routine_offer(int(item_id))
                    if kind == 'routine-late':
                        roll_forward_late_routine(int(item_id), date.fromisoformat(reminder_date))
            except Exception as e:
                print(f'Reminder scheduler error: {e}', flush=True)
            await asyncio.sleep(15)

def _telegram_main_keyboard() -> dict:
    """Persistent shortcuts for meal check-ins, today's agenda, and fitness logging."""
    return {
        'keyboard': [
            [{'text': '📅 Today'}, {'text': '🍽 Meals'}],
            [{'text': '✅ I ate it'}, {'text': '⏭ Not today'}],
            [{'text': '↩️ Undo eaten'}],
            [{'text': '⚖️ Add weight'}, {'text': '🏃 Add treadmill'}],
            [{'text': '📈 Fitness week'}],
        ],
        'resize_keyboard': True,
        'is_persistent': True,
        'input_field_placeholder': 'Today, meals, meal check-in, or fitness',
    }


def _telegram_bot_commands() -> list[dict[str, str]]:
    """Expose the main agenda, meal, check-in, and fitness actions in Telegram."""
    return [
        {'command': 'start', 'description': 'Open the XROB quick menu'},
        {'command': 'help', 'description': 'Show available actions'},
        {'command': 'about', 'description': 'Explain XROB features and how Mikasa can help'},
        {'command': 'features', 'description': 'Show the areas Mikasa can manage'},
        {'command': 'today', 'description': 'Show today agenda'},
        {'command': 'tomorrow', 'description': 'Show tomorrow agenda'},
        {'command': 'status', 'description': 'Show your XROB progress and app status'},
        {'command': 'meals', 'description': "Show today's plate or choose a meal"},
        {'command': 'yes', 'description': 'Confirm you ate the suggested plate'},
        {'command': 'undo_eaten', 'description': 'Undo an accidental eaten confirmation'},
        {'command': 'no', 'description': 'Move the plate and later plan forward'},
        {'command': 'choosemeal', 'description': 'Choose or replace today’s plate'},
        {'command': 'weight', 'description': 'Start guided weight entry'},
        {'command': 'treadmill', 'description': 'Start guided treadmill entry'},
        {'command': 'fitnessweek', 'description': 'Weight and treadmill summary for the last 7 days'},
        {'command': 'cancel', 'description': 'Cancel the current guided entry'},
    ]


def _telegram_incoming_message(message: dict[str, Any]) -> tuple[str, str | None]:
    """Return the user's text/caption or a media type that needs an acknowledgement."""
    text = str(message.get('text') or message.get('caption') or '').strip()
    if text:
        return text, None
    media_fields = (
        ('photo', 'photo'), ('voice', 'voice'), ('audio', 'audio'),
        ('video_note', 'video_note'), ('video', 'video'), ('animation', 'animation'),
        ('document', 'document'), ('sticker', 'sticker'), ('location', 'location'),
        ('venue', 'venue'), ('contact', 'contact'), ('poll', 'poll'), ('dice', 'dice'),
        ('game', 'game'), ('web_app_data', 'message'),
    )
    for field, kind in media_fields:
        if message.get(field):
            return '', kind
    return '', None


async def telegram_poll():
    """Poll Telegram with a guided menu and a durable reply queue.

    Incoming actions are executed once. Their reply is stored before sending;
    if Telegram is temporarily unavailable, the stored reply is retried without
    executing the user's command a second time.
    """
    import httpx
    keyboard = _telegram_main_keyboard()
    commands = _telegram_bot_commands()
    saved_offset = one("SELECT value FROM telegram_state WHERE key='update_offset'")
    try:
        offset = int(saved_offset['value']) if saved_offset else 0
    except (TypeError, ValueError):
        offset = 0

    async with httpx.AsyncClient(timeout=40) as client:
        retry_delay = 2
        try:
            setup = await client.post(f'https://api.telegram.org/bot{TOKEN}/setMyCommands', json={'commands': commands})
            _telegram_response_data(setup, 'setMyCommands')
        except Exception as exc:
            print(f'Telegram command-menu setup warning: {exc}', flush=True)

        # Give the bot a coherent, explicitly AI identity in Telegram's profile.
        # A profile update failure must never prevent polling or reminders.
        profile_updates = (
            ('setMyName', {'name': 'Mikasa'}),
            ('setMyDescription', {'description': mikasa_persona.TELEGRAM_PROFILE_DESCRIPTION}),
            ('setMyShortDescription', {'short_description': mikasa_persona.TELEGRAM_PROFILE_SHORT_DESCRIPTION}),
        )
        for method, payload in profile_updates:
            try:
                profile = await client.post(f'https://api.telegram.org/bot{TOKEN}/{method}', json=payload)
                _telegram_response_data(profile, method)
            except Exception as exc:
                print(f'Telegram {method} profile warning: {exc}', flush=True)

        while True:
            try:
                response = await client.get(
                    f'https://api.telegram.org/bot{TOKEN}/getUpdates',
                    params={'timeout': 30, 'offset': offset, 'allowed_updates': json.dumps(['message'])}
                )
                data = _telegram_response_data(response, 'getUpdates')

                for update in data.get('result', []):
                    update_id = int(update['update_id'])
                    message = update.get('message') or {}
                    chat = str((message.get('chat') or {}).get('id', ''))
                    message_text, media_kind = _telegram_incoming_message(message)

                    # Ignore unsupported update envelopes and unauthorized chats safely.
                    # User messages with text, captions or known media types are all acknowledged.
                    if chat != ALLOWED_CHAT or (not message_text and not media_kind):
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
                            reply = (await telegram_command(message_text)) if message_text else mikasa_persona.unsupported_message(media_kind or 'message')
                        except Exception as exc:
                            print(f'Telegram command failed for update {update_id}: {exc}', flush=True)
                            if message_text:
                                _record_mikasa_unhandled_message(message_text)
                            reply = mikasa_persona.processing_error()
                        if not str(reply or '').strip():
                            reply = mikasa_persona.processing_error()
                        with connect() as c:
                            c.execute('INSERT OR IGNORE INTO telegram_inbox(update_id,chat_id,reply) VALUES(?,?,?)', (update_id, chat, reply))
                            c.execute('DELETE FROM telegram_inbox WHERE update_id < (SELECT COALESCE(MAX(update_id),0)-2000 FROM telegram_inbox) AND sent_at IS NOT NULL')

                    reply_keyboard = _telegram_context_keyboard(keyboard)
                    await _send_telegram_text(
                        client, reply, chat_id=chat, reply_markup=reply_keyboard, context='message'
                    )

                    # Persist the reply as delivered and advance the offset only
                    # after Telegram confirms delivery. A failed send reuses the
                    # queued reply instead of repeating the command's side effect.
                    offset = update_id + 1
                    with connect() as c:
                        c.execute('UPDATE telegram_inbox SET sent_at=CURRENT_TIMESTAMP WHERE update_id=?', (update_id,))
                        c.execute("INSERT INTO telegram_state(key,value) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
                retry_delay = 2
            except Exception as exc:
                print(f'Telegram polling error: {exc}', flush=True)
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)

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



TELEGRAM_MANAGE_RESOURCE_ALIASES = {
    'task': 'tasks', 'tasks': 'tasks',
    'event': 'events', 'events': 'events', 'appointment': 'events', 'appointments': 'events',
    'routine': 'routines', 'routines': 'routines', 'habit': 'routines', 'habits': 'routines',
    'bill': 'bills', 'bills': 'bills',
    'document': 'documents', 'documents': 'documents', 'doc': 'documents', 'docs': 'documents',
    'meal': 'recipes', 'meals': 'recipes', 'recipe': 'recipes', 'recipes': 'recipes',
    'shopping': 'shopping_items', 'grocery': 'shopping_items', 'groceries': 'shopping_items',
    'workout': 'workouts', 'workouts': 'workouts',
    'workout plan': 'workout_plan',
    'weight': 'weights', 'weights': 'weights',
    'treadmill': 'treadmill',
}

TELEGRAM_EDIT_FIELD_ALIASES = {
    'tasks': {'name': 'title', 'date': 'due_date', 'due': 'due_date', 'time': 'due_time'},
    'events': {'name': 'title', 'date': 'start_at', 'when': 'start_at', 'time': 'start_at'},
    'bills': {'amount': 'amount_mad', 'due': 'due_date', 'date': 'due_date'},
    'routines': {'interval': 'interval_days', 'every': 'interval_days', 'time': 'routine_time', 'due': 'next_due', 'date': 'next_due'},
    'documents': {'date': 'expiry_date', 'expiry': 'expiry_date', 'expires': 'expiry_date'},
    'recipes': {'stock': 'plates_ready', 'plates': 'plates_ready', 'calories': 'calories_kcal', 'kcal': 'calories_kcal', 'interval': 'eating_interval_days'},
    'shopping_items': {'done': 'checked', 'purchased': 'checked', 'status': 'checked'},
    'workouts': {'title': 'name'},
    'workout_plan': {'title': 'name', 'day': 'weekday'},
    'weights': {'weight': 'kg'},
    'treadmill': {'minutes': 'duration_min', 'duration': 'duration_min'},
}


def _telegram_resource_items(resource: str) -> list[dict[str, Any]]:
    """Read selectable rows for Mikasa management, preserving bill-series semantics."""
    if resource == 'bills':
        return active_bill_occurrences()
    if resource == 'recipes':
        return rows('SELECT * FROM recipes WHERE is_active=1 ORDER BY name COLLATE NOCASE')
    if resource not in TABLES:
        return []
    table, _ = TABLES[resource]
    return rows(f'SELECT * FROM {table} ORDER BY id DESC LIMIT 500')


def _telegram_resource_name_field(resource: str) -> str:
    return 'title' if resource in ('tasks', 'events') else 'name'


def _telegram_find_resource_item(resource: str, query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    items = _telegram_resource_items(resource)
    if resource in {'weights', 'treadmill'}:
        # Fitness logs are naturally identified by their saved calendar date.
        try:
            day = _telegram_parse_date(query).isoformat()
            matches = [item for item in items if str(item.get('date') or '') == day]
        except ValueError:
            matches = []
        if not matches and resource == 'weights':
            wanted = _telegram_name_key(query)
            matches = [item for item in items if wanted and _telegram_name_key(str(item.get('kg') or '')) == wanted]
        return (matches[0] if len(matches) == 1 else None), matches
    field = _telegram_resource_name_field(resource)
    matched, matches = _telegram_match_named_item(query, items, field)
    if resource == 'workout_plan' and not matched:
        day_match, day_matches = _telegram_match_named_item(query, items, 'weekday')
        if day_match or day_matches:
            return day_match, day_matches
    return matched, matches


def _telegram_parse_datetime(value: str) -> str:
    text_value = re.sub(r'(?i)\bat\b', ' ', str(value or '').strip())
    time_match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', text_value)
    if not time_match:
        raise ValueError('Please include a time in 24-hour HH:MM format, for example 25/11/26 10:00.')
    time_value = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
    date_text = (text_value[:time_match.start()] + ' ' + text_value[time_match.end():]).strip(' ,')
    parsed_day = _telegram_parse_date(date_text or 'today')
    if parsed_day is None:
        raise ValueError('I could not understand that date.')
    return f'{parsed_day.isoformat()}T{time_value}'


def _telegram_parse_management_fields(resource: str, specification: str) -> dict[str, Any]:
    """Parse a small, explicit key=value patch; API validators remain authoritative."""
    _, allowed = TABLES[resource]
    fields: dict[str, Any] = {}
    segments = re.split(r',\s*(?=[a-zA-Z_][a-zA-Z0-9_ ]*\s*(?:=|:))', specification.strip())
    for segment in segments:
        match = re.match(r'^\s*([a-zA-Z_][a-zA-Z0-9_ ]*)\s*(?:=|:)\s*(.*?)\s*$', segment)
        if not match:
            raise ValueError('Use field=value pairs separated by commas, for example due_date=tomorrow,due_time=18:00.')
        key = re.sub(r'\s+', '_', match.group(1).strip().lower())
        value = match.group(2).strip()
        key = TELEGRAM_EDIT_FIELD_ALIASES.get(resource, {}).get(key, key)
        if key not in allowed:
            valid = ', '.join(allowed)
            raise ValueError(f"I can't change '{key}' for this item. Available fields: {valid}.")

        if key in {'due_date', 'next_due', 'last_done', 'expiry_date', 'date'}:
            fields[key] = None if value.lower() in {'none', 'clear', 'no date'} else _telegram_parse_date(value).isoformat()
        elif key in {'start_at', 'end_at'}:
            fields[key] = None if value.lower() in {'none', 'clear'} else _telegram_parse_datetime(value)
        elif key in {'due_time', 'routine_time', 'routine_late_time'}:
            if value.lower() in {'none', 'clear', 'no time'}:
                fields[key] = None
            else:
                parsed = datetime.strptime(value, '%H:%M')
                fields[key] = parsed.strftime('%H:%M')
        elif key in {'amount_mad', 'kg', 'duration_min', 'distance_km', 'speed_kmh', 'incline', 'calories_kcal', 'weight_kg', 'servings'}:
            fields[key] = None if value.lower() in {'none', 'clear'} and key == 'calories_kcal' else float(value.replace(',', '.'))
        elif key in {'interval_days', 'reminder_days', 'routine_rollover_after_late', 'plates_ready', 'eating_interval_days', 'batch_plates', 'sets', 'checked'}:
            if key == 'checked':
                if value.lower() in {'yes', 'true', '1', 'done', 'purchased', 'checked'}:
                    fields[key] = 1
                elif value.lower() in {'no', 'false', '0', 'open', 'pending', 'unchecked'}:
                    fields[key] = 0
                else:
                    raise ValueError('For checked, use yes or no.')
            else:
                number = float(value)
                if not number.is_integer():
                    raise ValueError(f'{key.replace("_", " ").title()} must be a whole number.')
                fields[key] = int(number)
        elif key in {'ingredients_json', 'excluded_weekdays_json'}:
            items = [part.strip() for part in re.split(r'[;|]', value) if part.strip()]
            fields[key] = json.dumps(items, ensure_ascii=False)
        else:
            fields[key] = value
    if not fields:
        raise ValueError('Tell me which field you want to change.')
    return fields


def _telegram_format_management_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = str(exc.detail)
        return detail[:700]
    if isinstance(exc, (ValueError, TypeError, OverflowError)):
        return str(exc)[:700]
    return 'I could not complete that change. Nothing has been confirmed as successful; please check the record and try again.'


def _telegram_manage_request(raw: str) -> str | None:
    """Handle explicit add/edit/delete requests for XROB records from Telegram."""
    text_value = str(raw or '').strip()
    if not text_value:
        return None

    # Generic editing uses an explicit, auditable field list after a pipe.
    aliases = sorted(TELEGRAM_MANAGE_RESOURCE_ALIASES, key=len, reverse=True)
    alias_pattern = '|'.join(re.escape(alias) for alias in aliases)
    edit_match = re.match(
        rf'(?is)^(?:/edit|edit|update|change|set)\s+(?P<kind>{alias_pattern})\s+(?P<target>.+?)\s*\|\s*(?P<fields>.+)$',
        text_value,
    )
    if edit_match:
        _telegram_set_state(None)
        resource = TELEGRAM_MANAGE_RESOURCE_ALIASES[edit_match.group('kind').lower()]
        target = edit_match.group('target').strip()
        try:
            matched, matches = _telegram_find_resource_item(resource, target)
            if not matched:
                if len(matches) > 1:
                    field = _telegram_resource_name_field(resource)
                    options = ', '.join(str(item.get(field) or 'Untitled') for item in matches[:6])
                    return f'I found more than one match. Which one did you mean: {options}?'
                return f"I couldn't find a saved {edit_match.group('kind')} called '{target}'."
            changes = _telegram_parse_management_fields(resource, edit_match.group('fields'))
            result = update_resource(resource, int(matched['id']), Payload(data=changes))
            field = _telegram_resource_name_field(resource)
            name = str((result or matched).get(field) or target)
            summary_fields = ', '.join(f'{key.replace("_", " ")}={value}' for key, value in changes.items())
            return f'✅ Updated {edit_match.group("kind")} · {name}\nSaved: {summary_fields}.'
        except Exception as exc:
            return f"I couldn't update that record: {_telegram_format_management_error(exc)}"

    # Deletion always enters a confirmation state before touching the database.
    delete_match = re.match(rf'(?is)^(?:/delete|delete|remove)\s+(?P<kind>{alias_pattern})\s+(?P<target>.+?)\s*$', text_value)
    if delete_match:
        _telegram_set_state(None)
        resource = TELEGRAM_MANAGE_RESOURCE_ALIASES[delete_match.group('kind').lower()]
        target = delete_match.group('target').strip()
        try:
            matched, matches = _telegram_find_resource_item(resource, target)
            if not matched:
                if len(matches) > 1:
                    field = _telegram_resource_name_field(resource)
                    options = ', '.join(str(item.get(field) or 'Untitled') for item in matches[:6])
                    return f'I found several matches. Please use the exact name: {options}.'
                return f"I couldn't find a saved {delete_match.group('kind')} called '{target}'."
            field = _telegram_resource_name_field(resource)
            name = str(matched.get(field) or target)
            _telegram_set_flow('delete_resource', 'confirm', {
                'resource': resource, 'id': int(matched['id']), 'name': name,
                'kind': delete_match.group('kind').lower(),
                'series_id': matched.get('series_id'),
            })
            extra = ' This removes only the selected bill occurrence, not its other history.' if resource == 'bills' else ''
            if resource == 'recipes':
                extra = ' Confirmed eaten-meal history is preserved.'
            return f"Should I remove {delete_match.group('kind')} '{name}'?{extra}\nReply YES to confirm or NO to keep it."
        except Exception as exc:
            return f"I couldn't prepare that removal: {_telegram_format_management_error(exc)}"

    # New record helpers for the remaining app areas; tasks, meals, shopping and
    # events also retain their existing dedicated/guided handlers below.
    add_match = re.match(r'(?is)^(?:/add|add|create|new|log)\s+(?P<kind>routine|bill|document|doc|workout|event|appointment)\s+(?P<body>.+)$', text_value)
    if add_match:
        _telegram_set_state(None)
        kind = add_match.group('kind').lower()
        body = add_match.group('body').strip()
        try:
            if kind in {'event', 'appointment'}:
                parts = [part.strip() for part in body.split('|', 1)]
                if len(parts) != 2 or not parts[0]:
                    raise ValueError('Use: add event Dentist|25/11/26 10:00.')
                data = {'title': parts[0], 'start_at': _telegram_parse_datetime(parts[1]), 'category': 'personal'}
                result = create_resource('events', Payload(data=data))
            elif kind == 'routine':
                parts = [part.strip() for part in body.split('|')]
                if len(parts) >= 2:
                    name = parts[0]
                    interval = int(parts[1])
                    routine_time = parts[2] if len(parts) >= 3 else '10:00'
                else:
                    pattern = re.match(r'(?is)^(.+?)(?:\s+every\s+(\d+)\s+days?)?(?:\s+at\s+(\d{1,2}:\d{2}))?$', body)
                    name = pattern.group(1).strip() if pattern else ''
                    interval = int(pattern.group(2) or 1) if pattern else 1
                    routine_time = pattern.group(3) if pattern and pattern.group(3) else '10:00'
                if not name:
                    raise ValueError('Give the routine a name, for example: add routine Stretching every 2 days at 08:00.')
                result = create_resource('routines', Payload(data={
                    'name': name, 'interval_days': interval, 'routine_time': routine_time,
                    'next_due': date.today().isoformat(), 'category': 'personal',
                }))
            elif kind == 'bill':
                parts = [part.strip() for part in body.split('|')]
                if len(parts) < 3:
                    raise ValueError('Use: add bill Internet|299|30/11/26|monthly. Recurrence can be none, monthly, quarterly or yearly.')
                recurrence = parts[3].lower() if len(parts) >= 4 and parts[3] else 'none'
                result = create_resource('bills', Payload(data={
                    'name': parts[0], 'amount_mad': float(parts[1].replace(',', '.')),
                    'due_date': _telegram_parse_date(parts[2]).isoformat(), 'recurrence': recurrence,
                    'status': 'unpaid', 'reminder_days': 3,
                }))
            elif kind in {'document', 'doc'}:
                parts = [part.strip() for part in body.split('|', 1)]
                if len(parts) != 2:
                    raise ValueError('Use: add document Passport|31/12/27.')
                result = create_resource('documents', Payload(data={
                    'name': parts[0], 'expiry_date': _telegram_parse_date(parts[1]).isoformat(),
                    'reminder_days': 30,
                }))
            else:  # workout / log workout
                name = body.split('|', 1)[0].strip()
                if not name:
                    raise ValueError('Give the workout a name, for example: log workout Upper body.')
                result = create_resource('workouts', Payload(data={
                    'name': name, 'date': date.today().isoformat(), 'notes': '',
                }))
            label_field = _telegram_resource_name_field({'event': 'events', 'appointment': 'events', 'doc': 'documents'}.get(kind, kind + 's'))
            name = str((result or {}).get(label_field) or body.split('|', 1)[0])
            return f'✅ Added {kind} · {name}. It is saved in XROB, love.'
        except Exception as exc:
            return f"I couldn't add that {kind}: {_telegram_format_management_error(exc)}"

    return None


def _mikasa_app_knowledge_reply(raw: str) -> str | None:
    """Answer XROB questions from a curated map of implemented behavior."""
    low = _normalize_telegram_command_text(raw)
    overview_queries = {
        '/about', '/features', 'about xrob', 'what is xrob', 'what is xrob assistant',
        'what do you know about this app', 'what do you know about xrob',
        'explain this app', 'how does xrob work', 'what does this app do',
        'what can you manage', 'what can you control', 'show app features', 'app features',
        'show me what you can manage', 'what parts of my app can you manage',
        'give me an overview of the app', 'explain all app features',
    }
    if low in overview_queries:
        return _telegram_capabilities_message()

    management_queries = {
        'how do i edit records', 'how can you edit records', 'how do i update records',
        'how do i change a saved item', 'how can you change my records', 'how do i manage records',
        'how can you manage my app', 'can you edit my records', 'can you update my records',
    }
    if low in management_queries:
        return (
            "Of course, love. I use XROB's own validated record handlers. To edit, write "
            "`edit task Study | due_date=tomorrow,priority=high` and replace the resource, saved name and fields. "
            "Supported types include tasks, events, routines, bills, documents, recipes, shopping items, workouts, "
            "workout plans, weights and treadmill logs. To remove, write `delete task Study`; I'll ask you to reply "
            "YES or NO before anything is deleted. Add tasks with `task Study tomorrow at 18:00`, routines/bills/" 
            "documents/events with `add ...`, workouts with `log workout ...`, recipes with `/addrecipe` and shopping "
            "items with `/additem`. 💗"
        )

    delete_queries = {'how do i delete a record', 'how do i remove an item', 'can you delete records', 'how does delete work'}
    if low in delete_queries:
        return (
            "Tell me the record type and its saved name, for example `delete task Study`. I'll identify the exact match, "
            "show you what will be removed and wait for YES or NO. A NO keeps it unchanged, love. 💗"
        )

    limitation_queries = {'what can you not do', 'what can you not manage', 'what are your limits', 'what are your limitations'}
    if low in limitation_queries:
        return (
            "I can manage the XROB records and flows listed in /help, but I cannot make payments, access unconnected "
            "external accounts, change Home Assistant settings, or perform arbitrary actions outside XROB. In this local "
            "build I also cannot analyse Telegram photos/videos, transcribe voice notes or open Telegram file contents. "
            "I would rather tell you clearly than pretend, 7obi. 🤍"
        )

    architecture_queries = {
        'how do you work', 'how do you think', 'are you connected to an ai model',
        'do you use chatgpt', 'are you using a language model', 'do you use cloud ai',
        'are you a real ai assistant',
    }
    if low in architecture_queries:
        return (
            "I'm Mikasa, the XROB assistant, love. This version uses a local rules-based command and conversation router "
            "connected to XROB's database and validated actions; it is not connected to a general-purpose cloud language "
            "model. That keeps your app records local to the add-on, but it means I understand the supported commands and "
            "documented app questions—not every possible sentence. 🤍"
        )

    meal_rule_queries = {
        'how does meal scheduling work', 'how does the meal planner work',
        'what are the meal scheduling rules', 'explain my meal plan rules',
        'why is this meal scheduled', 'how are meals scheduled',
    }
    if low in meal_rule_queries or ('meal' in low and any(term in low for term in ('rules', 'schedule logic', 'automatic scheduling'))):
        return (
            "Of course, 7obi. Here is how your meal planner behaves:\n"
            "• One plate is reserved per date.\n"
            "• Each recipe follows its saved minimum repeat interval from its latest planned or eaten occurrence.\n"
            "• Closed weekdays and dates you marked free are skipped.\n"
            "• Home meals need ready stock; scheduled plates reserve stock, and confirming a plate as eaten updates the kitchen count.\n"
            "• Your manual future choices and confirmed eating history are preserved when the automatic plan is rebuilt.\n"
            "• ‘Not today’ moves the pending plan forward without compressing a recipe's saved interval.\n"
            "I won't silently replace an eaten plate or refill a date you deliberately left empty. 💗"
        )

    backup_queries = {'how do backups work', 'where are my backups', 'does xrob make backups', 'database backup'}
    if low in backup_queries:
        return (
            "XROB stores its database in the add-on configuration directory. It can create an online SQLite snapshot "
            "before migrations, keep a rolling set of recent snapshots, and create a manual backup from Settings. "
            "Those snapshots are a safety net, not a replacement for a separate Home Assistant backup, love. 🤍"
        )

    privacy_queries = {'where is my data stored', 'is my xrob data local', 'does xrob use cloud ai', 'is this app local'}
    if low in privacy_queries:
        return (
            "Your XROB records are kept in the add-on's local configuration database. Telegram messages are processed "
            "by the configured bot connection; the app's command engine itself uses local rules and your saved records. "
            "This build does not send your records to a hosted language model, love. 🤍"
        )

    return None


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


def _telegram_pending_routine_offers(now: datetime | None = None) -> list[dict[str, Any]]:
    """Return recent routine reminders that the user may accept or defer."""
    now = now or datetime.now()
    row = one("SELECT value FROM telegram_state WHERE key='mikasa_pending_routine_offers'")
    try:
        offers = json.loads(row['value']) if row and row.get('value') else []
    except (TypeError, ValueError, json.JSONDecodeError):
        offers = []
    if not isinstance(offers, list):
        offers = []
    fresh = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        try:
            sent_at = datetime.fromisoformat(str(offer.get('sent_at') or ''))
            if timedelta(0) <= now - sent_at <= timedelta(hours=36):
                fresh.append(offer)
        except (TypeError, ValueError):
            continue
    if fresh != offers:
        _telegram_save_pending_routine_offers(fresh)
    return fresh


def _telegram_save_pending_routine_offers(offers: list[dict[str, Any]]) -> None:
    with connect() as c:
        if offers:
            c.execute(
                "INSERT INTO telegram_state(key,value) VALUES('mikasa_pending_routine_offers',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(offers[-20:], ensure_ascii=False),),
            )
        else:
            c.execute("DELETE FROM telegram_state WHERE key='mikasa_pending_routine_offers'")


def _telegram_record_routine_offer(routine_id: int, now: datetime | None = None) -> bool:
    """Remember a delivered routine reminder so a natural refusal has a safe target."""
    now = now or datetime.now()
    routine = one('SELECT id,name,next_due FROM routines WHERE id=?', (int(routine_id),))
    if not routine:
        return False
    offers = [offer for offer in _telegram_pending_routine_offers(now)
              if int(offer.get('routine_id') or 0) != int(routine_id)]
    offers.append({
        'routine_id': int(routine['id']),
        'name': str(routine['name']),
        'reminder_day': now.date().isoformat(),
        'sent_at': now.isoformat(timespec='seconds'),
    })
    _telegram_save_pending_routine_offers(offers)
    return True


def _is_routine_decline_message(text: str) -> bool:
    low = _normalize_telegram_command_text(text)
    patterns = (
        r"^(?:no[, ]*)?(?:i\s+)?(?:do not|don't|dont)\s+want\s+to(?:\s+do\s+.+)?$",
        r"^(?:no[, ]*)?(?:i\s+)?(?:don't|dont|do not)\s+feel\s+like\s+(?:it|doing\s+it|that)$",
        r"^(?:not now|not today|maybe tomorrow|tomorrow instead|move it to tomorrow|do it tomorrow|skip (?:this|that|the routine|it))$",
        r"^(?:no[, ]*)?(?:i\s+)?can't\s+(?:do\s+)?(?:it|that|this)(?:\s+today)?$",
    )
    return any(re.fullmatch(pattern, low) for pattern in patterns)


def _telegram_decline_pending_routine(text: str) -> str:
    """Move the referenced/currently offered due routine to tomorrow, never a random future item."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    offers = _telegram_pending_routine_offers()
    due = rows('SELECT id,name,next_due FROM routines WHERE next_due IS NOT NULL AND next_due<=? ORDER BY next_due,name',
               (today.isoformat(),))
    if not due:
        for offer in reversed(offers):
            already = one('SELECT id,name,next_due FROM routines WHERE id=?', (int(offer.get('routine_id') or 0),))
            if already and already.get('next_due') == tomorrow.isoformat():
                remaining = [item for item in offers if int(item.get('routine_id') or 0) != int(already['id'])]
                _telegram_save_pending_routine_offers(remaining)
                return mikasa_persona.routine_already_tomorrow(str(already['name']), _telegram_date_label(tomorrow.isoformat(), True))
        return "Nothing due is waiting to be postponed, love. I haven't changed any routine. 🤍"

    low = _normalize_telegram_command_text(text)
    named_query = low
    for pattern in (
        r"^(?:no[, ]*)?(?:i\s+)?(?:do not|don't|dont)\s+want\s+to(?:\s+do\s+)?",
        r"^(?:no[, ]*)?(?:i\s+)?(?:don't|dont|do not)\s+feel\s+like\s+",
        r"^(?:not now|not today|maybe tomorrow|tomorrow instead|move it to tomorrow|do it tomorrow|skip)\s*",
        r"^(?:no[, ]*)?(?:i\s+)?can't\s+(?:do\s+)?",
    ):
        named_query = re.sub(pattern, '', named_query).strip()
    named_query = re.sub(r"^(?:the|this|that|it|my)\s+", '', named_query).strip()
    named_query = re.sub(r"\s+(?:today|tomorrow|please)$", '', named_query).strip()

    selected = None
    if named_query and named_query not in {'it', 'that', 'this', 'routine'}:
        selected, matches = _telegram_match_named_item(named_query, due, 'name')
        if not selected and len(matches) > 1:
            return 'Which routine, 7obi? ' + ', '.join(item['name'] for item in matches[:6])

    if selected is None and offers:
        for offer in reversed(offers):
            candidate = next((item for item in due if int(item['id']) == int(offer.get('routine_id') or 0)), None)
            if candidate:
                selected = candidate
                break

    if selected is None and not offers:
        if len(due) == 1:
            selected = due[0]
        elif len(due) > 1:
            return 'Of course, zin dyali. Which routine should I move to tomorrow? ' + ', '.join(item['name'] for item in due[:6])

    if selected is None:
        return "I couldn't match that to a routine that's currently due, kbida. Nothing has been changed. 🤍"

    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT name,next_due FROM routines WHERE id=?', (int(selected['id']),)).fetchone()
        if not current or not current['next_due'] or current['next_due'] > today.isoformat():
            return "That routine has already been handled or rescheduled, 7obi. I left it unchanged. 💗"
        changed = c.execute('UPDATE routines SET next_due=? WHERE id=? AND next_due=?',
                            (tomorrow.isoformat(), int(selected['id']), current['next_due']))
        if changed.rowcount != 1:
            return "That routine changed while I was checking. I left the newer schedule untouched, love. 🤍"
    remaining = [offer for offer in offers if int(offer.get('routine_id') or 0) != int(selected['id'])]
    _telegram_save_pending_routine_offers(remaining)
    return mikasa_persona.routine_moved_to_tomorrow(str(current['name']), _telegram_date_label(tomorrow.isoformat(), True))


def _mikasa_event_countdown_query(raw: str) -> str | None:
    """Extract an event name from natural countdown questions."""
    low = _normalize_telegram_command_text(raw)
    patterns = (
        r"(?:how\s+(?:many|much)\s+days?\s+(?:are\s+)?(?:left|remaining|remain)\s+(?:for|until|before)\s+)(?P<name>.+)$",
        r"(?:how\s+(?:many|much)\s+days?\s+(?:until|before|to)\s+)(?P<name>.+)$",
        r"(?:days?\s+(?:left|remaining|remain)\s+(?:for|until|before)\s+)(?P<name>.+)$",
        r"(?:how\s+long\s+(?:until|before)\s+)(?P<name>.+)$",
        r"(?:when\s+(?:is|are)\s+(?:my\s+)?)(?P<name>.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, low)
        if match:
            query = match.group('name').strip(' .?!')
            query = re.sub(r"^(?:my|the)\s+", '', query).strip()
            query = re.sub(r"\s+(?:event|appointment)$", '', query).strip()
            if query:
                return query
    return None


def _mikasa_event_countdown_reply(raw: str, today: date | None = None) -> str | None:
    """Look up a saved event and return its exact calendar-day countdown."""
    query = _mikasa_event_countdown_query(raw)
    if not query:
        return None
    today = today or date.today()
    wanted_tokens = _telegram_name_key(query).split()
    wanted_tokens = [token[:-1] if len(token) > 3 and token.endswith('s') else token for token in wanted_tokens]
    wanted = ' '.join(wanted_tokens)
    matches: list[tuple[int, str, dict[str, Any], date, str | None]] = []
    for event in rows('SELECT id,title,start_at FROM events ORDER BY start_at,id'):
        try:
            start_raw = str(event.get('start_at') or '')
            event_day = date.fromisoformat(start_raw[:10])
            event_time = start_raw[11:16] if len(start_raw) >= 16 else None
            title_key = _telegram_name_key(event.get('title'))
            title_tokens = [token[:-1] if len(token) > 3 and token.endswith('s') else token for token in title_key.split()]
            title_normalized = ' '.join(title_tokens)
            title_set, wanted_set = set(title_tokens), set(wanted_tokens)
            if not wanted_set:
                continue
            if title_normalized == wanted:
                score = 100
            elif wanted in title_normalized or title_normalized in wanted:
                score = 90
            elif wanted_set.issubset(title_set):
                score = 80 + len(wanted_set)
            elif title_set.intersection(wanted_set) and len(wanted_set) == 1:
                score = 60
            else:
                continue
            matches.append((score, str(event.get('title') or query), event, event_day, event_time))
        except (TypeError, ValueError):
            continue
    if not matches:
        return mikasa_persona.event_not_found_reply(query)
    # Prefer the strongest name match, then the nearest upcoming occurrence.
    matches.sort(key=lambda item: (-item[0], 0 if item[3] >= today else 1,
                                   item[3] if item[3] >= today else -item[3].toordinal(), int(item[2]['id'])))
    _, title, _event, event_day, event_time = matches[0]
    return mikasa_persona.event_countdown_reply(
        title, event_day.strftime('%a %d/%m/%Y'), (event_day - today).days, event_time
    )


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
        buttons = []
        for value in choices:
            label = str(value.get('button') or value.get('name') or '').strip() if isinstance(value, dict) else str(value).strip()
            if label:
                buttons.append({'text': label[:64]})
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([{'text': '⬅️ Back'}])
        placeholder = 'Choose a meal for today'
    elif name == 'choose_today_meal_confirm' and step == 'confirm':
        rows = [[{'text': 'YES · Move it'}, {'text': 'NO · Keep it'}], [{'text': '⬅️ Back'}]]
        placeholder = 'Confirm reservation move'
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
    # Telegram-created meals enter the same refreshed upcoming schedule as web additions.
    _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
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
    _rebuild_automatic_meal_schedule(date.today() + timedelta(days=1))
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
    """Meals selectable today, including Home meals with a movable automatic reservation."""
    today = date.today()
    candidates = rows("""SELECT r.id,r.name,r.meal_source,r.plates_ready,r.calories_kcal,r.excluded_weekdays_json,
        COALESCE((SELECT COUNT(*) FROM daily_meals dm WHERE dm.recipe_id=r.id AND dm.status='pending' AND dm.plan_date>?),0) AS reserved_plates,
        EXISTS(SELECT 1 FROM daily_meals dm WHERE dm.recipe_id=r.id AND dm.status='pending'
               AND dm.assignment_source IN ('automatic','unknown') AND dm.plan_date>?) AS movable_reservation
        FROM recipes r WHERE r.is_active=1
          AND (r.meal_source='outside' OR (r.plates_ready>0 AND (
               r.plates_ready>COALESCE((SELECT COUNT(*) FROM daily_meals dm WHERE dm.recipe_id=r.id AND dm.status='pending' AND dm.plan_date>?),0)
               OR EXISTS(SELECT 1 FROM daily_meals dm WHERE dm.recipe_id=r.id AND dm.status='pending'
                         AND dm.assignment_source IN ('automatic','unknown') AND dm.plan_date>?))))
        ORDER BY CASE r.meal_source WHEN 'home' THEN 0 ELSE 1 END,r.name COLLATE NOCASE""",
        (today.isoformat(), today.isoformat(), today.isoformat(), today.isoformat()))
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


def telegram_agenda_line(entry: dict[str, Any]) -> str:
    """Compatibility wrapper; Mikasa's persona module owns the agenda wording."""
    return mikasa_persona.agenda_item_line(entry)


def _normalize_telegram_command_text(value: str) -> str:
    """Normalise harmless casing, spacing and trailing punctuation for menu commands."""
    normalized = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    normalized = re.sub(r'\s+', ' ', normalized)
    return re.sub(r'[\s.,!?؟،;:…]+$', '', normalized).strip()


def _is_today_agenda_request(low: str) -> bool:
    """Accept natural variations of the Today agenda shortcut."""
    aliases = {
        '📅 today', '/today', 'today', 'what is today', "what's today", 'whats today',
        'what do i have today', 'what do i have on today', 'what is on today',
        "what's on today", 'whats on today', 'what is on my schedule today',
        "what's on my schedule today", 'show me today', 'show today', 'today agenda',
        'agenda today', 'today schedule', "today's agenda", "today's schedule",
        "today's plan", 'my agenda today', 'my schedule today', 'today please', 'please today',
    }
    if low in aliases:
        return True
    return bool(re.fullmatch(
        r'(?:please )?(?:(?:show|tell) me )?(?:today(?:s)?(?: agenda| schedule| plan)?|my (?:agenda|schedule|plan) today)',
        low,
    ))


def _is_tomorrow_agenda_request(low: str) -> bool:
    """Accept natural short requests for the next day's plan."""
    aliases = {
        'tomorrow', 'tomorrow plan', 'plan tomorrow', 'tomorrow agenda',
        'agenda tomorrow', 'tomorrow schedule', 'schedule tomorrow',
        "tomorrow's plan", 'tomorrows plan', "tomorrow's agenda", 'tomorrows agenda',
        "what's tomorrow", 'whats tomorrow', 'what is tomorrow',
        "what's on tomorrow", 'whats on tomorrow', 'what is on tomorrow',
        'what do i have tomorrow', 'what is my plan tomorrow', 'my plan tomorrow',
        'show tomorrow', 'show me tomorrow', 'show tomorrow plan', 'show me tomorrow plan',
        'tomorrow please', 'please tomorrow',
    }
    if low in aliases:
        return True
    return bool(re.fullmatch(
        r'(?:please )?(?:(?:show|tell) me )?(?:tomorrow(?:s)?(?: agenda| schedule| plan)?|my (?:agenda|schedule|plan) tomorrow)',
        low,
    ))


async def _telegram_command_core(text: str) -> str:
    """Rules-based Telegram actions; the persona layer never changes these operations."""
    raw = text.strip()
    low = _normalize_telegram_command_text(raw)
    low = re.sub(r'(?m)^(\/[a-z0-9_]+)@[a-z0-9_]+', r'\1', low)
    today = date.today()
    today_iso = today.isoformat()

    if low in {'/status', 'status', 'app status', 'xrob status', 'my progress', 'progress'}:
        return mikasa_persona.app_status_reply(_mikasa_app_status_context())

    # Guided input flows. They survive restarts and can be cancelled with /cancel.
    state = _telegram_get_state()
    conversation_active = bool(state)
    flow_state = _telegram_get_flow()

    # Mikasa's generic delete flow is deliberately explicit and survives restarts.
    if flow_state and flow_state.get('flow') == 'delete_resource':
        if low in {'yes', 'y', 'confirm', 'delete', '/yes', '/confirm'}:
            payload = dict(flow_state.get('data') or {})
            _telegram_set_state(None)
            resource = str(payload.get('resource') or '')
            item_id = int(payload.get('id') or 0)
            name = str(payload.get('name') or 'record')
            kind = str(payload.get('kind') or resource.rstrip('s'))
            try:
                result = delete_resource(resource, item_id)
                if resource == 'recipes' and result.get('history_preserved'):
                    return f"Done, love. {name} was removed from your active meals; its confirmed history is safe. 💗"
                return f"Done, 7obi. {kind.title()} '{name}' has been removed from XROB. 💗"
            except Exception as exc:
                return f"I couldn't remove {name}: {_telegram_format_management_error(exc)}"
        if low in {'no', 'n', 'cancel', '/no', '/cancel', 'keep'}:
            _telegram_set_state(None)
            return 'Of course, love. I kept the record exactly as it was. 💗'
        return 'Please reply YES to confirm the removal or NO to keep the record. Nothing has been deleted yet.'

    if low in ('/cancel', 'cancel', 'stop'):
        _telegram_set_state(None)
        return 'Cancelled. Nothing was saved.'
    if low in ('⬅️ back', 'back', '/back'):
        _telegram_set_state(None)
        return '↩️ Back to the main menu. Choose an action from the menu below, or send /help for examples.'
    meal_menu_commands = (
        '🍽 today plate','🍽 today meals','🍽 meals','today plate','today meals','meals','/meals',
        '🗓 meal plan','🗓 7-day meals','🗓 upcoming meals','upcoming meals','meal plan','/mealplan',
        '✅ ate it','✅ i ate it','ate it','i ate it','⏭ not today','not today','yes','y','/yes','/eaten','no','n','/no','/skip',
        '↩️ undo eaten','undo eaten','undo meal','undo my meal','not eaten','i did not eat it',"i didn't eat it","i haven't eaten it",'/undo_eaten',
        '🛒 shopping list','shopping list','shopping','/shopping','➕ add recipe','add recipe','➕ add meal','add meal','/addrecipe',
        '🏠 home meals','home meals','/homemeals','🍴 outside meals','outside meals','/outsidemeals',
        '🔢 update plates','update plates','/stock','🗑 remove meal','remove meal','delete meal','/deletemeal','➕ add grocery','add grocery','/additem',
        '🍽 choose meal','choose meal','/choosemeal','⬅️ back','back','/back'
    )
    meal_interrupt_prefixes = ('bought ', 'got ', 'suggest meals', 'plan meals', '/bought', '/suggestmeals', 'add item ', 'add grocery ')
    guided_confirming = bool(flow_state and (flow_state.get('step') in ('confirm', 'delete_confirm') or flow_state.get('flow') == 'choose_today_meal_confirm'))
    guided_confirmation_reply = guided_confirming and low in ('yes', 'y', 'save', 'confirm', 'no', 'n', 'remove', 'delete', '/yes', '/no', '/save', '/confirm')
    guided_yes_no_reply = bool(conversation_active and low in ('yes', 'y', 'no', 'n'))
    flow_interrupted = (
        (low.startswith('/') and not guided_confirmation_reply)
        or low.startswith(('weight ', 'treadmill ', 'task ', 'paid ', 'done ', *meal_interrupt_prefixes))
        or (low in meal_menu_commands and not guided_yes_no_reply and not guided_confirmation_reply)
        or (_is_today_agenda_request(low) and not guided_yes_no_reply and not guided_confirmation_reply)
        or (_is_tomorrow_agenda_request(low) and not guided_yes_no_reply and not guided_confirmation_reply)
        or (low in ('⚖️ add weight', '🏃 add treadmill', '📈 fitness week', 'fitness week', 'week') and not guided_yes_no_reply and not guided_confirmation_reply)
    )
    meal_checkin_inputs = ('✅ ate it', '✅ i ate it', 'ate it', 'i ate it', 'yes', 'y', '/yes', '/eaten', '⏭ not today', 'not today', 'no', 'n', '/no', '/skip')
    if conversation_active and not guided_confirming and low in meal_checkin_inputs:
        return 'You are in the middle of a guided entry. Finish that step or send /cancel first; then use YES or NO for your meal check-in.'

    if flow_state and flow_state.get('flow') == 'choose_today_meal_confirm' and not flow_interrupted:
        if low in ('yes', 'y', 'move', 'yes · move it', 'confirm'):
            choice = flow_state.get('data', {})
            recipe_id = int(choice.get('recipe_id') or 0)
            available = _telegram_available_today_meals()
            matched = next((item for item in available if int(item['id']) == recipe_id), None)
            if not matched:
                _telegram_set_state(None)
                return 'That meal is no longer available today. Open Choose meal again to see current options.'
            try:
                selected = _select_daily_meal(recipe_id, today, replace_pending=True)
                _telegram_set_state(None)
                moved = selected.get('reallocated_from')
                move_text = (f"\nIts automatic reservation on {_telegram_date_label(moved, True)} was released." if moved else '')
                next_plan = one("SELECT MIN(plan_date) AS next_date FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>?", (recipe_id, today.isoformat()))
                repeat_text = f"\nNext planned repeat: {_telegram_date_label(next_plan['next_date'], True)}." if next_plan and next_plan.get('next_date') else f"\nThe next repeat follows the every {int(selected['eating_interval_days'] or 1)} day rule when stock and an open date are available."
                return f"✅ Today's plate selected: {selected['name']}{move_text}{repeat_text}. I'll check in tonight."
            except ValueError as exc:
                _telegram_set_state(None)
                return str(exc)
        if low in ('no', 'n', 'keep', 'no · keep it'):
            _telegram_set_state(None)
            return 'No change made. The existing reservation stays where it is.'
        return 'Please choose YES · Move it or NO · Keep it. You can also use Back.'

    if flow_state and flow_state.get('flow') == 'choose_today_meal' and not flow_interrupted:
        if flow_state.get('step') == 'name':
            query = raw.strip()
            available = _telegram_available_today_meals()
            id_match = re.search(r'#(\d+)\s*$', query)
            matched = next((item for item in available if int(item['id']) == int(id_match.group(1))), None) if id_match else None
            matches = []
            if not matched:
                matched, matches = _telegram_match_named_item(query, available)
            if not matched:
                if matches:
                    return 'I found more than one match. Please tap a meal button or type its full name:\n' + '\n'.join('• ' + item['name'] for item in matches[:8])
                return 'Please choose one of the meal buttons below, or use Back to return to the main menu.'
            ready = max(0, int(matched.get('plates_ready') or 0))
            reserved = max(0, int(matched.get('reserved_plates') or 0))
            current_today = one("SELECT recipe_id,status FROM daily_meals WHERE plan_date=?", (today.isoformat(),))
            already_today = bool(current_today and current_today.get('status') == 'pending' and int(current_today['recipe_id']) == int(matched['id']))
            if matched.get('meal_source') == 'home' and not already_today and ready <= reserved and bool(matched.get('movable_reservation')):
                _telegram_set_flow('choose_today_meal_confirm', 'confirm', {'recipe_id': int(matched['id']), 'name': matched['name']})
                return (f"🍽 {matched['name']} has {ready} ready plate(s), all reserved later in the plan.\n\n"
                        'Move this meal to today and rebuild its future repeats using the saved interval?\nReply YES to move it or NO to keep the current plan.')
            try:
                selected = _select_daily_meal(int(matched['id']), today, replace_pending=True)
                _telegram_set_state(None)
                kcal = f" · {float(selected['calories_kcal']):g} kcal/plate" if selected.get('calories_kcal') is not None else ''
                next_plan = one("SELECT MIN(plan_date) AS next_date FROM daily_meals WHERE recipe_id=? AND status='pending' AND plan_date>?", (int(matched['id']), today.isoformat()))
                repeat_text = f"Next planned repeat: {_telegram_date_label(next_plan['next_date'], True)}." if next_plan and next_plan.get('next_date') else f"The next repeat follows the every {int(selected['eating_interval_days'] or 1)} day rule when stock and an open date are available."
                return f"✅ Today's plate selected: {selected['name']}{kcal}. {repeat_text} I'll check in tonight."
            except ValueError as exc:
                refreshed = _telegram_available_today_meals()
                choices = [{'id': x['id'], 'name': x['name'], 'button': f"{x['name'][:34]} · {'Home' if x['meal_source']=='home' else 'Outside'} #{x['id']}"} for x in refreshed[:16]]
                _telegram_set_flow('choose_today_meal','name',{'choices':choices})
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
    if state and low not in ('yes', 'y', 'no', 'n') and (low.startswith('/') or _is_today_agenda_request(low) or _is_tomorrow_agenda_request(low) or low in ('✅ tasks','tasks','💳 bills','bills','🔁 routines','routines','🏃 fitness','fitness','📈 fitness week','fitness week','week','⚖️ add weight','🏃 add treadmill','❔ help','help') or low in meal_menu_commands or low.startswith(('weight ','log weight ','treadmill ','run ','walk ','task ','paid ','done ', *meal_interrupt_prefixes))):
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
    today_meal_commands = ('🍽 today plate','🍽 today meals','🍽 meals','today meals','today plate','meals','/meals','what should i eat today','what am i eating today')
    choose_meal_commands = ('🍽 choose meal','choose meal','/choosemeal')
    if low in today_meal_commands or low in choose_meal_commands:
        today_meal = one("SELECT dm.id,dm.plan_date,dm.status,r.name AS recipe_name,r.calories_kcal FROM daily_meals dm JOIN recipes r ON r.id=dm.recipe_id WHERE dm.plan_date=?", (today_iso,))
        if low in today_meal_commands and today_meal:
            return _telegram_meal_text(today,1)
        if today_meal and today_meal.get('status') == 'eaten':
            return '✅ Today’s plate is already confirmed as eaten. Choose a meal for another day from the weekly plan.'
        available = _telegram_available_today_meals()
        if not available:
            _telegram_set_state(None)
            return '🍽️ No meal is available to choose today. Add an Outside meal with /addrecipe, or update Home stock with /stock.\n\nUse Back to return to the main menu.'
        choices = [{'id': meal['id'], 'name': meal['name'], 'button': f"{meal['name'][:34]} · {'Home' if meal['meal_source']=='home' else 'Outside'} #{meal['id']}"} for meal in available[:16]]
        _telegram_set_flow('choose_today_meal','name',{'choices':choices})
        options='\n'.join(f"• {meal['name']} · {'Home' if meal['meal_source']=='home' else 'Outside'}" for meal in available[:12])
        more=f"\n…and {len(available)-12} more" if len(available)>12 else ''
        intro = 'Choose a replacement for today’s pending plate.' if today_meal else 'Choose a meal to schedule for today.'
        return '🍽️ WHAT WOULD YOU LIKE TO EAT TODAY?\n\n' + intro + '\n\n' + options + more + '\n\nTap a meal button or Back to return to the main menu.'
    if low in ('🗓 meal plan','🗓 upcoming meals','upcoming meals','meal plan','/mealplan','my meal plan'):
        return _telegram_meal_text(today,7)
    if low in ('🛒 shopping list','shopping list','shopping','/shopping','/groceries'):
        return _telegram_shopping_text()
    # Undo an accidental meal confirmation without shifting the plate to another day.
    undo_meal_inputs = (
        '↩️ undo eaten','undo eaten','undo meal','undo my meal','not eaten',
        'i did not eat it', "i didn't eat it", "i haven't eaten it", '/undo_eaten',
    )
    if low in undo_meal_inputs:
        eaten_today = one("SELECT id FROM daily_meals WHERE plan_date=? AND status='eaten' ORDER BY id DESC LIMIT 1", (today_iso,))
        if not eaten_today:
            return mikasa_persona.meal_undo_unavailable()
        try:
            _undo_daily_meal_confirmation(int(eaten_today['id']))
        except ValueError:
            return mikasa_persona.meal_undo_unavailable()
        return mikasa_persona.meal_unconfirmed()

    # The pending plate confirmation is intentionally a simple YES / NO reply.
    if not conversation_active and low in ('✅ ate it','✅ i ate it','ate it','yes','y','/yes','/eaten','i ate it','⏭ not today','not today','no','n','/no','/skip'):
        pending = one("SELECT id,plan_date FROM daily_meals WHERE status='pending' AND plan_date<=? ORDER BY plan_date,id LIMIT 1", (today_iso,))
        if not pending:
            if _is_routine_decline_message(low):
                return _telegram_decline_pending_routine(raw)
            return 'There is no plate waiting for confirmation.'
        eaten = low in ('✅ ate it','✅ i ate it','ate it','yes','y','/yes','/eaten','i ate it')
        try:
            updated = _confirm_daily_meal(int(pending['id']), eaten)
        except ValueError as exc:
            return str(exc)
        if eaten:
            return mikasa_persona.meal_confirmed()
        delay = int(updated.get('shift_days') or 1)
        return mikasa_persona.meal_moved(_telegram_date_label(updated['plan_date'], True), delay)

    if _is_routine_decline_message(low):
        return _telegram_decline_pending_routine(raw)

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
    if low in ('/start', 'start', '/menu', 'menu', 'hi', 'hello'):
        return mikasa_persona.start_message()
    if low in ('/help', 'help', '❔ help', '/capabilities', 'capabilities', 'what can you do'):
        return _telegram_capabilities_message()

    if _is_tomorrow_agenda_request(low):
        tomorrow = today + timedelta(days=1)
        agenda = build_today_agenda(tomorrow)
        return mikasa_persona.tomorrow_agenda_reply(agenda, tomorrow)

    if _is_today_agenda_request(low):
        agenda = build_today_agenda(today)
        return mikasa_persona.today_agenda_reply(agenda, today)

    if low in ('✅ tasks', '/tasks', 'tasks', 'my tasks', 'show tasks', 'list tasks', 'to do', 'todo', 'what are my tasks', 'what tasks do i have', 'show my tasks', 'what tasks are open', 'my open tasks'):
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

    if low in ('💳 bills', '/bills', 'bills', 'my bills', 'unpaid bills', 'what bills are due', 'show my bills', 'what do i owe', 'how much do i owe', 'show unpaid bills', 'what bills do i have', 'list my bills'):
        bills = active_bill_occurrences()[:10]
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

    if low in ('/documents', 'documents', 'document status', 'show my documents', 'what documents are expiring', 'any documents expiring'):
        documents = rows('SELECT id,name,expiry_date,reminder_days,reference FROM documents ORDER BY expiry_date,id LIMIT 20')
        if not documents:
            return 'No documents are saved yet. Add expiry dates in the Documents section of XROB Assistant.'
        lines = ['📄 DOCUMENTS & EXPIRY DATES']
        for document in documents:
            try:
                expiry = date.fromisoformat(document['expiry_date'])
                days = (expiry - today).days
                label = 'EXPIRED' if days < 0 else ('DUE TODAY' if days == 0 else f'in {days}d')
            except (TypeError, ValueError):
                label = 'DATE NEEDS CHECK'
            try:
                expiry_label = date.fromisoformat(document['expiry_date']).strftime('%d/%m/%y')
            except (TypeError, ValueError):
                expiry_label = 'date not set'
            reference = f" · {document['reference']}" if document.get('reference') else ''
            lines.append(f"• {document['name']} · {expiry_label} · {label}{reference}")
        return '\n'.join(lines)

    if low in ('🔁 routines', '🧴 personal care', 'care', 'personal care', '/care', 'routines', 'my routines', '/routines', 'what are my routines', 'show my routines', 'what routines are due', 'list my routines'):
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

    if low in ('🏃 fitness', '/fitness', 'fitness', 'activity', 'my fitness', 'workouts', 'how is my fitness', 'show my fitness progress', 'fitness stats', 'my fitness progress', 'how is my workout progress', 'what is my current weight', 'how much do i weigh', 'when did i last weigh in'):
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

    if low in ('📈 fitness week', 'fitness week', '/fitnessweek', '/week', 'week', 'this week', 'weekly fitness'):
        start = today - timedelta(days=6)
        weight_rows = rows(
            'SELECT date,kg FROM weights WHERE date>=? AND date<=? ORDER BY date,id',
            (start.isoformat(), today_iso),
        )
        latest_weight = weight_rows[-1] if weight_rows else one('SELECT date,kg FROM weights ORDER BY date DESC,id DESC LIMIT 1')
        treadmill = one(
            'SELECT COUNT(*) AS sessions,COALESCE(SUM(duration_min),0) AS mins,COALESCE(SUM(distance_km),0) AS km,COALESCE(SUM(calories_kcal),0) AS kcal FROM treadmill WHERE date>=? AND date<=?',
            (start.isoformat(), today_iso),
        ) or {}
        workouts = one('SELECT COUNT(*) AS sessions FROM workouts WHERE date>=? AND date<=?', (start.isoformat(), today_iso)) or {}
        lines = [f'📈 FITNESS · LAST 7 DAYS\n{start.strftime("%d/%m")} – {today.strftime("%d/%m/%y")}']
        if latest_weight:
            lines.append(f"⚖️ Latest weight · {float(latest_weight['kg']):.2f} kg · {_telegram_date_label(latest_weight['date'])}")
            if len(weight_rows) >= 2:
                change = float(weight_rows[-1]['kg']) - float(weight_rows[0]['kg'])
                lines.append(f"Change this week · {change:+.2f} kg")
            elif len(weight_rows) == 1:
                lines.append('Weight entries this week · 1')
            else:
                lines.append('No new weigh-in during this 7-day period.')
        else:
            lines.append('⚖️ Weight · no records yet')
        if int(treadmill.get('sessions') or 0):
            lines.append(
                f"🏃 Treadmill · {int(treadmill['sessions'])} session(s) · {int(treadmill['mins'])} min · "
                f"{float(treadmill['km']):.2f} km · {int(treadmill['kcal'])} estimated kcal"
            )
        else:
            lines.append('🏃 Treadmill · no sessions logged')
        lines.append(f"🏋️ Gym · {int(workouts.get('sessions') or 0)} workout(s)")
        lines.append('')
        lines.append('Quick log: tap Add weight or Add treadmill.')
        return '\n'.join(lines)

    if low in ('📆 next 7 days', 'next', 'next 7 days', 'agenda', '/agenda', 'show my agenda', 'show my calendar', 'what appointments do i have', 'what is on this week'):
        end = (today + timedelta(days=7)).isoformat()
        items: list[tuple[str, str]] = []
        for event in rows('SELECT title,start_at FROM events WHERE substr(start_at,1,10)>=? AND substr(start_at,1,10)<=?', (today_iso, end)):
            items.append((event['start_at'], f"{event['start_at'][8:10]}/{event['start_at'][5:7]} {event['start_at'][11:16]} · {event['title']}"))
        for task in rows("SELECT title,due_date,due_time FROM tasks WHERE status!='done' AND due_date>=? AND due_date<=?", (today_iso, end)):
            clock = task.get('due_time') or '23:59'
            items.append((task['due_date']+'T'+clock, f"{_telegram_date_label(task['due_date'])} {task.get('due_time') or 'Any time'} · {task['title']}"))
        for bill in [b for b in active_bill_occurrences() if today_iso <= b['due_date'] <= end]:
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
        bills = active_bill_occurrences()
        matched, matches = _telegram_match_named_item(query, bills)
        if not matched:
            if len(matches) > 1:
                return 'Which bill? ' + ', '.join(item['name'] for item in matches[:5])
            return f"I couldn't find an unpaid bill called '{query}'. Send Bills to see the list."
        marked = mark_bill_paid(matched['id'])
        series_id = int(marked.get('series_id') or matched['id'])
        next_open = next((bill for bill in active_bill_occurrences()
                          if int(bill.get('series_id') or bill['id']) == series_id), None)
        next_text = f" Next bill due {_telegram_date_label(next_open['due_date'])}." if next_open else ''
        return mikasa_persona.bill_paid(matched['name'], f"{float(matched['amount_mad']):.2f} MAD", next_text)

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
            c.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?", (today_iso, matched['id']))
        return mikasa_persona.task_completed(matched['title'])

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
                return mikasa_persona.task_created(title, f"{_telegram_date_label(due_date)} at {due_time}.")
            if due_date:
                return mikasa_persona.task_created(title, f"Due {_telegram_date_label(due_date)}.")
            return mikasa_persona.task_created(title, "There’s no reminder attached yet; add a date and time in the app whenever you like.") 
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
        return mikasa_persona.routine_completed(updated['name'], next_due) 

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


def _telegram_capabilities_message() -> str:
    """Describe only actions the current Telegram implementation really supports."""
    return (
        'Mikasa — your XROB assistant.\n' +
        mikasa_persona.help_intro() + '\n\n' +
        '📅 YOUR DAY\n'
        '• /today — tasks, routines, events, workouts, meals, bill and document alerts, and completed activity.\n'
        '• /tomorrow — tomorrow’s agenda; /status — app progress, latest fitness records, meal stock and bills.\n'
        '• /tasks, /bills, /routines, /documents, /agenda — see your saved lists and expiry dates.\n'
        '• Ask naturally: what are my tasks, what do I owe, what is left today, or what did I finish today.\n'
        '• task Study tomorrow at 18:00 — add a task; task done Study — finish it.\n'
        '• /event Dentist|25/11/26 10:00 or add event Dentist|25/11/26 10:00 — add an appointment.\n'
        '• Add records: add routine Stretching every 2 days at 08:00; add bill Internet|299|30/11/26|monthly; add document Passport|31/12/27; log workout Upper body.\n'
        '• Edit saved records with explicit fields: edit task Study | due_date=tomorrow,priority=high. Fields must use the names shown by XROB.\n'
        '• Edit/delete supported records: tasks, events, routines, bills, documents, recipes, shopping items, workouts, workout plans, weights and treadmill logs. Delete always asks YES/NO.\n'
        '• done Shower — complete a routine; paid Electricity — mark a bill paid.\n'
        '• Reply not now to a routine reminder to move that routine to tomorrow.\n\n'
        '🍽️ MEALS & SHOPPING\n'
        '• /meals, /choosemeal, /mealplan — view or choose plates.\n'
        '• YES / NO — confirm eating or safely move the pending meal plan.\n'
        '• /addrecipe, /stock, /homemeals, /outsidemeals, /deletemeal — manage meals and Home stock.\n'
        '• /shopping, /additem, bought tomatoes — manage shopping.\n\n'
        '💪 YOUR WELLBEING\n'
        '• Send 82.4 or weight 82.4 20/11/26 — record weight.\n'
        '• Send 30 or treadmill 30 yesterday — record treadmill minutes (5 km/h, 0% incline estimates).\n'
        '• /fitness, /fitnessweek — review recent fitness records.\n\n' +
        mikasa_persona.help_boundary() + '\n\n' +
        'Send /cancel to stop a guided entry.'
    )



def _mikasa_app_status_context() -> dict[str, Any]:
    """Collect a compact, factual snapshot from XROB's own records."""
    data = summary()
    home = one("SELECT COALESCE(SUM(plates_ready),0) AS plates FROM recipes WHERE is_active=1 AND meal_source='home'") or {}
    agenda = data.get('today_agenda') or {}
    agenda_items = list(agenda.get('items') or [])
    today = str(data.get('today') or date.today().isoformat())
    return {
        'open_count': int(data.get('today_open_items') or 0),
        'completed_count': int(agenda.get('completed_count') or 0),
        'unpaid_bills': int(data.get('unpaid_bills') or 0),
        'unpaid_total_mad': round(float(data.get('unpaid_total_mad') or 0), 2),
        'upcoming_bill': data.get('upcoming_bill'),
        'expiring_documents': int(data.get('expiring_documents') or 0),
        'events_today': int(data.get('events_today') or 0),
        'open_tasks': int(data.get('open_tasks') or 0),
        'routines_due_today': int(data.get('routines_due_today') or 0),
        'next_routine': data.get('next_routine'),
        'latest_weight': data.get('latest_weight'),
        'recent_treadmill': data.get('recent_treadmill'),
        'today_meal': data.get('today_meal'),
        'next_meal': data.get('next_meal'),
        'home_plates_ready': int(home.get('plates') or 0),
        'today_agenda_date': today,
        'agenda_items': agenda_items,
    }


def _mikasa_app_status_focus(raw: str) -> str | None:
    low = _normalize_telegram_command_text(raw)
    focus_aliases = {
        'meals': {
            'how are my meals', 'meal stock', 'how much food do i have', 'what is for dinner',
            "what's for dinner", 'what am i eating today', 'what is my meal today',
            'show my meal plan', 'what is my next meal', 'whats my next meal',
        },
        'open': {
            'what is left today', "what's left today", 'what do i have left', 'what needs my attention',
            'what should i focus on', 'show open items', 'what is still open',
        },
        'completed': {
            'what did i finish today', 'what have i completed today', 'show todays progress',
            "what's done today", 'what did i get done', 'show completed today',
        },
    }
    for focus, aliases in focus_aliases.items():
        if low in aliases:
            return focus
    return None


def _is_mikasa_status_request(raw: str) -> bool:
    low = _normalize_telegram_command_text(raw)
    aliases = {
        '/status', 'status', 'app status', 'xrob status', 'my progress', 'progress',
        'how am i doing', 'how am i doing today', 'give me my status',
        'show my progress', 'show me my progress', 'app overview', 'my app overview',
        'how am i doing with everything', 'how are things going', 'give me an overview',
        'show me my app status', 'how are my meals', 'meal stock', 'how much food do i have',
    }
    return low in aliases


def _is_mikasa_next_step_request(raw: str) -> bool:
    low = _normalize_telegram_command_text(raw)
    return low in {'what should i do next', 'what do i do next', 'what next', 'what is next', "what's next", 'next step'}

def _mikasa_context_for_message(raw: str) -> dict[str, int]:
    low = re.sub(r'\s+', ' ', str(raw or '').strip().lower())
    needs_agenda = (
        low in ('good morning', 'morning', 'gm', 'good afternoon', 'good evening')
        or any(term in low for term in (
            'motivate me', 'need motivation', 'give me motivation', 'i need a push',
            'pep talk', 'i can do this', 'i did it', 'i finally did it', 'i finished it',
            'i got it done', 'i made it', 'stressed', 'overwhelmed', 'tired', 'exhausted',
            'sad', 'down', 'anxious', 'lonely', 'burnt out', 'burned out',
        ))
    )
    if not needs_agenda:
        return {}
    agenda = build_today_agenda()
    return {
        'open_count': int(agenda.get('open_count') or 0),
        'completed_count': int(agenda.get('completed_count') or 0),
    }


def _telegram_social_reply(raw: str) -> str | None:
    """Route social messages and app-aware questions without guessing facts."""
    low = re.sub(r'\s+', ' ', str(raw or '').strip().lower()).rstrip('.!?')
    knowledge = _mikasa_app_knowledge_reply(raw)
    if knowledge is not None:
        return knowledge
    countdown = _mikasa_event_countdown_reply(raw)
    if countdown is not None:
        return countdown
    if low in ('what can you do', 'what do you do', 'how can you help', 'how can you help me',
               'what can you help with', 'capabilities'):
        return _telegram_capabilities_message()
    focus = _mikasa_app_status_focus(raw)
    if focus or _is_mikasa_status_request(raw):
        context = _mikasa_app_status_context()
        return mikasa_persona.app_status_reply(context, focus=focus)
    if _is_mikasa_next_step_request(raw):
        return mikasa_persona.today_agenda_reply(build_today_agenda())
    return mikasa_persona.social_reply(raw, _mikasa_context_for_message(raw))


def _telegram_persona_prefix(raw: str, reply: str) -> str:
    return mikasa_persona.command_prefix(raw, reply)


async def telegram_command(text: str) -> str:
    """Mikasa voice and app-aware shortcuts around XROB's validated command engine."""
    raw = str(text or '').strip()
    management = _telegram_manage_request(raw)
    if management is not None:
        return mikasa_persona.command_reply(raw, management)
    social = _telegram_social_reply(raw)
    if social is not None:
        return social
    reply = await _telegram_command_core(raw)
    if reply.startswith(MIKASA_UNKNOWN_REPLY_PREFIX):
        learned = _mikasa_learned_reply(raw)
        if learned:
            return learned
        _record_mikasa_unhandled_message(raw)
        return mikasa_persona.fallback_reply(raw)
    return mikasa_persona.command_reply(raw, reply)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8090)

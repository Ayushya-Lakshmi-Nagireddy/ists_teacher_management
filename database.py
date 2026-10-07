import json
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
INSTANCE = os.path.join(BASE, "instance")
DB_PATH = os.environ.get("TEACHERS_DB") or os.path.join(INSTANCE, "teachers.db")

DEFAULT_GRID = [
    {"no": 1, "start": "09:30", "end": "10:20"},
    {"no": 2, "start": "10:20", "end": "11:10"},
    {"no": 3, "start": "11:20", "end": "12:10"},
    {"no": 4, "start": "12:10", "end": "13:00"},
    {"no": 5, "start": "14:00", "end": "14:50"},
    {"no": 6, "start": "14:50", "end": "15:40"},
    {"no": 7, "start": "15:40", "end": "16:30"},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('hod', 'teacher')),
    full_name TEXT NOT NULL,
    teacher_id INTEGER UNIQUE,
    owner_id INTEGER
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS department_years (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    year_no INTEGER NOT NULL,
    class_count INTEGER NOT NULL,
    periods_per_day INTEGER NOT NULL DEFAULT 7,
    UNIQUE (department_id, year_no)
);

CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    department_id INTEGER REFERENCES departments(id) ON DELETE RESTRICT,
    available_days TEXT NOT NULL DEFAULT '0,1,2,3,4,5'
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    year_no INTEGER NOT NULL,
    section TEXT NOT NULL,
    subject TEXT NOT NULL,
    sem_periods INTEGER NOT NULL DEFAULT 60,
    periods_per_week INTEGER NOT NULL,
    block INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS timetable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    year_no INTEGER NOT NULL,
    section TEXT NOT NULL,
    day INTEGER NOT NULL,
    period_no INTEGER NOT NULL,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    UNIQUE (department_id, year_no, section, day, period_no)
);
CREATE INDEX IF NOT EXISTS idx_timetable_day ON timetable(day, period_no);

CREATE TABLE IF NOT EXISTS absences (
    date TEXT NOT NULL,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    PRIMARY KEY (date, teacher_id)
);

CREATE TABLE IF NOT EXISTS substitutions (
    date TEXT NOT NULL,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    year_no INTEGER NOT NULL,
    section TEXT NOT NULL,
    period_no INTEGER NOT NULL,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    PRIMARY KEY (date, department_id, year_no, section, period_no)
);

CREATE TABLE IF NOT EXISTS settings (
    owner_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (owner_id, key)
);
"""

DEFAULTS = {
    "working_days": "0,1,2,3,4,5",
    "grid": json.dumps(DEFAULT_GRID),
    "stale": "0",
    "weeks_per_sem": "15",
}


def get_db():
    os.makedirs(INSTANCE, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def create_tables():
    if os.path.exists(DB_PATH):
        old = sqlite3.connect(DB_PATH)
        cols = [r[1] for r in old.execute("PRAGMA table_info(departments)")]
        old.close()
        if cols and "owner_id" not in cols:
            os.replace(DB_PATH, DB_PATH + ".old")
    db = get_db()
    db.executescript(SCHEMA)
    cols = [r["name"] for r in db.execute("PRAGMA table_info(assignments)")]
    if "yearly_periods" in cols:
        db.execute("ALTER TABLE assignments RENAME COLUMN yearly_periods TO sem_periods")
    db.execute("UPDATE OR IGNORE settings SET key = 'weeks_per_sem' WHERE key = 'weeks_per_year'")
    db.commit()
    db.close()


def get_setting(db, key, ws):
    row = db.execute("SELECT value FROM settings WHERE owner_id = ? AND key = ?", (ws, key)).fetchone()
    return row["value"] if row else DEFAULTS.get(key)


def set_setting(db, key, value, ws):
    db.execute("INSERT INTO settings (owner_id, key, value) VALUES (?, ?, ?) "
               "ON CONFLICT(owner_id, key) DO UPDATE SET value = excluded.value", (ws, key, str(value)))

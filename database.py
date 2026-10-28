"""SQLite қосылымы және кестелерді автоматты құру."""
import os
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id               INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    language              TEXT    NOT NULL DEFAULT 'kk',
    timezone              TEXT    NOT NULL DEFAULT 'Asia/Almaty',
    browser_notifications INTEGER NOT NULL DEFAULT 1,
    default_reminder      TEXT    NOT NULL DEFAULT '30m',
    date_format           TEXT    NOT NULL DEFAULT 'long',
    telegram_chat_id      TEXT,
    telegram_name         TEXT,
    telegram_enabled      INTEGER NOT NULL DEFAULT 1,
    telegram_linked_at    TEXT,
    telegram_link_token   TEXT,
    telegram_link_expires TEXT
);

-- event_date/event_time — пайдаланушының жергілікті уақыты,
-- event_at_utc және reminder_datetime — UTC ('YYYY-MM-DD HH:MM:SS').
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    category          TEXT    NOT NULL DEFAULT 'other',
    event_date        TEXT    NOT NULL,
    event_time        TEXT    NOT NULL,
    event_at_utc      TEXT    NOT NULL,
    priority          TEXT    NOT NULL DEFAULT 'medium',
    reminder_enabled  INTEGER NOT NULL DEFAULT 0,
    reminder_mode     TEXT    NOT NULL DEFAULT 'none',
    reminder_datetime TEXT,
    reminder_sent     INTEGER NOT NULL DEFAULT 0,
    is_completed      INTEGER NOT NULL DEFAULT 0,
    recurrence        TEXT    NOT NULL DEFAULT 'none',  -- none/daily/weekly/monthly/yearly
    recurrence_start  TEXT,                             -- сериясының алғашқы күні
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user_date ON events(user_id, event_date, event_time);
CREATE INDEX IF NOT EXISTS idx_events_due ON events(reminder_sent, reminder_enabled, reminder_datetime);

-- Бір оқиғаның бірнеше еске салуы. events.reminder_* бағандары — осы кестенің қысқаша
-- жиынтығы (келесі еске салу уақыты), оны models.refresh_reminder_summary() жаңартады.
-- kind: 'offset' — оқиғадан N минут бұрын, 'custom' — өз уақыты, 'snooze' — кейінге қалдырылған.
CREATE TABLE IF NOT EXISTS reminders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL DEFAULT 'offset',
    offset_minutes INTEGER,
    remind_at      TEXT    NOT NULL,
    sent           INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_event ON reminders(event_id);

-- status: 'sent' — уақытында, 'late' — сервер тоқтағандықтан кешіктірілген,
--         'missed' — оқиға өтіп кеткендіктен жіберілмеген.
CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL,
    event_title   TEXT    NOT NULL,
    message       TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'sent',
    scheduled_for TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    delivered     INTEGER NOT NULL DEFAULT 0,
    telegram_sent     INTEGER NOT NULL DEFAULT 0,  -- 0 — жіберілмеген, 1 — жіберілді/өңделуде
    telegram_attempts INTEGER NOT NULL DEFAULT 0,
    push_sent         INTEGER NOT NULL DEFAULT 0,  -- Web Push арқылы жіберілді ме
    push_attempts     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(event_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, delivered);

-- Web Push жазылымдары: бір пайдаланушының бірнеше құрылғысы болуы мүмкін.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT    NOT NULL UNIQUE,
    p256dh     TEXT    NOT NULL,
    auth       TEXT    NOT NULL,
    user_agent TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);
"""

# Ескі базаларға жетіспейтін бағандарды қосу (деректер сақталады).
MIGRATIONS = {
    "user_settings": [
        ("telegram_chat_id", "TEXT"),
        ("telegram_name", "TEXT"),
        ("telegram_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("telegram_linked_at", "TEXT"),
        ("telegram_link_token", "TEXT"),
        ("telegram_link_expires", "TEXT"),
    ],
    "notifications": [
        ("telegram_sent", "INTEGER NOT NULL DEFAULT 0"),
        ("telegram_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("push_sent", "INTEGER NOT NULL DEFAULT 0"),
        ("push_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "events": [
        ("recurrence", "TEXT NOT NULL DEFAULT 'none'"),
        ("recurrence_start", "TEXT"),
    ],
}

# Ескі нұсқадағы (бір еске салу events кестесінде) деректерді reminders кестесіне көшіру.
LEGACY_REMINDERS_SQL = """
INSERT INTO reminders (event_id, user_id, kind, offset_minutes, remind_at, sent, created_at)
SELECT e.id, e.user_id,
       CASE WHEN e.reminder_mode = 'custom' THEN 'custom' ELSE 'offset' END,
       CAST(ROUND((julianday(e.event_at_utc) - julianday(e.reminder_datetime)) * 1440) AS INTEGER),
       e.reminder_datetime, e.reminder_sent, e.created_at
FROM events e
WHERE e.reminder_datetime IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.event_id = e.id)
"""


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_settings_tg_token ON user_settings(telegram_link_token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_settings_tg_chat ON user_settings(telegram_chat_id)")
    conn.execute(LEGACY_REMINDERS_SQL)


def connect(db_path):
    """Жаңа қосылым ашады (сұраныстан тыс жерде, мысалы жоспарлаушыда қолданылады)."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SQLite-тың lower()/LIKE тек ASCII әріптерін ескереді; кириллица мен қазақ әріптері үшін
    # Python-ның str.lower() функциясын тіркейміз.
    conn.create_function("py_lower", 1, lambda s: s.lower() if isinstance(s, str) else s,
                         deterministic=True)
    return conn


def get_db():
    """Ағымдағы HTTP сұранысқа арналған қосылым (g объектісінде сақталады)."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db_path):
    """База файлы мен кестелерді жоқ болса жасайды."""
    folder = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(folder, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def init_app(app):
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

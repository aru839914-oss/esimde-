"""Деректерге қол жеткізу қабаты.

Оқиғаларға қатысты барлық сұраныстар user_id бойынша сүзіледі — пайдаланушы
тек өз деректерін көреді және өзгертеді. Барлық SQL параметрленген.
"""
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from utils import (CATEGORIES, MONTHS_GENITIVE, PRIORITIES, RECURRENCE, REMINDER_MODES,
                   compute_reminder_utc, format_date, humanize_delta, local_now, local_to_utc,
                   next_occurrence, parse_utc_str, to_utc_str, utc_now, utc_to_local)

PAGE_SIZE = 12


def _now_str():
    return to_utc_str(utc_now())


# ===================== Пайдаланушылар =====================
def create_user(db, name, email, password):
    cur = db.execute(
        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (name, email.lower(), generate_password_hash(password), _now_str()),
    )
    db.execute("INSERT INTO user_settings (user_id) VALUES (?)", (cur.lastrowid,))
    db.commit()
    return cur.lastrowid


def get_user(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(db, email):
    return db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()


def verify_user(db, email, password):
    """Email мен құпиясөз дұрыс болса пайдаланушыны қайтарады, әйтпесе None."""
    user = get_user_by_email(db, email)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def get_settings(db, user_id):
    row = db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
        db.commit()
        row = db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row)


def update_settings(db, user_id, values):
    db.execute(
        """UPDATE user_settings
           SET timezone = ?, browser_notifications = ?, default_reminder = ?, date_format = ?,
               telegram_enabled = ?
           WHERE user_id = ?""",
        (values["timezone"], values["browser_notifications"], values["default_reminder"],
         values["date_format"], values["telegram_enabled"], user_id),
    )
    db.commit()


# ===================== Еске салулар (бір оқиғаға бірнеше) =====================
def modes_of(data_or_row):
    """Сақталған reminder_mode жолынан ('30m,1d') режимдер тізімі."""
    value = data_or_row["reminder_mode"] or "none"
    return [m for m in value.split(",") if m and m != "none" and m in REMINDER_MODES]


def desired_reminders(event_utc, modes, custom_local=None, tz_name=None):
    """Таңдалған режимдерден еске салулар: [(kind, offset_minutes, remind_at_utc)], уақыт бойынша."""
    wanted = {}
    for mode in modes:
        if mode == "custom":
            at = compute_reminder_utc(event_utc, "custom", custom_local, tz_name)
            if at:
                wanted[at] = ("custom", int(round((event_utc - at).total_seconds() / 60)))
        else:
            at = compute_reminder_utc(event_utc, mode)
            if at:
                wanted.setdefault(at, ("offset", REMINDER_MODES[mode]["minutes"]))
    return [(kind, offset, at) for at, (kind, offset) in sorted(wanted.items())]


def refresh_reminder_summary(db, event_id):
    """events.reminder_* бағандарын reminders кестесінен жаңартады (келесі еске салу)."""
    rows = db.execute("SELECT remind_at, sent FROM reminders WHERE event_id = ? ORDER BY remind_at",
                      (event_id,)).fetchall()
    pending = [r["remind_at"] for r in rows if not r["sent"]]
    if not rows:
        enabled, at, sent = 0, None, 0
    elif pending:
        enabled, at, sent = 1, pending[0], 0
    else:
        enabled, at, sent = 1, rows[-1]["remind_at"], 1
    db.execute("UPDATE events SET reminder_enabled = ?, reminder_datetime = ?, reminder_sent = ? WHERE id = ?",
               (enabled, at, sent, event_id))


def sync_reminders(db, event_id, user_id, desired):
    """Еске салуларды жаңартады. Уақыты өзгермегендері жіберілу күйін сақтайды (қайта жіберілмейді)."""
    wanted = {to_utc_str(at): (kind, offset) for kind, offset, at in desired}
    existing = db.execute("SELECT id, remind_at FROM reminders WHERE event_id = ? AND kind != 'snooze'",
                          (event_id,)).fetchall()
    for r in existing:
        if r["remind_at"] in wanted:
            kind, offset = wanted.pop(r["remind_at"])
            db.execute("UPDATE reminders SET kind = ?, offset_minutes = ? WHERE id = ?", (kind, offset, r["id"]))
        else:
            db.execute("DELETE FROM reminders WHERE id = ?", (r["id"],))
    now = _now_str()
    for at, (kind, offset) in wanted.items():
        db.execute("""INSERT INTO reminders (event_id, user_id, kind, offset_minutes, remind_at, sent, created_at)
                      VALUES (?, ?, ?, ?, ?, 0, ?)""", (event_id, user_id, kind, offset, at, now))
    refresh_reminder_summary(db, event_id)


def event_reminders(db, event_id):
    return db.execute("SELECT * FROM reminders WHERE event_id = ? ORDER BY remind_at", (event_id,)).fetchall()


def snooze_event(db, user_id, event_id, minutes):
    """«Кейінге қалдыру»: N минуттан кейін қосымша еске салу. Қайтарады: еске салу уақыты (UTC) немесе None."""
    row = get_event(db, user_id, event_id)
    if row is None or row["is_completed"]:
        return None
    at = utc_now().replace(second=0, microsecond=0) + timedelta(minutes=minutes)
    db.execute("""INSERT INTO reminders (event_id, user_id, kind, offset_minutes, remind_at, sent, created_at)
                  VALUES (?, ?, 'snooze', NULL, ?, 0, ?)""", (event_id, user_id, to_utc_str(at), _now_str()))
    refresh_reminder_summary(db, event_id)
    db.commit()
    return at


# ===================== Оқиғалар =====================
def create_event(db, user_id, data, tz_name):
    event_utc = local_to_utc(data["event_date"], data["event_time"], tz_name)
    recurrence = data.get("recurrence", "none")
    now = _now_str()
    cur = db.execute(
        """INSERT INTO events (user_id, title, description, category, event_date, event_time,
                               event_at_utc, priority, reminder_enabled, reminder_mode,
                               reminder_datetime, reminder_sent, is_completed, recurrence,
                               recurrence_start, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 0, 0, ?, ?, ?, ?)""",
        (user_id, data["title"], data["description"], data["category"], data["event_date"],
         data["event_time"], to_utc_str(event_utc), data["priority"], data["reminder_mode"],
         recurrence, data["event_date"] if recurrence != "none" else None, now, now),
    )
    event_id = cur.lastrowid
    sync_reminders(db, event_id, user_id,
                   desired_reminders(event_utc, data["reminders"], data.get("reminder_custom"), tz_name))
    db.commit()
    return event_id


def update_event(db, user_id, event_id, data, tz_name):
    """Оқиғаны жаңартады. Уақыты өзгерген еске салулар қайта жоспарланады."""
    old = get_event(db, user_id, event_id)
    if old is None:
        return False
    event_utc = local_to_utc(data["event_date"], data["event_time"], tz_name)
    recurrence = data.get("recurrence", "none")
    db.execute(
        """UPDATE events SET title = ?, description = ?, category = ?, event_date = ?, event_time = ?,
                  event_at_utc = ?, priority = ?, reminder_mode = ?, recurrence = ?, recurrence_start = ?,
                  updated_at = ?
           WHERE id = ? AND user_id = ?""",
        (data["title"], data["description"], data["category"], data["event_date"], data["event_time"],
         to_utc_str(event_utc), data["priority"], data["reminder_mode"], recurrence,
         data["event_date"] if recurrence != "none" else None, _now_str(), event_id, user_id),
    )
    if to_utc_str(event_utc) != old["event_at_utc"]:
        # Оқиға уақыты өзгерсе, кейінге қалдырылған (snooze) еске салулар өзектілігін жоғалтады
        db.execute("DELETE FROM reminders WHERE event_id = ? AND kind = 'snooze' AND sent = 0", (event_id,))
    sync_reminders(db, event_id, user_id,
                   desired_reminders(event_utc, data["reminders"], data.get("reminder_custom"), tz_name))
    db.commit()
    return True


def advance_occurrence(db, row, tz_name, now=None):
    """Қайталанатын оқиғаны келесі күніне ауыстырады және еске салуларды қайта құрады.

    Қайтарады: жаңа оқиға уақыты (жергілікті datetime).
    """
    now = now or utc_now()
    start = datetime.strptime(row["recurrence_start"] or row["event_date"], "%Y-%m-%d").date()
    current = datetime.strptime(row["event_date"], "%Y-%m-%d").date()
    nxt = next_occurrence(start, row["recurrence"], current)
    while local_to_utc(nxt.isoformat(), row["event_time"], tz_name) <= now:  # өткен қайталануларды өткізіп жібереміз
        nxt = next_occurrence(start, row["recurrence"], nxt)
    event_utc = local_to_utc(nxt.isoformat(), row["event_time"], tz_name)
    db.execute("""UPDATE events SET event_date = ?, event_at_utc = ?, is_completed = 0, updated_at = ?
                  WHERE id = ?""", (nxt.isoformat(), to_utc_str(event_utc), _now_str(), row["id"]))
    db.execute("DELETE FROM reminders WHERE event_id = ? AND kind = 'snooze'", (row["id"],))
    for r in db.execute("SELECT id, offset_minutes FROM reminders WHERE event_id = ?", (row["id"],)).fetchall():
        at = event_utc - timedelta(minutes=r["offset_minutes"] or 0)
        # Жаңа қайталанудың уақыты өтіп кеткен еске салуы жіберілмейді
        db.execute("UPDATE reminders SET remind_at = ?, sent = ? WHERE id = ?",
                   (to_utc_str(at), 1 if at <= now else 0, r["id"]))
    refresh_reminder_summary(db, row["id"])
    db.commit()
    return utc_to_local(event_utc, tz_name)


def advance_due_recurring(db, now=None, grace=timedelta(hours=1)):
    """Уақыты өткен (grace-тен асқан) қайталанатын оқиғаларды келесі күнге ауыстырады."""
    now = now or utc_now()
    rows = db.execute(
        """SELECT e.*, COALESCE(s.timezone, 'Asia/Almaty') AS tz FROM events e
           LEFT JOIN user_settings s ON s.user_id = e.user_id
           WHERE e.recurrence != 'none' AND e.event_at_utc < ?""",
        (to_utc_str(now - grace),),
    ).fetchall()
    for row in rows:
        advance_occurrence(db, row, row["tz"], now)
    return len(rows)


def delete_event(db, user_id, event_id):
    cur = db.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user_id))
    db.commit()
    return cur.rowcount > 0


def get_event(db, user_id, event_id):
    return db.execute("SELECT * FROM events WHERE id = ? AND user_id = ?",
                      (event_id, user_id)).fetchone()


def set_completed(db, user_id, event_id, completed, tz_name="Asia/Almaty"):
    """Орындалу күйін өзгертеді.

    Қайталанатын оқиға «орындалды» деп белгіленсе, ол келесі күніне ауысады.
    Қайтарады: None (табылмады), "completed", "reopened" немесе ("advanced", жаңа уақыт).
    """
    row = get_event(db, user_id, event_id)
    if row is None:
        return None
    if completed and row["recurrence"] != "none":
        return ("advanced", advance_occurrence(db, row, tz_name))
    db.execute("UPDATE events SET is_completed = ?, updated_at = ? WHERE id = ? AND user_id = ?",
               (1 if completed else 0, _now_str(), event_id, user_id))
    db.commit()
    return "completed" if completed else "reopened"


def list_events(db, user_id, q="", category="", priority="", status="", sort="date_asc", page=1):
    """Сүзгілер, іздеу, сұрыптау және беттеу. Қайтарады: (жолдар, жалпы саны)."""
    where = ["user_id = ?"]
    params = [user_id]
    if q:
        q = q.lower()
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(py_lower(title) LIKE ? ESCAPE '\\' OR py_lower(description) LIKE ? ESCAPE '\\')")
        params += [like, like]
    if category in CATEGORIES:
        where.append("category = ?")
        params.append(category)
    if priority in PRIORITIES:
        where.append("priority = ?")
        params.append(priority)
    now = _now_str()
    if status == "active":
        where.append("is_completed = 0")
    elif status == "completed":
        where.append("is_completed = 1")
    elif status == "overdue":
        where.append("is_completed = 0 AND event_at_utc < ?")
        params.append(now)

    order = {
        "date_asc": "event_at_utc ASC",
        "date_desc": "event_at_utc DESC",
        "created_desc": "created_at DESC, id DESC",
        "priority": "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, event_at_utc ASC",
    }.get(sort, "event_at_utc ASC")

    where_sql = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM events WHERE {where_sql}", params).fetchone()[0]
    offset = (max(page, 1) - 1) * PAGE_SIZE
    rows = db.execute(f"SELECT * FROM events WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
                      params + [PAGE_SIZE, offset]).fetchall()
    return rows, total


def upcoming_events(db, user_id, limit=6):
    return db.execute(
        """SELECT * FROM events WHERE user_id = ? AND is_completed = 0 AND event_at_utc >= ?
           ORDER BY event_at_utc ASC LIMIT ?""",
        (user_id, _now_str(), limit),
    ).fetchall()


def events_in_range(db, user_id, start_date, end_date):
    """Күнтізбе үшін: event_date [start_date, end_date) аралығындағы оқиғалар,
    сондай-ақ осы аралыққа дейін басталған қайталанатын оқиғалар."""
    return db.execute(
        """SELECT * FROM events WHERE user_id = ?
             AND ((event_date >= ? AND event_date < ?) OR (recurrence != 'none' AND event_date < ?))
           ORDER BY event_date, event_time""",
        (user_id, start_date, end_date, end_date),
    ).fetchall()


def get_stats(db, user_id, tz_name):
    """Басты беттегі статистика — тек деректер базасынан есептеледі."""
    now = utc_now()
    today = local_now(tz_name).date().isoformat()
    row = db.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN event_date = ? THEN 1 ELSE 0 END) AS today,
                  SUM(CASE WHEN is_completed = 0 AND event_at_utc >= ? AND event_at_utc <= ?
                      THEN 1 ELSE 0 END) AS week,
                  SUM(CASE WHEN is_completed = 1 THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN is_completed = 0 AND event_at_utc >= ? THEN 1 ELSE 0 END) AS upcoming,
                  SUM(CASE WHEN is_completed = 0 AND event_at_utc < ? THEN 1 ELSE 0 END) AS overdue,
                  SUM(CASE WHEN event_date = ? AND is_completed = 1 THEN 1 ELSE 0 END) AS today_done
           FROM events WHERE user_id = ?""",
        (today, to_utc_str(now), to_utc_str(now + timedelta(days=7)), to_utc_str(now),
         to_utc_str(now), today, user_id),
    ).fetchone()
    stats = {k: (row[k] or 0) for k in row.keys()}
    stats["completed_percent"] = round(stats["completed"] * 100 / stats["total"]) if stats["total"] else 0
    return stats


def nav_counts(db, user_id):
    """Sidebar-дағы сандар: орындалмаған оқиғалар, күтудегі еске салулар, санаттар."""
    active = db.execute("SELECT COUNT(*) FROM events WHERE user_id = ? AND is_completed = 0",
                        (user_id,)).fetchone()[0]
    reminders = db.execute(
        """SELECT COUNT(*) FROM reminders r JOIN events e ON e.id = r.event_id
           WHERE r.user_id = ? AND r.sent = 0 AND e.is_completed = 0""",
        (user_id,),
    ).fetchone()[0]
    return {"active": active, "reminders": reminders, "categories": category_counts(db, user_id)}


def category_counts(db, user_id):
    rows = db.execute(
        """SELECT category, COUNT(*) AS total, SUM(is_completed) AS done
           FROM events WHERE user_id = ? GROUP BY category""",
        (user_id,),
    ).fetchall()
    return {r["category"]: {"total": r["total"], "done": r["done"] or 0} for r in rows}


# ===================== Еске салулар мен хабарландырулар =====================
def reminder_events(db, user_id):
    """Барлық еске салулар (әрқайсысы — жеке жол), оқиға мәліметтерімен, уақыт бойынша."""
    return db.execute(
        """SELECT e.id, e.user_id, e.title, e.description, e.category, e.event_date, e.event_time,
                  e.event_at_utc, e.priority, e.reminder_enabled, e.reminder_mode, e.is_completed,
                  e.recurrence, e.recurrence_start, e.created_at, e.updated_at,
                  r.id AS reminder_id, r.kind AS reminder_kind,
                  r.remind_at AS reminder_datetime, r.sent AS reminder_sent,
                  n.status AS notification_status, n.created_at AS notified_at
           FROM reminders r
           JOIN events e ON e.id = r.event_id
           LEFT JOIN notifications n ON n.event_id = e.id AND n.scheduled_for = r.remind_at
           WHERE r.user_id = ?
           ORDER BY r.remind_at ASC""",
        (user_id,),
    ).fetchall()


def list_notifications(db, user_id, limit=50):
    return db.execute(
        """SELECT * FROM notifications WHERE user_id = ? AND status IN ('sent', 'late')
           ORDER BY created_at DESC, id DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()


def pending_notifications(db, user_id):
    """Браузерге әлі көрсетілмеген хабарландырулар."""
    return db.execute(
        """SELECT * FROM notifications WHERE user_id = ? AND delivered = 0 AND status IN ('sent', 'late')
           ORDER BY created_at ASC""",
        (user_id,),
    ).fetchall()


def ack_notifications(db, user_id, ids):
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = db.execute(
        f"UPDATE notifications SET delivered = 1 WHERE user_id = ? AND id IN ({placeholders})",
        [user_id] + list(ids),
    )
    db.commit()
    return cur.rowcount


# ===================== Көрсетуге дайындау =====================
def enrich_event(row, tz_name, date_fmt="long"):
    """Базадағы жолды шаблон мен JSON үшін ыңғайлы сөздікке айналдырады."""
    ev = dict(row)
    event_utc = parse_utc_str(ev["event_at_utc"])
    event_local = utc_to_local(event_utc, tz_name)
    now_local = local_now(tz_name)
    ev["category_meta"] = CATEGORIES.get(ev["category"], CATEGORIES["other"])
    ev["priority_meta"] = PRIORITIES.get(ev["priority"], PRIORITIES["medium"])
    modes = modes_of(ev)
    ev["reminder_modes"] = modes
    ev["reminder_shorts"] = [REMINDER_MODES[m]["short"] for m in modes]
    parts = []
    offsets = [REMINDER_MODES[m]["short"] for m in modes if m != "custom"]
    if offsets:
        parts.append(", ".join(offsets) + " бұрын")
    if "custom" in modes:
        parts.append("өз уақыты")
    ev["reminder_label"] = "; ".join(parts).capitalize() if parts else "Еске салусыз"
    ev["recurrence"] = ev.get("recurrence") or "none"
    ev["recurrence_label"] = RECURRENCE.get(ev["recurrence"], RECURRENCE["none"])["short"]
    ev["date_display"] = format_date(event_local.date(), date_fmt)
    ev["date_short"] = f"{event_local.day} {MONTHS_GENITIVE[event_local.month - 1]}"
    ev["day_num"] = event_local.day
    ev["month_name"] = MONTHS_GENITIVE[event_local.month - 1]
    ev["is_past"] = event_utc < utc_now()
    ev["is_overdue"] = ev["is_past"] and not ev["is_completed"]
    ev["relative"] = "Орындалды" if ev["is_completed"] else humanize_delta(event_local, now_local)
    if ev["reminder_datetime"]:
        rem_local = utc_to_local(parse_utc_str(ev["reminder_datetime"]), tz_name)
        ev["reminder_local"] = rem_local
        ev["reminder_display"] = f"{format_date(rem_local.date(), date_fmt)}, {rem_local:%H:%M}"
        ev["reminder_input"] = rem_local.strftime("%Y-%m-%dT%H:%M")
    else:
        ev["reminder_local"] = None
        ev["reminder_display"] = None
        ev["reminder_input"] = ""
    return ev


def event_to_json(row, tz_name, date_fmt="long"):
    ev = enrich_event(row, tz_name, date_fmt)
    return {
        "id": ev["id"],
        "title": ev["title"],
        "description": ev["description"],
        "category": ev["category"],
        "category_label": ev["category_meta"]["label"],
        "category_color": ev["category_meta"]["color"],
        "category_icon": ev["category_meta"]["icon"],
        "priority": ev["priority"],
        "priority_label": ev["priority_meta"]["label"],
        "event_date": ev["event_date"],
        "event_time": ev["event_time"],
        "date_display": ev["date_display"],
        "relative": ev["relative"],
        "reminder_label": ev["reminder_label"],
        "reminder_display": ev["reminder_display"],
        "recurrence": ev["recurrence"],
        "recurrence_label": ev["recurrence_label"],
        "reminder_sent": bool(ev["reminder_sent"]),
        "is_completed": bool(ev["is_completed"]),
        "is_overdue": ev["is_overdue"],
    }


def parse_iso_date(value):
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

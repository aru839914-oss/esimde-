"""Еске салу механизмі.

Алгоритм:
1. Оқиға сақталғанда оның әр еске салуының уақыты (UTC) есептеліп, reminders кестесіне жазылады
   (бір оқиғаға бірнеше еске салу; «кейінге қалдыру» да жаңа жол ретінде қосылады).
2. APScheduler әр REMINDER_CHECK_SECONDS секунд сайын process_due_reminders() шақырады.
3. Функция уақыты келген, әлі жіберілмеген, оқиғасы орындалмаған еске салуларды табады.
4. Әр еске салу атомарлы түрде "иеленеді":
       UPDATE reminders SET sent = 1 WHERE id = ? AND sent = 0
   Егер rowcount = 0 болса, оны басқа процесс/ағын өңдеп қойған — қайталау болмайды.
5. Сол транзакцияда notifications кестесіне жазба қосылады (UNIQUE(event_id, scheduled_for)).
6. Браузер /api/notifications/pending арқылы жаңа хабарландыруларды алып, көрсетеді.
7. Қайталанатын оқиғалар өткен соң advance_recurring() оларды келесі күніне ауыстырады.

Жоспар жадта емес, базада сақталатындықтан, сервер қайта іске қосылғанда
барлық еске салулар өздігінен "қалпына келеді".

Сервер тоқтап тұрған кезде өткізіліп алынған еске салулар:
  * оқиға әлі басталмаса — бірден жіберіледі, күйі 'late' (кешіктірілген);
  * оқиға өтіп кетсе — жіберілмейді, күйі 'missed' (өткізіліп алынды).
"""
import atexit
import logging
from datetime import timedelta

from database import connect
from models import advance_due_recurring, refresh_reminder_summary
from utils import (describe_lead_time, local_now, parse_utc_str, to_utc_str, utc_now,
                   utc_to_local)

log = logging.getLogger(__name__)

# Еске салу уақытынан осы аралықтан кеш өңделсе — "кешіктірілген" деп белгіленеді.
LATE_GRACE = timedelta(minutes=2)
# Кейінге қалдырылған еске салу оқиға басталғаннан кейін де осы уақыт ішінде жіберіледі.
SNOOZE_GRACE = timedelta(hours=12)

_scheduler = None


def build_message(title, event_utc, now_utc, tz_name):
    """Қазақ тіліндегі хабарландыру мәтіні."""
    event_local = utc_to_local(event_utc, tz_name)
    now_local = utc_to_local(now_utc, tz_name)
    days_diff = (event_local.date() - now_local.date()).days
    remaining = event_utc - now_utc
    if days_diff == 1 and remaining >= timedelta(hours=12):
        return f"Еске салу: Ертең «{title}» оқиғасы бар ({event_local:%H:%M})."
    lead = describe_lead_time(event_utc, now_utc)  # минутқа дейін дөңгелектенеді
    if lead is None:
        return f"Еске салу: «{title}» оқиғасы қазір басталады!"
    return f"Еске салу: «{title}» оқиғасына {lead} қалды!"


def process_due_reminders(db_path, now=None):
    """Уақыты келген еске салуларды өңдейді. Нәтиже: {'sent': n, 'late': n, 'missed': n}."""
    now = now or utc_now()
    now_str = to_utc_str(now)
    result = {"sent": 0, "late": 0, "missed": 0}
    conn = connect(db_path)
    try:
        due = conn.execute(
            """SELECT r.id AS reminder_id, r.kind, r.remind_at,
                      e.id, e.user_id, e.title, e.event_at_utc,
                      COALESCE(s.timezone, 'Asia/Almaty') AS timezone
               FROM reminders r
               JOIN events e ON e.id = r.event_id
               LEFT JOIN user_settings s ON s.user_id = e.user_id
               WHERE r.sent = 0 AND e.is_completed = 0 AND r.remind_at <= ?
               ORDER BY r.remind_at""",
            (now_str,),
        ).fetchall()

        for row in due:
            # Атомарлы "иелену": басқа процесс/ағын өңдеп қойса, rowcount = 0
            claimed = conn.execute("UPDATE reminders SET sent = 1 WHERE id = ? AND sent = 0",
                                   (row["reminder_id"],)).rowcount
            if claimed == 0:
                conn.rollback()
                continue

            event_utc = parse_utc_str(row["event_at_utc"])
            reminder_utc = parse_utc_str(row["remind_at"])
            snooze_alive = row["kind"] == "snooze" and now - event_utc <= SNOOZE_GRACE
            if event_utc <= now and not snooze_alive:
                status, delivered = "missed", 1
                message = (f"«{row['title']}» оқиғасының еске салуы өткізіліп алынды: "
                           f"сервер сол уақытта жұмыс істемеді.")
            else:
                status = "late" if now - reminder_utc > LATE_GRACE else "sent"
                delivered = 0
                if event_utc <= now:  # кейінге қалдырылған, бірақ оқиға басталып кеткен
                    started = utc_to_local(event_utc, row["timezone"])
                    message = f"Еске салу: «{row['title']}» оқиғасы {started:%H:%M}-де басталды."
                else:
                    message = build_message(row["title"], event_utc, now, row["timezone"])

            conn.execute(
                """INSERT OR IGNORE INTO notifications
                       (user_id, event_id, event_title, message, status, scheduled_for, created_at, delivered)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["user_id"], row["id"], row["title"], message, status,
                 row["remind_at"], now_str, delivered),
            )
            refresh_reminder_summary(conn, row["id"])
            conn.commit()
            result[status] += 1
            log.info("Reminder %s for event %s (%s)", status, row["id"], row["title"])
    finally:
        conn.close()
    return result


def advance_recurring(db_path, now=None):
    """Өтіп кеткен қайталанатын оқиғаларды келесі күніне ауыстырады."""
    conn = connect(db_path)
    try:
        return advance_due_recurring(conn, now)
    finally:
        conn.close()


def start_scheduler(app):
    """APScheduler-ді іске қосады (бір процесте бір рет қана)."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    db_path = app.config["DATABASE"]
    interval = app.config.get("REMINDER_CHECK_SECONDS", 20)
    telegram = app.extensions.get("telegram")
    push = app.extensions.get("push")
    secret_key = app.config["SECRET_KEY"]

    def run_cycle():
        process_due_reminders(db_path)
        advance_recurring(db_path)
        if push is not None:
            from push_service import deliver_pending_push
            deliver_pending_push(db_path, push, secret_key)
        if telegram is not None:
            from telegram_bot import deliver_pending
            deliver_pending(db_path, telegram)

    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    # next_run_time=қазір: іске қосылғанда өткізіліп алынған еске салулар бірден өңделеді.
    scheduler.add_job(run_cycle, "interval", seconds=interval,
                      id="process_due_reminders", max_instances=1, coalesce=True,
                      next_run_time=utc_now())
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    _scheduler = scheduler
    log.info("Reminder scheduler started (every %s s)", interval)
    return scheduler


def reminder_buckets(rows, tz_name):
    """Еске салулар бетіне арналған топтар: бүгінгі, алдағы, өткен."""
    from models import enrich_event

    now = utc_now()
    today = local_now(tz_name).date()
    today_list, upcoming, past = [], [], []
    for row in rows:
        ev = enrich_event(row, tz_name)
        ev["notification_status"] = row["notification_status"]
        if row["reminder_sent"] or parse_utc_str(row["reminder_datetime"]) <= now:
            past.append(ev)
        elif ev["reminder_local"].date() == today:
            today_list.append(ev)
        else:
            upcoming.append(ev)
    past.reverse()  # ең соңғысы жоғарыда
    return today_list, upcoming, past

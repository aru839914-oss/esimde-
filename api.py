"""JSON API: күнтізбе, оқиға мәліметтері, орындалу күйі, хабарландырулар."""
from datetime import datetime, timedelta
from urllib.parse import quote

from flask import Blueprint, abort, current_app, g, jsonify, request

import models
import push_service
import telegram_bot
from auth import login_required
from database import get_db
from utils import (SNOOZE_OPTIONS, format_date, occurrences_between, parse_utc_str, utc_now,
                   utc_to_local)

bp = Blueprint("api", __name__, url_prefix="/api")


def _tz():
    return g.settings["timezone"]


@bp.route("/events")
@login_required
def events_feed():
    """FullCalendar форматындағы оқиғалар.

    Параметрлер: start, end — аралық; category — санат; priority — үтірмен бөлінген
    маңыздылықтар (мысалы, high,medium); completed=0 — орындалғандарды жасыру.
    """
    start = models.parse_iso_date(request.args.get("start", ""))
    end = models.parse_iso_date(request.args.get("end", ""))
    if start is None or end is None:
        today = utc_to_local(utc_now(), _tz()).date()
        start, end = today.replace(day=1), today.replace(day=1) + timedelta(days=42)
    category = request.args.get("category", "")
    priorities = {p for p in request.args.get("priority", "").split(",") if p}
    hide_completed = request.args.get("completed") == "0"
    rows = models.events_in_range(get_db(), g.user["id"], start.isoformat(), end.isoformat())
    feed = []
    for row in rows:
        if category and row["category"] != category:
            continue
        if priorities and row["priority"] not in priorities:
            continue
        if hide_completed and row["is_completed"]:
            continue
        data = models.event_to_json(row, _tz(), g.settings["date_format"])
        color = data["category_color"]
        event_date = datetime.strptime(row["event_date"], "%Y-%m-%d").date()
        if row["recurrence"] != "none":
            # Қайталанатын оқиға: ағымдағы күннен бастап аралыққа түсетін барлық қайталанулар
            series_start = datetime.strptime(row["recurrence_start"] or row["event_date"], "%Y-%m-%d").date()
            dates = occurrences_between(series_start, row["recurrence"], max(start, event_date), end)
        else:
            dates = [event_date]
        for d in dates:
            start_dt = datetime.strptime(f"{d.isoformat()} {row['event_time']}", "%Y-%m-%d %H:%M")
            end_dt = start_dt + timedelta(hours=1)  # ұзақтығы сақталмайды — күнтізбеде 1 сағат
            classes = ["ev-completed"] if data["is_completed"] else []
            if d != event_date:
                classes.append("ev-repeat")  # келешектегі қайталану
            feed.append({
                "id": f"{data['id']}:{d.isoformat()}",
                "title": data["title"],
                "start": start_dt.strftime("%Y-%m-%dT%H:%M:00"),
                "end": end_dt.strftime("%Y-%m-%dT%H:%M:00"),
                "allDay": False,
                "backgroundColor": color,
                "borderColor": color,
                "classNames": classes,
                "extendedProps": dict(data, time_range=f"{start_dt:%H:%M}–{end_dt:%H:%M}"),
            })
    return jsonify(feed)


@bp.route("/events/<int:event_id>")
@login_required
def event_detail(event_id):
    row = models.get_event(get_db(), g.user["id"], event_id)
    if row is None:
        abort(404)
    return jsonify(models.event_to_json(row, _tz(), g.settings["date_format"]))


@bp.route("/events/<int:event_id>/complete", methods=["POST"])
@login_required
def toggle_complete(event_id):
    """Орындалу күйін өзгертеді. JSON {"completed": true/false}; болмаса — ауыстырады."""
    db = get_db()
    row = models.get_event(db, g.user["id"], event_id)
    if row is None:
        abort(404)
    payload = request.get_json(silent=True) or {}
    completed = payload.get("completed")
    if not isinstance(completed, bool):
        completed = not row["is_completed"]
    result = models.set_completed(db, g.user["id"], event_id, completed, _tz())
    if isinstance(result, tuple):  # қайталанатын оқиға келесі күнге ауысты
        nxt = result[1]
        message = f"Орындалды! Келесі қайталану: {format_date(nxt.date(), g.settings['date_format'])}, {nxt:%H:%M}."
    else:
        message = "Оқиға орындалды деп белгіленді." if completed else "Оқиға қайта белсенді болды."
    row = models.get_event(db, g.user["id"], event_id)
    return jsonify(event=models.event_to_json(row, _tz(), g.settings["date_format"]),
                   stats=models.get_stats(db, g.user["id"], _tz()),
                   advanced=isinstance(result, tuple), message=message)


@bp.route("/events/<int:event_id>/snooze", methods=["POST"])
@login_required
def snooze(event_id):
    """«Кейінге қалдыру»: JSON {"minutes": 10 | 30 | 60}."""
    payload = request.get_json(silent=True) or {}
    minutes = payload.get("minutes")
    if minutes not in SNOOZE_OPTIONS:
        return jsonify(error="Кейінге қалдыру уақыты қате."), 400
    db = get_db()
    if models.get_event(db, g.user["id"], event_id) is None:
        abort(404)
    at = models.snooze_event(db, g.user["id"], event_id, minutes)
    if at is None:
        return jsonify(error="Орындалған оқиғаны кейінге қалдыру мүмкін емес."), 400
    local = utc_to_local(at, _tz())
    return jsonify(message=f"Жарайды! {SNOOZE_OPTIONS[minutes]}тан кейін ({local:%H:%M}) қайта еске саламын.",
                   remind_at=f"{local:%H:%M}")


@bp.route("/stats")
@login_required
def stats():
    return jsonify(models.get_stats(get_db(), g.user["id"], _tz()))


@bp.route("/reminders/upcoming")
@login_required
def upcoming_reminders():
    """Басты беттегі «Жақын еске салулар» виджеті үшін."""
    db = get_db()
    now = utc_now()
    items = []
    for row in models.reminder_events(db, g.user["id"]):
        if row["reminder_sent"] or row["is_completed"] or parse_utc_str(row["reminder_datetime"]) <= now:
            continue
        ev = models.enrich_event(row, _tz(), g.settings["date_format"])
        items.append({"id": ev["id"], "title": ev["title"], "when": ev["reminder_display"],
                      "color": ev["category_meta"]["color"], "icon": ev["category_meta"]["icon"]})
        if len(items) == 5:
            break
    recent = [{"id": n["id"], "event_id": n["event_id"], "message": n["message"],
               "status": n["status"]} for n in models.list_notifications(db, g.user["id"], limit=3)]
    return jsonify(upcoming=items, recent=recent)


@bp.route("/notifications/pending")
@login_required
def notifications_pending():
    rows = models.pending_notifications(get_db(), g.user["id"])
    return jsonify([{"id": r["id"], "event_id": r["event_id"], "title": r["event_title"],
                     "message": r["message"], "status": r["status"]} for r in rows])

def _push():
    return current_app.extensions.get("push")


@bp.route("/push/key")
@login_required
def push_key():
    """Service Worker жазылымы үшін VAPID ашық кілті."""
    sender = _push()
    count = len(push_service.subscriptions_of(get_db(), g.user["id"]))
    return jsonify(enabled=sender is not None, public_key=getattr(sender, "public_key", None), devices=count)


@bp.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    if _push() is None:
        return jsonify(error="Push хабарландырулары серверде өшірулі."), 503
    endpoint = push_service.save_subscription(get_db(), g.user["id"], request.get_json(silent=True),
                                              request.headers.get("User-Agent", ""))
    if endpoint is None:
        return jsonify(error="Жазылым деректері қате."), 400
    return jsonify(message="Push хабарландырулары осы құрылғыда қосылды.")


@bp.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint", "")
    push_service.delete_subscription(get_db(), g.user["id"], endpoint)
    return jsonify(message="Push хабарландырулары осы құрылғыда өшірілді.")


@bp.route("/push/test", methods=["POST"])
@login_required
def push_test():
    if _push() is None:
        return jsonify(error="Push хабарландырулары серверде өшірулі."), 503
    sent = push_service.send_to_user(get_db(), _push(), g.user["id"], {
        "title": "Esimde", "body": "Еске салу: бұл тексеру хабарламасы. Push дұрыс жұмыс істейді!",
        "tag": "test", "url": "/settings"})
    if not sent:
        return jsonify(error="Push жазылымы табылмады. Алдымен «Push-ты қосу» батырмасын басыңыз."), 400
    return jsonify(message=f"Тексеру хабарламасы {sent} құрылғыға жіберілді.")


@bp.route("/push/action", methods=["POST"])
def push_action():
    """Push хабарламасындағы батырмалар (Service Worker-ден). Рұқсат — қол қойылған токен арқылы."""
    payload = request.get_json(silent=True) or {}
    ids = push_service.read_action_token(current_app.config["SECRET_KEY"], payload.get("token", ""))
    if ids is None:
        return jsonify(error="Токен жарамсыз немесе мерзімі өткен."), 403
    user_id, event_id, event_date = ids
    db = get_db()
    row = models.get_event(db, user_id, event_id)
    if row is None:
        abort(404)
    tz = models.get_settings(db, user_id)["timezone"]
    action = payload.get("action")
    if action == "done":
        if row["is_completed"] or row["event_date"] != event_date:
            return jsonify(message="Бұл оқиға бұрын белгіленген.")  # екі рет басу немесе ескі хабарлама
        models.set_completed(db, user_id, event_id, True, tz)
        return jsonify(message="Оқиға орындалды деп белгіленді.")
    if action in ("snooze10", "snooze60"):
        at = models.snooze_event(db, user_id, event_id, 10 if action == "snooze10" else 60)
        if at is None:
            return jsonify(error="Оқиға табылмады немесе орындалған."), 400
        return jsonify(message=f"{utc_to_local(at, tz):%H:%M}-де қайта еске саламын.")
    return jsonify(error="Белгісіз әрекет."), 400


def _telegram():
    return current_app.extensions.get("telegram")


@bp.route("/telegram/status")
@login_required
def telegram_status():
    settings = models.get_settings(get_db(), g.user["id"])
    return jsonify(configured=_telegram() is not None, **telegram_bot.status_for(settings))


@bp.route("/telegram/link", methods=["POST"])
@login_required
def telegram_link():
    """Бір реттік сілтеме: t.me/<бот>?start=<токен>."""
    if _telegram() is None:
        return jsonify(error="Telegram бот бапталмаған (TELEGRAM_BOT_TOKEN жоқ)."), 503
    token = telegram_bot.create_link_token(get_db(), g.user["id"],
                                           current_app.config["TELEGRAM_LINK_MINUTES"])
    bot = current_app.config["TELEGRAM_BOT_USERNAME"]
    app_url = f"tg://resolve?domain={bot}&start={token}"
    return jsonify(
        url=f"https://t.me/{bot}?start={token}",  # QR-код үшін (телефонда бірден ашылады)
        app_url=app_url,                            # Telegram Desktop қосымшасы
        web_url=f"https://web.telegram.org/k/#?tgaddr={quote(app_url, safe='')}",
        command=f"/start {token}",                 # қолмен жіберу үшін
        bot=bot,
    )


@bp.route("/telegram/test", methods=["POST"])
@login_required
def telegram_test():
    if _telegram() is None:
        return jsonify(error="Telegram бот бапталмаған."), 503
    try:
        ok = telegram_bot.send_test_message(get_db(), _telegram(), g.user["id"])
    except telegram_bot.TelegramError:
        return jsonify(error="Telegram-ға жіберу мүмкін болмады. Кейінірек қайталаңыз."), 502
    if not ok:
        return jsonify(error="Telegram аккаунты қосылмаған."), 400
    return jsonify(message="Тексеру хабарламасы Telegram-ға жіберілді.")


@bp.route("/telegram/unlink", methods=["POST"])
@login_required
def telegram_unlink():
    telegram_bot.unlink_user(get_db(), g.user["id"])
    return jsonify(message="Telegram аккаунты ажыратылды.")


@bp.route("/notifications/ack", methods=["POST"])
@login_required
def notifications_ack():
    payload = request.get_json(silent=True) or {}
    ids = [i for i in payload.get("ids", []) if isinstance(i, int)]
    count = models.ack_notifications(get_db(), g.user["id"], ids)
    return jsonify(acknowledged=count)

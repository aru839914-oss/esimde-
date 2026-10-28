"""HTML беттері және оқиғаларды басқару (CRUD)."""
import math
from datetime import timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

import models
import telegram_bot
from auth import login_required
from database import get_db
from reminder_service import reminder_buckets
from utils import (CATEGORIES, DATE_FORMATS, REMINDER_MODES, TIMEZONES, format_date,
                   local_now, parse_utc_str, utc_to_local)
from validators import validate_event_form, validate_settings

bp = Blueprint("main", __name__)


def _tz():
    return g.settings["timezone"]


def _fmt():
    return g.settings["date_format"]


@bp.route("/")
@login_required
def dashboard():
    db = get_db()
    stats = models.get_stats(db, g.user["id"], _tz())
    upcoming = [models.enrich_event(r, _tz(), _fmt())
                for r in models.upcoming_events(db, g.user["id"], limit=6)]
    today = local_now(_tz()).date()
    return render_template("dashboard.html", stats=stats, upcoming=upcoming,
                           today_display=format_date(today, _fmt(), with_weekday=True))


@bp.route("/calendar")
@login_required
def calendar():
    return render_template("calendar.html")


@bp.route("/events")
@login_required
def events():
    filters = {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", ""),
        "priority": request.args.get("priority", ""),
        "status": request.args.get("status", ""),
        "sort": request.args.get("sort", "date_asc"),
    }
    page = request.args.get("page", 1, type=int) or 1
    rows, total = models.list_events(get_db(), g.user["id"], page=page, **filters)
    items = [models.enrich_event(r, _tz(), _fmt()) for r in rows]
    pages = max(1, math.ceil(total / models.PAGE_SIZE))
    return render_template("events.html", events=items, total=total, page=page, pages=pages,
                           filters=filters)


def _render_form(data, errors, event=None, status=200):
    return render_template("event_form.html", data=data, errors=errors, event=event), status


@bp.route("/events/add", methods=["GET", "POST"])
@login_required
def add_event():
    if request.method == "POST":
        data, errors, warnings = validate_event_form(request.form, _tz())
        if errors:
            flash("Оқиғаны сақтау мүмкін болмады. Қателерді түзетіңіз.", "danger")
            return _render_form(data, errors, status=400)
        models.create_event(get_db(), g.user["id"], data, _tz())
        for w in warnings:
            flash(w, "warning")
        flash(f"«{data['title']}» оқиғасы сәтті қосылды.", "success")
        if request.form.get("return_to") == "calendar":
            return redirect(url_for("main.calendar"))
        return redirect(url_for("main.events"))

    # Әдепкі уақыт — келесі толық сағат (өткен уақыт ескертуі бос формада шықпауы үшін)
    next_hour = (local_now(_tz()) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    date_arg = request.args.get("date")
    data = {
        "title": "", "description": "", "category": request.args.get("category", "personal"),
        "event_date": date_arg or next_hour.strftime("%Y-%m-%d"),
        "event_time": request.args.get("time") or ("09:00" if date_arg else next_hour.strftime("%H:%M")),
        "priority": "medium", "reminder_custom": "",
        "reminders": [g.settings["default_reminder"]] if g.settings["default_reminder"] != "none" else [],
    }
    if data["category"] not in CATEGORIES:
        data["category"] = "personal"
    data["recurrence"] = "yearly" if data["category"] == "birthday" else "none"
    return _render_form(data, {})


@bp.route("/events/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def edit_event(event_id):
    db = get_db()
    row = models.get_event(db, g.user["id"], event_id)
    if row is None:
        abort(404)
    event = models.enrich_event(row, _tz(), _fmt())
    if request.method == "POST":
        data, errors, warnings = validate_event_form(request.form, _tz())
        if errors:
            flash("Өзгерістерді сақтау мүмкін болмады. Қателерді түзетіңіз.", "danger")
            return _render_form(data, errors, event=event, status=400)
        models.update_event(db, g.user["id"], event_id, data, _tz())
        for w in warnings:
            flash(w, "warning")
        flash("Өзгерістер сақталды.", "success")
        return redirect(url_for("main.events"))

    data = dict(row)
    data["reminders"] = models.modes_of(row)
    data["reminder_custom"] = ""
    custom = [r for r in models.event_reminders(db, event_id) if r["kind"] == "custom"]
    if custom:
        local = utc_to_local(parse_utc_str(custom[0]["remind_at"]), _tz())
        data["reminder_custom"] = local.strftime("%Y-%m-%dT%H:%M")
    return _render_form(data, {}, event=event)


@bp.route("/events/<int:event_id>/delete", methods=["POST"])
@login_required
def delete_event(event_id):
    db = get_db()
    row = models.get_event(db, g.user["id"], event_id)
    if row is None:
        abort(404)
    models.delete_event(db, g.user["id"], event_id)
    flash(f"«{row['title']}» оқиғасы жойылды.", "success")
    target = request.form.get("next") or ""
    return redirect(target if target.startswith("/") and not target.startswith("//")
                    else url_for("main.events"))


@bp.route("/reminders")
@login_required
def reminders():
    db = get_db()
    today_list, upcoming, past = reminder_buckets(models.reminder_events(db, g.user["id"]), _tz())
    notifications = []
    for n in models.list_notifications(db, g.user["id"]):
        item = dict(n)
        sent_local = utc_to_local(parse_utc_str(n["created_at"]), _tz())
        item["sent_display"] = f"{format_date(sent_local.date(), _fmt())}, {sent_local:%H:%M}"
        notifications.append(item)
    return render_template("reminders.html", today_list=today_list, upcoming=upcoming,
                           past=past, notifications=notifications)


@bp.route("/categories")
@login_required
def categories():
    counts = models.category_counts(get_db(), g.user["id"])
    return render_template("categories.html", counts=counts)


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    errors = {}
    if request.method == "POST":
        values, errors = validate_settings(request.form)
        if not errors:
            models.update_settings(get_db(), g.user["id"], values)
            flash("Баптаулар сақталды.", "success")
            return redirect(url_for("main.settings"))
        flash("Баптауларды сақтау мүмкін болмады.", "danger")
    modes = {k: v for k, v in REMINDER_MODES.items() if k != "custom"}
    return render_template("settings.html", errors=errors, timezones=TIMEZONES,
                           date_formats=DATE_FORMATS, default_modes=modes,
                           tg=telegram_bot.status_for(g.settings),
                           tg_configured=current_app.extensions.get("telegram") is not None,
                           bot_username=current_app.config["TELEGRAM_BOT_USERNAME"])


@bp.route("/profile")
@login_required
def profile():
    stats = models.get_stats(get_db(), g.user["id"], _tz())
    joined = utc_to_local(parse_utc_str(g.user["created_at"]), _tz())
    return render_template("profile.html", stats=stats,
                           joined=format_date(joined.date(), _fmt()))

"""Форма деректерін тексеру. Барлық қате хабарламалары қазақ тілінде."""
import re
from datetime import datetime

from utils import (CATEGORIES, DATE_FORMATS, PRIORITIES, RECURRENCE, REMINDER_MODES, TIMEZONES,
                   compute_reminder_utc, local_to_utc, utc_now)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
TITLE_MAX = 150
DESCRIPTION_MAX = 2000
PASSWORD_MIN = 6


def _valid_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _valid_time(value):
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def validate_event_form(form, tz_name):
    """Оқиға формасын тексереді.

    Қайтарады: (data, errors, warnings). errors бос болса, data базаға сақтауға дайын.
    errors — {өріс: хабарлама}, warnings — ескертулер тізімі (сақтауға кедергі емес).
    """
    # Бірнеше еске салу: reminders=10m&reminders=1d ... (ескі формадағы бір reminder_mode-ты да қабылдаймыз)
    modes = form.getlist("reminders") if hasattr(form, "getlist") else []
    if not modes and form.get("reminder_mode"):
        modes = [form.get("reminder_mode")]
    modes = [m for m in dict.fromkeys(modes) if m and m != "none"]
    data = {
        "title": (form.get("title") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "category": form.get("category") or "other",
        "event_date": (form.get("event_date") or "").strip(),
        "event_time": (form.get("event_time") or "").strip(),
        "priority": form.get("priority") or "medium",
        "reminders": modes,
        "reminder_mode": ",".join(modes) or "none",
        "reminder_custom": (form.get("reminder_custom") or "").strip(),
        "recurrence": form.get("recurrence") or "none",
    }
    errors, warnings = {}, []

    if not data["title"]:
        errors["title"] = "Оқиға атауын енгізіңіз."
    elif len(data["title"]) > TITLE_MAX:
        errors["title"] = f"Атау {TITLE_MAX} таңбадан аспауы керек."
    if len(data["description"]) > DESCRIPTION_MAX:
        errors["description"] = f"Сипаттама {DESCRIPTION_MAX} таңбадан аспауы керек."
    if data["category"] not in CATEGORIES:
        errors["category"] = "Санатты тізімнен таңдаңыз."
    if data["priority"] not in PRIORITIES:
        errors["priority"] = "Маңыздылық деңгейін тізімнен таңдаңыз."
    if any(m not in REMINDER_MODES for m in modes):
        errors["reminder_mode"] = "Еске салу режимін тізімнен таңдаңыз."
    if data["recurrence"] not in RECURRENCE:
        errors["recurrence"] = "Қайталану түрін тізімнен таңдаңыз."
    if not _valid_date(data["event_date"]):
        errors["event_date"] = "Күнді дұрыс енгізіңіз (мысалы, 2026-11-10)."
    if not _valid_time(data["event_time"]):
        errors["event_time"] = "Уақытты дұрыс енгізіңіз (мысалы, 09:00)."

    if errors:
        return data, errors, warnings

    now = utc_now()
    event_utc = local_to_utc(data["event_date"], data["event_time"], tz_name)
    if event_utc < now and data["recurrence"] == "none":
        warnings.append("Назар аударыңыз: оқиға уақыты өтіп кеткен. Оған еске салу жіберілмейді.")

    reminder_times = []
    if "custom" in modes:
        try:
            custom_utc = compute_reminder_utc(event_utc, "custom", data["reminder_custom"], tz_name)
        except ValueError:
            custom_utc = None
        if custom_utc is None:
            errors["reminder_custom"] = "Еске салу күні мен уақытын көрсетіңіз."
        elif custom_utc > event_utc:
            errors["reminder_custom"] = "Еске салу уақыты оқиға басталғаннан кейін болмауы керек."
        else:
            reminder_times.append(custom_utc)
    else:
        data["reminder_custom"] = ""
    reminder_times += [compute_reminder_utc(event_utc, m) for m in modes if m != "custom"]

    if not errors and event_utc >= now and any(t and t < now for t in reminder_times):
        warnings.append("Кейбір еске салулардың уақыты өтіп кеткен, сондықтан олар бірден жіберіледі.")
    return data, errors, warnings


def validate_registration(form):
    data = {
        "name": (form.get("name") or "").strip(),
        "email": (form.get("email") or "").strip().lower(),
        "password": form.get("password") or "",
        "password_confirm": form.get("password_confirm") or "",
    }
    errors = {}
    if not data["name"]:
        errors["name"] = "Атыңызды енгізіңіз."
    elif len(data["name"]) > 80:
        errors["name"] = "Аты 80 таңбадан аспауы керек."
    if not data["email"]:
        errors["email"] = "Электрондық поштаны енгізіңіз."
    elif not EMAIL_RE.match(data["email"]) or len(data["email"]) > 120:
        errors["email"] = "Электрондық пошта форматы қате (мысалы, name@mail.kz)."
    if not data["password"]:
        errors["password"] = "Құпиясөзді енгізіңіз."
    elif len(data["password"]) < PASSWORD_MIN:
        errors["password"] = f"Құпиясөз кемінде {PASSWORD_MIN} таңбадан тұруы керек."
    if not data["password_confirm"]:
        errors["password_confirm"] = "Құпиясөзді қайталаңыз."
    elif data["password"] != data["password_confirm"]:
        errors["password_confirm"] = "Құпиясөздер сәйкес келмейді."
    return data, errors


def validate_settings(form):
    values = {
        "timezone": form.get("timezone") or "Asia/Almaty",
        "browser_notifications": 1 if form.get("browser_notifications") else 0,
        "telegram_enabled": 1 if form.get("telegram_enabled") else 0,
        "default_reminder": form.get("default_reminder") or "30m",
        "date_format": form.get("date_format") or "long",
    }
    errors = {}
    if values["timezone"] not in dict(TIMEZONES):
        errors["timezone"] = "Уақыт белдеуін тізімнен таңдаңыз."
    if values["default_reminder"] not in REMINDER_MODES or values["default_reminder"] == "custom":
        errors["default_reminder"] = "Әдепкі еске салу уақытын тізімнен таңдаңыз."
    if values["date_format"] not in DATE_FORMATS:
        errors["date_format"] = "Күн форматын тізімнен таңдаңыз."
    return values, errors

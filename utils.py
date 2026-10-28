"""Тұрақтылар (санаттар, маңыздылық, еске салу режимдері) және күн/уақыт көмекші функциялары."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- Санаттар: кілт -> (атауы, түсі, иконкасы) ---------------------------------
CATEGORIES = {
    "personal": {"label": "Жеке", "color": "#10b981", "icon": "bi-person-heart"},
    "study": {"label": "Оқу", "color": "#3b82f6", "icon": "bi-mortarboard"},
    "work": {"label": "Жұмыс", "color": "#8b5cf6", "icon": "bi-briefcase"},
    "birthday": {"label": "Туған күн", "color": "#ec4899", "icon": "bi-gift"},
    "meeting": {"label": "Кездесу", "color": "#f97316", "icon": "bi-people"},
    "holiday": {"label": "Мереке", "color": "#eab308", "icon": "bi-stars"},
    "other": {"label": "Басқа", "color": "#64748b", "icon": "bi-bookmark"},
}

PRIORITIES = {
    "low": {"label": "Төмен", "class": "low"},
    "medium": {"label": "Орташа", "class": "medium"},
    "high": {"label": "Жоғары", "class": "high"},
}

# Еске салу режимі -> оқиғаға дейінгі уақыт (минутпен). custom — пайдаланушы өзі таңдайды.
# Бір оқиғаға бірнеше режим таңдауға болады (none-нан басқа).
REMINDER_MODES = {
    "none": {"label": "Еске салусыз", "short": "Жоқ", "minutes": None},
    "10m": {"label": "Оқиғаға 10 минут қалғанда", "short": "10 мин", "minutes": 10},
    "30m": {"label": "Оқиғаға 30 минут қалғанда", "short": "30 мин", "minutes": 30},
    "1h": {"label": "Оқиғаға 1 сағат қалғанда", "short": "1 сағат", "minutes": 60},
    "1d": {"label": "Оқиғаға 1 күн қалғанда", "short": "1 күн", "minutes": 1440},
    "1w": {"label": "Оқиғаға 1 апта қалғанда", "short": "1 апта", "minutes": 10080},
    "custom": {"label": "Өз уақытын таңдау", "short": "Өз уақыты", "minutes": None},
}

# «Кейінге қалдыру» мүмкін болатын аралықтар (минут)
SNOOZE_OPTIONS = {10: "10 минут", 30: "30 минут", 60: "1 сағат"}

# Қайталану ережелері
RECURRENCE = {
    "none": {"label": "Қайталанбайды", "short": ""},
    "daily": {"label": "Күн сайын", "short": "Күн сайын"},
    "weekly": {"label": "Апта сайын", "short": "Апта сайын"},
    "monthly": {"label": "Ай сайын", "short": "Ай сайын"},
    "yearly": {"label": "Жыл сайын", "short": "Жыл сайын"},
}

DATE_FORMATS = {
    "long": "27 қазан 2026",
    "dmy": "27.10.2026",
    "iso": "2026-10-27",
}

TIMEZONES = [
    ("Asia/Almaty", "Алматы / Астана (UTC+5)"),
    ("Asia/Aqtobe", "Ақтөбе (UTC+5)"),
    ("Asia/Aqtau", "Ақтау (UTC+5)"),
    ("Asia/Oral", "Орал (UTC+5)"),
    ("Asia/Tashkent", "Ташкент (UTC+5)"),
    ("Asia/Bishkek", "Бішкек (UTC+6)"),
    ("Europe/Moscow", "Мәскеу (UTC+3)"),
    ("UTC", "UTC"),
]

MONTHS_GENITIVE = ["қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым",
                   "шілде", "тамыз", "қыркүйек", "қазан", "қараша", "желтоқсан"]
WEEKDAYS = ["дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі"]

UTC_FORMAT = "%Y-%m-%d %H:%M:%S"


# --- Уақыт белдеуі ---------------------------------------------------------------
def get_zone(tz_name):
    """Уақыт белдеуін қайтарады; белгісіз болса — Asia/Almaty."""
    try:
        return ZoneInfo(tz_name or "Asia/Almaty")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Almaty")


def utc_now():
    return datetime.now(timezone.utc)


def to_utc_str(dt):
    """Aware datetime -> UTC жолы (базада бірізді сақтау үшін)."""
    return dt.astimezone(timezone.utc).strftime(UTC_FORMAT)


def parse_utc_str(value):
    if not value:
        return None
    return datetime.strptime(value, UTC_FORMAT).replace(tzinfo=timezone.utc)


def local_to_utc(date_str, time_str, tz_name):
    """'2026-11-10' + '09:00' (пайдаланушының белдеуінде) -> UTC aware datetime."""
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=get_zone(tz_name)).astimezone(timezone.utc)


def utc_to_local(dt_utc, tz_name):
    return dt_utc.astimezone(get_zone(tz_name))


def local_now(tz_name):
    return datetime.now(get_zone(tz_name))


def compute_reminder_utc(event_utc, mode, custom_local=None, tz_name=None):
    """Еске салудың нақты уақытын есептейді (UTC). Еске салу жоқ болса — None.

    custom_local — 'YYYY-MM-DDTHH:MM' форматындағы жергілікті уақыт (custom режимі үшін).
    """
    if mode == "custom":
        if not custom_local:
            return None
        naive = datetime.strptime(custom_local, "%Y-%m-%dT%H:%M")
        return naive.replace(tzinfo=get_zone(tz_name)).astimezone(timezone.utc)
    minutes = REMINDER_MODES.get(mode, {}).get("minutes")
    if minutes is None:
        return None
    return event_utc - timedelta(minutes=minutes)


# --- Қайталанатын оқиғалар -------------------------------------------------------------
def _add_months(d, months):
    """Айды қосады; күн айдың соңынан асса, соңғы күнге келтіріледі (31 қаңтар + 1 ай = 28/29 ақпан)."""
    import calendar
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def occurrence(start_date, rule, k):
    """Сериясының k-шы қайталануының күні (start_date — алғашқы күн)."""
    if rule == "daily":
        return start_date + timedelta(days=k)
    if rule == "weekly":
        return start_date + timedelta(weeks=k)
    if rule == "monthly":
        return _add_months(start_date, k)
    if rule == "yearly":
        return _add_months(start_date, 12 * k)
    return start_date


def next_occurrence(start_date, rule, after_date):
    """after_date-тен КЕЙІНГІ (қатаң) бірінші қайталану күні."""
    if rule not in RECURRENCE or rule == "none":
        return None
    # Жылдамдату: шамамен қажетті k-дан бастаймыз
    span = (after_date - start_date).days
    approx = {"daily": span, "weekly": span // 7, "monthly": span // 31, "yearly": span // 366}[rule]
    k = max(approx - 1, 0)
    while True:
        d = occurrence(start_date, rule, k)
        if d > after_date:
            return d
        k += 1


def occurrences_between(start_date, rule, from_date, to_date, limit=500):
    """[from_date, to_date) аралығындағы қайталанулар (start_date-тен бастап)."""
    result = []
    if rule not in RECURRENCE or rule == "none":
        return result
    k = 0
    if from_date > start_date:
        first = next_occurrence(start_date, rule, from_date - timedelta(days=1))
        while occurrence(start_date, rule, k) < first:
            k += 1
    while len(result) < limit:
        d = occurrence(start_date, rule, k)
        if d >= to_date:
            break
        if d >= from_date:
            result.append(d)
        k += 1
    return result


# --- Форматтау ---------------------------------------------------------------------
def format_date(date_obj, fmt="long", with_weekday=False):
    if fmt == "dmy":
        text = date_obj.strftime("%d.%m.%Y")
    elif fmt == "iso":
        text = date_obj.strftime("%Y-%m-%d")
    else:
        text = f"{date_obj.day} {MONTHS_GENITIVE[date_obj.month - 1]} {date_obj.year}"
    if with_weekday:
        text += f", {WEEKDAYS[date_obj.weekday()]}"
    return text


def humanize_delta(target, now):
    """Оқиғаға дейін қалған уақытты қазақ тілінде сипаттайды."""
    seconds = (target - now).total_seconds()
    if seconds < 0:
        return "Уақыты өтті"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "Қазір басталады"
    if minutes < 60:
        return f"{minutes} минут қалды"
    days_diff = (target.date() - now.date()).days
    if days_diff == 0:
        return f"Бүгін, {minutes // 60} сағат қалды"
    if days_diff == 1:
        return "Ертең"
    return f"{days_diff} күн қалды"


def describe_lead_time(event_utc, reminder_utc):
    """Еске салу мен оқиға арасындағы уақытты мәтінге айналдырады: '30 минут', '1 күн'."""
    minutes = int(round((event_utc - reminder_utc).total_seconds() / 60))
    if minutes <= 0:
        return None
    if minutes % 1440 == 0:
        return f"{minutes // 1440} күн"
    if minutes % 60 == 0:
        return f"{minutes // 60} сағат"
    if minutes > 60:
        return f"{minutes // 60} сағат {minutes % 60} минут"
    return f"{minutes} минут"

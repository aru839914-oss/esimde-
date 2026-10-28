"""Telegram арқылы еске салу.

Жұмыс принципі:
1. Пайдаланушы сайттағы «Баптаулар» бетінде «Telegram-ды қосу» батырмасын басады.
   Сервер бір реттік токен жасап (15 минутқа), t.me/<бот>?start=<токен> сілтемесін ашады.
2. Пайдаланушы ботта Start басады → бот "/start <токен>" хабарламасын алады →
   токен бойынша аккаунт табылып, chat_id базаға жазылады.
3. Жоспарлаушы еске салу жасағанда (reminder_service), deliver_pending() оны Telegram-ға жібереді.
   Әр хабарлама атомарлы түрде "иеленеді" — қайталанбайды; сәтсіз болса 5 ретке дейін қайталанады.
4. Хабарламадағы «✅ Орындалды» батырмасы оқиғаны орындалды деп белгілейді.

Бот long polling (getUpdates) режимінде жұмыс істейді — HTTPS немесе webhook қажет емес.
Қосымша кітапхана қолданылмайды: Telegram Bot API стандартты urllib арқылы шақырылады.
"""
import html
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

import models
from database import connect
from utils import (CATEGORIES, PRIORITIES, SNOOZE_OPTIONS, format_date, parse_utc_str, to_utc_str,
                   utc_now, utc_to_local)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
FRESH_WINDOW = timedelta(hours=1)  # бұдан ескі хабарландырулар Telegram-ға жіберілмейді
PRIORITY_FLAGS = {"high": "🔴", "medium": "🟠", "low": "🟢"}

COMMANDS = [
    {"command": "upcoming", "description": "Алдағы оқиғалар"},
    {"command": "help", "description": "Көмек"},
    {"command": "stop", "description": "Аккаунтты ажырату"},
]


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


class TelegramClient:
    """Telegram Bot API-дің ең қажетті әдістері."""

    def __init__(self, token, timeout=35):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def _call(self, method, payload=None, timeout=None):
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(self.base + method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except ValueError:
                raise TelegramError(exc.code, str(exc)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(0, f"network: {exc}") from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", 0), body.get("description", ""))
        return body["result"]

    def get_me(self):
        return self._call("getMe")

    def send_message(self, chat_id, text, buttons=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", payload)

    def edit_message_text(self, chat_id, message_id, text):
        return self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                              "text": text, "parse_mode": "HTML"})

    def answer_callback(self, callback_id, text):
        return self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset, timeout=25):
        return self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "allowed_updates": ["message", "callback_query"]},
                          timeout=timeout + 10)

    def set_commands(self):
        return self._call("setMyCommands", {"commands": COMMANDS})

    def delete_webhook(self):
        return self._call("deleteWebhook", {"drop_pending_updates": False})


# ===================== Аккаунтты байланыстыру =====================
def create_link_token(db, user_id, minutes=15):
    """Бір реттік токен жасайды (ескісін ауыстырады)."""
    token = secrets.token_urlsafe(16)
    expires = to_utc_str(utc_now() + timedelta(minutes=minutes))
    db.execute("UPDATE user_settings SET telegram_link_token = ?, telegram_link_expires = ? WHERE user_id = ?",
               (token, expires, user_id))
    db.commit()
    return token


def link_chat(db, token, chat_id, tg_name):
    """Токен дұрыс әрі мерзімі өтпеген болса, chat_id-ді пайдаланушыға байланыстырады."""
    row = db.execute(
        """SELECT s.user_id, s.telegram_link_expires, u.name FROM user_settings s
           JOIN users u ON u.id = s.user_id WHERE s.telegram_link_token = ?""",
        (token,),
    ).fetchone()
    if row is None or parse_utc_str(row["telegram_link_expires"]) < utc_now():
        return None
    chat_id = str(chat_id)
    # Бір чат тек бір аккаунтқа байланысады
    db.execute("""UPDATE user_settings SET telegram_chat_id = NULL, telegram_linked_at = NULL
                  WHERE telegram_chat_id = ? AND user_id != ?""", (chat_id, row["user_id"]))
    db.execute(
        """UPDATE user_settings SET telegram_chat_id = ?, telegram_name = ?, telegram_linked_at = ?,
                  telegram_link_token = NULL, telegram_link_expires = NULL, telegram_enabled = 1
           WHERE user_id = ?""",
        (chat_id, tg_name, to_utc_str(utc_now()), row["user_id"]),
    )
    # Байланыстыруға дейінгі ескі хабарландырулар Telegram-ға жіберілмейді
    db.execute("UPDATE notifications SET telegram_sent = 1 WHERE user_id = ? AND telegram_sent = 0",
               (row["user_id"],))
    db.commit()
    return row["name"]


def unlink_user(db, user_id):
    db.execute("""UPDATE user_settings SET telegram_chat_id = NULL, telegram_name = NULL,
                  telegram_linked_at = NULL, telegram_link_token = NULL, telegram_link_expires = NULL
                  WHERE user_id = ?""", (user_id,))
    db.commit()


def user_by_chat(db, chat_id):
    return db.execute(
        """SELECT u.id, u.name, s.timezone, s.date_format FROM user_settings s
           JOIN users u ON u.id = s.user_id WHERE s.telegram_chat_id = ?""",
        (str(chat_id),),
    ).fetchone()


# ===================== Хабарлама мәтіндері =====================
def format_reminder(row):
    """Еске салу хабарламасы (HTML)."""
    lines = [f"🔔 <b>Еске салу</b>", html.escape(row["message"].removeprefix("Еске салу: "))]
    if row["event_date"]:
        tz = row["timezone"]
        event_local = utc_to_local(parse_utc_str(row["event_at_utc"]), tz)
        cat = CATEGORIES.get(row["category"], CATEGORIES["other"])["label"]
        prio = PRIORITIES.get(row["priority"], PRIORITIES["medium"])["label"]
        lines.append("")
        lines.append(f"📅 {format_date(event_local.date(), 'long')}, {event_local:%H:%M}")
        lines.append(f"🏷 {cat}  ·  {PRIORITY_FLAGS.get(row['priority'], '')} {prio}")
    if row["status"] == "late":
        lines.append("<i>Сервер уақытша тоқтағандықтан кешіктіріліп жіберілді.</i>")
    return "\n".join(lines)


def upcoming_text(db, user):
    rows = db.execute(
        """SELECT title, event_at_utc, priority FROM events
           WHERE user_id = ? AND is_completed = 0 AND event_at_utc >= ?
           ORDER BY event_at_utc LIMIT 7""",
        (user["id"], to_utc_str(utc_now())),
    ).fetchall()
    if not rows:
        return "📭 Алдағы оқиғалар жоқ. Жаңа оқиғаны сайтта қосыңыз."
    lines = ["📋 <b>Алдағы оқиғалар</b>", ""]
    for r in rows:
        local = utc_to_local(parse_utc_str(r["event_at_utc"]), user["timezone"])
        lines.append(f"{PRIORITY_FLAGS.get(r['priority'], '•')} <b>{html.escape(r['title'])}</b>\n"
                     f"     {format_date(local.date(), 'long')}, {local:%H:%M}")
    return "\n".join(lines)


HELP_TEXT = (
    "🤖 <b>Esimde</b> — маңызды оқиғалар туралы еске салу жүйесінің боты.\n\n"
    "Бот сайттағы оқиғаларыңыз туралы уақытында хабарлайды.\n\n"
    "<b>Командалар:</b>\n"
    "/upcoming — алдағы оқиғалар\n"
    "/stop — аккаунтты ажырату\n"
    "/help — көмек"
)
NOT_LINKED_TEXT = (
    "👋 Сәлеметсіз бе! Бұл — <b>Esimde</b> жүйесінің боты.\n\n"
    "Еске салуларды осы жерде алу үшін сайтқа кіріп, "
    "<b>Баптаулар → Telegram-ды қосу</b> батырмасын басыңыз."
)


# ===================== Жаңартуларды өңдеу =====================
def handle_update(db_path, update, client):
    """Telegram-нан келген бір жаңартуды (хабарлама немесе батырма) өңдейді."""
    db = connect(db_path)
    try:
        if "callback_query" in update:
            _handle_callback(db, update["callback_query"], client)
        elif "message" in update:
            _handle_message(db, update["message"], client)
    finally:
        db.close()


def _handle_message(db, message, client):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()
    user = user_by_chat(db, chat_id)

    if command == "/start":
        if arg:
            sender = message.get("from", {})
            tg_name = sender.get("username") or sender.get("first_name") or ""
            name = link_chat(db, arg.strip(), chat_id, tg_name)
            if name:
                client.send_message(chat_id, f"✅ Аккаунт сәтті қосылды, <b>{html.escape(name)}</b>!\n\n"
                                             "Енді еске салулар осы чатқа келеді.\n/upcoming — алдағы оқиғалар")
            else:
                client.send_message(chat_id, "⚠️ Сілтеме жарамсыз немесе мерзімі өтті.\n"
                                             "Сайттағы <b>Баптаулар</b> бетінен қайта қосып көріңіз.")
        elif user:
            client.send_message(chat_id, f"Сіз <b>{html.escape(user['name'])}</b> аккаунтына қосылғансыз.\n\n"
                                         + HELP_TEXT)
        else:
            client.send_message(chat_id, NOT_LINKED_TEXT)
    elif user is None:
        client.send_message(chat_id, NOT_LINKED_TEXT)
    elif command in ("/upcoming", "/next"):
        client.send_message(chat_id, upcoming_text(db, user))
    elif command == "/stop":
        unlink_user(db, user["id"])
        client.send_message(chat_id, "🔕 Аккаунт ажыратылды. Еске салулар енді бұл чатқа келмейді.")
    else:
        client.send_message(chat_id, HELP_TEXT)


def reminder_buttons(event_id):
    """Еске салу хабарламасының астындағы батырмалар: орындалды және кейінге қалдыру."""
    return [
        [{"text": "✅ Орындалды", "callback_data": f"done:{event_id}"}],
        [{"text": "⏰ 10 мин", "callback_data": f"snooze:{event_id}:10"},
         {"text": "⏰ 1 сағат", "callback_data": f"snooze:{event_id}:60"}],
    ]


def _handle_callback(db, query, client):
    """done:<id> — орындалды; snooze:<id>:<минут> — кейінге қалдыру. Тек өз оқиғасына рұқсат."""
    chat_id = query["message"]["chat"]["id"]
    parts = query.get("data", "").split(":")
    user = user_by_chat(db, chat_id)
    if user is None:
        client.answer_callback(query["id"], "Аккаунт қосылмаған.")
        return
    try:
        action, event_id = parts[0], int(parts[1])
        minutes = int(parts[2]) if action == "snooze" else None
    except (IndexError, ValueError):
        client.answer_callback(query["id"], "Қате сұраныс.")
        return

    if action == "done":
        result = models.set_completed(db, user["id"], event_id, True, user["timezone"])
        if result is None:
            client.answer_callback(query["id"], "Оқиға табылмады.")
            return
        if isinstance(result, tuple):
            nxt = result[1]
            note = f"✅ <b>Орындалды.</b> Келесі қайталану: {format_date(nxt.date(), 'long')}, {nxt:%H:%M}"
        else:
            note = "✅ <b>Орындалды деп белгіленді</b>"
        client.answer_callback(query["id"], "Орындалды деп белгіленді ✅")
    elif action == "snooze" and minutes in SNOOZE_OPTIONS:
        if models.get_event(db, user["id"], event_id) is None:
            client.answer_callback(query["id"], "Оқиға табылмады.")
            return
        at = models.snooze_event(db, user["id"], event_id, minutes)
        if at is None:
            client.answer_callback(query["id"], "Оқиға орындалған.")
            return
        local = utc_to_local(at, user["timezone"])
        note = f"⏰ <b>{SNOOZE_OPTIONS[minutes]}тан кейін</b> ({local:%H:%M}) қайта еске саламын"
        client.answer_callback(query["id"], f"{local:%H:%M}-де қайта еске саламын ⏰")
    else:
        client.answer_callback(query["id"], "Қате сұраныс.")
        return

    old_text = query["message"].get("text", "")
    try:
        client.edit_message_text(chat_id, query["message"]["message_id"], html.escape(old_text) + "\n\n" + note)
    except TelegramError:
        pass  # хабарламаны өзгерту міндетті емес


# ===================== Еске салуларды жеткізу =====================
def deliver_pending(db_path, client, now=None):
    """Telegram-ға әлі жіберілмеген еске салуларды жібереді. Нәтиже: жіберілгендер саны."""
    now = now or utc_now()
    conn = connect(db_path)
    sent = 0
    try:
        rows = conn.execute(
            """SELECT n.id, n.user_id, n.event_id, n.message, n.status,
                      s.telegram_chat_id, s.timezone,
                      e.event_date, e.event_at_utc, e.category, e.priority, e.is_completed
               FROM notifications n
               JOIN user_settings s ON s.user_id = n.user_id
               LEFT JOIN events e ON e.id = n.event_id
               WHERE n.status IN ('sent', 'late') AND n.telegram_sent = 0
                 AND n.telegram_attempts < ?
                 AND s.telegram_chat_id IS NOT NULL AND s.telegram_enabled = 1
                 AND n.created_at >= ?
               ORDER BY n.id""",
            (MAX_ATTEMPTS, to_utc_str(now - FRESH_WINDOW)),
        ).fetchall()
        for row in rows:
            claimed = conn.execute(
                "UPDATE notifications SET telegram_sent = 1, telegram_attempts = telegram_attempts + 1 "
                "WHERE id = ? AND telegram_sent = 0", (row["id"],)).rowcount
            conn.commit()
            if not claimed:
                continue
            buttons = None
            if row["event_id"] and not row["is_completed"]:
                buttons = reminder_buttons(row["event_id"])
            try:
                client.send_message(row["telegram_chat_id"], format_reminder(row), buttons)
                sent += 1
            except TelegramError as exc:
                log.warning("Telegram send failed for notification %s: %s", row["id"], exc)
                if exc.code == 403:  # пайдаланушы ботты бұғаттаған — байланысты үземіз
                    unlink_user(conn, row["user_id"])
                else:
                    conn.execute("UPDATE notifications SET telegram_sent = 0 WHERE id = ?", (row["id"],))
                    conn.commit()
    finally:
        conn.close()
    return sent


def send_test_message(db, client, user_id):
    row = db.execute("SELECT telegram_chat_id FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row["telegram_chat_id"]:
        return False
    client.send_message(row["telegram_chat_id"],
                        "🔔 <b>Тексеру хабарламасы</b>\nTelegram хабарландырулары дұрыс жұмыс істейді!")
    return True


# ===================== Long polling ағыны =====================
_poller = None


def start_polling(db_path, client):
    """Telegram жаңартуларын фондық ағында тыңдайды (бір процесте бір рет)."""
    global _poller
    if _poller is not None:
        return _poller

    def loop():
        offset = 0
        try:
            client.delete_webhook()
            me = client.get_me()
            client.set_commands()
            log.info("Telegram bot @%s started", me.get("username"))
        except TelegramError as exc:
            log.error("Telegram bot could not start: %s", exc)
        while True:
            try:
                for update in client.get_updates(offset):
                    offset = update["update_id"] + 1
                    try:
                        handle_update(db_path, update, client)
                    except Exception:  # бір жаңарту бүкіл ботты тоқтатпауы керек
                        log.exception("Failed to handle Telegram update")
            except TelegramError as exc:
                if exc.code in (401, 404):  # токен жарамсыз немесе қате форматта
                    log.error("Telegram token is invalid — bot stopped")
                    return
                log.warning("Telegram polling error: %s", exc)
                time.sleep(30 if exc.code == 409 else 5)

    _poller = threading.Thread(target=loop, name="telegram-polling", daemon=True)
    _poller.start()
    return _poller


def status_for(settings):
    """Баптаулар беті үшін Telegram күйі."""
    linked = bool(settings.get("telegram_chat_id"))
    linked_at = None
    if linked and settings.get("telegram_linked_at"):
        local = utc_to_local(parse_utc_str(settings["telegram_linked_at"]), settings["timezone"])
        linked_at = f"{format_date(local.date(), settings['date_format'])}, {local:%H:%M}"
    return {"linked": linked, "name": settings.get("telegram_name") or "",
            "enabled": bool(settings.get("telegram_enabled")), "linked_at": linked_at}

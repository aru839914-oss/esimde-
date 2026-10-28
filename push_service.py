"""Web Push хабарландырулары (браузер жабық болса да телефонға/компьютерге келеді).

Жұмыс принципі:
1. Сервер бір рет VAPID кілттер жұбын жасайды (INSTANCE_DIR/vapid_private.pem).
2. Браузер Service Worker тіркейді және ашық кілтпен push-жазылым жасайды
   (endpoint + p256dh + auth) → /api/push/subscribe арқылы push_subscriptions кестесіне сақталады.
3. Жоспарлаушы еске салу жасағанда deliver_pending_push() оны пайдаланушының барлық
   құрылғыларына шифрланған түрде жібереді (pywebpush). Әр хабарлама атомарлы иеленеді — қайталанбайды.
4. Service Worker хабарламаны көрсетеді; «Орындалды» / «10 мин» батырмалары қол қойылған
   токенмен /api/push/action арқылы жұмыс істейді (сайт ашық болмаса да).
"""
import base64
import json
import logging
import os
from datetime import timedelta

from itsdangerous import BadSignature, URLSafeTimedSerializer

from database import connect
from utils import to_utc_str, utc_now

log = logging.getLogger(__name__)

FRESH_WINDOW = timedelta(hours=1)   # бұдан ескі хабарландырулар push арқылы жіберілмейді
MAX_ATTEMPTS = 5
ACTION_TOKEN_MAX_AGE = 3 * 24 * 3600  # хабарламадағы батырмалар 3 күн жарамды


class PushError(Exception):
    def __init__(self, status, message=""):
        super().__init__(f"{status}: {message}")
        self.status = status


def load_or_create_vapid(instance_dir):
    """VAPID кілттерін жүктейді (болмаса жасайды). Қайтарады: (private_key_path, public_key_b64url)."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    os.makedirs(instance_dir, exist_ok=True)
    path = os.path.join(instance_dir, "vapid_private.pem")
    if not os.path.exists(path):
        key = Vapid01()
        key.generate_keys()
        key.save_key(path)
        log.info("VAPID keys generated: %s", path)
    key = Vapid01.from_file(path)
    raw = key.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return path, base64.urlsafe_b64encode(raw).decode().rstrip("=")


class WebPushSender:
    """pywebpush арқылы нақты жіберу."""

    def __init__(self, instance_dir, subject):
        self.key_path, self.public_key = load_or_create_vapid(instance_dir)
        self.subject = subject

    def send(self, subscription, payload, ttl=3600):
        from pywebpush import WebPushException, webpush
        try:
            webpush(subscription, json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=self.key_path, vapid_claims={"sub": self.subject},
                    ttl=ttl, timeout=10)
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise PushError(status, str(exc)) from None
        except Exception as exc:  # желі қатесі т.б.
            raise PushError(0, str(exc)) from None


# ===================== Жазылымдар =====================
def save_subscription(db, user_id, data, user_agent=""):
    """Браузердің PushSubscription.toJSON() нәтижесін сақтайды. Қате болса None қайтарады."""
    endpoint = (data or {}).get("endpoint", "")
    keys = (data or {}).get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return None
    db.execute(
        """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh,
                                               auth = excluded.auth, user_agent = excluded.user_agent""",
        (user_id, endpoint, keys["p256dh"], keys["auth"], user_agent[:200], to_utc_str(utc_now())),
    )
    # Жазылымға дейінгі ескі хабарландырулар push арқылы жіберілмейді
    db.execute("UPDATE notifications SET push_sent = 1 WHERE user_id = ? AND push_sent = 0", (user_id,))
    db.commit()
    return endpoint


def delete_subscription(db, user_id, endpoint):
    cur = db.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?", (user_id, endpoint))
    db.commit()
    return cur.rowcount


def subscriptions_of(db, user_id):
    return db.execute("SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()


def _as_info(row):
    return {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}


# ===================== Батырмаларға арналған қол қойылған токен =====================
def _serializer(secret_key):
    return URLSafeTimedSerializer(secret_key, salt="push-action")


def make_action_token(secret_key, user_id, event_id, event_date):
    """event_date — хабарлама қай қайталануға тиесілі екенін көрсетеді (екі рет басудан қорғау)."""
    return _serializer(secret_key).dumps({"u": user_id, "e": event_id, "d": event_date})


def read_action_token(secret_key, token):
    """Қайтарады: (user_id, event_id, event_date) немесе None."""
    try:
        data = _serializer(secret_key).loads(token, max_age=ACTION_TOKEN_MAX_AGE)
        return int(data["u"]), int(data["e"]), data.get("d")
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


# ===================== Жеткізу =====================
def send_to_user(db, sender, user_id, payload):
    """Пайдаланушының барлық құрылғыларына жібереді. Қайтарады: сәтті жіберілгендер саны."""
    ok = 0
    for sub in subscriptions_of(db, user_id):
        try:
            sender.send(_as_info(sub), payload)
            ok += 1
        except PushError as exc:
            if exc.status in (404, 410):  # жазылым жойылған/мерзімі өткен
                db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
                db.commit()
            else:
                log.warning("Push failed for subscription %s: %s", sub["id"], exc)
    return ok


def build_payload(secret_key, row):
    payload = {"title": "Esimde", "body": row["message"], "tag": f"notif-{row['id']}", "url": "/"}
    if row["event_id"] and row["event_date"]:
        payload.update(event_id=row["event_id"], url=f"/?event={row['event_id']}",
                       token=make_action_token(secret_key, row["user_id"], row["event_id"], row["event_date"]),
                       tag=f"event-{row['event_id']}")
    return payload


def deliver_pending_push(db_path, sender, secret_key, now=None):
    """Әлі push арқылы жіберілмеген еске салуларды жібереді. Нәтиже: жіберілген хабарландырулар саны."""
    now = now or utc_now()
    conn = connect(db_path)
    delivered = 0
    try:
        rows = conn.execute(
            """SELECT n.id, n.user_id, n.event_id, n.message, e.event_date FROM notifications n
               JOIN user_settings s ON s.user_id = n.user_id
               LEFT JOIN events e ON e.id = n.event_id
               WHERE n.status IN ('sent', 'late') AND n.push_sent = 0 AND n.push_attempts < ?
                 AND s.browser_notifications = 1 AND n.created_at >= ?
                 AND EXISTS (SELECT 1 FROM push_subscriptions p WHERE p.user_id = n.user_id)
               ORDER BY n.id""",
            (MAX_ATTEMPTS, to_utc_str(now - FRESH_WINDOW)),
        ).fetchall()
        for row in rows:
            claimed = conn.execute(
                "UPDATE notifications SET push_sent = 1, push_attempts = push_attempts + 1 "
                "WHERE id = ? AND push_sent = 0", (row["id"],)).rowcount
            conn.commit()
            if not claimed:
                continue
            if send_to_user(conn, sender, row["user_id"], build_payload(secret_key, row)):
                delivered += 1
            elif conn.execute("SELECT 1 FROM push_subscriptions WHERE user_id = ?", (row["user_id"],)).fetchone():
                # Құрылғы бар, бірақ жіберу сәтсіз — келесі циклде қайталаймыз
                conn.execute("UPDATE notifications SET push_sent = 0 WHERE id = ?", (row["id"],))
                conn.commit()
    finally:
        conn.close()
    return delivered

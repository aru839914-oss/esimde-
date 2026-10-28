"""Демо деректер: скриншоттар мен көрсетілім үшін (міндетті емес).

Іске қосу:  python seed_demo.py
Кіру:       demo@mail.kz / demo123
"""
from datetime import timedelta

from config import Config
from database import connect, init_db
import models
from utils import local_now

TZ = "Asia/Almaty"
EMAIL = "demo@mail.kz"


def main():
    init_db(Config.DATABASE)
    db = connect(Config.DATABASE)
    if models.get_user_by_email(db, EMAIL):
        print(f"{EMAIL} аккаунты бұрыннан бар — демо деректер қосылмады.")
        return
    user_id = models.create_user(db, "Демо", EMAIL, "demo123")
    now = local_now(TZ)
    samples = [
        ("Математика емтиханы", "study", 0, 2, "high", "30m", "Аудитория 305, калькулятор алу"),
        ("Айгерімнің туған күні", "birthday", 1, None, "medium", "1d", "Сыйлық дайындау"),
        ("Жобалық топпен кездесу", "meeting", 2, None, "medium", "1h", ""),
        ("Тоқсандық есеп дайындау", "work", 4, None, "high", "30m", ""),
        ("Курстық жұмысты тапсыру", "study", 6, None, "high", "1d", "Түсіндірме жазба + презентация"),
        ("Дәрігерге бару", "personal", 9, None, "medium", "10m", ""),
        ("Тәуелсіздік күні", "holiday", None, None, "low", "none", ""),
        ("Ағылшын тілі тесті", "study", -1, None, "medium", "none", ""),
        ("Кітапханаға кітап қайтару", "personal", -2, None, "low", "none", ""),
    ]
    times = ["10:00", "19:00", "14:30", "11:00", "10:00", "08:30", "10:00", "15:00", "12:00"]
    for (title, cat, days, hours, prio, rem, desc), t in zip(samples, times):
        if days is None:
            date = now.replace(month=12, day=16).strftime("%Y-%m-%d")
        elif hours is not None:
            soon = now + timedelta(hours=hours)
            date, t = soon.strftime("%Y-%m-%d"), soon.strftime("%H:%M")
        else:
            date = (now + timedelta(days=days)).strftime("%Y-%m-%d")
        event_id = models.create_event(db, user_id, {
            "title": title, "description": desc, "category": cat, "event_date": date, "event_time": t,
            "priority": prio, "reminder_mode": rem, "reminder_custom": ""}, TZ)
        if title == "Ағылшын тілі тесті":
            models.set_completed(db, user_id, event_id, True)
    db.close()
    print(f"Демо деректер қосылды. Кіру: {EMAIL} / demo123")


if __name__ == "__main__":
    main()

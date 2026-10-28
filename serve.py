"""Өндірістік (хостинг) іске қосу: waitress WSGI сервері, БІР процесс.

Бір процесс маңызды: еске салу жоспарлаушысы мен Telegram бот (long polling) екі рет
іске қосылмауы керек. waitress көп ағынды (threads), сондықтан бір процесс жеткілікті.

Railway: Start command = python serve.py ; PORT айнымалысын Railway өзі береді.
"""
import logging
import os

from waitress import serve

from app import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    application = create_app()
    port = int(os.environ.get("PORT", 8080))
    logging.info("Esimde is starting on port %s", port)
    serve(application, host="0.0.0.0", port=port, threads=8)

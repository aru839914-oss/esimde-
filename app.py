"""Маңызды оқиғалар туралы еске салу жүйесі — Flask қосымшасының кіру нүктесі.

Іске қосу:  python app.py   (http://127.0.0.1:5000)
"""
import logging
import os

from flask import (Flask, flash, g, jsonify, redirect, render_template, request, send_from_directory,
                   url_for)
from flask_wtf.csrf import CSRFError, CSRFProtect

import database
from config import INSTANCE_DIR, Config, load_secret_key
from utils import CATEGORIES, PRIORITIES, RECURRENCE, REMINDER_MODES

csrf = CSRFProtect()


def create_app(test_config=None):
    """Application factory: конфигурация, база, маршруттар, жоспарлаушы."""
    app = Flask(__name__, instance_path=INSTANCE_DIR)
    app.config.from_object(Config)
    app.json.ensure_ascii = False  # JSON-да қазақ әріптері оқылатын түрде
    if app.config.get("BEHIND_PROXY"):
        # Хостинг (Railway) HTTPS-ті өзі қабылдап, сұранысты прокси арқылы береді
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = load_secret_key(app.instance_path)

    csrf.init_app(app)
    database.init_app(app)

    # Telegram клиенті: тесттерде жалған клиент беріледі, әйтпесе токен болса ғана жасалады
    from telegram_bot import TelegramClient
    telegram = app.config.get("TELEGRAM_CLIENT")
    if telegram is None and app.config.get("TELEGRAM_BOT_TOKEN"):
        telegram = TelegramClient(app.config["TELEGRAM_BOT_TOKEN"])
    app.extensions["telegram"] = telegram

    # Web Push: тесттерде жалған жіберуші беріледі; кілттер INSTANCE_DIR ішінде автоматты жасалады
    push = app.config.get("PUSH_SENDER")
    if push is None and not app.config.get("TESTING"):
        try:
            from push_service import WebPushSender
            push = WebPushSender(app.instance_path, app.config["VAPID_SUBJECT"])
        except Exception:  # кітапхана немесе кілт мәселесі push-ты ғана өшіреді
            logging.getLogger(__name__).exception("Web Push disabled")
    app.extensions["push"] = push

    import api
    import auth
    import views
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(api.bp)
    # Push хабарламасындағы батырмалар Service Worker-ден келеді (CSRF токенсіз);
    # оның орнына қол қойылған бір реттік токен тексеріледі.
    csrf.exempt(api.push_action)
    _register_pwa_routes(app)

    @app.context_processor
    def inject_globals():
        nav = None
        if g.get("user"):
            import models
            nav = models.nav_counts(database.get_db(), g.user["id"])
        return {
            "nav_counts": nav,
            "CATEGORIES": CATEGORIES,
            "PRIORITIES": PRIORITIES,
            "REMINDER_MODES": REMINDER_MODES,
            "RECURRENCE": RECURRENCE,
            "current_user": g.get("user"),
            "user_settings": g.get("settings"),
        }

    _register_error_handlers(app)

    if app.config.get("SCHEDULER_ENABLED") and not app.config.get("TESTING"):
        # flask --debug режимінде reloader-дің ата-процесінде жоспарлаушы іске қосылмайды.
        if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            from reminder_service import start_scheduler
            start_scheduler(app)
            if telegram is not None:
                from telegram_bot import start_polling
                start_polling(app.config["DATABASE"], telegram)
    return app


def _register_pwa_routes(app):
    """PWA: Service Worker сайт түбірінде болуы керек (scope = "/"), манифест және офлайн бет."""
    static_js = os.path.join(app.root_path, "static", "js")

    @app.route("/sw.js")
    def service_worker():
        resp = send_from_directory(static_js, "sw.js", mimetype="application/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/manifest.webmanifest")
    def manifest():
        resp = send_from_directory(os.path.join(app.root_path, "static"), "manifest.webmanifest",
                                   mimetype="application/manifest+json")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/offline")
    def offline():
        return render_template("offline.html")

    @app.route("/healthz")
    def healthz():
        """Хостингтің денсаулық тексеруі: база қолжетімді ме."""
        database.get_db().execute("SELECT 1").fetchone()
        return jsonify(status="ok")


def _wants_json():
    return request.path.startswith("/api/")


def _register_error_handlers(app):
    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        if _wants_json():
            return jsonify(error="Қауіпсіздік токені жарамсыз. Бетті жаңартыңыз."), 400
        flash("Қауіпсіздік токенінің мерзімі өтті. Әрекетті қайталаңыз.", "danger")
        return redirect(request.referrer or url_for("main.dashboard"))

    @app.errorhandler(404)
    def not_found(_e):
        if _wants_json():
            return jsonify(error="Сұралған ресурс табылмады."), 404
        return render_template("error.html", code=404, title="Бет табылмады",
                               message="Сіз іздеген бет немесе оқиға жоқ немесе жойылған."), 404

    @app.errorhandler(405)
    def method_not_allowed(_e):
        if _wants_json():
            return jsonify(error="Бұл әдіске рұқсат жоқ."), 405
        return render_template("error.html", code=405, title="Рұқсат етілмеген әрекет",
                               message="Бұл сұраныс түріне рұқсат жоқ."), 405

    @app.errorhandler(500)
    def server_error(_e):
        if _wants_json():
            return jsonify(error="Сервер қатесі. Кейінірек қайталаңыз."), 500
        return render_template("error.html", code=500, title="Сервер қатесі",
                               message="Күтпеген қате пайда болды. Кейінірек қайталап көріңіз."), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # әр 20 секундтағы жазбаларсыз
    application = create_app()
    # Бір процесс, reloader-сіз — еске салу жоспарлаушысы бір рет қана іске қосылады.
    application.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)),
                    debug=False, use_reloader=False)

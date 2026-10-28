"""Тіркелу, кіру, шығу және қорғалған беттерге арналған декоратор."""
import sqlite3
from functools import wraps

from flask import (Blueprint, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

import models
from database import get_db
from validators import validate_registration

bp = Blueprint("auth", __name__)


@bp.before_app_request
def load_logged_in_user():
    """Әр сұраныс алдында сессиядан ағымдағы пайдаланушыны жүктейді."""
    user_id = session.get("user_id")
    g.user = None
    g.settings = None
    if user_id is None or request.endpoint == "static":
        return
    db = get_db()
    g.user = models.get_user(db, user_id)
    if g.user is None:  # пайдаланушы жойылған — сессияны тазалаймыз
        session.clear()
        return
    g.settings = models.get_settings(db, user_id)


def login_required(view):
    """Тек жүйеге кірген пайдаланушыларға рұқсат береді."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Жүйеге кіру қажет."), 401
            flash("Жалғастыру үшін жүйеге кіріңіз.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _safe_next(target):
    """Open redirect-тен қорғау: тек осы сайттағы салыстырмалы жолдарға рұқсат."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _start_session(user, remember=False):
    session.clear()  # session fixation-нан қорғау
    session["user_id"] = user["id"]
    session.permanent = remember


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("main.dashboard"))
    data, errors = {}, {}
    if request.method == "POST":
        data, errors = validate_registration(request.form)
        db = get_db()
        if not errors and models.get_user_by_email(db, data["email"]):
            errors["email"] = "Бұл электрондық пошта бұрын тіркелген."
        if not errors:
            try:
                user_id = models.create_user(db, data["name"], data["email"], data["password"])
            except sqlite3.IntegrityError:
                errors["email"] = "Бұл электрондық пошта бұрын тіркелген."
            else:
                _start_session(models.get_user(db, user_id))
                flash(f"Тіркелу сәтті аяқталды! Қош келдіңіз, {data['name']}.", "success")
                return redirect(url_for("main.dashboard"))
        flash("Формада қателер бар. Тексеріп, қайталап көріңіз.", "danger")
    return render_template("register.html", data=data, errors=errors), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.dashboard"))
    email = ""
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not email or not password:
            error = "Электрондық пошта мен құпиясөзді енгізіңіз."
        else:
            user = models.verify_user(get_db(), email, password)
            if user is None:
                error = "Электрондық пошта немесе құпиясөз қате."
            else:
                _start_session(user, remember=bool(request.form.get("remember")))
                flash(f"Қайта оралуыңызбен, {user['name']}!", "success")
                return redirect(_safe_next(request.args.get("next")) or url_for("main.dashboard"))
    return render_template("login.html", email=email, error=error), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Сіз жүйеден шықтыңыз.", "info")
    return redirect(url_for("auth.login"))

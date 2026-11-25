#!/usr/bin/env python3
"""IT Stock Manager — in/out stock tracking for an office IT team.

A self-contained Flask + SQLite web app that runs anywhere Python 3.9+ runs,
including a Windows 11 PC without admin rights:

    python app.py            # serves on http://localhost:8077

Data is stored in a single stock.db file next to this script.
"""

import argparse
import csv
import io
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import time
import zipfile
from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import db as database
from db import PRESET_CATEGORIES

DEFAULT_PORT = 8077
PAGE_SIZE = 50

# Available on-shelf stock — condemned units stay registered but are not
# counted as usable inventory (dashboard, items list, low-stock, etc.).
UNITS_IN_SQL = (
    "(SELECT COUNT(*) FROM units u WHERE u.item_id = i.id "
    "AND u.status = 'in_stock' AND u.condemned = 0)"
)
UNITS_OUT_SQL = (
    "(SELECT COUNT(*) FROM units u WHERE u.item_id = i.id AND u.status = 'checked_out')"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get("STOCK_DB", os.path.join(BASE_DIR, "stock.db"))
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # Excel imports; reject oversized uploads
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("STOCK_COOKIE_SECURE", "").lower() \
    in ("1", "true", "yes")  # set when serving over HTTPS
# SMTP — dummy placeholders for now; override with STOCK_SMTP_* / STOCK_ADMIN_EMAIL.
app.config["SMTP_HOST"] = os.environ.get("STOCK_SMTP_HOST", "smtp.example.com")
app.config["SMTP_PORT"] = int(os.environ.get("STOCK_SMTP_PORT", "587"))
app.config["SMTP_USER"] = os.environ.get("STOCK_SMTP_USER", "stock-noreply@example.com")
app.config["SMTP_PASSWORD"] = os.environ.get("STOCK_SMTP_PASSWORD", "change-me")
app.config["SMTP_FROM"] = os.environ.get(
    "STOCK_SMTP_FROM", os.environ.get("STOCK_SMTP_USER", "stock-noreply@example.com")
)
app.config["SMTP_USE_TLS"] = os.environ.get("STOCK_SMTP_TLS", "1").lower() in (
    "1", "true", "yes",
)
app.config["ADMIN_EMAIL"] = os.environ.get("STOCK_ADMIN_EMAIL", "admin@example.com")

log = logging.getLogger("stock")


def _load_secret_key():
    """Persist a secret key on disk so sessions survive server restarts."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as fh:
            stored = fh.read().strip()
            if stored:
                return stored
    except OSError:
        pass
    generated = secrets.token_hex(32)
    try:
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write(generated)
    except OSError:
        pass
    return generated


app.secret_key = _load_secret_key()
# Trust the proxy's X-Forwarded-Proto so request.is_secure is right when the
# app is served over HTTPS (e.g. behind hosted TLS termination).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


class DynamicSecureCookieSession(SecureCookieSessionInterface):
    """Flag the session cookie Secure when the request arrived over HTTPS.

    The cookie is written after after_request hooks run, so the scheme must be
    checked here at save time. SESSION_COOKIE_SECURE still forces it on.
    """

    def get_cookie_secure(self, app):
        if app.config["SESSION_COOKIE_SECURE"]:
            return True
        return request.is_secure


app.session_interface = DynamicSecureCookieSession()
database.init_app(app)

with app.app_context():
    database.init_db(database.get_db())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def form_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralize spreadsheet formula injection in CSV exports (OWASP):
    a cell starting with =, +, -, @, tab or CR would be executed as a
    formula when opened in Excel/LibreOffice, so prefix it with a quote."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped and stripped[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_stocked_in(raw):
    """Parse a stocked-in date/time from a form. Returns 'YYYY-MM-DD HH:MM' or None."""
    text = (raw or "").strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                return dt.strftime("%Y-%m-%d 00:00")
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def send_mail(subject, body, to_addrs=None):
    """Send a plain-text email via the configured SMTP server.

    Returns (ok, detail). Dummy host/credentials are fine for wiring —
    failures are logged and returned so callers can flash a warning
    without rolling back the stock movement.
    """
    recipients = to_addrs or [app.config["ADMIN_EMAIL"]]
    recipients = [a for a in recipients if a]
    if not recipients:
        return False, "No admin email configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = app.config["SMTP_FROM"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    host = app.config["SMTP_HOST"]
    port = app.config["SMTP_PORT"]
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if app.config["SMTP_USE_TLS"]:
                smtp.starttls()
            user = app.config["SMTP_USER"]
            password = app.config["SMTP_PASSWORD"]
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True, f"Sent to {', '.join(recipients)}"
    except Exception as exc:  # noqa: BLE001 — surface any SMTP/network failure
        log.warning("SMTP send failed (%s:%s): %s", host, port, exc)
        return False, str(exc)


def _stock_out_admin_alert(ops, dept, ref_no, staff_id, notes, follow_up, username):
    """Email admin when a stock-out has no custodian."""
    lines = [
        "A stock-out was recorded without a custodian.",
        "",
        f"When:        {now_str()}",
        f"Operator:    {username or '(unknown)'}",
        f"Staff ID:    {staff_id or '(blank)'}",
        f"Department:  {dept or '(blank)'}",
        f"Ticket/ref:  {ref_no or '(blank)'}",
        f"Follow up:   {'Yes' if follow_up else 'No'}",
        f"Notes:       {notes or '(none)'}",
        "",
        "Items:",
    ]
    for op in ops:
        if op["kind"] == "unit":
            u = op["unit"]
            code = u["eam_no"] or u["serial_no"] or f"unit #{u['id']}"
            lines.append(f"  • {u['item_name']} ({code})")
        elif op["kind"] == "bulk":
            item = op.get("item")
            name = item["name"] if item is not None else f"item #{op['ref']}"
            lines.append(f"  • {name} × {op['qty']}")
        else:
            lines.append(f"  • {op.get('kind')} ref={op.get('ref')}")
    lines.extend([
        "",
        "(This message was sent automatically by IT Stock Manager.",
        "SMTP settings: STOCK_SMTP_HOST / STOCK_ADMIN_EMAIL.)",
    ])
    return send_mail(
        subject="Stock out with no custodian — follow up needed",
        body="\n".join(lines),
    )


def get_item_or_404(item_id):
    item = database.get_db().execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        abort(404)
    return item


def history_filters(args):
    """Build the WHERE clause shared by the history page and CSV export."""
    clauses, params = [], []
    tx_type = args.get("type", "").strip().upper()
    q = args.get("q", "").strip()
    date_from = args.get("date_from", "").strip()
    date_to = args.get("date_to", "").strip()
    if tx_type in ("IN", "OUT"):
        clauses.append("t.type = ?")
        params.append(tx_type)
    if q:
        like = f"%{q}%"
        clauses.append(
            "(i.name LIKE ? OR i.sku LIKE ? OR t.person LIKE ? OR t.staff_id LIKE ? "
            "OR t.department LIKE ? OR t.reference LIKE ? OR t.site LIKE ? "
            "OR u.eam_no LIKE ? OR u.serial_no LIKE ?)"
        )
        params += [like] * 9
    if date_from:
        clauses.append("t.created_at >= ?")
        params.append(date_from + " 00:00")
    if date_to:
        clauses.append("t.created_at <= ?")
        params.append(date_to + " 23:59")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, tx_type, q, date_from, date_to


# ---------------------------------------------------------------------------
# Request hooks
# ---------------------------------------------------------------------------

@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, description="Invalid form token. Reload the page and try again.")


AUTH_EXEMPT_ENDPOINTS = {"login", "static"}


@app.before_request
def require_login():
    if request.endpoint is None or request.endpoint in AUTH_EXEMPT_ENDPOINTS:
        return None
    uid = session.get("user_id")
    if uid:
        # Re-validate the session against the users table on every request:
        # a removed account, a changed password (session_version bump) or an
        # aged-out session all send the browser back to sign in.
        row = database.get_db().execute(
            "SELECT id, session_version FROM users WHERE id = ?", (uid,)
        ).fetchone()
        expired = time.time() - session.get("iat", 0) > SESSION_MAX_AGE
        if row is not None and row["session_version"] == session.get("sv") and not expired:
            return None
        session.clear()
        flash("Your session ended — please sign in again.", "error")
    if request.method != "GET":
        abort(401, description="Please sign in first.")
    return redirect(url_for("login", next=request.path))


@app.context_processor
def inject_globals():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return {"csrf_token": session["_csrf"]}


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'",
    )
    if request.is_secure:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


# ---------------------------------------------------------------------------
# Sign-in and accounts
# ---------------------------------------------------------------------------

# Brute-force throttle: 5 failures within 5 minutes locks the username for
# 2 minutes. In-memory is fine for a small office server; resets on restart.
_login_fails = {}
_FAIL_WINDOW_SEC = 300
_FAIL_LIMIT = 5
_LOCK_SEC = 120


def _login_locked_seconds(username):
    rec = _login_fails.get(username.lower())
    if rec and rec[2] > time.time():
        return int(rec[2] - time.time()) + 1
    return 0


def _record_login_fail(username):
    now = time.time()
    fails, first, _ = _login_fails.get(username.lower(), (0, now, 0))
    if now - first > _FAIL_WINDOW_SEC:
        fails, first = 0, now
    fails += 1
    locked_until = now + _LOCK_SEC if fails >= _FAIL_LIMIT else 0
    _login_fails[username.lower()] = (fails, first, locked_until)


def _validate_password(password, confirm):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if password != confirm:
        return "Passwords do not match."
    return None


def _validate_new_user(conn, username, password, confirm):
    if not username or len(username) > 40:
        return "Username is required (max 40 characters)."
    if conn.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone():
        return f"“{username}” already has a login."
    return _validate_password(password, confirm)


def _sign_in(user):
    session.clear()  # drop any pre-login session state (fixation)
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["sv"] = user["session_version"]
    session["iat"] = int(time.time())


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = database.get_db()
    setup = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
    if request.method == "POST":
        form = request.form
        username = form.get("username", "").strip()
        password = form.get("password", "")
        if setup:
            wait = _setup_locked_seconds()
            if wait:
                flash(f"Too many wrong setup codes — try again in {wait} seconds.", "error")
                return render_template("login.html", setup=True), 429
            if not secrets.compare_digest(form.get("setup_code", ""), SETUP_CODE):
                _record_setup_fail()
                flash("Wrong setup code. It is printed in the window where the app is "
                      "running, or set as STOCK_SETUP_CODE by whoever hosts it.", "error")
                return render_template("login.html", setup=True), 403
            err = _validate_new_user(conn, username, password, form.get("confirm", ""))
            if err:
                flash(err, "error")
                return render_template("login.html", setup=True), 400
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (username, generate_password_hash(password), now_str()),
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            _sign_in(user)
            flash(f"Account created — welcome, {user['username']}.", "success")
            return redirect(url_for("dashboard"))
        wait = _login_locked_seconds(username)
        if wait:
            flash(f"Too many failed attempts — try again in {wait} seconds.", "error")
            return render_template("login.html", setup=False), 429
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            _record_login_fail(username)
            flash("Wrong username or password.", "error")
            return render_template("login.html", setup=False), 401
        _login_fails.pop(username.lower(), None)
        _sign_in(user)
        nxt = request.args.get("next", "")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = url_for("dashboard")
        return redirect(nxt)
    return render_template("login.html", setup=setup)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    conn = database.get_db()
    users = conn.execute(
        "SELECT id, username, created_at FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    if request.method == "POST":
        form = request.form
        me = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        err = None
        if me is None or not check_password_hash(me["password_hash"], form.get("current_password", "")):
            err = "Current password is wrong."
        else:
            err = _validate_password(form.get("new_password", ""), form.get("confirm_password", ""))
        if err:
            flash(err, "error")
            return render_template("account.html", users=users), 400
        conn.execute(
            "UPDATE users SET password_hash = ?, session_version = session_version + 1 "
            "WHERE id = ?",
            (generate_password_hash(form.get("new_password", "")), me["id"]),
        )
        conn.commit()
        # The version bump kills every other browser signed in as this user;
        # adopt the new version here so the current session survives.
        session["sv"] = conn.execute(
            "SELECT session_version FROM users WHERE id = ?", (me["id"],)
        ).fetchone()["session_version"]
        flash("Password changed.", "success")
        return redirect(url_for("account"))
    return render_template("account.html", users=users)


@app.route("/account/users", methods=["POST"])
def add_user():
    form = request.form
    username = form.get("username", "").strip()
    conn = database.get_db()
    err = _validate_new_user(conn, username, form.get("password", ""), form.get("confirm", ""))
    if err:
        flash(err, "error")
        return redirect(url_for("account"))
    conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
        (username, generate_password_hash(form.get("password", "")), now_str()),
    )
    conn.commit()
    flash(f"Added a login for “{username}”.", "success")
    return redirect(url_for("account"))


@app.route("/account/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    conn = database.get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    if user_id == session.get("user_id"):
        flash("You can't delete the account you are signed in with.", "error")
        return redirect(url_for("account"))
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] <= 1:
        flash("You can't delete the last account.", "error")
        return redirect(url_for("account"))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    flash(f"Removed the login for “{user['username']}”.", "success")
    return redirect(url_for("account"))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    conn = database.get_db()
    totals = conn.execute(
        """SELECT (SELECT COUNT(*) FROM items) AS items,
                  ((SELECT COALESCE(SUM(quantity), 0) FROM items) +
                   (SELECT COUNT(*) FROM units WHERE status = 'in_stock'
                      AND condemned = 0)) AS units"""
    ).fetchone()
    low = conn.execute(
        f"""SELECT * FROM (
              SELECT i.*, (i.quantity + {UNITS_IN_SQL}) AS available FROM items i
            )
            WHERE min_quantity > 0 AND available <= min_quantity
            ORDER BY available ASC, name COLLATE NOCASE"""
    ).fetchall()
    month_prefix = datetime.now().strftime("%Y-%m") + "%"
    month_out = conn.execute(
        """SELECT COALESCE(SUM(quantity), 0) AS units, COUNT(*) AS moves
           FROM transactions
           WHERE type = 'OUT' AND created_at LIKE ?""",
        (month_prefix,),
    ).fetchone()
    recent = conn.execute(
        """SELECT t.*, i.name AS item_name, u.eam_no AS unit_eam, u.serial_no AS unit_serial
           FROM transactions t
           JOIN items i ON i.id = t.item_id
           LEFT JOIN units u ON u.id = t.unit_id
           ORDER BY t.created_at DESC, t.id DESC LIMIT 9"""
    ).fetchall()
    cat_rows = conn.execute(
        """SELECT category, COUNT(*) AS items, COALESCE(SUM(quantity), 0) AS bulk
           FROM items GROUP BY category"""
    ).fetchall()
    units_by_cat = {
        r["category"]: r["c"]
        for r in conn.execute(
            """SELECT i.category AS category, COUNT(*) AS c
               FROM units u JOIN items i ON i.id = u.item_id
               WHERE u.status = 'in_stock' AND u.condemned = 0
               GROUP BY i.category"""
        )
    }
    cats = [
        {
            "name": c["category"],
            "items": c["items"],
            "units": c["bulk"] + units_by_cat.get(c["category"], 0),
        }
        for c in cat_rows
    ]
    cats.sort(key=lambda c: (-c["units"], c["name"]))
    max_units = max((c["units"] for c in cats), default=0) or 1
    categories = [
        {**c, "pct": max(2, int(c["units"] * 100 / max_units)) if c["units"] else 0}
        for c in cats
    ]
    # Tracked products that already have at least one registered unit —
    # still listed when every unit is checked out (on_shelf = 0). Brand-new
    # item rows with no units yet stay off this list. Keyboards/mice that
    # have already been added (by SKU) appear too, counted by quantity.
    acc_skus = sorted(ACCESSORY_SKUS)
    acc_placeholders = ",".join("?" * len(acc_skus))
    on_shelf = conn.execute(
        f"""SELECT i.id, i.name, i.category, i.brand, i.model, i.sku,
                   CASE
                     WHEN UPPER(i.sku) IN ({acc_placeholders}) THEN i.quantity
                     ELSE {UNITS_IN_SQL}
                   END AS on_shelf
            FROM items i
            WHERE EXISTS (SELECT 1 FROM units u WHERE u.item_id = i.id)
               OR UPPER(i.sku) IN ({acc_placeholders})
            ORDER BY on_shelf DESC, i.name COLLATE NOCASE""",
        acc_skus + acc_skus,
    ).fetchall()
    return render_template(
        "dashboard.html",
        totals=totals,
        low=low,
        month_out=month_out,
        recent=recent,
        categories=categories,
        on_shelf=on_shelf,
    )


@app.route("/items")
def items():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    clauses, params = [], []
    if q:
        like = f"%{q}%"
        clauses.append(
            "(i.name LIKE ? OR i.sku LIKE ? OR i.brand LIKE ? OR i.model LIKE ? "
            "OR i.location LIKE ? OR i.id IN "
            "(SELECT item_id FROM units WHERE serial_no LIKE ? OR eam_no LIKE ?))"
        )
        params += [like] * 7
    if category:
        clauses.append("i.category = ?")
        params.append(category)
    sql = f"SELECT i.*, {UNITS_IN_SQL} AS units_in, {UNITS_OUT_SQL} AS units_out FROM items i"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY i.name COLLATE NOCASE"
    conn = database.get_db()
    rows = conn.execute(sql, params).fetchall()
    cats = [
        r["category"]
        for r in conn.execute("SELECT DISTINCT category FROM items ORDER BY category")
    ]
    return render_template("items.html", items=rows, q=q, category=category, cats=cats)


@app.route("/find")
def find_asset():
    """Quick-find from the nav: EAM / S/N → transaction history for that asset."""
    code = request.args.get("code", "").strip()
    if not code:
        flash("Scan or type an EAM or S/N to find an asset.", "error")
        return redirect(request.referrer or url_for("dashboard"))
    conn = database.get_db()
    unit = conn.execute(
        """SELECT u.id, u.item_id, u.eam_no, u.serial_no, i.name AS item_name
           FROM units u
           JOIN items i ON i.id = u.item_id
           WHERE (u.eam_no <> '' AND u.eam_no = ? COLLATE NOCASE)
              OR (u.serial_no <> '' AND u.serial_no = ? COLLATE NOCASE)
           LIMIT 1""",
        (code, code),
    ).fetchone()
    if unit is not None:
        # Prefer the canonical stored code so history search hits cleanly.
        lookup = unit["eam_no"] if unit["eam_no"] and unit["eam_no"].lower() == code.lower() \
            else (unit["serial_no"] or unit["eam_no"] or code)
        flash(
            f"Showing history for “{unit['item_name']}” "
            f"({unit['eam_no'] or '—'} / {unit['serial_no'] or '—'}).",
            "success",
        )
        return redirect(url_for("history", q=lookup))
    item = conn.execute(
        "SELECT id, name, sku FROM items WHERE sku = ? COLLATE NOCASE LIMIT 1",
        (code,),
    ).fetchone()
    if item is not None:
        flash(f"Showing history for “{item['name']}” (SKU {item['sku']}).", "success")
        return redirect(url_for("history", q=item["sku"] or code))
    flash(f"No asset found for “{code}”.", "error")
    return redirect(request.referrer or url_for("history"))


@app.route("/items/<int:item_id>")
def item_detail(item_id):
    conn = database.get_db()
    item = get_item_or_404(item_id)
    counts = conn.execute(
        """SELECT (SELECT COUNT(*) FROM units WHERE item_id = ? AND status = 'in_stock'
                     AND condemned = 0) AS units_in,
                  (SELECT COUNT(*) FROM units WHERE item_id = ? AND status = 'checked_out') AS units_out,
                  (SELECT COUNT(*) FROM units WHERE item_id = ? AND status = 'checked_out'
                     AND on_loan = 1) AS units_loan""",
        (item_id, item_id, item_id),
    ).fetchone()
    units = conn.execute(
        """SELECT u.*,
                  (SELECT t.person FROM transactions t
                   WHERE t.unit_id = u.id AND t.type = 'OUT'
                   ORDER BY t.created_at DESC, t.id DESC LIMIT 1) AS holder,
                  COALESCE(
                    (SELECT t.created_at FROM transactions t
                     WHERE t.unit_id = u.id AND t.type = 'IN'
                     ORDER BY t.created_at DESC, t.id DESC LIMIT 1),
                    u.created_at
                  ) AS stocked_in_at
           FROM units u WHERE u.item_id = ?
           ORDER BY u.status DESC, u.eam_no, u.serial_no""",
        (item_id,),
    ).fetchall()
    history = conn.execute(
        """SELECT t.*, u.eam_no AS unit_eam, u.serial_no AS unit_serial
           FROM transactions t
           LEFT JOIN units u ON u.id = t.unit_id
           WHERE t.item_id = ?
           ORDER BY t.created_at DESC, t.id DESC LIMIT 50""",
        (item_id,),
    ).fetchall()
    highlight_unit_id = form_int(request.args.get("unit"), default=0) or None
    if highlight_unit_id is not None:
        if not any(u["id"] == highlight_unit_id for u in units):
            highlight_unit_id = None
    return render_template(
        "item_detail.html",
        item=item,
        counts=counts,
        units=units,
        history=history,
        highlight_unit_id=highlight_unit_id,
    )


@app.route("/items/<int:item_id>/min-level", methods=["POST"])
def set_min_level(item_id):
    item = get_item_or_404(item_id)
    level = form_int(request.form.get("min_quantity"), default=-1)
    if level < 0 or level > 100000:
        flash("Warning level must be a number from 0 to 100000 (0 = no alert).", "error")
        return redirect(url_for("item_detail", item_id=item_id))
    conn = database.get_db()
    conn.execute(
        "UPDATE items SET min_quantity = ?, updated_at = ? WHERE id = ?",
        (level, now_str(), item_id),
    )
    conn.commit()
    if level:
        flash(f"Low-stock warning for “{item['name']}” set at {level}.", "success")
    else:
        flash(f"Low-stock warning for “{item['name']}” turned off.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):
    conn = database.get_db()
    item = get_item_or_404(item_id)
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    flash(f"Deleted “{item['name']}” and its transaction history.", "success")
    return redirect(url_for("items"))


# ---------------------------------------------------------------------------
# Excel / CSV import — one row = one unit (item, model, asset/EAM no., S/N)
# ---------------------------------------------------------------------------

IMPORT_ALIASES = {
    "item": {"item", "item name", "product", "product name", "equipment", "name", "description"},
    "model": {"model", "model no", "model name", "model number"},
    "eam": {
        "asset/eam no", "asset/eam no.", "asset eam no", "asset no", "asset no.",
        "asset number", "asset tag", "asset id", "asset", "eam", "eam no",
        "eam no.", "eam number", "tag no", "tag number",
    },
    "serial": {"s/n", "sn", "serial", "serial no", "serial no.", "serial number"},
    "brand": {"brand", "manufacturer", "make"},
    "funding": {"funding", "funding source", "fund"},
    "project": {"project", "project name"},
    "project_code": {"project code", "project no", "project number", "proj code"},
    "site": {"site", "site name", "storeroom"},
    "quantity": {"quantity", "qty", "qty.", "amount", "count", "quality"},
    # Yes = S/N or EAM required, Quantity blank. No = Quantity required, no codes.
    "tagged": {
        "tagged", "tagged item", "tagged items", "is tagged", "need tag",
        "needs tag", "tag required", "has tag", "track by tag",
    },
}

# Suggested Item values in the Excel template (users may also type their own).
IMPORT_ITEM_CHOICES = (
    "OA PC",
    "Ultra Slim PC",
    "iMac",
    "Laptop",
    "Monitor",
    "iPad",
    "Keyboard (Wired)",
    "Keyboard (Wireless)",
    "Mouse (Wired)",
    "Mouse (Wireless)",
    "LAN Cable (3 Metres)",
)

# Stock In “new product” category list (order kept in the dropdown).
STOCK_IN_CATEGORY_CHOICES = IMPORT_ITEM_CHOICES

# The two storerooms stock can be received into. Forms offer exactly these;
# the import maps "A"/"Site A" etc. onto them (blank in a file -> Site B).
SITES = ("Site A", "Site B")
_SITE_ALIASES = {"a": "Site A", "site a": "Site A", "b": "Site B", "site b": "Site B"}


def site_for(raw):
    """Normalize a site value from a form or an import cell; '' if unknown."""
    return _SITE_ALIASES.get((raw or "").strip().lower(), "")


IMPORT_TEMPLATE_HEADERS = [
    "Item", "Model", "Tagged", "S/N", "Asset/EAM No.", "Quantity",
    "Brand", "Funding", "Project", "Project Code", "Site",
]
# Example rows shipped in the Excel/CSV template (delete or replace when importing).
IMPORT_TEMPLATE_SAMPLE = [
]
IMPORT_MAX_ROWS = 5000
# An .xlsx is a zip: a small upload can expand enormously (zip bomb). Cap the
# number of uncompressed bytes the importer will read out of the archive.
XLSX_MAX_UNCOMPRESSED = 64 * 1024 * 1024
# Absolute session lifetime: a signed-in browser is asked to sign in again
# after this long, even if it keeps making requests.
SESSION_MAX_AGE = int(os.environ.get("STOCK_SESSION_MAX_AGE", 7 * 24 * 3600))
# The first account may only be created by whoever holds this code. When the
# env var is not set (local runs) a fresh code is generated and printed to
# the console on startup, so a brand-new deployment is never open to the
# first visitor who happens to reach it.
_env_setup_code = os.environ.get("STOCK_SETUP_CODE", "").strip()
SETUP_CODE = _env_setup_code or secrets.token_hex(4)
SETUP_CODE_FROM_ENV = bool(_env_setup_code)
_setup_fails = {"count": 0, "locked_until": 0.0}
SETUP_LOCK_AFTER = 10
SETUP_LOCK_SECONDS = 300


def _setup_locked_seconds():
    return max(int(_setup_fails["locked_until"] - time.time()), 0)


def _record_setup_fail():
    _setup_fails["count"] += 1
    if _setup_fails["count"] >= SETUP_LOCK_AFTER:
        _setup_fails["locked_until"] = time.time() + SETUP_LOCK_SECONDS
        _setup_fails["count"] = 0

# The Item column in the Excel file is the category. Common spellings map onto
# the preset categories; anything else is kept as typed (blank -> "Other").
CATEGORY_ALIASES = {c.lower(): c for c in PRESET_CATEGORIES}
CATEGORY_ALIASES.update({
    "pc": "PC", "laptop": "Laptop", "notebook": "Laptop",
    "desktop": "PC", "computer": "PC",
    "screen": "Monitor", "display": "Monitor", "monitor": "Monitor",
    "ipad": "iPad", "i-pad": "iPad",
    "headset": "Peripheral", "webcam": "Peripheral",
    "router": "Network", "switch": "Network", "access point": "Network",
    "ssd": "Storage", "hdd": "Storage", "hard drive": "Storage", "usb drive": "Storage",
    "hub": "Accessory", "dock": "Accessory", "docking station": "Accessory",
    "phone": "Mobile", "tablet": "Mobile",
    "lan cable": "LAN Cable (3 Metres)",
    "lan cable (3 metres)": "LAN Cable (3 Metres)",
    "lan cable (3 meters)": "LAN Cable (3 Metres)",
    "3 metres lan cable": "LAN Cable (3 Metres)",
    "3 meters lan cable": "LAN Cable (3 Metres)",
    "3m lan cable": "LAN Cable (3 Metres)",
    "lan": "LAN Cable (3 Metres)",
    "network cable": "LAN Cable (3 Metres)",
    "cat5": "LAN Cable (3 Metres)",
    "cat6": "LAN Cable (3 Metres)",
    "ethernet cable": "LAN Cable (3 Metres)",
})


def _category_for(raw):
    if not raw:
        return "Other"
    return CATEGORY_ALIASES.get(raw.strip().lower(), raw.strip())


def _accessory_from_item(item_raw, model_raw=""):
    """If Item is a KB/MS/LAN accessory row, return (kind, variant); else None.

    KB/MS: connection defaults to wired; mention wireless in Item or Model
    to pick the wireless SKU. LAN cable maps to the fixed 3-metre SKU.
    """
    item = (item_raw or "").strip()
    if not item:
        return None
    blob = f"{item} {model_raw or ''}"
    if _LAN_WORD.search(blob):
        key = ("lan_cable", "3m")
        return key if key in ACCESSORY_ITEMS else None
    if not _ACCESSORY_WORD.search(item):
        return None
    kind = "keyboard" if re.search(r"keyboards?", item, re.I) else "mouse"
    connection = "wireless" if re.search(r"wireless", blob, re.I) else "wired"
    if (kind, connection) not in ACCESSORY_ITEMS:
        return None
    return kind, connection


def _parse_import_qty(raw):
    """Return int quantity, 0 if blank, or None if the cell is not a whole number."""
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        # Excel may yield "5.0"
        num = float(text)
    except ValueError:
        return None
    if not num.is_integer() or num < 0:
        return None
    return int(num)


def _parse_import_tagged(raw):
    """Return True/False for Yes/No, None if blank, or 'invalid' if unrecognised."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in {"yes", "y", "true", "1", "tagged"}:
        return True
    if text in {"no", "n", "false", "0", "untagged", "n/a", "na", "none"}:
        return False
    return "invalid"


def _item_has_tagged_units(conn, item_id):
    """True when the product already has at least one S/N or EAM unit."""
    return conn.execute(
        """SELECT 1 FROM units
           WHERE item_id = ?
             AND ((serial_no IS NOT NULL AND serial_no <> '')
                  OR (eam_no IS NOT NULL AND eam_no <> ''))
           LIMIT 1""",
        (item_id,),
    ).fetchone() is not None


def _find_item_by_category_model(conn, category, model):
    return conn.execute(
        "SELECT * FROM items WHERE category = ? COLLATE NOCASE AND name = ? COLLATE NOCASE",
        (category, model),
    ).fetchone()


class ImportFileError(Exception):
    """The uploaded file cannot be read or is missing required columns."""


# Untagged accessories (no EAM/SN) — tracked by quantity only, one item per
# fixed SKU, added on the Import page. Scan pages move them by SKU code.
ACCESSORY_CATEGORY = "Peripheral"
ACCESSORY_ITEMS = {
    ("mouse", "wired"): ("Mouse (Wired)", "MS-WIRED"),
    ("mouse", "wireless"): ("Mouse (Wireless)", "MS-WIRELESS"),
    ("keyboard", "wired"): ("Keyboard (Wired)", "KB-WIRED"),
    ("keyboard", "wireless"): ("Keyboard (Wireless)", "KB-WIRELESS"),
    ("lan_cable", "3m"): ("LAN Cable (3 Metres)", "LAN-3M"),
}
ACCESSORY_SKUS = {sku for _name, sku in ACCESSORY_ITEMS.values()}
# Stock-in category labels → the same (kind, variant) keys as ACCESSORY_ITEMS.
ACCESSORY_CATEGORY_ALIASES = {
    "keyboard (wired)": ("keyboard", "wired"),
    "keyboard - wired": ("keyboard", "wired"),
    "keyboard — wired": ("keyboard", "wired"),
    "kb wired": ("keyboard", "wired"),
    "keyboard (wireless)": ("keyboard", "wireless"),
    "keyboard - wireless": ("keyboard", "wireless"),
    "keyboard — wireless": ("keyboard", "wireless"),
    "kb wireless": ("keyboard", "wireless"),
    "mouse (wired)": ("mouse", "wired"),
    "mouse - wired": ("mouse", "wired"),
    "mouse — wired": ("mouse", "wired"),
    "ms wired": ("mouse", "wired"),
    "mouse (wireless)": ("mouse", "wireless"),
    "mouse - wireless": ("mouse", "wireless"),
    "mouse — wireless": ("mouse", "wireless"),
    "ms wireless": ("mouse", "wireless"),
    "lan cable (3 metres)": ("lan_cable", "3m"),
    "lan cable (3 meters)": ("lan_cable", "3m"),
    "lan cable 3 metres": ("lan_cable", "3m"),
    "lan cable 3 meters": ("lan_cable", "3m"),
    "lan cable": ("lan_cable", "3m"),
    "3 metres lan cable": ("lan_cable", "3m"),
    "3 meters lan cable": ("lan_cable", "3m"),
    "3m lan cable": ("lan_cable", "3m"),
}
_ACCESSORY_WORD = re.compile(r"\b(keyboards?|mouse|mice)\b", re.IGNORECASE)
_LAN_WORD = re.compile(
    r"\b(lan\s*cables?|ethernet(?:\s*cables?)?|network\s*cables?|cat\s*[56]e?)\b",
    re.IGNORECASE,
)
ACCESSORY_MAX_QTY = 10000
# OA PC / desktop stock-out kit: one of each per PC scanned.
OA_PC_ACCESSORY_SKUS = ("KB-WIRED", "MS-WIRED", "LAN-3M")


def _accessory_kind_from_category(category):
    """Return (kind, variant) when category is a fixed accessory choice."""
    key = re.sub(r"\s+", " ", (category or "").strip().lower())
    return ACCESSORY_CATEGORY_ALIASES.get(key)


def _ensure_accessory_item(conn, kind, connection, brand="", now=None):
    """Find or create the fixed SKU item for an accessory variant."""
    if (kind, connection) not in ACCESSORY_ITEMS:
        raise ValueError("Unknown accessory type.")
    name, sku = ACCESSORY_ITEMS[(kind, connection)]
    now = now or now_str()
    brand = (brand or "").strip()
    item = conn.execute(
        "SELECT * FROM items WHERE sku = ? COLLATE NOCASE", (sku,)
    ).fetchone()
    if item is None:
        item = conn.execute(
            "SELECT * FROM items WHERE category = ? COLLATE NOCASE AND name = ? COLLATE NOCASE",
            (ACCESSORY_CATEGORY, name),
        ).fetchone()
    if item is None:
        cur = conn.execute(
            """INSERT INTO items
               (name, sku, category, brand, model, quantity, min_quantity,
                location, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,0,0,?,?,?,?)""",
            (name, sku, ACCESSORY_CATEGORY, brand, name, "", "", now, now),
        )
        return {
            "id": cur.lastrowid,
            "name": name,
            "sku": sku,
            "category": ACCESSORY_CATEGORY,
            "brand": brand,
            "model": name,
            "quantity": 0,
            "created": True,
        }
    item_id = item["id"]
    if not item["sku"]:
        conn.execute(
            "UPDATE items SET sku = ?, updated_at = ? WHERE id = ?",
            (sku, now, item_id),
        )
    if brand and not (item["brand"] or "").strip():
        conn.execute(
            "UPDATE items SET brand = ?, updated_at = ? WHERE id = ?",
            (brand, now, item_id),
        )
        item = dict(item)
        item["brand"] = brand
    return {
        "id": item_id,
        "name": item["name"],
        "sku": item["sku"] or sku,
        "category": item["category"] or ACCESSORY_CATEGORY,
        "brand": item["brand"] or brand,
        "model": item["model"] or name,
        "quantity": item["quantity"],
        "created": False,
    }


def _add_accessory_stock(conn, kind, connection, qty, *, reference="", site="Site B",
                         notes="", now=None):
    """Create/top-up an accessory item by quantity. Returns (name, sku)."""
    if (kind, connection) not in ACCESSORY_ITEMS:
        raise ValueError("Unknown accessory type.")
    if qty < 1 or qty > ACCESSORY_MAX_QTY:
        raise ValueError(f"Quantity must be between 1 and {ACCESSORY_MAX_QTY}.")
    name, sku = ACCESSORY_ITEMS[(kind, connection)]
    now = now or now_str()
    item = conn.execute(
        "SELECT * FROM items WHERE sku = ? COLLATE NOCASE", (sku,)
    ).fetchone()
    if item is None:
        item = conn.execute(
            "SELECT * FROM items WHERE category = ? COLLATE NOCASE AND name = ? COLLATE NOCASE",
            (ACCESSORY_CATEGORY, name),
        ).fetchone()
    if item is None:
        cur = conn.execute(
            """INSERT INTO items
               (name, sku, category, brand, model, quantity, min_quantity,
                location, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,0,0,?,?,?,?)""",
            (name, sku, ACCESSORY_CATEGORY, "", name, "", "", now, now),
        )
        item_id = cur.lastrowid
    else:
        item_id = item["id"]
        if not item["sku"]:
            conn.execute(
                "UPDATE items SET sku = ?, updated_at = ? WHERE id = ?",
                (sku, now, item_id),
            )
    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (qty, now, item_id),
    )
    conn.execute(
        """INSERT INTO transactions
           (item_id, unit_id, type, quantity, person, department, reference, site, notes, created_at)
           VALUES (?,NULL,'IN',?,?,'',?,?,?,?)""",
        (item_id, qty, "", reference, site,
         notes or "Quantity added on the Import page", now),
    )
    return name, sku


def _record_accessory_add(conn, form):
    """Quantity top-up for an untagged accessory item (Import page)."""
    kind = form.get("accessory_type", "").strip().lower()
    connection = form.get("connection", "").strip().lower()
    if kind == "lan_cable":
        connection = "3m"
    if (kind, connection) not in ACCESSORY_ITEMS:
        flash(
            "Choose the type (mouse, keyboard, or LAN cable) and connection "
            "(wired or wireless for KB/MS).",
            "error",
        )
        return redirect(url_for("import_stocks"))
    qty = form_int(form.get("qty"), default=0)
    if qty < 1 or qty > ACCESSORY_MAX_QTY:
        flash(f"Quantity must be between 1 and {ACCESSORY_MAX_QTY}.", "error")
        return redirect(url_for("import_stocks"))
    reference = form.get("reference", "").strip()
    site = site_for(form.get("site")) or "Site B"
    notes = form.get("notes", "").strip()
    try:
        name, sku = _add_accessory_stock(
            conn, kind, connection, qty, reference=reference, site=site, notes=notes,
        )
        conn.commit()
    except (sqlite3.Error, ValueError):
        conn.rollback()
        flash("Could not save the quantity. Please try again.", "error")
        return redirect(url_for("import_stocks"))
    flash(f"Added {qty} × {name}. Scan or type {sku} on the Stock In / Out pages "
          "to move it later.", "success")
    return redirect(url_for("import_stocks"))


def _norm_header(value):
    h = re.sub(r"[._]", " ", str(value or "").strip().lower())
    return re.sub(r"\s+", " ", h).strip()


def _cell_str(value):
    """Excel may hand codes back as numbers; serials are identifiers, so
    render them without a trailing '.0'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _map_import_rows(rows, limit=IMPORT_MAX_ROWS):
    """First non-empty row is the header; map it to item/model/eam/serial
    fields via IMPORT_ALIASES and yield (row_no, row_dict) for each data row.
    Every scanned row — blank ones included — counts against the row cap, so
    padding a file with empty rows can't keep the parser busy for free."""
    header = None
    scanned = 0
    for row_no, cells in enumerate(rows, start=1):
        scanned += 1
        if scanned > limit:
            raise ImportFileError(
                f"That file has more than {limit} rows — split it into smaller files."
            )
        if header is None:
            if not any(cells):
                continue
            mapping = {}
            for idx, cell in enumerate(cells):
                h = _norm_header(cell)
                for field, aliases in IMPORT_ALIASES.items():
                    if h in aliases and field not in mapping.values():
                        mapping[idx] = field
            fields = set(mapping.values())
            # Item is always required. Tagged Yes rows need S/N or EAM; Tagged No
            # (and Keyboard/Mouse) rows need Quantity — a file may mix both.
            if "item" not in fields or not ({"eam", "serial", "quantity", "tagged"} & fields):
                raise ImportFileError(
                    "Could not find the required columns. The first row must be a header "
                    "with Item, plus Tagged (Yes/No), and/or S/N / Asset/EAM No. for tagged "
                    "units, and/or Quantity for untagged rows."
                )
            header = mapping
            continue
        values = {field: (cells[idx] if idx < len(cells) else "") for idx, field in header.items()}
        if not any(values.values()):
            continue
        yield row_no, {
            "item": values.get("item", ""),
            "model": values.get("model", ""),
            "tagged": values.get("tagged", ""),
            "eam": values.get("eam", ""),
            "serial": values.get("serial", ""),
            "quantity": values.get("quantity", ""),
            "brand": values.get("brand", ""),
            "funding": values.get("funding", ""),
            "project": values.get("project", ""),
            "project_code": values.get("project_code", ""),
            "site": values.get("site", ""),
        }


def _sanitize_xlsx(stream):
    """Re-pack an uploaded .xlsx (a zip) into a clean in-memory archive while
    counting the bytes that actually come out of the decompressor. The size
    fields in a zip's directory are attacker-controlled, so the cap is
    enforced on real decompressed output, not on what the file claims."""
    try:
        src = zipfile.ZipFile(stream)
    except zipfile.BadZipFile as exc:
        raise ImportFileError("That does not look like a valid .xlsx file.") from exc
    out = io.BytesIO()
    total = 0
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.is_dir():
                    continue
                chunks = []
                with src.open(info) as member:
                    while True:
                        chunk = member.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > XLSX_MAX_UNCOMPRESSED:
                            raise ImportFileError(
                                "That Excel file expands to more than 64 MB — "
                                "split it into smaller files."
                            )
                        chunks.append(chunk)
                dst.writestr(info.filename, b"".join(chunks))
    except ImportFileError:
        raise
    except Exception as exc:
        raise ImportFileError("That does not look like a valid .xlsx file.") from exc
    out.seek(0)
    return out


def _read_import_rows(filename, stream):
    if filename.lower().endswith(".xlsx"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ImportFileError(
                "Excel support is missing on this PC — run: pip install -r requirements.txt"
            ) from exc
        try:
            wb = load_workbook(_sanitize_xlsx(stream), read_only=True, data_only=True)
        except ImportFileError:
            raise
        except Exception as exc:
            raise ImportFileError("That does not look like a valid .xlsx file.") from exc
        rows = ([_cell_str(c) for c in row] for row in wb.active.iter_rows(values_only=True))
        yield from _map_import_rows(rows)
    elif filename.lower().endswith(".csv"):
        text = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace")
        yield from _map_import_rows(([c.strip() for c in r] for r in csv.reader(text)))
    else:
        raise ImportFileError("Unsupported file type — upload a .xlsx or .csv file.")


@app.route("/import", methods=["GET", "POST"])
def import_stocks():
    if request.method == "POST":
        if request.form.get("mode") == "accessory":
            return _record_accessory_add(database.get_db(), request.form)
        upload = request.files.get("file")
        reference = request.form.get("reference", "").strip()
        if not upload or not upload.filename:
            flash("Choose an Excel (.xlsx) or CSV file to import.", "error")
            return render_template("import.html", report=None), 400
        try:
            parsed = list(_read_import_rows(upload.filename, upload.stream))
        except ImportFileError as exc:
            flash(str(exc), "error")
            return render_template("import.html", report=None), 400
        if not parsed:
            flash("The file has no data rows below the header.", "error")
            return render_template("import.html", report=None), 400

        conn = database.get_db()
        now = now_str()
        report, seen_codes, item_cache = [], {}, {}

        def item_id_for(category, model, brand):
            """Items are found or created per (category, model) pair; the
            model is the item's display name — the Excel Item column is the
            category, not the product name. A Brand column in the file fills
            the item's brand (existing brands are never overwritten)."""
            key = (category.lower(), model.lower())
            if key in item_cache:
                return item_cache[key]
            row = conn.execute(
                "SELECT id, brand FROM items WHERE category = ? COLLATE NOCASE AND name = ? COLLATE NOCASE",
                (category, model),
            ).fetchone()
            if row:
                if brand and not row["brand"]:
                    conn.execute(
                        "UPDATE items SET brand = ?, updated_at = ? WHERE id = ?",
                        (brand, now, row["id"]),
                    )
                item_cache[key] = (row["id"], False)
                return row["id"], False
            cur = conn.execute(
                """INSERT INTO items
                   (name, sku, category, brand, model, quantity, min_quantity,
                    location, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,0,0,?,?,?,?)""",
                (model, "", category, brand, model, "", "", now, now),
            )
            item_cache[key] = (cur.lastrowid, True)
            return cur.lastrowid, True

        try:
            for row_no, row in parsed:
                entry = {"row": row_no, **row}
                model, eam, serial = row["model"], row["eam"], row["serial"]
                site = site_for(row["site"])
                qty = _parse_import_qty(row.get("quantity", ""))
                tagged_flag = _parse_import_tagged(row.get("tagged", ""))
                accessory = _accessory_from_item(row["item"], model)

                # Blank Tagged: Keyboard/Mouse stay quantity-based; everything
                # else defaults to tagged (S/N or EAM) for older templates.
                if tagged_flag is True:
                    is_tagged = True
                elif tagged_flag is False:
                    is_tagged = False
                elif accessory:
                    is_tagged = False
                else:
                    is_tagged = True

                if tagged_flag == "invalid":
                    entry.update(
                        result="error",
                        detail="Tagged must be Yes or No",
                    )
                elif qty is None:
                    entry.update(result="error", detail="Quantity must be a whole number")
                elif not is_tagged:
                    # Untagged — Quantity required; S/N / EAM not allowed.
                    if eam or serial:
                        entry.update(
                            result="error",
                            detail="Tagged=No rows must leave S/N and Asset/EAM blank — "
                                   "use Quantity only",
                        )
                    elif qty < 1:
                        entry.update(
                            result="error",
                            detail="Tagged=No rows need a Quantity of at least 1",
                        )
                    elif qty > ACCESSORY_MAX_QTY:
                        entry.update(
                            result="error",
                            detail=f"Quantity cannot exceed {ACCESSORY_MAX_QTY}",
                        )
                    elif row["site"].strip() and not site:
                        entry.update(
                            result="error",
                            detail=f"Unknown site “{row['site'].strip()}” — use Site A or Site B",
                        )
                    elif accessory:
                        kind, connection = accessory
                        site = site or "Site B"
                        name, sku = _add_accessory_stock(
                            conn, kind, connection, qty,
                            reference=reference, site=site, notes="Excel import", now=now,
                        )
                        entry.update(
                            result="imported",
                            detail=f"Added {qty} × {name} ({sku})",
                        )
                    elif not model:
                        entry.update(result="error", detail="Missing model")
                    else:
                        site = site or "Site B"
                        category = _category_for(row["item"])
                        existing_item = _find_item_by_category_model(conn, category, model)
                        if existing_item and _item_has_tagged_units(conn, existing_item["id"]):
                            entry.update(
                                result="error",
                                detail="This product already has tagged units (S/N or EAM) "
                                       "in stock — cannot import as untagged (Tagged=No)",
                            )
                        else:
                            item_id, created = item_id_for(category, model, row["brand"])
                            conn.execute(
                                "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                                (qty, now, item_id),
                            )
                            conn.execute(
                                """INSERT INTO transactions
                                   (item_id, unit_id, type, quantity, person, department,
                                    reference, site, notes, created_at)
                                   VALUES (?,NULL,'IN',?,'','',?,?,'Excel import',?)""",
                                (item_id, qty, reference, site, now),
                            )
                            entry.update(
                                result="imported",
                                detail=(
                                    f"Added {qty} untagged"
                                    + (" — new item created" if created else "")
                                ),
                            )
                else:
                    # Tagged — S/N or EAM required; Quantity must be blank.
                    if qty > 0:
                        entry.update(
                            result="error",
                            detail="Tagged=Yes rows cannot have a Quantity — leave it blank "
                                   "and provide S/N and/or Asset/EAM No.",
                        )
                    elif not model:
                        entry.update(result="error", detail="Missing model")
                    elif not eam and not serial:
                        entry.update(
                            result="error",
                            detail="Tagged=Yes needs an S/N or an Asset/EAM no. "
                                   "(either one — EAM can be blank)",
                        )
                    elif row["site"].strip() and not site:
                        entry.update(
                            result="error",
                            detail=f"Unknown site “{row['site'].strip()}” — use Site A or Site B",
                        )
                    elif any(c and c.lower() in seen_codes for c in (eam, serial)):
                        dup = next(c for c in (eam, serial) if c and c.lower() in seen_codes)
                        entry.update(
                            result="skipped",
                            detail=f"“{dup}” already appears on row {seen_codes[dup.lower()]}",
                        )
                    else:
                        existing, resolve_err = _resolve_import_unit(conn, serial, eam)
                        if resolve_err:
                            entry.update(result="error", detail=resolve_err)
                        elif existing is not None:
                            # Only apply site when the file actually named one —
                            # blank site on a re-import must not force Site B.
                            result, detail = _apply_import_unit_update(
                                conn, existing, serial, eam, site,
                                row["funding"], row["project"], row["project_code"],
                                row["brand"], now,
                            )
                            entry.update(result=result, detail=detail)
                            if result == "updated":
                                for c in (eam, serial):
                                    if c:
                                        seen_codes[c.lower()] = row_no
                        else:
                            site = site or "Site B"
                            category = _category_for(row["item"])
                            item_id, created = item_id_for(category, model, row["brand"])
                            cur = conn.execute(
                                """INSERT INTO units
                                   (item_id, serial_no, eam_no, status, site, funding, project,
                                    project_code, created_at, updated_at)
                                   VALUES (?,?,?,'in_stock',?,?,?,?,?,?)""",
                                (item_id, serial, eam, site, row["funding"], row["project"],
                                 row["project_code"], now, now),
                            )
                            conn.execute(
                                """INSERT INTO transactions
                                   (item_id, unit_id, type, quantity, person, department,
                                    reference, site, notes, created_at)
                                   VALUES (?,?,'IN',1,'','',?,?,'Excel import',?)""",
                                (item_id, cur.lastrowid, reference, site, now),
                            )
                            for c in (eam, serial):
                                if c:
                                    seen_codes[c.lower()] = row_no
                            entry.update(
                                result="imported",
                                detail="New item created" if created else "Added to existing item",
                            )
                report.append(entry)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            flash("Could not save the import. Please try again.", "error")
            return render_template("import.html", report=None), 500

        counts = {
            "imported": sum(1 for e in report if e["result"] == "imported"),
            "updated": sum(1 for e in report if e["result"] == "updated"),
            "skipped": sum(1 for e in report if e["result"] == "skipped"),
            "errors": sum(1 for e in report if e["result"] == "error"),
        }
        parts = []
        if counts["imported"]:
            n = counts["imported"]
            parts.append(f"Imported {n} unit{'s' if n != 1 else ''}")
        if counts["updated"]:
            n = counts["updated"]
            parts.append(f"updated {n}")
        if parts:
            flash(f"{'; '.join(parts)}.", "success")
        return render_template("import.html", report=report, counts=counts)
    return render_template("import.html", report=None)


@app.route("/import/template.xlsx")
def import_template_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.comments import Comment
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        abort(500, description="Excel support is missing — run: pip install -r requirements.txt")
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(IMPORT_TEMPLATE_HEADERS)
    for row in IMPORT_TEMPLATE_SAMPLE:
        ws.append(row)

    # Hidden Lists sheet — Excel dropdowns are more reliable when they
    # reference cells instead of an inline comma list.
    lists = wb.create_sheet("Lists")
    for i, name in enumerate(IMPORT_ITEM_CHOICES, start=1):
        lists.cell(row=i, column=1, value=name)
    lists.cell(row=1, column=2, value="Yes")
    lists.cell(row=2, column=2, value="No")
    lists.sheet_state = "hidden"

    dv = DataValidation(
        type="list",
        formula1=f"=Lists!$A$1:$A${len(IMPORT_ITEM_CHOICES)}",
        allow_blank=True,
        showErrorMessage=False,  # custom Item text is allowed
        showDropDown=False,      # openpyxl: False = show the arrow in Excel
    )
    dv.prompt = "Choose PC, Laptop, Monitor, iPad, Keyboard or Mouse — or type your own"
    dv.promptTitle = "Item"
    ws.add_data_validation(dv)
    dv.add("A2:A5000")

    dv_tagged = DataValidation(
        type="list",
        formula1="=Lists!$B$1:$B$2",
        allow_blank=True,
        showErrorMessage=True,
        showDropDown=False,
        errorTitle="Tagged",
        error="Use Yes (S/N or EAM) or No (Quantity only).",
    )
    dv_tagged.prompt = "Yes = S/N or EAM, no Quantity. No = Quantity only, no codes."
    dv_tagged.promptTitle = "Tagged"
    ws.add_data_validation(dv_tagged)
    dv_tagged.add("C2:C5000")

    header_fill = PatternFill("solid", fgColor="EEF2FF")
    header_font = Font(bold=True)
    for col_idx, title in enumerate(IMPORT_TEMPLATE_HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
    ws["A1"].comment = Comment(
        "Dropdown: PC, Laptop, Monitor, iPad, Keyboard, Mouse — or type any other item name.",
        "IT Stock Manager",
    )
    ws["C1"].comment = Comment(
        "Yes = tracked by S/N or EAM (leave Quantity blank). "
        "No = untagged quantity stock (leave S/N and EAM blank). "
        "Rejected if this product already has tagged units in the database.",
        "IT Stock Manager",
    )
    ws["F1"].comment = Comment(
        "Required when Tagged=No (or Keyboard/Mouse). Leave blank when Tagged=Yes.",
        "IT Stock Manager",
    )
    widths = (14, 18, 10, 16, 16, 10, 14, 16, 16, 14, 10)
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=stock-import-template.xlsx",
            "Cache-Control": "no-store",
        },
    )


@app.route("/import/template.csv")
def import_template_csv():
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(IMPORT_TEMPLATE_HEADERS)
    writer.writerows(IMPORT_TEMPLATE_SAMPLE)
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=stock-import-template.csv",
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Individual units (tracked assets with S/N + EAM)
# ---------------------------------------------------------------------------

def get_unit_or_404(unit_id):
    unit = database.get_db().execute(
        """SELECT u.*, i.name AS item_name FROM units u
           JOIN items i ON i.id = u.item_id WHERE u.id = ?""",
        (unit_id,),
    ).fetchone()
    if unit is None:
        abort(404)
    return unit


def code_clash(conn, serial, eam, exclude_unit_id=None):
    """Return an existing unit holding either code (case-insensitive), if any.

    The OR of the two code matches must be parenthesized so an exclude_unit_id
    filter applies to both — otherwise AND binds tighter than OR and editing a
    unit's own S/N (unchanged) falsely looks like a clash with itself.
    """
    sql = """SELECT id FROM units
             WHERE ((serial_no <> '' AND serial_no = ? COLLATE NOCASE)
                 OR (eam_no <> '' AND eam_no = ? COLLATE NOCASE))"""
    params = [serial, eam]
    if exclude_unit_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_unit_id)
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _unit_by_serial(conn, serial):
    if not serial:
        return None
    return conn.execute(
        "SELECT * FROM units WHERE serial_no <> '' AND serial_no = ? COLLATE NOCASE",
        (serial,),
    ).fetchone()


def _unit_by_eam(conn, eam):
    if not eam:
        return None
    return conn.execute(
        "SELECT * FROM units WHERE eam_no <> '' AND eam_no = ? COLLATE NOCASE",
        (eam,),
    ).fetchone()


def _resolve_import_unit(conn, serial, eam):
    """Match an import row to an existing unit by S/N and/or EAM.

    Returns (unit_or_None, error_detail_or_None). A conflict (S/N and EAM
    belonging to two different units) yields an error string.
    """
    by_serial = _unit_by_serial(conn, serial)
    by_eam = _unit_by_eam(conn, eam)
    if by_serial is not None and by_eam is not None and by_serial["id"] != by_eam["id"]:
        return None, (
            f"S/N matches unit #{by_serial['id']} but Asset/EAM matches "
            f"unit #{by_eam['id']} — fix the file before re-importing"
        )
    return by_serial or by_eam, None


def _fill_blank(current, incoming):
    """Keep an existing non-empty value; otherwise take the import value."""
    return current if (current or "").strip() else (incoming or "")


def _apply_provided(current, incoming):
    """Use the import value when the cell is non-empty; otherwise keep current."""
    incoming = (incoming or "").strip()
    return incoming if incoming else (current or "")


def _apply_import_unit_update(conn, unit, serial, eam, site, funding, project,
                              project_code, brand, now):
    """Update a matched unit from an import row.

    S/N and EAM only fill blanks (never overwrite an existing code).
    Funding, project and project code are replaced whenever the file
    provides a value. Site and brand still fill blanks only. Returns
    (result, detail) where result is 'updated', 'skipped' or 'error'.
    """
    changes = []
    new_serial = unit["serial_no"] or ""
    new_eam = unit["eam_no"] or ""

    if serial:
        if not new_serial:
            # Ensure the incoming S/N is not already on another unit.
            clash = _unit_by_serial(conn, serial)
            if clash is not None and clash["id"] != unit["id"]:
                return "error", f"S/N “{serial}” is already on another unit"
            new_serial = serial
            changes.append(f"S/N set to {serial}")
        elif new_serial.lower() != serial.lower():
            return "error", (
                f"This unit already has S/N “{new_serial}” — "
                f"cannot change it to “{serial}” via import"
            )

    if eam:
        if not new_eam:
            clash = _unit_by_eam(conn, eam)
            if clash is not None and clash["id"] != unit["id"]:
                return "error", f"Asset/EAM “{eam}” is already on another unit"
            new_eam = eam
            changes.append(f"EAM set to {eam}")
        elif new_eam.lower() != eam.lower():
            return "error", (
                f"This unit already has EAM “{new_eam}” — "
                f"cannot change it to “{eam}” via import"
            )

    new_site = _fill_blank(unit["site"], site)
    new_funding = _apply_provided(unit["funding"], funding)
    new_project = _apply_provided(unit["project"], project)
    new_project_code = _apply_provided(unit["project_code"], project_code)

    if new_site != (unit["site"] or "") and site:
        changes.append(f"site set to {new_site}")
    if (funding or "").strip() and new_funding != (unit["funding"] or ""):
        changes.append(f"funding → {new_funding}")
    if (project or "").strip() and new_project != (unit["project"] or ""):
        changes.append(f"project → {new_project}")
    if (project_code or "").strip() and new_project_code != (unit["project_code"] or ""):
        changes.append(f"project code → {new_project_code}")

    # Fill blank brand on the parent item (never overwrite).
    if brand:
        item = conn.execute(
            "SELECT id, brand FROM items WHERE id = ?", (unit["item_id"],)
        ).fetchone()
        if item is not None and not (item["brand"] or "").strip():
            conn.execute(
                "UPDATE items SET brand = ?, updated_at = ? WHERE id = ?",
                (brand, now, item["id"]),
            )
            changes.append("brand filled in")

    if not changes:
        return "skipped", "Already registered — nothing new to update"

    conn.execute(
        """UPDATE units SET serial_no = ?, eam_no = ?, site = ?, funding = ?,
           project = ?, project_code = ?, updated_at = ? WHERE id = ?""",
        (new_serial, new_eam, new_site, new_funding, new_project,
         new_project_code, now, unit["id"]),
    )
    return "updated", "; ".join(changes)


@app.route("/items/<int:item_id>/units/new", methods=["GET", "POST"])
def new_unit(item_id):
    item = get_item_or_404(item_id)
    conn = database.get_db()
    if request.method == "POST":
        form = request.form
        serial = form.get("serial_no", "").strip()
        eam = form.get("eam_no", "").strip()
        site = site_for(form.get("site")) or "Site A"
        funding = form.get("funding", "").strip()
        project = form.get("project", "").strip()
        project_code = form.get("project_code", "").strip()
        notes = form.get("notes", "").strip()
        if not serial and not eam:
            flash("Give the unit at least an S/N or an EAM number.", "error")
            return render_template("unit_form.html", item=item, unit=None, form=form), 400
        if code_clash(conn, serial, eam):
            flash("That S/N or EAM number is already registered to another unit.", "error")
            return render_template("unit_form.html", item=item, unit=None, form=form), 400
        now = now_str()
        try:
            cur = conn.execute(
                """INSERT INTO units
                   (item_id, serial_no, eam_no, status, site, funding, project, project_code,
                    notes, created_at, updated_at)
                   VALUES (?,?,?,'in_stock',?,?,?,?,?,?,?)""",
                (item_id, serial, eam, site, funding, project, project_code, notes, now, now),
            )
            conn.execute(
                """INSERT INTO transactions
                   (item_id, unit_id, type, quantity, person, department, reference, site, notes, created_at)
                   VALUES (?,?,'IN',1,'','','REGISTER',?,'Unit registered',?)""",
                (item_id, cur.lastrowid, site, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("That S/N or EAM number is already registered to another unit.", "error")
            return render_template("unit_form.html", item=item, unit=None, form=form), 400
        flash(f"Registered unit {eam or serial} for “{item['name']}”.", "success")
        return redirect(url_for("item_detail", item_id=item_id))
    return render_template("unit_form.html", item=item, unit=None, form={})


def _unit_stocked_in_at(conn, unit_id, fallback=""):
    row = conn.execute(
        """SELECT created_at FROM transactions
           WHERE unit_id = ? AND type = 'IN'
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (unit_id,),
    ).fetchone()
    if row is not None:
        return row["created_at"]
    return fallback or ""


@app.route("/units/<int:unit_id>/edit", methods=["GET", "POST"])
def edit_unit(unit_id):
    unit = get_unit_or_404(unit_id)
    conn = database.get_db()
    stocked_in_at = _unit_stocked_in_at(conn, unit_id, unit["created_at"])
    if request.method == "POST":
        form = request.form
        serial = form.get("serial_no", "").strip()
        eam = form.get("eam_no", "").strip()
        site = site_for(form.get("site")) or unit["site"] or "Site A"
        funding = form.get("funding", "").strip()
        project = form.get("project", "").strip()
        project_code = form.get("project_code", "").strip()
        notes = form.get("notes", "").strip()
        stocked_in = parse_stocked_in(form.get("stocked_in_at"))
        if not serial and not eam:
            flash("Give the unit at least an S/N or an EAM number.", "error")
            return render_template(
                "unit_form.html", item=None, unit=unit, form=form,
                stocked_in_at=stocked_in_at,
            ), 400
        if form.get("stocked_in_at", "").strip() and stocked_in is None:
            flash("Stocked-in date must look like YYYY-MM-DD or YYYY-MM-DD HH:MM.", "error")
            return render_template(
                "unit_form.html", item=None, unit=unit, form=form,
                stocked_in_at=stocked_in_at,
            ), 400
        if code_clash(conn, serial, eam, exclude_unit_id=unit_id):
            flash("That S/N or EAM number is already registered to another unit.", "error")
            return render_template(
                "unit_form.html", item=None, unit=unit, form=form,
                stocked_in_at=stocked_in_at,
            ), 400
        conn.execute(
            """UPDATE units SET serial_no = ?, eam_no = ?, site = ?, funding = ?, project = ?,
               project_code = ?, notes = ?, updated_at = ? WHERE id = ?""",
            (serial, eam, site, funding, project, project_code, notes, now_str(), unit_id),
        )
        # Temporary: allow correcting the stocked-in date shown on Items.
        if stocked_in:
            in_tx = conn.execute(
                """SELECT id FROM transactions
                   WHERE unit_id = ? AND type = 'IN'
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (unit_id,),
            ).fetchone()
            if in_tx is not None:
                conn.execute(
                    "UPDATE transactions SET created_at = ? WHERE id = ?",
                    (stocked_in, in_tx["id"]),
                )
            conn.execute(
                "UPDATE units SET created_at = ? WHERE id = ?",
                (stocked_in, unit_id),
            )
        conn.commit()
        flash(f"Saved changes to unit {eam or serial}.", "success")
        return redirect(url_for("item_detail", item_id=unit["item_id"]))
    return render_template(
        "unit_form.html", item=None, unit=unit, form={},
        stocked_in_at=stocked_in_at,
    )


@app.route("/units/<int:unit_id>/delete", methods=["POST"])
def delete_unit(unit_id):
    unit = get_unit_or_404(unit_id)
    conn = database.get_db()
    conn.execute("DELETE FROM units WHERE id = ?", (unit_id,))
    conn.commit()
    flash(f"Deleted unit {unit['eam_no'] or unit['serial_no']}. Its past movements stay in history.", "success")
    return redirect(url_for("item_detail", item_id=unit["item_id"]))


# ---------------------------------------------------------------------------
# Packages (stock-out kit checklists)
# ---------------------------------------------------------------------------

PACKAGE_MATCH_TYPES = ("category", "sku", "item")
ACCESSORY_SKU_LABELS = {
    "MS-WIRED": "Mouse (Wired)",
    "MS-WIRELESS": "Mouse (Wireless)",
    "KB-WIRED": "Keyboard (Wired)",
    "KB-WIRELESS": "Keyboard (Wireless)",
    "LAN-3M": "LAN Cable (3 Metres)",
}


def _package_or_404(package_id):
    pkg = database.get_db().execute(
        "SELECT * FROM packages WHERE id = ?", (package_id,)
    ).fetchone()
    if pkg is None:
        abort(404)
    return pkg


def _package_lines(conn, package_id):
    return conn.execute(
        """SELECT * FROM package_lines WHERE package_id = ?
           ORDER BY sort_order, id""",
        (package_id,),
    ).fetchall()


def _packages_payload(conn):
    """JSON-friendly package list for the stock-out checklist."""
    pkgs = conn.execute(
        "SELECT * FROM packages ORDER BY name COLLATE NOCASE"
    ).fetchall()
    out = []
    for p in pkgs:
        lines = [
            {
                "id": ln["id"],
                "label": ln["label"],
                "match_type": ln["match_type"],
                "match_value": ln["match_value"],
                "quantity": ln["quantity"],
            }
            for ln in _package_lines(conn, p["id"])
        ]
        out.append({"id": p["id"], "name": p["name"], "notes": p["notes"], "lines": lines})
    return out


def _package_form_options(conn):
    cats = {
        r["category"]
        for r in conn.execute(
            "SELECT DISTINCT category FROM items WHERE category <> ''"
        ).fetchall()
    }
    for c in (*IMPORT_ITEM_CHOICES, *PRESET_CATEGORIES):
        if c.lower() not in {"keyboard", "mouse"}:
            cats.add(c)
    categories = sorted(cats, key=lambda s: s.lower())
    items = conn.execute(
        """SELECT id, name, sku, category FROM items
           WHERE sku = '' OR UPPER(sku) NOT IN ({skus})
           ORDER BY name COLLATE NOCASE""".format(
            skus=",".join("?" * len(ACCESSORY_SKUS))
        ),
        sorted(ACCESSORY_SKUS),
    ).fetchall()
    return categories, items


def _parse_package_lines(form):
    """Build validated package line dicts from parallel form arrays."""
    labels = form.getlist("line_label")
    types = form.getlist("line_match_type")
    values = form.getlist("line_match_value")
    qtys = form.getlist("line_qty")
    n = len(labels)
    if not (len(types) == len(values) == len(qtys) == n):
        return None, "Package lines are incomplete — please try again."
    lines = []
    for i in range(n):
        label = labels[i].strip()
        match_type = types[i].strip()
        match_value = values[i].strip()
        qty = form_int(qtys[i], default=0)
        if not label and not match_value and qty <= 0:
            continue
        if not label:
            return None, "Each package line needs a label."
        if match_type not in PACKAGE_MATCH_TYPES:
            return None, f"Unknown match type on line “{label}”."
        if not match_value:
            return None, f"Choose what “{label}” matches (category, SKU or item)."
        if qty < 1 or qty > 100:
            return None, f"Quantity for “{label}” must be between 1 and 100."
        if match_type == "sku":
            match_value = match_value.upper()
            if match_value not in ACCESSORY_SKUS:
                return None, f"SKU “{match_value}” is not a keyboard/mouse SKU."
        elif match_type == "item":
            if form_int(match_value, default=-1) < 1:
                return None, f"Pick a catalog item for “{label}”."
        lines.append({
            "label": label,
            "match_type": match_type,
            "match_value": match_value,
            "quantity": qty,
            "sort_order": len(lines),
        })
    if not lines:
        return None, "Add at least one line to the package."
    return lines, None


def _save_package_lines(conn, package_id, lines):
    conn.execute("DELETE FROM package_lines WHERE package_id = ?", (package_id,))
    for ln in lines:
        conn.execute(
            """INSERT INTO package_lines
               (package_id, label, match_type, match_value, quantity, sort_order)
               VALUES (?,?,?,?,?,?)""",
            (package_id, ln["label"], ln["match_type"], ln["match_value"],
             ln["quantity"], ln["sort_order"]),
        )


@app.route("/packages")
def packages():
    conn = database.get_db()
    rows = conn.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM package_lines pl WHERE pl.package_id = p.id) AS line_count
           FROM packages p ORDER BY p.name COLLATE NOCASE"""
    ).fetchall()
    return render_template("packages.html", packages=rows)


@app.route("/packages/new", methods=["GET", "POST"])
def new_package():
    conn = database.get_db()
    categories, items = _package_form_options(conn)
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        notes = form.get("notes", "").strip()
        lines, err = _parse_package_lines(form)
        if not name:
            flash("Give the package a name (e.g. OA PC).", "error")
            return render_template(
                "package_form.html", package=None, lines=[], form=form,
                categories=categories, items=items, accessory_skus=ACCESSORY_SKU_LABELS,
            ), 400
        if err:
            flash(err, "error")
            return render_template(
                "package_form.html", package=None, lines=[], form=form,
                categories=categories, items=items, accessory_skus=ACCESSORY_SKU_LABELS,
            ), 400
        now = now_str()
        try:
            cur = conn.execute(
                "INSERT INTO packages (name, notes, created_at, updated_at) VALUES (?,?,?,?)",
                (name, notes, now, now),
            )
            _save_package_lines(conn, cur.lastrowid, lines)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash(f"A package named “{name}” already exists.", "error")
            return render_template(
                "package_form.html", package=None, lines=[], form=form,
                categories=categories, items=items, accessory_skus=ACCESSORY_SKU_LABELS,
            ), 400
        flash(f"Created package “{name}” with {len(lines)} line{'s' if len(lines) != 1 else ''}.",
              "success")
        return redirect(url_for("packages"))
    return render_template(
        "package_form.html", package=None, lines=[], form={},
        categories=categories, items=items, accessory_skus=ACCESSORY_SKU_LABELS,
    )


@app.route("/packages/<int:package_id>/edit", methods=["GET", "POST"])
def edit_package(package_id):
    pkg = _package_or_404(package_id)
    conn = database.get_db()
    categories, items = _package_form_options(conn)
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        notes = form.get("notes", "").strip()
        lines, err = _parse_package_lines(form)
        if not name:
            flash("Give the package a name (e.g. OA PC).", "error")
            return render_template(
                "package_form.html", package=pkg, lines=_package_lines(conn, package_id),
                form=form, categories=categories, items=items,
                accessory_skus=ACCESSORY_SKU_LABELS,
            ), 400
        if err:
            flash(err, "error")
            return render_template(
                "package_form.html", package=pkg, lines=_package_lines(conn, package_id),
                form=form, categories=categories, items=items,
                accessory_skus=ACCESSORY_SKU_LABELS,
            ), 400
        try:
            conn.execute(
                "UPDATE packages SET name = ?, notes = ?, updated_at = ? WHERE id = ?",
                (name, notes, now_str(), package_id),
            )
            _save_package_lines(conn, package_id, lines)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash(f"A package named “{name}” already exists.", "error")
            return render_template(
                "package_form.html", package=pkg, lines=_package_lines(conn, package_id),
                form=form, categories=categories, items=items,
                accessory_skus=ACCESSORY_SKU_LABELS,
            ), 400
        flash(f"Saved package “{name}”.", "success")
        return redirect(url_for("packages"))
    return render_template(
        "package_form.html", package=pkg, lines=_package_lines(conn, package_id),
        form={}, categories=categories, items=items, accessory_skus=ACCESSORY_SKU_LABELS,
    )


@app.route("/packages/<int:package_id>/delete", methods=["POST"])
def delete_package(package_id):
    pkg = _package_or_404(package_id)
    conn = database.get_db()
    conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))
    conn.commit()
    flash(f"Deleted package “{pkg['name']}”.", "success")
    return redirect(url_for("packages"))


# ---------------------------------------------------------------------------
# Stock in / out
# ---------------------------------------------------------------------------

def _stock_in_categories(conn):
    """Categories offered when registering a new product on Stock In.

    Preferred choices (OA PC, KB/MS wired-wireless, LAN Cable 3m, …) come first;
    any other categories already in the DB follow, excluding the plain
    Keyboard/Mouse/LAN Cable labels replaced by fixed accessory variants.
    """
    preferred = list(STOCK_IN_CATEGORY_CHOICES)
    seen = {c.lower() for c in preferred}
    skip = {"keyboard", "mouse", "lan cable"}
    extras = []
    for c in (*PRESET_CATEGORIES,):
        if c.lower() in seen or c.lower() in skip:
            continue
        extras.append(c)
        seen.add(c.lower())
    for r in conn.execute(
        "SELECT DISTINCT category FROM items WHERE category <> ''"
    ).fetchall():
        c = r["category"]
        if c.lower() in seen or c.lower() in skip:
            continue
        extras.append(c)
        seen.add(c.lower())
    extras.sort(key=lambda s: s.lower())
    return preferred + extras


def _ticket_day_key():
    return datetime.now().strftime("%Y%m%d")


def _ticket_no(day_key, seq, tx_type):
    return f"KCCITD-{day_key}-{seq:03d}-{tx_type}"


def _next_ticket_seq(conn, day_key, tx_type):
    """Highest used sequence for this day + IN/OUT, or -1 if none."""
    prefix = f"KCCITD-{day_key}-"
    suffix = f"-{tx_type}"
    rows = conn.execute(
        "SELECT ticket_no, seq FROM tickets WHERE type = ? AND ticket_no LIKE ?",
        (tx_type, prefix + "%" + suffix),
    ).fetchall()
    max_seq = -1
    for r in rows:
        try:
            max_seq = max(max_seq, int(r["seq"]))
        except (TypeError, ValueError):
            parts = (r["ticket_no"] or "").split("-")
            if len(parts) >= 4:
                try:
                    max_seq = max(max_seq, int(parts[2]))
                except ValueError:
                    pass
    # Also respect legacy transaction references with the same pattern.
    for r in conn.execute(
        "SELECT DISTINCT reference FROM transactions WHERE type = ? AND reference LIKE ?",
        (tx_type, prefix + "%" + suffix),
    ).fetchall():
        parts = (r["reference"] or "").split("-")
        if len(parts) >= 4 and parts[0] == "KCCITD" and parts[1] == day_key and parts[-1] == tx_type:
            try:
                max_seq = max(max_seq, int(parts[2]))
            except ValueError:
                pass
    return max_seq


def peek_ticket_no(conn, tx_type):
    """Predicted next ticket number (not reserved)."""
    day = _ticket_day_key()
    seq = _next_ticket_seq(conn, day, tx_type) + 1
    if seq > 999:
        return _ticket_no(day, 999, tx_type) + " (daily limit)"
    return _ticket_no(day, seq, tx_type)


def allocate_ticket(conn, tx_type, *, person="", department="", staff_id="",
                    site="", notes="", on_loan=False, condemned=False,
                    follow_up=False, created_by="", created_at=None):
    """Reserve the next ticket number and insert the case row. Returns ticket dict."""
    if tx_type not in ("IN", "OUT"):
        raise ValueError("Ticket type must be IN or OUT.")
    day = _ticket_day_key()
    created_at = created_at or now_str()
    last_err = None
    for _ in range(20):
        seq = _next_ticket_seq(conn, day, tx_type) + 1
        if seq > 999:
            raise ValueError(
                f"Daily ticket limit (1000) reached for {tx_type} on {day}."
            )
        ticket_no = _ticket_no(day, seq, tx_type)
        try:
            cur = conn.execute(
                """INSERT INTO tickets
                   (ticket_no, type, seq, person, department, staff_id, site, notes,
                    on_loan, condemned, follow_up, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticket_no, tx_type, seq, person, department, staff_id, site, notes,
                    1 if on_loan else 0, 1 if condemned else 0, 1 if follow_up else 0,
                    created_by, created_at,
                ),
            )
            return {
                "id": cur.lastrowid,
                "ticket_no": ticket_no,
                "type": tx_type,
                "seq": seq,
            }
        except sqlite3.IntegrityError as exc:
            last_err = exc
            continue
    raise ValueError(f"Could not allocate a ticket number ({last_err}).")


def _stock_form(tx_type):
    conn = database.get_db()
    # has_tagged: at least one unit with an S/N or EAM — those stay out of the
    # N/A (untagged) existing-product list on Stock In.
    item_rows = conn.execute(
        f"""SELECT i.*,
                   EXISTS (
                     SELECT 1 FROM units u
                     WHERE u.item_id = i.id
                       AND (u.serial_no <> '' OR u.eam_no <> '')
                   ) AS has_tagged
            FROM items i
            ORDER BY i.name COLLATE NOCASE"""
    ).fetchall()
    accessory = conn.execute(
        f"""SELECT * FROM items WHERE sku IN
            ({",".join("?" * len(ACCESSORY_SKUS))})""",
        sorted(ACCESSORY_SKUS),
    ).fetchall()
    # Stock out quantity picker: anything with shelf quantity and no S/N/EAM units.
    untagged_items = [
        r for r in item_rows
        if r["quantity"] > 0 and not r["has_tagged"]
    ]
    packages = []
    categories = []
    if tx_type == "OUT":
        packages = _packages_payload(conn)
    else:
        categories = _stock_in_categories(conn)
    ticket_no = peek_ticket_no(conn, tx_type)
    return render_template(
        "scan_form.html",
        tx_type=tx_type,
        items=item_rows,
        items_by_sku={r["sku"].upper(): r for r in accessory},
        untagged_items=untagged_items,
        packages=packages,
        categories=categories,
        ticket_no=ticket_no,
    )


def _record_stock(tx_type):
    """Record a batch of scanned rows in one go.

    The scan page submits parallel arrays describing each pending row:
      rtype = 'unit'  → rref = unit id     (tracked asset: return on IN, hand out on OUT)
      rtype = 'new'   → rref = item id + rserial/ream codes (register a brand-new unit; IN only)
      rtype = 'bulk'  → rref = item id + rqty (untagged stock)
    Everything is validated first; the whole batch is written in a single
    database transaction (all-or-nothing).
    """
    conn = database.get_db()
    if request.method == "POST":
        form = request.form
        types = form.getlist("rtype")
        refs = form.getlist("rref")
        qtys = form.getlist("rqty")
        serials = form.getlist("rserial")
        eams = form.getlist("ream")
        n = len(types)
        if n == 0 or not (len(refs) == len(qtys) == len(serials) == len(eams) == n):
            flash("Scan at least one item before finishing.", "error")
            return _stock_form(tx_type), 400

        ops = []  # dicts: kind, ref, qty, serial, eam, unit, item
        seen_units, seen_codes = set(), set()
        for i in range(n):
            kind = types[i]
            ref = form_int(refs[i], default=-1)
            qty = max(1, form_int(qtys[i], default=1))
            serial = serials[i].strip()
            eam = eams[i].strip()
            if kind not in ("unit", "new", "bulk") or ref < 0:
                flash("One of the scanned rows is invalid — please rescan.", "error")
                return _stock_form(tx_type), 400
            if kind == "new" and tx_type != "IN":
                flash("New units can only be registered on the Stock In page.", "error")
                return _stock_form(tx_type), 400
            if kind == "unit":
                if ref in seen_units:
                    continue
                seen_units.add(ref)
            if kind == "new":
                if not serial and not eam:
                    flash("A new unit needs at least an S/N or an EAM number.", "error")
                    return _stock_form(tx_type), 400
                for code in (serial, eam):
                    if code:
                        key = code.lower()
                        if key in seen_codes:
                            flash(f"“{code}” appears twice in this batch.", "error")
                            return _stock_form(tx_type), 400
                        seen_codes.add(key)
            ops.append({"kind": kind, "ref": ref, "qty": qty, "serial": serial, "eam": eam})

        for op in ops:
            if op["kind"] == "unit":
                unit = conn.execute(
                    """SELECT u.*, i.name AS item_name FROM units u
                       JOIN items i ON i.id = u.item_id WHERE u.id = ?""",
                    (op["ref"],),
                ).fetchone()
                if unit is None:
                    flash("One of the scanned units no longer exists — please rescan.", "error")
                    return _stock_form(tx_type), 400
                if tx_type == "IN" and unit["status"] != "checked_out":
                    flash(f"“{unit['item_name']}” ({unit['eam_no'] or unit['serial_no']}) "
                          "is already on the shelf.", "error")
                    return _stock_form(tx_type), 400
                if tx_type == "OUT" and unit["status"] != "in_stock":
                    flash(f"“{unit['item_name']}” ({unit['eam_no'] or unit['serial_no']}) "
                          "is already checked out.", "error")
                    return _stock_form(tx_type), 400
                op["unit"] = unit
            else:
                item = conn.execute("SELECT * FROM items WHERE id = ?", (op["ref"],)).fetchone()
                if item is None:
                    flash("One of the scanned items no longer exists — please rescan.", "error")
                    return _stock_form(tx_type), 400
                op["item"] = item
                if op["kind"] == "new":
                    if code_clash(conn, op["serial"], op["eam"]):
                        flash("One of the scanned codes is already registered to another unit. "
                              "Nothing was recorded.", "error")
                        return _stock_form(tx_type), 400
                if op["kind"] == "bulk":
                    # Untagged stock (KB/MS, LAN cables, etc.) moves by quantity.
                    # Products that already have S/N or EAM units stay unit-tracked
                    # — except the dedicated accessory SKUs.
                    sku = (item["sku"] or "").upper()
                    if sku not in ACCESSORY_SKUS and _item_has_tagged_units(conn, item["id"]):
                        flash(
                            f"“{item['name']}” is tracked per unit — scan its "
                            "EAM tag or serial number instead. Nothing was recorded.",
                            "error",
                        )
                        return _stock_form(tx_type), 400
                    if op["qty"] < 1 or op["qty"] > ACCESSORY_MAX_QTY:
                        flash(
                            f"Quantity for “{item['name']}” must be between 1 and "
                            f"{ACCESSORY_MAX_QTY}. Nothing was recorded.",
                            "error",
                        )
                        return _stock_form(tx_type), 400
                    if tx_type == "OUT" and op["qty"] > item["quantity"]:
                        flash(
                            f"Not enough stock: only {item['quantity']} × "
                            f"“{item['name']}” on the shelf. Nothing was recorded.",
                            "error",
                        )
                        return _stock_form(tx_type), 400

        # On loan (stock out only): the batch goes out under a specific
        # custodian and is expected back — tracked per scanned unit.
        # Condemnation (stock out only): the batch is written off; the units
        # stay flagged so any later return warns the operator.
        on_loan = tx_type == "OUT" and form.get("on_loan") == "1"
        condemned = tx_type == "OUT" and form.get("condemned") == "1"
        follow_up = tx_type == "OUT" and form.get("follow_up") == "1"
        if on_loan and condemned:
            flash("A batch can't be both on loan and condemned — pick one. "
                  "Nothing was recorded.", "error")
            return _stock_form(tx_type), 400
        if condemned:
            if not any(op["kind"] == "unit" for op in ops):
                flash("Condemned batches need at least one scanned unit — untagged "
                      "keyboards and mice can't be tracked for condemnation. "
                      "Nothing was recorded.", "error")
                return _stock_form(tx_type), 400
            if any(op["kind"] != "unit" for op in ops):
                flash("Only scanned units can be condemned — remove the keyboard/mouse "
                      "rows from this batch. Nothing was recorded.", "error")
                return _stock_form(tx_type), 400
        if on_loan:
            if not any(op["kind"] == "unit" for op in ops):
                flash("On-loan batches need at least one scanned unit — untagged "
                      "keyboards and mice can't go on loan. Nothing was recorded.", "error")
                return _stock_form(tx_type), 400
            if any(op["kind"] != "unit" for op in ops):
                flash("Only scanned units can go on loan — remove the keyboard/mouse "
                      "rows from this batch. Nothing was recorded.", "error")
                return _stock_form(tx_type), 400
            # A loan is only useful if you know who has it and why.
            # Ticket/request no. is auto-assigned — not required from the form.
            missing = [label for label, key in (
                ("Custodian", "person"),
                ("Department", "department"),
            ) if not form.get(key, "").strip()]
            if missing:
                flash("On-loan batches need " + ", ".join(missing) +
                      ". Nothing was recorded.", "error")
                return _stock_form(tx_type), 400

        person = form.get("person", "").strip()
        dept = form.get("department", "").strip()
        staff_id = form.get("staff_id", "").strip()
        site = site_for(form.get("site")) or ("Site A" if tx_type == "IN" else "")
        notes = form.get("notes", "").strip()
        now = now_str()
        moved = 0
        condemned_back = []  # codes of condemned units coming back in
        ticket = None
        try:
            ticket = allocate_ticket(
                conn, tx_type,
                person=person, department=dept, staff_id=staff_id, site=site,
                notes=notes, on_loan=on_loan, condemned=condemned,
                follow_up=follow_up, created_by=session.get("username", ""),
                created_at=now,
            )
            ref_no = ticket["ticket_no"]
            for op in ops:
                if op["kind"] == "unit":
                    unit = op["unit"]
                    new_status = "in_stock" if tx_type == "IN" else "checked_out"
                    if tx_type == "IN":
                        # A return lands on a specific site's shelf; any loan
                        # on the unit is settled by the return. Condemnation
                        # sticks to the unit so future stock-ins keep warning.
                        if unit["condemned"]:
                            condemned_back.append(unit["eam_no"] or unit["serial_no"])
                        conn.execute(
                            "UPDATE units SET status = ?, site = ?, on_loan = 0, updated_at = ? WHERE id = ?",
                            (new_status, site, now, unit["id"]),
                        )
                    else:
                        # Condemnation sticks for life — a later ordinary stock
                        # out must not wipe the flag (only a condemn-out can set it).
                        if condemned:
                            conn.execute(
                                "UPDATE units SET status = ?, on_loan = ?, condemned = 1, updated_at = ? WHERE id = ?",
                                (new_status, 1 if on_loan else 0, now, unit["id"]),
                            )
                        else:
                            conn.execute(
                                "UPDATE units SET status = ?, on_loan = ?, updated_at = ? WHERE id = ?",
                                (new_status, 1 if on_loan else 0, now, unit["id"]),
                            )
                    conn.execute(
                        """INSERT INTO transactions
                           (item_id, unit_id, type, quantity, person, department, reference, staff_id, site, on_loan, condemned, follow_up, notes, created_at)
                           VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?)""",
                        (unit["item_id"], unit["id"], tx_type, person, dept, ref_no, staff_id,
                         site, 1 if on_loan else 0, 1 if condemned else 0,
                         1 if follow_up else 0, notes, now),
                    )
                    moved += 1
                elif op["kind"] == "new":
                    cur = conn.execute(
                        """INSERT INTO units (item_id, serial_no, eam_no, status, site, created_at, updated_at)
                           VALUES (?,?,?,'in_stock',?,?,?)""",
                        (op["ref"], op["serial"], op["eam"], site, now, now),
                    )
                    conn.execute(
                        """INSERT INTO transactions
                           (item_id, unit_id, type, quantity, person, department, reference, staff_id, site, notes, created_at)
                           VALUES (?,?,'IN',1,?,?,?,?,?,?,?)""",
                        (op["ref"], cur.lastrowid, person, dept, ref_no, staff_id, site, notes, now),
                    )
                    moved += 1
                else:
                    delta = op["qty"] if tx_type == "IN" else -op["qty"]
                    conn.execute(
                        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                        (delta, now, op["ref"]),
                    )
                    conn.execute(
                        """INSERT INTO transactions
                           (item_id, unit_id, type, quantity, person, department, reference, staff_id, site, on_loan, condemned, follow_up, notes, created_at)
                           VALUES (?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (op["ref"], tx_type, op["qty"], person, dept, ref_no, staff_id,
                         site, 1 if on_loan else 0, 1 if condemned else 0,
                         1 if follow_up else 0, notes, now),
                    )
                    moved += op["qty"]
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), "error")
            return _stock_form(tx_type), 400
        except sqlite3.Error:
            conn.rollback()
            flash("Could not save the batch. Please try again.", "error")
            return _stock_form(tx_type), 500

        direction = "into" if tx_type == "IN" else "out of"
        flash(
            f"Recorded {moved} unit{'s' if moved != 1 else ''} across "
            f"{len(ops)} row{'s' if len(ops) != 1 else ''} {direction} stock"
            f" as {ticket['ticket_no'] if ticket else 'ticket'}"
            f"{' — marked as on loan' if on_loan else ''}"
            f"{' — marked as condemned' if condemned else ''}"
            f"{' — follow up' if follow_up else ''}.",
            "success",
        )
        if condemned_back:
            flash(
                "Condemned stock returned: " + ", ".join(condemned_back) +
                " — inspect and quarantine before shelving.", "warning",
            )
        if tx_type == "OUT" and not person:
            ok, detail = _stock_out_admin_alert(
                ops, dept, ref_no, staff_id, notes, follow_up,
                session.get("username", ""),
            )
            if ok:
                flash("No custodian entered — admin was emailed.", "warning")
            else:
                flash(
                    "No custodian entered — could not email admin "
                    f"({detail}). Check STOCK_SMTP_* settings.",
                    "warning",
                )
        if ticket:
            return redirect(url_for("ticket_detail", ticket_id=ticket["id"]))
        return redirect(url_for("history"))

    return _stock_form(tx_type)


@app.route("/stock/in", methods=["GET", "POST"])
def stock_in():
    return _record_stock("IN")


@app.route("/stock/out", methods=["GET", "POST"])
def stock_out():
    return _record_stock("OUT")


# ---------------------------------------------------------------------------
# Tickets (auto stock in/out cases)
# ---------------------------------------------------------------------------

@app.route("/tickets")
def tickets():
    conn = database.get_db()
    q = request.args.get("q", "").strip()
    tx_type = request.args.get("type", "").strip().upper()
    clauses, params = [], []
    if tx_type in ("IN", "OUT"):
        clauses.append("t.type = ?")
        params.append(tx_type)
    if q:
        like = f"%{q}%"
        clauses.append(
            "(t.ticket_no LIKE ? OR t.person LIKE ? OR t.department LIKE ? "
            "OR t.staff_id LIKE ? OR t.notes LIKE ? OR t.created_by LIKE ?)"
        )
        params += [like] * 6
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM tickets t{where}", params
    ).fetchone()["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, form_int(request.args.get("page"), default=1)), pages)
    rows = conn.execute(
        f"""SELECT t.*,
                   (SELECT COUNT(*) FROM transactions x WHERE x.reference = t.ticket_no) AS line_count,
                   (SELECT COALESCE(SUM(x.quantity), 0) FROM transactions x
                    WHERE x.reference = t.ticket_no) AS unit_count
            FROM tickets t{where}
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return render_template(
        "tickets.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        q=q,
        tx_type=tx_type,
    )


@app.route("/tickets/<int:ticket_id>")
def ticket_detail(ticket_id):
    conn = database.get_db()
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if ticket is None:
        abort(404)
    lines = conn.execute(
        """SELECT t.*, i.name AS item_name, i.sku AS item_sku, i.category AS item_category,
                  u.eam_no AS unit_eam, u.serial_no AS unit_serial
           FROM transactions t
           JOIN items i ON i.id = t.item_id
           LEFT JOIN units u ON u.id = t.unit_id
           WHERE t.reference = ?
           ORDER BY t.id""",
        (ticket["ticket_no"],),
    ).fetchall()
    unit_total = sum(int(r["quantity"] or 0) for r in lines)
    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        lines=lines,
        unit_total=unit_total,
    )


@app.route("/tickets/no/<path:ticket_no>")
def ticket_by_no(ticket_no):
    conn = database.get_db()
    row = conn.execute(
        "SELECT id FROM tickets WHERE ticket_no = ?", (ticket_no.strip(),)
    ).fetchone()
    if row is None:
        abort(404)
    return redirect(url_for("ticket_detail", ticket_id=row["id"]))


# ---------------------------------------------------------------------------
# History, export, JSON API
# ---------------------------------------------------------------------------

@app.route("/history")
def history():
    where, params, tx_type, q, date_from, date_to = history_filters(request.args)
    conn = database.get_db()
    total = conn.execute(
        f"""SELECT COUNT(*) AS c FROM transactions t
            JOIN items i ON i.id = t.item_id
            LEFT JOIN units u ON u.id = t.unit_id{where}""",
        params,
    ).fetchone()["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, form_int(request.args.get("page"), default=1)), pages)
    rows = conn.execute(
        f"""SELECT t.*, i.name AS item_name, i.sku AS item_sku,
                   u.eam_no AS unit_eam, u.serial_no AS unit_serial
            FROM transactions t
            JOIN items i ON i.id = t.item_id
            LEFT JOIN units u ON u.id = t.unit_id{where}
            ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return render_template(
        "history.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        q=q,
        tx_type=tx_type,
        date_from=date_from,
        date_to=date_to,
    )


@app.route("/history.csv")
def history_csv():
    where, params, _tx, _q, _df, _dt = history_filters(request.args)
    rows = database.get_db().execute(
        f"""SELECT t.*, i.name AS item_name, i.sku AS item_sku,
                   u.eam_no AS unit_eam, u.serial_no AS unit_serial
            FROM transactions t
            JOIN items i ON i.id = t.item_id
            LEFT JOIN units u ON u.id = t.unit_id{where}
            ORDER BY t.created_at DESC, t.id DESC""",
        params,
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Date", "Type", "Item", "SKU", "Unit EAM", "Unit S/N", "Quantity",
         "Person", "Department", "Reference", "Site", "Staff ID", "On Loan",
         "Condemned", "Follow up", "Notes"]
    )
    for r in rows:
        writer.writerow(
            [
                r["created_at"], r["type"], csv_safe(r["item_name"]), csv_safe(r["item_sku"]),
                csv_safe(r["unit_eam"] or ""), csv_safe(r["unit_serial"] or ""),
                r["quantity"], csv_safe(r["person"]), csv_safe(r["department"]),
                csv_safe(r["reference"]), csv_safe(r["site"]), csv_safe(r["staff_id"]),
                "Yes" if r["on_loan"] else "", "Yes" if r["condemned"] else "",
                "Yes" if r["follow_up"] else "",
                csv_safe(r["notes"]),
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=stock-history.csv"},
    )


@app.route("/items.csv")
def items_csv():
    rows = database.get_db().execute(
        "SELECT * FROM items ORDER BY name COLLATE NOCASE"
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Name", "SKU", "Category", "Brand", "Model", "In stock", "Min level", "Location", "Notes"]
    )
    for r in rows:
        writer.writerow(
            [
                csv_safe(r["name"]), csv_safe(r["sku"]), csv_safe(r["category"]),
                csv_safe(r["brand"]), csv_safe(r["model"]), r["quantity"],
                r["min_quantity"], csv_safe(r["location"]), csv_safe(r["notes"]),
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=stock-items.csv"},
    )


@app.route("/api/lookup")
def api_lookup():
    """Resolve a scanned barcode: a tracked unit (S/N or EAM) or an item (SKU)."""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "empty code"}), 400
    conn = database.get_db()
    unit = conn.execute(
        """SELECT u.id AS unit_id, u.status, u.serial_no, u.eam_no, u.on_loan, u.condemned,
                  i.id, i.name, i.sku, i.category,
                  (SELECT t.person FROM transactions t
                   WHERE t.unit_id = u.id AND t.type = 'OUT'
                   ORDER BY t.created_at DESC, t.id DESC LIMIT 1) AS holder
           FROM units u JOIN items i ON i.id = u.item_id
           WHERE u.eam_no = ? COLLATE NOCASE OR u.serial_no = ? COLLATE NOCASE
           LIMIT 1""",
        (code, code),
    ).fetchone()
    if unit is not None:
        row = dict(unit)
        if row["status"] != "checked_out":
            row["holder"] = ""
        return jsonify({"type": "unit", **row})
    item = conn.execute(
        f"""SELECT i.id, i.name, i.sku, i.category, i.quantity, {UNITS_IN_SQL} AS units_in
            FROM items i WHERE i.sku = ? COLLATE NOCASE LIMIT 1""",
        (code,),
    ).fetchone()
    if item is not None:
        return jsonify({"type": "item", **dict(item)})
    return jsonify({"error": "not found"}), 404


@app.route("/api/items", methods=["GET", "POST"])
def api_items():
    conn = database.get_db()
    if request.method == "GET":
        rows = conn.execute(
            f"""SELECT i.id, i.name, i.sku, i.category, i.brand, i.model,
                       i.quantity AS untagged, i.min_quantity, i.location,
                       {UNITS_IN_SQL} AS tracked_in, {UNITS_OUT_SQL} AS tracked_out
                FROM items i ORDER BY i.name COLLATE NOCASE"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    # Create (or reuse) a catalog item so Stock In can register units against it.
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or request.form.get("name") or "").strip()
    category_raw = (payload.get("category") or request.form.get("category") or "").strip()
    brand = (payload.get("brand") or request.form.get("brand") or "").strip()
    if len(brand) > 80:
        return jsonify({"error": "Brand is too long."}), 400
    if len(category_raw) > 80:
        return jsonify({"error": "Category is too long."}), 400

    # KB/MS wired/wireless categories map onto the fixed accessory SKU rows.
    acc = _accessory_kind_from_category(category_raw)
    if acc is not None:
        try:
            row = _ensure_accessory_item(conn, acc[0], acc[1], brand=brand)
            conn.commit()
        except (ValueError, sqlite3.Error) as exc:
            conn.rollback()
            return jsonify({"error": str(exc) or "Could not match keyboard/mouse stock."}), 500
        status = 201 if row.pop("created") else 200
        row["created"] = status == 201
        return jsonify(row), status

    category = _category_for(category_raw)
    if not name:
        return jsonify({"error": "Product name is required."}), 400
    if len(name) > 120:
        return jsonify({"error": "Product name is too long."}), 400
    if not category:
        return jsonify({"error": "Item category is required."}), 400
    existing = conn.execute(
        """SELECT id, name, sku, category, brand, model, quantity FROM items
           WHERE name = ? COLLATE NOCASE AND category = ? COLLATE NOCASE""",
        (name, category),
    ).fetchone()
    if existing is not None:
        if brand and not (existing["brand"] or "").strip():
            conn.execute(
                "UPDATE items SET brand = ?, updated_at = ? WHERE id = ?",
                (brand, now_str(), existing["id"]),
            )
            conn.commit()
            existing = dict(existing)
            existing["brand"] = brand
        return jsonify({"created": False, **dict(existing)})
    now = now_str()
    try:
        cur = conn.execute(
            """INSERT INTO items
               (name, sku, category, brand, model, quantity, min_quantity,
                location, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,0,0,?,?,?,?)""",
            (name, "", category, brand, name, "", "", now, now),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return jsonify({"error": "Could not create the product."}), 500
    return jsonify({
        "created": True,
        "id": cur.lastrowid,
        "name": name,
        "sku": "",
        "category": category,
        "brand": brand,
        "model": name,
        "quantity": 0,
    }), 201


@app.route("/api/summary")
def api_summary():
    conn = database.get_db()
    totals = conn.execute(
        "SELECT COUNT(*) AS items, COALESCE(SUM(quantity), 0) AS units FROM items"
    ).fetchone()
    low = conn.execute(
        """SELECT COUNT(*) AS c FROM items
           WHERE min_quantity > 0 AND quantity <= min_quantity"""
    ).fetchone()["c"]
    return jsonify(
        {
            "items": totals["items"],
            "units_in_stock": totals["units"],
            "low_stock_items": low,
        }
    )


# ---------------------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------------------

@app.errorhandler(400)
def bad_request(err):
    message = getattr(err, "description", "The request could not be understood.")
    return render_template("error.html", code=400, message=message), 400


@app.errorhandler(404)
def not_found(_err):
    return render_template("error.html", code=404, message="That page does not exist."), 404


@app.errorhandler(500)
def server_error(_err):
    return (
        render_template(
            "error.html", code=500, message="Something went wrong on the server."
        ),
        500,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IT Stock Manager")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                        help="address to bind (default 127.0.0.1; use 0.0.0.0 to let colleagues' PCs connect)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                        help=f"port to listen on (default {DEFAULT_PORT})")
    args = parser.parse_args()
    print(f"\n  IT Stock Manager is running at http://localhost:{args.port}", flush=True)
    print("  Keep this window open while using the app. Press Ctrl+C to stop.", flush=True)
    if not SETUP_CODE_FROM_ENV:
        print(f"\n  First-account setup code: {SETUP_CODE}", flush=True)
        print("  (Needed once, to create the first login. Set STOCK_SETUP_CODE to fix it.)\n",
              flush=True)
    else:
        print(flush=True)
    try:
        from waitress import serve
    except ImportError:
        app.run(host=args.host, port=args.port)
        return
    # Waitress 3 strips X-Forwarded-* from untrusted clients by default. This
    # app is self-hosted and may sit behind any TLS-terminating proxy, so trust
    # the proto/host headers: spoofing them only affects the spoofer's own
    # cookie handling, never authorization decisions.
    serve(app, host=args.host, port=args.port, threads=8, trusted_proxy="*",
          trusted_proxy_headers={"x-forwarded-proto", "x-forwarded-host"})


if __name__ == "__main__":
    main()

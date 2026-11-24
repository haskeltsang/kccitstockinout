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
import os
import re
import secrets
import sqlite3
import sys
import time
import zipfile
from datetime import datetime

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
            "OR t.department LIKE ? OR t.reference LIKE ? OR t.site LIKE ?)"
        )
        params += [like] * 7
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
        sent = request.form.get("_csrf", "")
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
                  (SELECT t.created_at FROM transactions t
                   WHERE t.unit_id = u.id AND t.type = 'IN'
                   ORDER BY t.created_at DESC, t.id DESC LIMIT 1) AS stocked_in_at
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
    return render_template(
        "item_detail.html", item=item, counts=counts, units=units, history=history
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
        "eam no.", "eam number", "tag", "tag no",
    },
    "serial": {"s/n", "sn", "serial", "serial no", "serial no.", "serial number"},
    "brand": {"brand", "manufacturer", "make"},
    "funding": {"funding", "funding source", "fund"},
    "project": {"project", "project name"},
    "project_code": {"project code", "project no", "project number", "proj code"},
    "site": {"site", "site name", "storeroom"},
}

# The two storerooms stock can be received into. Forms offer exactly these;
# the import maps "A"/"Site A" etc. onto them (blank in a file -> Site B).
SITES = ("Site A", "Site B")
_SITE_ALIASES = {"a": "Site A", "site a": "Site A", "b": "Site B", "site b": "Site B"}


def site_for(raw):
    """Normalize a site value from a form or an import cell; '' if unknown."""
    return _SITE_ALIASES.get((raw or "").strip().lower(), "")


IMPORT_TEMPLATE_HEADERS = [
    "Item", "Model", "S/N", "Asset/EAM No.", "Brand", "Funding", "Project", "Project Code", "Site",
]
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
    "screen": "Monitor", "display": "Monitor",
    "headset": "Peripheral", "keyboard": "Peripheral", "mouse": "Peripheral",
    "webcam": "Peripheral",
    "router": "Network", "switch": "Network", "access point": "Network",
    "ssd": "Storage", "hdd": "Storage", "hard drive": "Storage", "usb drive": "Storage",
    "hub": "Accessory", "dock": "Accessory", "docking station": "Accessory",
    "phone": "Mobile", "tablet": "Mobile",
})


def _category_for(raw):
    if not raw:
        return "Other"
    return CATEGORY_ALIASES.get(raw.strip().lower(), raw.strip())


class ImportFileError(Exception):
    """The uploaded file cannot be read or is missing required columns."""


# Keyboards and mice are the only stock without an EAM/SN — tracked by
# quantity only, one item per type + connection, added on the Import page.
# Each carries a fixed SKU so the scan pages can move them by code.
ACCESSORY_CATEGORY = "Peripheral"
ACCESSORY_ITEMS = {
    ("mouse", "wired"): ("Mouse (Wired)", "MS-WIRED"),
    ("mouse", "wireless"): ("Mouse (Wireless)", "MS-WIRELESS"),
    ("keyboard", "wired"): ("Keyboard (Wired)", "KB-WIRED"),
    ("keyboard", "wireless"): ("Keyboard (Wireless)", "KB-WIRELESS"),
}
ACCESSORY_SKUS = {sku for _name, sku in ACCESSORY_ITEMS.values()}
_ACCESSORY_WORD = re.compile(r"\b(keyboards?|mouse|mice)\b", re.IGNORECASE)
ACCESSORY_MAX_QTY = 10000


def _record_accessory_add(conn, form):
    """Quantity top-up for an untagged keyboard/mouse item (Import page)."""
    kind = form.get("accessory_type", "").strip().lower()
    connection = form.get("connection", "").strip().lower()
    if (kind, connection) not in ACCESSORY_ITEMS:
        flash("Choose the type (mouse or keyboard) and connection (wired or wireless).", "error")
        return redirect(url_for("import_stocks"))
    qty = form_int(form.get("qty"), default=0)
    if qty < 1 or qty > ACCESSORY_MAX_QTY:
        flash(f"Quantity must be between 1 and {ACCESSORY_MAX_QTY}.", "error")
        return redirect(url_for("import_stocks"))
    reference = form.get("reference", "").strip()
    site = site_for(form.get("site")) or "Site B"
    notes = form.get("notes", "").strip()
    name, sku = ACCESSORY_ITEMS[(kind, connection)]
    now = now_str()
    item = conn.execute(
        "SELECT * FROM items WHERE sku = ? COLLATE NOCASE", (sku,)
    ).fetchone()
    if item is None:
        item = conn.execute(
            "SELECT * FROM items WHERE category = ? COLLATE NOCASE AND name = ? COLLATE NOCASE",
            (ACCESSORY_CATEGORY, name),
        ).fetchone()
    try:
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
            # Make sure the item carries its scan SKU (older rows may predate it).
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
            (item_id, qty, "", reference, site, notes or "Quantity added on the Import page", now),
        )
        conn.commit()
    except sqlite3.Error:
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
            if "item" not in fields or not ({"eam", "serial"} & fields):
                raise ImportFileError(
                    "Could not find the required columns. The first row must be a header "
                    "such as “Item, Model, Asset/EAM No., S/N” — one code column "
                    "(Asset/EAM No. or S/N) is enough."
                )
            header = mapping
            continue
        values = {field: (cells[idx] if idx < len(cells) else "") for idx, field in header.items()}
        if not any(values.values()):
            continue
        yield row_no, {
            "item": values.get("item", ""),
            "model": values.get("model", ""),
            "eam": values.get("eam", ""),
            "serial": values.get("serial", ""),
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
                category, model, eam, serial = (
                    _category_for(row["item"]), row["model"], row["eam"], row["serial"],
                )
                site = site_for(row["site"])
                if not model:
                    entry.update(result="error", detail="Missing model")
                elif _ACCESSORY_WORD.search(row["item"] or "") or _ACCESSORY_WORD.search(model):
                    entry.update(
                        result="error",
                        detail="Keyboards and mice have no asset tags — add them "
                               "by quantity on the Import page",
                    )
                elif not eam and not serial:
                    entry.update(
                        result="error",
                        detail="Needs an S/N or an Asset/EAM no. (either one — EAM can be blank)",
                    )
                elif row["site"].strip() and not site:
                    entry.update(
                        result="error",
                        detail=f"Unknown site “{row['site'].strip()}” — use Site A or Site B",
                    )
                elif any(c and c.lower() in seen_codes for c in (eam, serial)):
                    dup = next(c for c in (eam, serial) if c and c.lower() in seen_codes)
                    entry.update(result="skipped",
                                 detail=f"“{dup}” already appears on row {seen_codes[dup.lower()]}")
                elif code_clash(conn, serial, eam):
                    entry.update(result="skipped", detail="Asset/EAM no. or S/N already registered")
                else:
                    site = site or "Site B"
                    item_id, created = item_id_for(category, model, row["brand"])
                    cur = conn.execute(
                        """INSERT INTO units
                           (item_id, serial_no, eam_no, status, site, funding, project, project_code,
                            created_at, updated_at)
                           VALUES (?,?,?,'in_stock',?,?,?,?,?,?)""",
                        (item_id, serial, eam, site, row["funding"], row["project"],
                         row["project_code"], now, now),
                    )
                    conn.execute(
                        """INSERT INTO transactions
                           (item_id, unit_id, type, quantity, person, department, reference, site, notes, created_at)
                           VALUES (?,?,'IN',1,'','',?,?,'Excel import',?)""",
                        (item_id, cur.lastrowid, reference, site, now),
                    )
                    for c in (eam, serial):
                        if c:
                            seen_codes[c.lower()] = row_no
                    entry.update(result="imported",
                                 detail="New item created" if created else "Added to existing item")
                report.append(entry)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            flash("Could not save the import. Please try again.", "error")
            return render_template("import.html", report=None), 500

        counts = {
            "imported": sum(1 for e in report if e["result"] == "imported"),
            "skipped": sum(1 for e in report if e["result"] == "skipped"),
            "errors": sum(1 for e in report if e["result"] == "error"),
        }
        if counts["imported"]:
            flash(f"Imported {counts['imported']} unit{'s' if counts['imported'] != 1 else ''}.",
                  "success")
        return render_template("import.html", report=report, counts=counts)
    return render_template("import.html", report=None)


@app.route("/import/template.xlsx")
def import_template_xlsx():
    try:
        from openpyxl import Workbook
    except ImportError:
        abort(500, description="Excel support is missing — run: pip install -r requirements.txt")
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(IMPORT_TEMPLATE_HEADERS)
    for row in IMPORT_TEMPLATE_SAMPLE:
        ws.append(row)
    for col, width in zip("ABCDEFGHI", (26, 18, 16, 20, 14, 20, 16, 14, 10)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=stock-import-template.xlsx"},
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
        headers={"Content-Disposition": "attachment; filename=stock-import-template.csv"},
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


@app.route("/units/<int:unit_id>/edit", methods=["GET", "POST"])
def edit_unit(unit_id):
    unit = get_unit_or_404(unit_id)
    conn = database.get_db()
    if request.method == "POST":
        form = request.form
        serial = form.get("serial_no", "").strip()
        eam = form.get("eam_no", "").strip()
        site = site_for(form.get("site")) or unit["site"] or "Site A"
        funding = form.get("funding", "").strip()
        project = form.get("project", "").strip()
        project_code = form.get("project_code", "").strip()
        notes = form.get("notes", "").strip()
        if not serial and not eam:
            flash("Give the unit at least an S/N or an EAM number.", "error")
            return render_template("unit_form.html", item=None, unit=unit, form=form), 400
        if code_clash(conn, serial, eam, exclude_unit_id=unit_id):
            flash("That S/N or EAM number is already registered to another unit.", "error")
            return render_template("unit_form.html", item=None, unit=unit, form=form), 400
        conn.execute(
            """UPDATE units SET serial_no = ?, eam_no = ?, site = ?, funding = ?, project = ?,
               project_code = ?, notes = ?, updated_at = ? WHERE id = ?""",
            (serial, eam, site, funding, project, project_code, notes, now_str(), unit_id),
        )
        conn.commit()
        flash(f"Saved changes to unit {eam or serial}.", "success")
        return redirect(url_for("item_detail", item_id=unit["item_id"]))
    return render_template("unit_form.html", item=None, unit=unit, form={})


@app.route("/units/<int:unit_id>/delete", methods=["POST"])
def delete_unit(unit_id):
    unit = get_unit_or_404(unit_id)
    conn = database.get_db()
    conn.execute("DELETE FROM units WHERE id = ?", (unit_id,))
    conn.commit()
    flash(f"Deleted unit {unit['eam_no'] or unit['serial_no']}. Its past movements stay in history.", "success")
    return redirect(url_for("item_detail", item_id=unit["item_id"]))


# ---------------------------------------------------------------------------
# Stock in / out
# ---------------------------------------------------------------------------

def _stock_form(tx_type):
    conn = database.get_db()
    item_rows = conn.execute(
        "SELECT * FROM items ORDER BY name COLLATE NOCASE"
    ).fetchall()
    accessory = conn.execute(
        f"""SELECT * FROM items WHERE sku IN
            ({",".join("?" * len(ACCESSORY_SKUS))})""",
        sorted(ACCESSORY_SKUS),
    ).fetchall()
    return render_template(
        "scan_form.html",
        tx_type=tx_type,
        items=item_rows,
        items_by_sku={r["sku"].upper(): r for r in accessory},
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
                    # Only keyboards and mice are untagged; everything else
                    # moves as individually scanned units.
                    if item["sku"].upper() not in ACCESSORY_SKUS:
                        flash(
                            f"“{item['name']}” is tracked per unit — scan its "
                            "EAM tag or serial number instead. Nothing was recorded.",
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
            missing = [label for label, key in (
                ("Custodian", "person"),
                ("Department", "department"),
                ("Ticket / request no.", "reference"),
            ) if not form.get(key, "").strip()]
            if missing:
                flash("On-loan batches need " + ", ".join(missing) +
                      ". Nothing was recorded.", "error")
                return _stock_form(tx_type), 400

        person = form.get("person", "").strip()
        dept = form.get("department", "").strip()
        ref_no = form.get("reference", "").strip()
        staff_id = form.get("staff_id", "").strip()
        site = site_for(form.get("site")) or ("Site A" if tx_type == "IN" else "")
        notes = form.get("notes", "").strip()
        now = now_str()
        moved = 0
        condemned_back = []  # codes of condemned units coming back in
        try:
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
                           (item_id, unit_id, type, quantity, person, department, reference, staff_id, site, on_loan, condemned, notes, created_at)
                           VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?)""",
                        (unit["item_id"], unit["id"], tx_type, person, dept, ref_no, staff_id,
                         site, 1 if on_loan else 0, 1 if condemned else 0, notes, now),
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
                           (item_id, unit_id, type, quantity, person, department, reference, staff_id, site, notes, created_at)
                           VALUES (?,NULL,?,?,?,?,?,?,?,?,?)""",
                        (op["ref"], tx_type, op["qty"], person, dept, ref_no, staff_id,
                         site, notes, now),
                    )
                    moved += op["qty"]
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            flash("Could not save the batch. Please try again.", "error")
            return _stock_form(tx_type), 500

        direction = "into" if tx_type == "IN" else "out of"
        flash(
            f"Recorded {moved} unit{'s' if moved != 1 else ''} across "
            f"{len(ops)} row{'s' if len(ops) != 1 else ''} {direction} stock"
            f"{' — marked as on loan' if on_loan else ''}"
            f"{' — marked as condemned' if condemned else ''}.",
            "success",
        )
        if condemned_back:
            flash(
                "Condemned stock returned: " + ", ".join(condemned_back) +
                " — inspect and quarantine before shelving.", "warning",
            )
        return redirect(url_for("history"))

    return _stock_form(tx_type)


@app.route("/stock/in", methods=["GET", "POST"])
def stock_in():
    return _record_stock("IN")


@app.route("/stock/out", methods=["GET", "POST"])
def stock_out():
    return _record_stock("OUT")


# ---------------------------------------------------------------------------
# History, export, JSON API
# ---------------------------------------------------------------------------

@app.route("/history")
def history():
    where, params, tx_type, q, date_from, date_to = history_filters(request.args)
    conn = database.get_db()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM transactions t JOIN items i ON i.id = t.item_id{where}",
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
         "Condemned", "Notes"]
    )
    for r in rows:
        writer.writerow(
            [
                r["created_at"], r["type"], csv_safe(r["item_name"]), csv_safe(r["item_sku"]),
                csv_safe(r["unit_eam"] or ""), csv_safe(r["unit_serial"] or ""),
                r["quantity"], csv_safe(r["person"]), csv_safe(r["department"]),
                csv_safe(r["reference"]), csv_safe(r["site"]), csv_safe(r["staff_id"]),
                "Yes" if r["on_loan"] else "", "Yes" if r["condemned"] else "",
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
                  i.id, i.name, i.sku,
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
        f"""SELECT i.id, i.name, i.sku, i.quantity, {UNITS_IN_SQL} AS units_in
            FROM items i WHERE i.sku = ? COLLATE NOCASE LIMIT 1""",
        (code,),
    ).fetchone()
    if item is not None:
        return jsonify({"type": "item", **dict(item)})
    return jsonify({"error": "not found"}), 404


@app.route("/api/items")
def api_items():
    rows = database.get_db().execute(
        f"""SELECT i.id, i.name, i.sku, i.category, i.brand, i.model,
                   i.quantity AS untagged, i.min_quantity, i.location,
                   {UNITS_IN_SQL} AS tracked_in, {UNITS_OUT_SQL} AS tracked_out
            FROM items i ORDER BY i.name COLLATE NOCASE"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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

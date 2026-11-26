"""SQLite storage layer for IT Stock Manager.

Everything lives in a single stock.db file next to app.py, so the whole app
can be copied to any Windows 11 PC and run without installing a database
server or needing admin rights.

Model:
- items  — the product catalog (Dell Latitude 5440, HDMI cable…). `quantity`
           counts *untagged* stock (bulk consumables without asset tags).
- units  — individually tracked physical assets: one row per unit, each with
           its own serial number and unique EAM tag, and a status of
           in_stock / checked_out.
- transactions — every movement; unit_id links the exact asset when a
           tracked unit moved.
"""

import re
import sqlite3
from datetime import datetime

from flask import current_app, g

# Kept for older call sites; UI categories come from app.IMPORT_ITEM_CHOICES.
PRESET_CATEGORIES = [
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
    "Cable",
    "Speaker",
    "Headset",
    "Webcam",
    "Printer",
    "Network",
    "Storage",
    "Mobile",
    "Accessory",
    "Other",
]

# Fixed accessory SKUs → category label (must match IMPORT_ITEM_CHOICES).
ACCESSORY_SKU_CATEGORIES = {
    "MS-WIRED": "Mouse (Wired)",
    "MS-WIRELESS": "Mouse (Wireless)",
    "KB-WIRED": "Keyboard (Wired)",
    "KB-WIRELESS": "Keyboard (Wireless)",
    "LAN-3M": "LAN Cable (3 Metres)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    sku          TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'Other',
    brand        TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    quantity     INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    min_quantity INTEGER NOT NULL DEFAULT 0 CHECK (min_quantity >= 0),
    location     TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    deleted_at   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_sku ON items (sku)
    WHERE sku <> '' AND (deleted_at IS NULL OR deleted_at = '');

CREATE TABLE IF NOT EXISTS units (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    serial_no    TEXT NOT NULL DEFAULT '',
    eam_no       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'in_stock' CHECK (status IN ('in_stock', 'checked_out')),
    on_loan      INTEGER NOT NULL DEFAULT 0,
    condemned    INTEGER NOT NULL DEFAULT 0,
    site         TEXT NOT NULL DEFAULT '',
    funding      TEXT NOT NULL DEFAULT '',
    project      TEXT NOT NULL DEFAULT '',
    project_code TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    deleted_at   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_units_eam ON units (eam_no)
    WHERE eam_no <> '' AND (deleted_at IS NULL OR deleted_at = '');
CREATE UNIQUE INDEX IF NOT EXISTS idx_units_serial ON units (serial_no)
    WHERE serial_no <> '' AND (deleted_at IS NULL OR deleted_at = '');
CREATE INDEX IF NOT EXISTS idx_units_item ON units (item_id);
CREATE INDEX IF NOT EXISTS idx_units_status ON units (status);

CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER REFERENCES items (id) ON DELETE SET NULL,
    unit_id    INTEGER REFERENCES units (id) ON DELETE SET NULL,
    type       TEXT NOT NULL CHECK (type IN ('IN', 'OUT', 'DELETE', 'EDIT')),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    person     TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    reference  TEXT NOT NULL DEFAULT '',
    staff_id   TEXT NOT NULL DEFAULT '',
    site       TEXT NOT NULL DEFAULT '',
    on_loan    INTEGER NOT NULL DEFAULT 0,
    condemned  INTEGER NOT NULL DEFAULT 0,
    follow_up  INTEGER NOT NULL DEFAULT 0,
    notes      TEXT NOT NULL DEFAULT '',
    item_name  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_item ON transactions (item_id);
CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions (created_at);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- Stock-out kits (e.g. "OA PC" = 1 PC + 1 Monitor + wired KB/MS).
CREATE TABLE IF NOT EXISTS packages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS package_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id  INTEGER NOT NULL REFERENCES packages (id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    match_type  TEXT NOT NULL CHECK (match_type IN ('category', 'sku', 'item')),
    match_value TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_package_lines_pkg ON package_lines (package_id);

-- Stock cases (auto ticket numbers: KCCITD-YYYYMMDD-NNN-IN/OUT/DELETE).
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no   TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL CHECK (type IN ('IN', 'OUT', 'DELETE')),
    seq         INTEGER NOT NULL CHECK (seq >= 0 AND seq <= 999),
    person      TEXT NOT NULL DEFAULT '',
    department  TEXT NOT NULL DEFAULT '',
    staff_id    TEXT NOT NULL DEFAULT '',
    site        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    on_loan     INTEGER NOT NULL DEFAULT 0,
    condemned   INTEGER NOT NULL DEFAULT 0,
    follow_up   INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets (created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_type ON tickets (type);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)


def init_db(db):
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()


def _migrate(db):
    """Bring databases created by older versions up to the current model.

    v3 introduced per-unit tracking. Older databases keep item-level
    serial_no/eam_no columns; the single tagged unit each item described is
    converted into a real units row and the remainder stays untagged stock.
    v4 adds the users table for sign-in (created by SCHEMA above, nothing
    to convert).
    v5 adds users.session_version so sessions can be killed server-side when
    a password changes or an account is removed.
    v6 adds transactions.staff_id so the operator's staff ID (typed or
    card-scanned on the stock in/out pages) is stored with each movement.
    v7 adds units.funding / project / project_code so each asset can carry
    its purchasing source (imported from Excel or set on the unit form).
    v8 adds site tracking: units.site is the shelf the asset sits on now
    (set on every stock-in), transactions.site records where a movement went.
    v9 adds on-loan tracking: transactions.on_loan marks a stock-out batch
    handed out as a loan; units.on_loan stays set on the unit until it is
    returned (stocked back in).
    v10 adds condemnation: transactions.condemned marks a stock-out batch
    written off as condemned; units.condemned stays set on the unit so any
    later stock-in of that unit raises a warning.
    v11 adds packages / package_lines for stock-out kit checklists
    (e.g. OA PC = 1 PC + 1 Monitor + wired keyboard + mouse).
    v12 adds transactions.follow_up for stock-out batches that need
    a later chase (and triggers admin mail when custodian is blank).
    v13 adds tickets (cases) with auto numbers KCCITD-YYYYMMDD-NNN-IN/OUT.
    v14 replaces broad “Peripheral” with specific categories (Mouse, Keyboard,
    Cable, Speaker, Headset, Webcam, …) on fixed accessory SKUs and best-effort
    rename of other Peripheral rows from the product name.
    v15 aligns fixed accessory SKU categories with IMPORT_ITEM_CHOICES labels
    (e.g. Mouse → Mouse (Wired), Cable → LAN Cable (3 Metres) for LAN-3M).
    v16 widens transactions.type for DELETE / EDIT (stocked-in date), keeps
    item_name snapshot, and uses ON DELETE SET NULL so history survives deletes.
    v17 allows tickets.type = DELETE for justified product / unit removals.
    v18 adds items.deleted_at / units.deleted_at for soft delete (hidden when set).
    v19 makes unique SKU / EAM / S/N indexes ignore soft-deleted rows.
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 3:
        tx_cols = {r["name"] for r in db.execute("PRAGMA table_info(transactions)")}
        if "unit_id" not in tx_cols:
            db.execute(
                "ALTER TABLE transactions ADD COLUMN unit_id INTEGER "
                "REFERENCES units (id) ON DELETE SET NULL"
            )
        db.execute("CREATE INDEX IF NOT EXISTS idx_tx_unit ON transactions (unit_id)")
        item_cols = {r["name"] for r in db.execute("PRAGMA table_info(items)")}
        if "serial_no" in item_cols:
            legacy = db.execute(
                "SELECT id, serial_no, eam_no, quantity FROM items "
                "WHERE serial_no <> '' OR eam_no <> ''"
            ).fetchall()
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            for r in legacy:
                db.execute(
                    """INSERT OR IGNORE INTO units
                       (item_id, serial_no, eam_no, status, notes, created_at, updated_at)
                       VALUES (?,?,?,'in_stock','Migrated from item record',?,?)""",
                    (r["id"], r["serial_no"], r["eam_no"], now, now),
                )
                db.execute(
                    "UPDATE items SET quantity = MAX(0, quantity - 1) WHERE id = ?",
                    (r["id"],),
                )
    user_cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    if "session_version" not in user_cols:
        db.execute(
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"
        )
    tx_cols = {r["name"] for r in db.execute("PRAGMA table_info(transactions)")}
    if "staff_id" not in tx_cols:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN staff_id TEXT NOT NULL DEFAULT ''"
        )
    if "site" not in tx_cols:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN site TEXT NOT NULL DEFAULT ''"
        )
    if "on_loan" not in tx_cols:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN on_loan INTEGER NOT NULL DEFAULT 0"
        )
    if "condemned" not in tx_cols:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN condemned INTEGER NOT NULL DEFAULT 0"
        )
    unit_cols = {r["name"] for r in db.execute("PRAGMA table_info(units)")}
    for col in ("funding", "project", "project_code", "site"):
        if col not in unit_cols:
            db.execute(f"ALTER TABLE units ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    if "on_loan" not in unit_cols:
        db.execute(
            "ALTER TABLE units ADD COLUMN on_loan INTEGER NOT NULL DEFAULT 0"
        )
    if "condemned" not in unit_cols:
        db.execute(
            "ALTER TABLE units ADD COLUMN condemned INTEGER NOT NULL DEFAULT 0"
        )
    # v11 — packages may already exist from SCHEMA on a fresh DB; CREATE IF NOT
    # EXISTS is enough to bring older files up without rewriting tables.
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS packages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
            notes      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS package_lines (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id  INTEGER NOT NULL REFERENCES packages (id) ON DELETE CASCADE,
            label       TEXT NOT NULL,
            match_type  TEXT NOT NULL CHECK (match_type IN ('category', 'sku', 'item')),
            match_value TEXT NOT NULL,
            quantity    INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
            sort_order  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_package_lines_pkg ON package_lines (package_id);
        """
    )
    tx_cols = {r["name"] for r in db.execute("PRAGMA table_info(transactions)")}
    if "follow_up" not in tx_cols:
        db.execute(
            "ALTER TABLE transactions ADD COLUMN follow_up INTEGER NOT NULL DEFAULT 0"
        )
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_no   TEXT NOT NULL UNIQUE,
            type        TEXT NOT NULL CHECK (type IN ('IN', 'OUT')),
            seq         INTEGER NOT NULL CHECK (seq >= 0 AND seq <= 999),
            person      TEXT NOT NULL DEFAULT '',
            department  TEXT NOT NULL DEFAULT '',
            staff_id    TEXT NOT NULL DEFAULT '',
            site        TEXT NOT NULL DEFAULT '',
            notes       TEXT NOT NULL DEFAULT '',
            on_loan     INTEGER NOT NULL DEFAULT 0,
            condemned   INTEGER NOT NULL DEFAULT 0,
            follow_up   INTEGER NOT NULL DEFAULT 0,
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets (created_at);
        CREATE INDEX IF NOT EXISTS idx_tickets_type ON tickets (type);
        """
    )
    # v14 — split Peripheral into specific categories.
    if version < 14:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for sku, cat in ACCESSORY_SKU_CATEGORIES.items():
            db.execute(
                "UPDATE items SET category = ?, updated_at = ? "
                "WHERE sku = ? COLLATE NOCASE AND category <> ? COLLATE NOCASE",
                (cat, now, sku, cat),
            )
        peripheral_rows = db.execute(
            "SELECT id, name, model, sku FROM items "
            "WHERE category = 'Peripheral' COLLATE NOCASE"
        ).fetchall()
        for r in peripheral_rows:
            sku = (r["sku"] or "").upper()
            if sku in ACCESSORY_SKU_CATEGORIES:
                continue  # already handled
            blob = f"{r['name'] or ''} {r['model'] or ''}".lower()
            if re.search(r"\b(keyboards?)\b", blob):
                cat = "Keyboard"
            elif re.search(r"\b(mouse|mice)\b", blob):
                cat = "Mouse"
            elif re.search(r"\b(speakers?)\b", blob):
                cat = "Speaker"
            elif re.search(r"\b(headsets?|headphones?)\b", blob):
                cat = "Headset"
            elif re.search(r"\b(webcams?|web\s*cams?)\b", blob):
                cat = "Webcam"
            elif re.search(r"\b(cables?|lan|ethernet|cat\s*[56])\b", blob):
                cat = "Cable"
            else:
                cat = "Accessory"
            db.execute(
                "UPDATE items SET category = ?, updated_at = ? WHERE id = ?",
                (cat, now, r["id"]),
            )
        # Package checklist lines that matched the old broad category.
        db.execute(
            """UPDATE package_lines SET match_value = 'Accessory'
               WHERE match_type = 'category'
                 AND match_value = 'Peripheral' COLLATE NOCASE"""
        )
    # v15 — accessory SKU categories match Excel IMPORT_ITEM_CHOICES labels.
    if version < 15:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for sku, cat in ACCESSORY_SKU_CATEGORIES.items():
            db.execute(
                "UPDATE items SET category = ?, updated_at = ? "
                "WHERE sku = ? COLLATE NOCASE AND category <> ? COLLATE NOCASE",
                (cat, now, sku, cat),
            )
        # Short categories from v14 → wired/wireless defaults when unambiguous.
        for old, new in (
            ("Mouse", "Mouse (Wired)"),
            ("Keyboard", "Keyboard (Wired)"),
        ):
            db.execute(
                "UPDATE items SET category = ?, updated_at = ? "
                "WHERE category = ? COLLATE NOCASE "
                "AND (sku = '' OR UPPER(sku) NOT IN ('MS-WIRELESS','KB-WIRELESS'))",
                (new, now, old),
            )
        db.execute(
            "UPDATE items SET category = 'Mouse (Wireless)', updated_at = ? "
            "WHERE category = 'Mouse' COLLATE NOCASE AND UPPER(sku) = 'MS-WIRELESS'",
            (now,),
        )
        db.execute(
            "UPDATE items SET category = 'Keyboard (Wireless)', updated_at = ? "
            "WHERE category = 'Keyboard' COLLATE NOCASE AND UPPER(sku) = 'KB-WIRELESS'",
            (now,),
        )
    # v16 — audit history: DELETE / EDIT types, item_name, survive item deletes.
    if version < 16:
        db.executescript(
            """
            CREATE TABLE transactions_v16 (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    INTEGER REFERENCES items (id) ON DELETE SET NULL,
                unit_id    INTEGER REFERENCES units (id) ON DELETE SET NULL,
                type       TEXT NOT NULL CHECK (type IN ('IN', 'OUT', 'DELETE', 'EDIT')),
                quantity   INTEGER NOT NULL CHECK (quantity > 0),
                person     TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                reference  TEXT NOT NULL DEFAULT '',
                staff_id   TEXT NOT NULL DEFAULT '',
                site       TEXT NOT NULL DEFAULT '',
                on_loan    INTEGER NOT NULL DEFAULT 0,
                condemned  INTEGER NOT NULL DEFAULT 0,
                follow_up  INTEGER NOT NULL DEFAULT 0,
                notes      TEXT NOT NULL DEFAULT '',
                item_name  TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            INSERT INTO transactions_v16 (
                id, item_id, unit_id, type, quantity, person, department, reference,
                staff_id, site, on_loan, condemned, follow_up, notes, item_name, created_at
            )
            SELECT t.id, t.item_id, t.unit_id, t.type, t.quantity, t.person, t.department,
                   t.reference, t.staff_id, t.site, t.on_loan, t.condemned, t.follow_up,
                   t.notes, COALESCE(i.name, ''), t.created_at
            FROM transactions t
            LEFT JOIN items i ON i.id = t.item_id;
            DROP TABLE transactions;
            ALTER TABLE transactions_v16 RENAME TO transactions;
            CREATE INDEX IF NOT EXISTS idx_tx_item ON transactions (item_id);
            CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions (created_at);
            CREATE INDEX IF NOT EXISTS idx_tx_unit ON transactions (unit_id);
            """
        )
    # v17 — tickets may be DELETE (justified product / unit removals).
    if version < 17:
        db.executescript(
            """
            CREATE TABLE tickets_v17 (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no   TEXT NOT NULL UNIQUE,
                type        TEXT NOT NULL CHECK (type IN ('IN', 'OUT', 'DELETE')),
                seq         INTEGER NOT NULL CHECK (seq >= 0 AND seq <= 999),
                person      TEXT NOT NULL DEFAULT '',
                department  TEXT NOT NULL DEFAULT '',
                staff_id    TEXT NOT NULL DEFAULT '',
                site        TEXT NOT NULL DEFAULT '',
                notes       TEXT NOT NULL DEFAULT '',
                on_loan     INTEGER NOT NULL DEFAULT 0,
                condemned   INTEGER NOT NULL DEFAULT 0,
                follow_up   INTEGER NOT NULL DEFAULT 0,
                created_by  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            );
            INSERT INTO tickets_v17 (
                id, ticket_no, type, seq, person, department, staff_id, site, notes,
                on_loan, condemned, follow_up, created_by, created_at
            )
            SELECT id, ticket_no, type, seq, person, department, staff_id, site, notes,
                   on_loan, condemned, follow_up, created_by, created_at
            FROM tickets;
            DROP TABLE tickets;
            ALTER TABLE tickets_v17 RENAME TO tickets;
            CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets (created_at);
            CREATE INDEX IF NOT EXISTS idx_tickets_type ON tickets (type);
            """
        )
    # v18 — soft delete: deleted_at on items and units (hidden when set).
    # Unique EAM / S/N only among active (non-deleted) units so codes can be
    # re-registered after soft-delete; restore fails if the code is already live.
    if version < 18:
        cols_items = {r[1] for r in db.execute("PRAGMA table_info(items)").fetchall()}
        if "deleted_at" not in cols_items:
            db.execute(
                "ALTER TABLE items ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''"
            )
        cols_units = {r[1] for r in db.execute("PRAGMA table_info(units)").fetchall()}
        if "deleted_at" not in cols_units:
            db.execute(
                "ALTER TABLE units ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''"
            )
        db.execute("DROP INDEX IF EXISTS idx_units_eam")
        db.execute("DROP INDEX IF EXISTS idx_units_serial")
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_units_eam ON units (eam_no)
               WHERE eam_no <> '' AND (deleted_at IS NULL OR deleted_at = '')"""
        )
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_units_serial ON units (serial_no)
               WHERE serial_no <> '' AND (deleted_at IS NULL OR deleted_at = '')"""
        )
    # v19 — unique SKU / EAM / S/N ignore soft-deleted rows.
    if version < 19:
        db.execute("DROP INDEX IF EXISTS idx_items_sku")
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_items_sku ON items (sku)
               WHERE sku <> '' AND (deleted_at IS NULL OR deleted_at = '')"""
        )
        db.execute("DROP INDEX IF EXISTS idx_units_eam")
        db.execute("DROP INDEX IF EXISTS idx_units_serial")
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_units_eam ON units (eam_no)
               WHERE eam_no <> '' AND (deleted_at IS NULL OR deleted_at = '')"""
        )
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_units_serial ON units (serial_no)
               WHERE serial_no <> '' AND (deleted_at IS NULL OR deleted_at = '')"""
        )
        db.execute("PRAGMA user_version = 19")

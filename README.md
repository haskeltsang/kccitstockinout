# IT Stock Manager

An in/out stock system for an office IT team. Track printers, PCs, monitors,
peripherals, cables and any other equipment as it moves **in** to the storeroom
(supplier deliveries, returns) and **out** to colleagues (new joiners,
replacements, event kits).

**End-user how-to:** see [USER_GUIDE.md](USER_GUIDE.md) (stock in/out, tickets,
packages, import).

Built with Python + Flask + SQLite. It runs on a **Windows 11 PC without admin
rights** — no installer, no database server, no services. Everything is stored
in a single `stock.db` file next to the app.

## How stock is modelled

Two kinds of stock, side by side:

- **Tracked units** — one row per physical asset: each laptop, monitor or
  printer has its own **serial number** and a **unique EAM tag**, and a status
  of *on the shelf* or *checked out* (and who has it). Scanning a tag always
  identifies one specific unit.
- **Untagged stock** — quantity items (keyboards, mice, cables, speakers,
  etc.) under specific categories. Standard KB/MS/LAN use fixed SKUs:
  `MS-WIRED`, `MS-WIRELESS`, `KB-WIRED`, `KB-WIRELESS`, `LAN-3M`.

An item's *In stock* number is tracked units on the shelf plus untagged stock.

## Features

- **Sign-in required** — nobody reaches the inventory without an account.
  The first launch asks you to create the first account, which requires the
  **setup code** printed in the console window (or the `STOCK_SETUP_CODE`
  value, when set) so a fresh copy can't be claimed by a stranger; add more
  colleagues from the **Account** page. Passwords are stored as salted hashes
  (Werkzeug PBKDF2), never in plain text, and repeated wrong passwords lock
  the username for two minutes. Sessions are re-checked against the account
  list on every request and expire after 7 days — removing a login or
  changing a password signs out its other browsers.
- **Excel import** — upload your asset list (`.xlsx` or CSV) and every row
  becomes one tracked unit. Columns: **Item, Model, Asset/EAM, S/N, Tagged, Quantity**, …
  Items are created automatically from the file — no manual setup. Rows
  already registered are skipped, so re-importing the same file is safe.
- **Dashboard** — units on the shelf, low-stock alerts, hand-outs this month,
  recent activity, stock by category.
- **Items** — browse what you stock with search and filters. Items come from
  the Excel import, not from manual data entry.
- **Units** — register tagged assets per item, edit or retire them, and see at
  a glance which unit is with which colleague. Filter the unit list by status
  (on shelf / checked out) or by EAM, S/N, holder or notes text.
- **Barcode scanning** — on the Stock In / Out pages, scan an EAM tag or
  serial number with a barcode scanner (or type the code). Each scan adds one
  unit to an on-screen list; press **Finish** to record the whole batch in one
  go — perfect for deliveries or hand-outs of several units at a time.
- **Stock In** — scanning a known checked-out unit marks it as returned;
  scanning an unknown code registers a brand-new unit under the chosen
  product; untagged stock can be topped up by quantity. Supplier and
  PO/delivery-order reference recorded per batch.
- **Stock Out** — hand units to a colleague with department and ticket number.
  You cannot hand out a unit that is already checked out, nor more untagged
  stock than is on the shelf.
- **History** — full audit trail of every movement with search, type and date
  filters, plus **CSV export** (history and item list) for Excel.
- **JSON API** — read-only `GET /api/items` and `GET /api/summary` if you want
  to hook it into another internal tool.
- Works **fully offline** — no CDNs, no external services.

## Quick start on Windows 11 (no admin rights)

1. **Install Python (once).** Either:
   - Open the **Microsoft Store**, search for **Python 3.12** and click *Get*
     (installs per-user, no admin needed), or
   - Download from <https://www.python.org/downloads/> and during setup choose
     **Install for me only** and tick **Add python.exe to PATH**.
2. **Double-click `start-windows.bat`** in this folder.
   - The first run creates a private Python environment and installs the
     dependencies — Flask, the waitress web server, openpyxl (needs internet, once).
   - Your browser opens at <http://localhost:8077>.
3. **Create the first account** on the sign-in page (first launch only) —
   the form asks for the **setup code** shown in the console window, which
   proves it's you at the keyboard and not someone else on the network.
   Then you're in. Add more logins for colleagues later from the
   **Account** page — everyone should change their own password after
   first signing in.
4. Keep the black console window open while using the app; close it to stop.

The inventory starts empty — load your own asset list via **Import**.
(To reset later: stop the server, delete `stock.db`, start again.)

### Let colleagues use it from their own PCs

Run the app on one always-on PC, started so it accepts connections from
other PCs:

```bat
python app.py --host 0.0.0.0
```

Colleagues then open `http://<that-pc's-IP>:8077` (find the IP with
`ipconfig`) and sign in with a login you created for them on the **Account**
page. By default the app listens on this PC only (`127.0.0.1`), so
sharing is an explicit choice. Windows may show a firewall prompt the first
time — choose *Allow* on private networks (on some company PCs this prompt
needs IT to approve; without it, only the local PC can use the app).

## Importing stock

The **Import** page offers two choices:

- **Mouse / Keyboard** — no file needed. Pick the type and wired/wireless,
  enter the quantity, done. Each combination is one item
  (*Mouse (Wired)*, *Keyboard (Wireless)*, …) created automatically the first
  time. These are the only items tracked by quantity alone.
- **Other items (Excel / CSV)** — upload the asset list as below. Keyboard
  and mouse rows in the file are rejected with a pointer to the quantity
  form, since they have no asset tags.

The quickest way to load your storeroom is **Import** in the sidebar:

1. Click **Download Excel template** (or use your own file) — one row per
   physical unit with header order: **Item**, **Model**, **Asset/EAM**,
   **S/N**, **Tagged**, **Quantity**, **Brand**, **Funding**, **Project**,
   **Project Code**, **Site**. **Item is the category** and **Model is the
   product**; rows with the same Item + Model group under one item.
2. Upload the `.xlsx` (or a CSV with the same columns). Every row becomes a
   unit *on the shelf*.
3. The result report shows each row as **Imported**, **Skipped** (code already
   registered — re-imports never duplicate) or **Error** (missing model or
   both codes), so you can fix and re-upload just the problem rows.

Tips: format the code columns as *Text* in Excel so leading zeros survive, and
add an optional reference (e.g. the PO number) to stamp the whole batch in the
history. Header names are flexible — “Product”, “Asset Tag”, “Serial Number”
and similar variants are recognised.

## Daily workflow

| Situation | What to do |
| --- | --- |
| First setup / new batch of assets | **Import** → upload the Excel file |
| New equipment arrives | **Stock In** → scan each unit's EAM/SN, supplier + PO no., Finish |
| Colleague returns a laptop | **Stock In** → scan its tag — it's queued as a return automatically |
| Colleague needs a monitor/PC/printer | **Stock Out** → scan the unit's tag, recipient + ticket no., Finish |
| Who has the ThinkVision with EAM-3101? | Search `EAM-3101` in **Items**, or check the item's Tracked units table |
| Running low | Set a **warning level** on the item's page — the Dashboard **Low stock** card then lists it whenever stock drops to that number |
| Monthly report | **History → Export CSV** (includes per-unit EAM/SN columns) |

## Barcode scanning workflow

Any USB or Bluetooth barcode scanner works — it behaves like a keyboard,
typing the code and pressing Enter. No drivers or admin rights needed.

**Stock In (deliveries and returns):**

1. Open **Stock In** and click the scan box once.
2. Scan each unit's **EAM tag** or **serial number**:
   - A known checked-out unit → queued as a **return** (its status flips back
     to *on the shelf*).
   - An unknown code → queued as a **new unit** under the product chosen in
     *Register new units as*. Use the **EAM no. / S/N** toggle to say which
     code you are scanning; scan the box's SKU barcode first to set the
     product automatically.
3. Untagged keyboards and mice? Type their SKU (e.g. `MS-WIRED`) into the
   scan box — each Enter adds one more to the list.
4. Fill in the supplier and PO number once for the batch, then press
   **Finish**. Everything is recorded in a single transaction.

**Stock Out (hand-outs):**

1. Open **Stock Out** and scan each unit you hand over — every tag identifies
   one specific unit, so there is no quantity to enter for tracked assets.
2. Keyboards and mice need no scan — enter the quantities in the
   **Keyboards & mice** panel (only those four items appear there). Typing
   their SKU (`KB-WIRELESS` …) into the scan box works too.
3. Fill in the recipient, department and ticket number, then press **Finish**.
   A unit that is already checked out, or an unknown tag, is rejected on the
   spot — and the server re-checks the whole batch atomically, so a mistake
   can never record half a batch.

No scanner? Register units from the item page (*Register unit*) and type the
codes into the scan box by hand.

## Data & backup

All data lives in `stock.db` in this folder. To back up, copy that one file
somewhere safe (or into your synced folder). To move the app to another PC,
copy the whole folder including `stock.db`.

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8077` | Port to listen on |
| `HOST` | `127.0.0.1` | Bind address (`0.0.0.0` = other PCs can connect) |
| `STOCK_DB` | `./stock.db` | Database file location |
| `SECRET_KEY` | auto-generated | Session signing key (auto-saved to `.secret_key`) |
| `STOCK_COOKIE_SECURE` | auto | Session cookie is marked `Secure` automatically on HTTPS requests; set `1` to force it |
| `STOCK_SETUP_CODE` | generated each start | Code required to create the first account. Set it to keep one fixed code — required for a hosted deployment, where the console isn't visible |
| `STOCK_SESSION_MAX_AGE` | `604800` (7 days) | Absolute session lifetime in seconds before sign-in is required again |

Command line: `python app.py --host 127.0.0.1 --port 9000`

## Run on macOS / Linux

```bash
./start-mac-linux.sh        # or: python3 -m venv .venv && . .venv/bin/activate
                            #     pip install -r requirements.txt && python app.py
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Project layout:

```
app.py            Flask routes, sign-in, Excel/CSV import, validation, CSV export, JSON API
db.py             SQLite schema, connection handling, migrations
templates/        Jinja2 pages (login, dashboard, items, import, scan, history)
static/           style.css + app.js (no external assets)
stock.db          created on first run (git-ignored)
start-windows.bat double-click launcher for Windows 11
```

No admin rights are required at any step: Python installs per-user, the
virtual environment and Flask install into this folder, and the server binds
an unprivileged port.

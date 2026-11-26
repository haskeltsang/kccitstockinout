# IT Stock Manager — User Guide

Day-to-day guide for recording stock in/out, importing assets, tickets, and kits.

---

## 1. What this app tracks

| Kind | Examples | How it moves |
|------|----------|--------------|
| **Tagged units** | PC, laptop, monitor, iPad | One physical unit = one S/N and/or EAM. Scan the code. |
| **Untagged stock** | Keyboard, mouse, cable, speaker, headset, etc. | No barcode. Move by **quantity** (or type SKU for standard KB/MS/LAN-3M). |

Use specific categories (**Mouse**, **Keyboard**, **Cable**, **Speaker**, **Headset**, **Webcam**, …) — avoid a broad “Peripheral” label.

**In stock** for a product = untagged quantity on the shelf + tagged units still on the shelf.

---

## 2. Sign in

1. Open the app in your browser (usually `http://localhost:8077`).
2. Sign in with the username/password your team gave you.
3. Use **Account** to change your password. Ask an existing user to create new logins there.

Keep the console/start window open while you work; closing it stops the app.

---

## 3. Sidebar — where to go

| Menu | Use it for |
|------|------------|
| **Stock In** | Deliveries, returns, registering new tagged units, topping up untagged stock |
| **Stock Out** | Handing equipment to staff |
| **Dashboard** | Snapshot: shelf totals, low stock, recent activity |
| **Items** | Browse products, open a product, edit category/model |
| **Import** | Excel/CSV load, and add keyboard/mouse/LAN by quantity |
| **Packages** | Define kits (e.g. OA PC) for stock-out checklists |
| **Tickets** | View each stock in/out case and its line items |
| **History** | Search every movement; export CSV |
| **Account** | Password and user management |
| **Quick find** (top) | Scan/type EAM or S/N → jumps to that asset’s history |

---

## 4. Tickets (auto case numbers)

Every finished **Stock In** or **Stock Out** batch (and justified **deletes** / untagged count changes) gets a ticket number like:

`KCCITD-20261126-003-OUT`

- Format: `KCCITD-YYYYMMDD-000…999-IN`, `-OUT`, or `-DELETE`
- The ticket field on the form is **filled automatically and locked** — you cannot type your own
- After you finish, you land on that ticket’s page (what was moved)
- Open **Tickets** anytime to search cases by number, person, department, or notes

History also links `KCCITD-…` references back to the ticket.

---

## 5. Stock In

### Tagged units (scan)

1. Open **Stock In**.
2. Optionally enter your **Staff ID**.
3. Choose how new codes are registered:
   - **S/N** — serial only (common for new stock)
   - **EAM no.** — asset tag only
   - **S/N + EAM** — both required in the extra fields
   - **N/A** — untagged quantity (no barcode)
4. For a **new product**, choose **New product** and set category (and name/brand as needed). Keyboard/Mouse/LAN Cable (3 Metres) map to shared accessory stock.
5. For an **existing product**, pick it from the list.
6. Scan (or type) each code → it appears in **Scanned items**.
7. Check the locked ticket number, add notes if useful, then **Finish**.

**Tips**

- Scanning a unit that was checked out marks it as **returned** to the shelf.
- Site defaults to Site A for stock in when not set.
- Condemned units that come back raise a warning — inspect before shelving.

### Untagged stock (N/A)

1. Set mode to **N/A**.
2. Pick or create the product (only products **without** S/N/EAM units appear under existing).
3. Enter **quantity** → **Add to list**.
4. Finish the batch as usual.

---

## 6. Stock Out

1. Open **Stock Out**.
2. Enter **Staff ID** if your team uses it.
3. Scan tagged units into the list (EAM or S/N).
4. For untagged items, use the side panel **Untagged items — quantity to hand out**: pick product → quantity → **Add**.  
   Or type accessory SKUs: `MS-WIRED`, `MS-WIRELESS`, `KB-WIRED`, `KB-WIRELESS`, `LAN-3M`.
5. Fill **Custodian** (and **Department** when required). Ticket number is auto-assigned.
6. Optional batch flags:
   - **On loan** — needs custodian + department; units stay trackable as loaned
   - **Condemnation** — write-off (not with on-loan; not with untagged-only batches)
   - **Follow up** — marks the batch for follow-up (stock out without a custodian can also email admin if SMTP is configured)
7. **Finish** — you’re taken to the new ticket.

### OA PC kits

Scanning an **OA PC / desktop PC** (not laptop) auto-adds **1 wired keyboard + 1 wired mouse + 1 × 3 m LAN cable** per PC, if stock allows. Shortfalls are shown in the status line.

To **condemn an OA PC only**, turn on **Condemnation** (kit lines are removed) or press **×** on the keyboard/mouse/LAN rows — Condemnation is units-only, so those accessories must not stay in the batch.

### Package checklist

1. Under **Package checklist**, pick a kit (e.g. OA PC).
2. As you scan/add items, lines tick off (`have / need`).
3. Required counts **scale with the first line** (e.g. 3 PCs → accessories show `/3`).
4. Finish is still allowed if the checklist is incomplete — it’s a guide, not a hard lock.

Create/edit kits under **Packages**.

---

## 7. Packages (kits)

1. Open **Packages** → **New package**.
2. Name the kit (e.g. `OA PC`).
3. Add checklist lines:
   - **Label** — fills from the Matches choice
   - **Match by** — Category, Accessory SKU, or Catalog item
   - **Matches** — what to count on Stock Out
   - **Qty** — how many per kit (scaled when you hand out multiple of the first line)
4. For keyboard/mouse/LAN, Category names match Accessory SKU names (e.g. `Mouse (Wired)` ≡ `MS-WIRED`).

---

## 8. Import

### A. Keyboard / mouse / LAN by quantity

On **Import**, choose **Mouse / Keyboard / LAN cable**:

- Pick type (Mouse, Keyboard, or **LAN Cable (3 Metres)**)
- Wired/Wireless for KB/MS (LAN is fixed at 3 m, SKU `LAN-3M`)
- Enter quantity, site, optional reference → **Add to stock**

### B. Excel / CSV

1. Download the **Excel template**.
2. Column order: **Item**, **Model**, **Asset/EAM**, **S/N**, **Tagged**, **Quantity**, **Brand**, **Funding**, **Project**, **Project Code**, **Site**.
3. Rules for **Tagged**:

| Tagged | What to fill |
|--------|----------------|
| **Yes** | Model + S/N and/or EAM. Leave Quantity blank. |
| **No** | Quantity only. Leave S/N and EAM blank. Model is optional (Item used as name if blank). Brand is ignored (like KB/MS/LAN). |
| Blank | Keyboard/Mouse → treated as untagged; other items → treated as tagged |

4. If a product **already has tagged units** in the database, a **Tagged = No** row is **rejected**.
5. Re-import is safe: matching S/N or EAM can fill missing codes and refresh funding/project when those cells have values.
6. Upload and review the result table (imported / updated / skipped / errors).
7. Every import that adds stock creates **one stock-in ticket** (`KCCITD-…-IN`) for the whole batch — open it from **Tickets** or the result header. Optional PO/ref goes on the ticket notes.

Format S/N and EAM columns as **Text** in Excel so leading zeros are kept.

---

## 9. Items

- Search/filter on **Items**; open a product for units and movement history.
- **Edit category & model** on the product page (display name follows model). Fixed accessories (`MS-*`, `KB-*`, `LAN-3M`) are locked. If the new category + model already exists (e.g. fixing an “Other” import), this product is merged into that one automatically.
- **Edit untagged quantity** — only on products with **no** S/N/EAM units; a **justification is required** and a stock-in or stock-out **ticket** is created for the change.
- **Delete product or unit** — a **justification is required**; a **delete ticket** (`KCCITD-…-DELETE`) is created. Deletes are **soft**: the row stays in the database with a `deleted_at` stamp and is hidden from Items / Stock / lookups. Open the delete ticket and use **Restore** to clear `deleted_at` and show it again.
- **Register unit** / **Edit** unit for S/N, EAM, site, funding, etc.
- **Low-stock warning** — set a minimum; Dashboard alerts when shelf stock drops to that level.

---

## 10. History & export

- **History** — filter by type (**In**, **Out**, **Delete**, **Edit**), dates, and search (EAM, S/N, person, department, ticket, etc.).
- **Delete** events are logged when you soft-delete a product or unit; the item name stays on the history row. Restore from the delete ticket if needed.
- **Edit** events are logged when you change a unit’s **stocked-in date** (notes show old → new).
- **Export CSV** or **Export Excel** from History (current filters) or Items.
  Items Excel uses the same header style as the import template and includes every tagged unit plus untagged quantity rows (status, holder, funding, etc.).

---

## 11. Typical workflows

### New delivery of tagged PCs

1. **Import** the supplier list (Tagged = Yes), *or* **Stock In** → New/Existing product → scan S/N or EAM.
2. Finish → note the ticket on **Tickets**.

### Hand out an OA PC desk kit

1. **Stock Out** → optional package **OA PC**.
2. Scan the PC (KB + MS + LAN-3M auto-add if stocked).
3. Scan monitor if required by the package.
4. Enter custodian + department → Finish.

### Top up mice / keyboards / 3 m LAN

1. **Import** → accessory quantity, *or* Excel with Tagged = No.
2. Hand out later via Stock Out untagged picker or SKU.

### Return of loaned / checked-out unit

1. **Stock In** → scan EAM or S/N → Finish.

---

## 12. Sites

- Storerooms: **Site A** and **Site B**.
- Stock In: blank site → Site A (form default may vary; import blank → Site B).
- Put the unit on the correct site when registering or returning.

---

## 13. Good habits

- Always **Finish** the batch so a ticket is saved; clearing the list discards the draft.
- Prefer scanning over typing codes.
- Use **Tickets** when someone asks “what went out on this request?”
- Fix category/model typos with **Edit** or **Merge**, don’t create a third duplicate.
- Don’t delete `stock.db` unless you intend to wipe all data.

---

## 14. If something goes wrong

| Problem | What to try |
|---------|-------------|
| Can’t sign in | Check username; wait if locked after failed attempts; ask for a password reset via Account |
| Scan does nothing | Click the scan box first; scanner must send Enter after the code |
| “Already checked out” | Return it on Stock In first, or find who has it in History / Items |
| Untagged quantity rejected | Product may already have tagged units — use scan mode, or fix category/model on Items |
| OA PC kit short | Top up KB-WIRED, MS-WIRED, LAN-3M on Import |
| Ticket field greyed out | Expected — numbers are assigned when you finish |

For install, network sharing, and technical settings, see `README.md`.

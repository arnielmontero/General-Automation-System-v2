# MO's To Be Close — Technical Design

Implements the process in [FLOW.md](FLOW.md).

## Decisions

| Topic | Decision |
|---|---|
| Hosting | Company PC / server on the office network, **Docker** |
| Login | Odoo account **email + password** (no API key), checked live against erp.sawo.com |
| Roles | Roles & Permissions page: roles are created by an Admin and get **module checkmarks**; each user gets one role from a dropdown. Built-in: **Admin** (full access), **Checker / Supervisor**, **User** (default) |
| Transfers saved (Step 2) | **All transfers not Done** — PC (Picking Components) and SFP (Store Finished Product) |
| Mark done (Step 3) | **Each MO row and each transfer row** separately, each checked against its own Odoo state |
| Odoo access | **Read-only.** The system never writes to Odoo |

## Stack

| Layer | Choice | Why |
|---|---|---|
| App | **Python 3.12 + Django 5** | Built-in users/permissions, admin screens, migrations, audit-friendly ORM |
| UI | Django templates + Bootstrap 5 + HTMX | Simple server-rendered pages; tick boxes update without page reload |
| Database | **PostgreSQL 16** | Reliable, runs in Docker |
| PDF reading | `pdfplumber` | Reads the packing list text and table positions directly (no AI needed for a fixed layout) |
| Odoo | JSON-RPC (Odoo 17) | Same calls the Odoo web UI uses |
| Excel export | `openpyxl` | |
| PDF export | `ReportLab` | Pure Python, no system packages needed in the container |
| Run | `docker compose`: `web` (Django + Gunicorn) + `db` (PostgreSQL) | One command to start |

Repository layout (room for future systems in the General Automation System):

```
General-Automation-System/
├─ compose.yaml, Dockerfile, .env.example
├─ gas/                  Django project settings
├─ core/                 Login via Odoo, users & roles, audit log, shared Odoo client
└─ mo_close/             System_001 — MO's To Be Close
```

## Login (Odoo account)

1. User enters Odoo email + password on the login page.
2. System calls Odoo `/web/session/authenticate` on erp.sawo.com.
3. Success → a local user is created/updated (Odoo user id, name, email). The **Odoo session** is kept in the
   user's server-side session and used for all Odoo reads, so the system only sees what that user can see in Odoo.
   The password is **never stored**.
4. New users start as **User**. The first Admin is set in `.env` (`ADMIN_ODOO_LOGIN`); Admins change roles.
5. Odoo session expired → user is asked to log in again.
6. Login is by **password only** — no API keys. An account with Odoo two-step verification turned on cannot
   log in; the login page shows a clear message for that case.

## Data model

```
PackingList  (one record per SO)                   ← Step 1
 ├─ PackingLine  (every row from the PDF)           ← Step 1
 └─ MORecord     (matched MO per FG code)           ← Step 2
     └─ TransferRecord  (MO's transfers, not Done)  ← Step 2
AuditLog  (every action, linked to the record)      ← Step 3
```

**PackingList** — `name` (= SO, e.g. S04743, unique), header info from the PDF: Sold To (name + address),
Tel. No., Fax No., ATTN, Date, Invoice No., Container No., Seal No., Destination; uploaded PDF file;
created by / at; status: *Draft → Lines saved → Generated → Closed*.

**PackingLine** — External Code, Description, UM, Number of Packages, Net Weight, Gross Weight, CBM, Qty.
All PDF rows are saved as-is. The record screen shows them **grouped by External Code with Qty summed**
(as in FLOW image 3: S04743 · NRNS-90NS-BL-Z-C · 10).

**MORecord** — SO#, FG Code, FG (description), Qty, MO (reference, e.g. PL3/MO/06873), State, Odoo id,
last synced; plus the Step 3 tracking fields below.

**TransferRecord** — Reference (e.g. PL3/SFP/04631, PL3/PC/17852), Type (PC / SFP from the Odoo operation type),
Status, Odoo id, last synced; plus the Step 3 tracking fields below.

**Step 3 tracking fields** (on both MORecord and TransferRecord):

| Field | Set by |
|---|---|
| User done ✔ + date + who | User ticks the row |
| System check: Odoo Done / Not Done + checked at + Odoo state seen | System, right after the tick (and on Refresh) |
| Final confirmation ✔ + date + who | Checker / Admin only |
| Comment | **Required** when the Checker confirms a row the system did **not** confirm |

**AuditLog** — who, when, action, which record/row, old → new value, comment. Shown per SO record
(“Logs” section) and as a global log for Admins.

## Process per step

### Step 1 — New record + PDF

1. **New Record** → enter SO (e.g. S04743). Duplicate SO is blocked.
2. **Upload PDF** → system reads it and shows a **preview**:
   - Header info (Sold To, Date, Invoice No., Container No., …)
   - All list rows, plus the grouped view (Invoice No · External Code · Description · Qty)
   - Warning if the PDF's Invoice No. ≠ the SO entered.
3. **Save** → header + rows stored.
4. **Records list** → all SO records with status, created by/date, search and filter.

Note: the PDF shows codes with spaces (`NRNS 60NS BL Z C`) while Odoo uses dashes (`NRNS-60NS-BL-Z-C`).
The system stores the code as printed and matches Odoo ignoring spaces/dashes.

### Step 2 — Generate from Odoo

Button **Generate from Odoo** on a saved record:

1. Search Manufacturing Orders where **Source contains the SO** and **State is Confirmed, In Progress
   or To Close**. The correct form is **`S0…`** (`S08027`). Wrong spellings in the Source — `SO8027`, `So8027`,
   `SS08027`, `S8027`, `O8027`, `08027`, `SO# 08027`, and a bare `8027` with no S0 — are **auto-corrected** (still matched) and the MO gets a **fix flag**
   *Source fix: SO8027 → S08027* (also a *Source Fix* column in Excel) so the Source can be corrected in Odoo.
   Never matched as part of another number or a date / time (`S080271`, `PO8027`, `10/16/2026`, `2026-08-07`),
   A bare number (no S / O) is only taken as the SO when it is the only order reference in the Source. It is
   skipped when it is alone in brackets (`UKRAINE(4300)`, `UKRAINE ( 4300 )`), next to another SO
   (`SO6863 - Australia - 8200`: 8200 is the customer code), followed by a model code or quantity (`1414RS`,
   `100pcs`), or the year of a date (`July 30, 2026`, `Jan.08,2026`). And when the number is used as a customer
   code in any Source the search returns (in brackets or next to another SO), bare numbers are ignored for that SO
   altogether, so `Heater for UKRAINE 4300` is not taken for S04300 — only its prefixed spellings count.
2. For each grouped packing line, match MOs whose product **Internal Reference = External Code**.
   Every matching MO in those states is saved.
3. MOs of the SO whose product is **not on the packing list** (components / sub-assemblies made for the SO,
   e.g. `PL1/MO/34770-002` Plastic Backing for S08027) are saved too, flagged *Component · not on packing
   list*. They are tracked like any other MO and must also be confirmed before the record closes
   (decision 2026-10-20).
4. For each saved MO, read its **Transfers** and save those **not Done** (and not Cancelled):
   Reference + Status.
5. **Result view**: SO# · FG Code · FG · Qty · MO · State, with each MO's transfers (Reference · Status)
   listed under it; component MOs follow in their own group. Lines with **no matching MO** are listed
   separately as *No MO found*.

Generate can be run again; it refreshes Odoo values but **keeps** user ticks, confirmations and comments.

### Step 3 — Progress monitoring, mark done, counter check

- **Progress page**: every SO record with progress (e.g. 12 / 30 rows confirmed); open one to see every MO and
  transfer row with its current tracking state. Filters: Not done / Waiting final confirmation / Confirmed.
- **User marks done**: tick on any MO or transfer row → date and user recorded.
- **System counter check**: immediately after the tick the system reads that MO / transfer in Odoo:
  - Odoo state **Done** → *System confirmed* ✔
  - Not Done → *Not confirmed by Odoo* ✖ (row highlighted)
  - Also re-checked with **Refresh from Odoo**.
- **Final confirmation**: only **Checker / Admin**. If the system did not confirm, a **comment is mandatory**.
- **Logs**: every tick, untick, system check, confirmation and comment is logged.
- **Reports**: per SO record, export **Excel** or **PDF** with all rows, transfers, tracking state and dates.
- When every row is finally confirmed, the record becomes **Closed**.

## Configuration (`.env`)

```
ODOO_URL=https://erp.sawo.com
ODOO_DB=<database name>
ADMIN_ODOO_LOGIN=<first admin's Odoo email>
DJANGO_SECRET_KEY=...
POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD
WEB_PORT=8080
TIME_ZONE=Asia/Manila
```

## Needed to build and test

1. **Odoo database name** for erp.sawo.com.
2. **2–3 real packing list PDFs** (put them in `docs/system_001/samples/`) — to build and test the PDF reader.
3. Docker Desktop must be running on the PC that hosts the system.

# General Automation System (SAWO)

A standalone web system for automating SAWO work that is done by hand today. Each automation is its own
module. The first one is **MO's To Be Close** (System_001).

## MO's To Be Close

Packing list → Odoo Manufacturing Orders → tracked until everything is confirmed done.

| Step | What happens |
|---|---|
| 1 | **New Record** named by SO → upload the Odoo packing list PDF → the system separates the information (Sold To, Date, Invoice No., Container No., …) from the list data (External Code, Description, Qty, …) → review → **Save** |
| 2 | **Generate from Odoo**: MOs whose Source contains the SO (correct form S08027; wrong spellings like SO8027, So8027, S8027, O8027, 08027 or a bare 8027 are auto-corrected and get a **Source fix** flag; a bare number is skipped when it is a customer code, model code, quantity or year), in state Confirmed / In Progress / To Close, matched to the packing list External Codes; MOs for products not on the packing list (components) are saved too, in their own group; each MO's transfers (PC, SFP …) that are not Done are saved |
| 3 | **Progress**: users tick each MO / transfer row as done → the system checks Odoo right away (Done in Odoo / not) → a **Checker / Supervisor** gives final confirmation (comment required when Odoo does not show Done). Everything is logged. Excel / PDF reports per SO and for all open items |

Process: [docs/system_001/FLOW.md](docs/system_001/FLOW.md) · Design: [docs/system_001/DESIGN.md](docs/system_001/DESIGN.md)

**Login** is with the Odoo account (email + password) of erp.sawo.com. Passwords are never stored. Odoo is only
read, never changed.

**Roles & Permissions** (Administration): each role is a set of ticked **modules** (Dashboard, Records, New Record,
Generate from Odoo, Mark done, Final confirmation, Progress, Export, Delete, Users, Roles, Logs). Admins create
new roles with **New role** and tick their modules; each user gets one role from a dropdown on **Users**.
Built-in roles: **Admin** (always every module, cannot be changed or deleted), **Checker / Supervisor**, and
**User** (the default role given on first login — any role can be made the default). The Odoo login(s) in
`ADMIN_ODOO_LOGIN` are always Admin. Only an Admin can give or remove the Admin role. Modules are defined in
[core/modules.py](core/modules.py).

## Run (Docker)

Runs as its own Docker project `general-automation-system` (web + PostgreSQL), separate from any other
containers on the PC.

```bash
cp .env.example .env          # set DJANGO_SECRET_KEY, POSTGRES_PASSWORD, ADMIN_ODOO_LOGIN
docker compose up -d --build  # migrations run automatically
```

Open **http://localhost:8085** (or `http://<this-pc-ip>:8085` from other PCs on the network; then add that
address to `CSRF_TRUSTED_ORIGINS` in `.env` and run `docker compose up -d` again).

| Task | Command |
|---|---|
| Logs | `docker compose logs -f web` |
| Tests | `docker compose run --rm web python manage.py test` |
| Stop | `docker compose down` (data is kept in the `db_data` and `media` volumes) |
| Backup database | `docker compose exec db pg_dump -U gas gas > backup.sql` |

## Layout

```
gas/         Django project settings
core/        Odoo login, users & roles, audit log, Odoo JSON-RPC client
mo_close/    System_001 — MO's To Be Close (PDF reader, Odoo sync, tracking, reports)
templates/   Shared CMS layout
static/      CSS and the SAWO logo
docs/        Process, design and reference images
```

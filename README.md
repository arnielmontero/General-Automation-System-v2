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
| Backup database | see [Backup](#backup) below |

## Update to a new version (keep the data)

The database and the uploaded PDFs live in the Docker volumes `db_data` and `media`, not in the containers.
Rebuilding the web container replaces only the program code; the data stays.

1. **Pull** the new version: `git pull`
2. **Back up** the data first — see [Backup](#backup).
3. **Rebuild only the web container** (the database container is not touched; new migrations run on start):
   ```bash
   docker compose up -d --build --no-deps web
   ```
4. **Check**: `docker compose logs web --tail 20` shows `Applying … OK` for new migrations (or none) and
   `Listening at`; `docker compose exec web python manage.py showmigrations` shows every line `[X]`; open the site.

Before updating, read any new files in `*/migrations/`: `AddField`, `CreateModel` and data-adding steps are safe;
`RemoveField`, `DeleteModel` or `RunSQL` that deletes rows remove data — stop and check first.

> **Never** run `docker compose down -v`, `docker volume rm …`, `docker volume prune` or
> `docker system prune --volumes` — they **delete the database and the uploaded files**.
> Plain `docker compose down` (without `-v`) is safe.

## Backup

Backups go in the `DatabaseBackup/` folder (kept out of git and the Docker image). Use a new folder name each time
(date and time), e.g. `DatabaseBackup/20261021-0933`:

```bash
mkdir DatabaseBackup/20261021-0933
docker compose exec db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/db.dump'
docker cp general-automation-system-db-1:/tmp/db.dump DatabaseBackup/20261021-0933/database.dump
docker compose exec db rm /tmp/db.dump
docker compose exec web sh -c 'tar -cf /tmp/media.tar -C /app media'
docker cp general-automation-system-web-1:/tmp/media.tar DatabaseBackup/20261021-0933/media.tar
docker compose exec web rm /tmp/media.tar
```

`database.dump` is the whole database, `media.tar` the uploaded packing list PDFs. Keep a copy of `.env` too
(it is not in the backup).

**Restore** (replaces the current data with the backup):

```bash
docker compose stop web
docker cp DatabaseBackup/20261021-0933/database.dump general-automation-system-db-1:/tmp/db.dump
docker compose exec db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner /tmp/db.dump && rm /tmp/db.dump'
docker cp DatabaseBackup/20261021-0933/media.tar general-automation-system-web-1:/tmp/media.tar
docker compose start web
docker compose exec web sh -c 'tar -xf /tmp/media.tar -C /app && rm /tmp/media.tar'
```

## Layout

```
gas/         Django project settings
core/        Odoo login, users & roles, audit log, Odoo JSON-RPC client
mo_close/    System_001 — MO's To Be Close (PDF reader, Odoo sync, tracking, reports)
templates/   Shared CMS layout
static/      CSS and the SAWO logo
docs/        Process, design and reference images
```

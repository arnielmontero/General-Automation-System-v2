"""Business logic of MO's To Be Close: create records (Step 1), generate from Odoo (Step 2),
mark done / system counter check / final confirmation (Step 3)."""

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import audit
from core.odoo import MO_STATES, PICKING_STATES, OdooAccessError, OdooError, OdooSessionExpired

from .models import (
    SYSTEM,
    MORecord,
    PackingDocument,
    PackingLine,
    PackingList,
    SalesOrder,
    TransferRecord,
    norm_code,
    so_base_digits,
    so_digits,
    so_in_source,
    so_is_customer_code,
    so_parent_number,
    so_root,
)
from .pdf_parser import parse_date

Section = audit.Section

# Step 2 requirement: only MOs in these states are collected.
REQUIRED_MO_STATES = ("confirmed", "progress", "to_close")
# Transfers in these states are not saved when generating.
SKIPPED_TRANSFER_STATES = ("done", "cancel")

MO_FIELDS = ["name", "product_id", "product_qty", "state", "origin", "picking_ids"]
PICKING_FIELDS = ["name", "state", "picking_type_id", "scheduled_date"]


def log(request, section, action, record, **kwargs):
    return audit.log(request, section, action, system=SYSTEM, record=record, **kwargs)


# --- Step 1 ----------------------------------------------------------------------------------


def normalize_so(value):
    """'4743', 's04743', ' S04743 ', 'SO4743' → 'S04743'; child SOs keep their suffix: '4743-1' → 'S04743-1'."""
    value = (value or "").strip().upper().replace(" ", "")
    m = re.match(r"^(?:SO|S)?0*(\d+)((?:-\d+)*)$", value)
    if m:
        value = "S" + m.group(1).zfill(5) + m.group(2)
    return value


SO_RE = re.compile(r"^S\d{3,}(?:-\d+)*$")


def validate_so(so):
    if not SO_RE.match(so):
        raise ValidationError("Enter a Sales Order number like S04743, or a child SO like S04743-1.")


def so_exists_in_odoo(client, so):
    """True / False, or None when this user cannot read Sales Orders in Odoo. A child SO (S04743-1) is checked
    by its parent S0 (S04743) — the suffix only exists in this system, not in Odoo."""
    try:
        return client.search_count("sale.order", [("name", "=", so_root(so))]) > 0
    except OdooAccessError:
        return None


def find_so(so):
    return SalesOrder.objects.select_related("record", "parent").filter(so_number=so).first()


def _check_new_so(so):
    node = find_so(so)
    if node is not None:
        raise ValidationError(f"{so} already exists in record {node.record.so_number}.")
    if PackingList.objects.filter(so_number=so).exists():
        raise ValidationError(f"A record for {so} already exists.")


@transaction.atomic
def place_so(request, so, parent_so="", record=None):
    """The SalesOrder node an upload goes to, created when new: under ``parent_so`` when given, else under
    the parent its name shows (S04743-1-2 → S04743-1 → S04743; missing ones are created), else under the
    root of ``record`` (adding from a record page), else as a new record of its own. An SO that already
    exists is only reused when adding from its record's page."""
    existing = find_so(so)
    if existing is not None:
        if record is None:
            raise ValidationError(
                f"{so} already exists in record {existing.record.so_number} — open the record and use Add packing lists."
            )
        if existing.record_id != record.pk:
            raise ValidationError(f"{so} belongs to record {existing.record.so_number}.")
        return existing
    _check_new_so(so)

    if parent_so:
        parent = find_so(parent_so)
        if parent is None:
            raise ValidationError(f"Parent SO {parent_so} was not found. Create it first.")
        if record is not None and parent.record_id != record.pk:
            raise ValidationError(f"Parent SO {parent_so} belongs to record {parent.record.so_number}.")
        return _new_so(request, parent.record, parent, so)

    # Parent by name: walk up until an existing SO; the ones in between are created too.
    chain, name, parent = [so], so_parent_number(so), None
    while name:
        parent = find_so(name)
        if parent is not None:
            break
        chain.append(name)
        name = so_parent_number(name)
    if parent is not None:
        if record is not None and parent.record_id != record.pk:
            raise ValidationError(f"{so} belongs under {parent.so_number}, which is in record {parent.record.so_number}.")
    elif record is not None:
        parent = record.ensure_root_so()
    else:
        top = chain.pop()
        _check_new_so(top)
        pl = PackingList.objects.create(so_number=top, status=PackingList.Status.DRAFT, created_by=request.user)
        log(request, Section.RECORD, "Record created", pl, new=top)
        parent = pl.ensure_root_so()
    for name in reversed(chain):
        parent = _new_so(request, parent.record, parent, name)
    return parent


def _new_so(request, pl, parent, so):
    node = SalesOrder.objects.create(record=pl, parent=parent, so_number=so, created_by=request.user)
    log(request, Section.RECORD, "Child SO added", pl, target=so, new=f"under {parent.so_number}")
    return node


@transaction.atomic
def add_document(request, node, uploaded_file, parsed, odoo_check):
    """One uploaded packing list PDF for an SO of the tree, waiting for review (Save / Cancel)."""
    pl = node.record
    so = node.so_number
    header = parsed.header
    warnings = list(parsed.warnings)
    # The parent S0 is the key: a packing list of S04743-1 carries Invoice No. S04743, which is right.
    root = so_root(so)
    invoice = (header.get("invoice_no") or "").strip().upper()
    if invoice and so_root(normalize_so(invoice)) != root:
        warnings.insert(0, f"The PDF Invoice No. is {invoice}, but this packing list is for {root}.")
    if odoo_check is False:
        warnings.insert(0, f"Sales Order {root} was not found in Odoo.")
    elif odoo_check is None:
        warnings.append("Could not verify the Sales Order in Odoo (no access to Sales Orders).")

    totals = parsed.totals or {}
    doc = PackingDocument.objects.create(
        record=pl,
        sales_order=node,
        sold_to=header.get("sold_to", ""),
        tel=header.get("tel", ""),
        fax=header.get("fax", ""),
        attn=header.get("attn", ""),
        doc_date_text=header.get("date", ""),
        doc_date=parse_date(header.get("date", "")) if header.get("date") else None,
        invoice_no=header.get("invoice_no", ""),
        container_no=header.get("container_no", ""),
        seal_no=header.get("seal_no", ""),
        destination=header.get("destination", ""),
        total_packages=totals.get("packages"),
        total_net_weight=totals.get("net_weight"),
        total_gross_weight=totals.get("gross_weight"),
        total_cbm=totals.get("cbm"),
        total_qty=totals.get("qty"),
        pdf=uploaded_file,
        pdf_name=uploaded_file.name[:255],
        parse_warnings=warnings,
        created_by=request.user,
    )
    PackingLine.objects.bulk_create(
        PackingLine(
            packing_list=pl,
            document=doc,
            line_no=line["line_no"],
            external_code=line["external_code"][:100],
            description=line["description"][:500],
            um=line["um"][:20],
            packages=line["packages"],
            net_weight=line["net_weight"],
            gross_weight=line["gross_weight"],
            cbm=line["cbm"],
            qty=line["qty"] or Decimal("0"),
        )
        for line in parsed.lines
    )
    pl.sync_header()
    log(request, Section.RECORD, "PDF uploaded", pl, target=doc.pdf_name, new=f"{so} · {len(parsed.lines)} rows")
    return doc


@transaction.atomic
def save_drafts(request, pl):
    """Save the packing lists waiting for review. Their lines count from the next Generate from Odoo."""
    now = timezone.now()
    drafts = list(pl.documents.filter(saved=False).select_related("sales_order"))
    for doc in drafts:
        doc.saved, doc.saved_at = True, now
        doc.save(update_fields=["saved", "saved_at"])
        log(request, Section.RECORD, "Packing list saved", pl, target=doc.pdf_name,
            new=f"{doc.sales_order.so_number} · {doc.lines.count()} rows")
    if pl.status == PackingList.Status.DRAFT:
        pl.status, pl.saved_at = PackingList.Status.SAVED, now
        pl.save(update_fields=["status", "saved_at"])
    elif pl.status == PackingList.Status.CLOSED and drafts:
        pl.status, pl.closed_at = PackingList.Status.GENERATED, None  # new packing lists: generate again
        pl.save(update_fields=["status", "closed_at"])
    pl.sync_header()
    return drafts


def _prune_empty_sos(pl):
    """Remove child SOs left without packing lists, MOs or children (e.g. after a cancelled upload)."""
    while True:
        empty = SalesOrder.objects.filter(record=pl, parent__isnull=False, documents=None, mos=None, children=None)
        if not empty.exists():
            return
        empty.delete()


@transaction.atomic
def discard_drafts(request, pl):
    """Cancel an upload: its PDFs, the child SOs created for it, and the record itself when nothing is saved."""
    for doc in pl.documents.filter(saved=False):
        log(request, Section.RECORD, "Upload cancelled", pl, target=doc.pdf_name)
        doc.pdf.delete(save=False)
        doc.delete()
    _prune_empty_sos(pl)
    if pl.status == PackingList.Status.DRAFT and not pl.documents.exists():
        pl.delete()
        return None
    pl.sync_header()
    return pl


@transaction.atomic
def delete_document(request, doc):
    pl = doc.record
    log(request, Section.RECORD, "Packing list removed", pl, target=doc.pdf_name, old=doc.sales_order.so_number)
    doc.pdf.delete(save=False)
    doc.delete()
    _prune_empty_sos(pl)
    pl.sync_header()


@transaction.atomic
def remove_child_so(request, node, comment):
    """Remove a child SO added by mistake, with everything under it: its child SOs, their packing lists (PDFs
    and rows) and the MOs / transfers found for them. The reason is required and kept in the logs."""
    if not request.user.can("mo_close.delete"):
        raise PermissionDenied
    if node.is_root:
        raise ValidationError("The parent SO cannot be removed here — delete the record instead.")
    comment = (comment or "").strip()
    if not comment:
        raise ValidationError(f"A comment is required to remove {node.so_number}.")
    pl = node.record
    subtree, todo = [], [node]
    while todo:
        n = todo.pop()
        subtree.append(n)
        todo.extend(n.children.all())
    docs = list(PackingDocument.objects.filter(sales_order__in=subtree))
    mos = MORecord.objects.filter(sales_order__in=subtree)
    transfers = TransferRecord.objects.filter(mo__in=mos).count()
    summary = (
        f"{len(subtree)} SO(s), {len(docs)} packing list(s), {PackingLine.objects.filter(document__in=docs).count()} rows, "
        f"{mos.count()} MO(s), {transfers} transfer(s)"
    )
    log(request, Section.RECORD, "Child SO removed", pl, target=node.so_number, old=summary, comment=comment)
    for doc in docs:
        doc.pdf.delete(save=False)
    mos.delete()
    node.delete()  # cascades to its child SOs, packing lists and rows
    pl.sync_header()
    pl.update_status()
    return summary


def delete_record(request, pl):
    log(request, Section.RECORD, "Record deleted", pl, old=pl.get_status_display())
    for doc in pl.documents.all():
        doc.pdf.delete(save=False)
    pl.delete()


# --- Step 2 ----------------------------------------------------------------------------------


@dataclass
class GenerateResult:
    mos_new: int = 0
    mos_updated: int = 0
    transfers_new: int = 0
    transfers_updated: int = 0
    state_changes: list = field(default_factory=list)


def _m2o(value):
    """Odoo many2one [id, "name"] → (id, name)."""
    if isinstance(value, (list, tuple)) and value:
        return value[0], (value[1] if len(value) > 1 else "")
    return None, ""


def _odoo_dt(value):
    if not value:
        return None
    dt = parse_datetime(value.replace(" ", "T"))
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, datetime.timezone.utc)  # Odoo stores UTC
    return dt


def _type_code(reference):
    parts = reference.split("/")
    return parts[1] if len(parts) >= 3 else ""


def _set_state(row, new_state, now, result, labels):
    if new_state and new_state != row.state:
        result.state_changes.append((row, labels.get(row.state, row.state), labels.get(new_state, new_state)))
        row.state = new_state
    row.synced_at = now


def generate_from_odoo(request, pl, client):
    """Step 2: find the SO's MOs (Confirmed / In Progress / To Close) and save them with their transfers
    that are not Done. MOs whose product is on the packing list are matched to its line; MOs for other
    products (components / sub-assemblies made for the SO) are saved too, flagged as not on the packing
    list. Running it again refreshes Odoo values and adds anything new, keeping every tick, confirmation
    and comment."""
    now = timezone.now()
    result = GenerateResult()
    pl.ensure_root_so()
    nodes = pl.so_tree()
    groups = {g["norm"]: g for g in pl.grouped_lines() if g["norm"]}
    node_groups = {n.pk: {g["norm"]: g for g in n.grouped_lines() if g["norm"]} for n in nodes}

    # Search the numbers only (the Source may say S08027, SO8027 …) for every SO of the tree, then keep
    # whole-SO matches. Each MO goes to the most specific SO its Source names (S04743-1 over S04743).
    bases = sorted({so_base_digits(n.so_number) for n in nodes})
    domain = ["|"] * (len(bases) - 1) + [("origin", "ilike", b) for b in bases]
    mos = client.search_read(
        "mrp.production", domain + [("state", "in", list(REQUIRED_MO_STATES))], MO_FIELDS, order="name"
    )
    origins = [m.get("origin") for m in mos]
    # A bare number (no S / O) only counts when the number is not also a customer code in these Sources.
    bare = {n.pk: not so_is_customer_code(n.so_number, origins) for n in nodes}

    def owner(origin):
        found = [n for n in nodes if so_in_source(n.so_number, origin, bare=bare[n.pk])]
        return max(found, key=lambda n: (len(so_digits(n.so_number)), n.depth)) if found else None

    mos = [(m, node) for m in mos if (node := owner(m.get("origin"))) is not None]

    product_ids = {_m2o(m["product_id"])[0] for m, _ in mos} - {None}
    products = {p["id"]: p for p in client.read("product.product", product_ids, ["default_code", "name"])}

    with transaction.atomic():
        matched = []  # (MORecord, picking ids)
        for m, node in mos:
            product_id, product_label = _m2o(m["product_id"])
            product = products.get(product_id) or {}
            code = product.get("default_code") or ""
            norm = norm_code(code)
            if norm not in node_groups[node.pk] and node.so_number == so_root(node.so_number):
                # The Source names the S0 (S04743); the product is only on an additional packing list of it (S04743-1).
                node = next(
                    (n for n in nodes if n.depth and so_root(n.so_number) == node.so_number and norm in node_groups[n.pk]),
                    node,
                )
            group = node_groups[node.pk].get(norm) or groups.get(norm)  # own SO first
            on_packing_list = group is not None
            if not on_packing_list:
                # MO of this SO for a product that is not on the packing list (component / sub-assembly).
                group = {"code": code or "(no code)", "description": product.get("name") or product_label, "qty": 0}
            rec = pl.mos.filter(odoo_id=m["id"]).first()
            if rec is None:
                rec = MORecord(packing_list=pl, odoo_id=m["id"], state=m["state"])
                result.mos_new += 1
            else:
                result.mos_updated += 1
                _set_state(rec, m["state"], now, result, MO_STATES)
            rec.reference = m["name"]
            rec.state = m["state"]
            rec.sales_order = node
            rec.on_packing_list = on_packing_list
            rec.fg_code = group["code"][:100]
            rec.fg_description = group["description"][:500]
            rec.qty = group["qty"]
            rec.product_qty = Decimal(str(m.get("product_qty") or 0))
            rec.product_label = product_label[:500]
            rec.origin = (m.get("origin") or "")[:500]
            rec.synced_at = now
            rec.save()
            matched.append((rec, m.get("picking_ids") or []))

        # Saved MOs that are no longer in the required states (e.g. now Done): refresh their state.
        matched_ids = [rec.odoo_id for rec, _ in matched]
        _refresh_mos(client, pl.mos.exclude(odoo_id__in=matched_ids), now, result)

        picking_ids = {pid for _, pids in matched for pid in pids}
        pickings = {p["id"]: p for p in client.read("stock.picking", picking_ids, PICKING_FIELDS)}
        for rec, pids in matched:
            for pid in pids:
                p = pickings.get(pid)
                if p is None:
                    continue
                tr = rec.transfers.filter(odoo_id=pid).first()
                if tr is None:
                    if p["state"] in SKIPPED_TRANSFER_STATES:
                        continue
                    tr = TransferRecord(mo=rec, odoo_id=pid, state=p["state"])
                    result.transfers_new += 1
                else:
                    result.transfers_updated += 1
                    _set_state(tr, p["state"], now, result, PICKING_STATES)
                _fill_transfer(tr, p, now)
                tr.save()

        # Transfers of the MOs not returned by the search: refresh their state too.
        others = TransferRecord.objects.filter(mo__packing_list=pl).exclude(mo__odoo_id__in=matched_ids)
        _refresh_transfers(client, others, now, result)

        _recheck_marked_rows(pl, now)
        pl.status = PackingList.Status.GENERATED if pl.status != PackingList.Status.CLOSED else pl.status
        pl.generated_by, pl.generated_at, pl.refreshed_at = request.user, now, now
        pl.save(update_fields=["status", "generated_by", "generated_at", "refreshed_at"])
        pl.update_status()

    log(
        request,
        Section.GENERATE,
        "Generated from Odoo",
        pl,
        new=f"MOs +{result.mos_new} new / {result.mos_updated} updated; "
        f"transfers +{result.transfers_new} new / {result.transfers_updated} updated",
    )
    _log_state_changes(request, pl, result)
    return result


def _fill_transfer(tr, p, now):
    _, type_name = _m2o(p.get("picking_type_id"))
    tr.reference = p["name"]
    tr.state = p["state"]
    tr.type_code = _type_code(p["name"])
    tr.type_name = type_name[:200]
    tr.scheduled_date = _odoo_dt(p.get("scheduled_date"))
    tr.synced_at = now


def _refresh_mos(client, queryset, now, result):
    rows = list(queryset)
    if not rows:
        return
    data = {m["id"]: m for m in client.read("mrp.production", [r.odoo_id for r in rows], ["name", "state"])}
    for row in rows:
        m = data.get(row.odoo_id)
        if m:
            _set_state(row, m["state"], now, result, MO_STATES)
            row.save(update_fields=["state", "synced_at"])


def _refresh_transfers(client, queryset, now, result):
    rows = list(queryset)
    if not rows:
        return
    ids = {r.odoo_id for r in rows}
    data = {p["id"]: p for p in client.read("stock.picking", ids, PICKING_FIELDS)}
    for row in rows:
        p = data.get(row.odoo_id)
        if p:
            _set_state(row, p["state"], now, result, PICKING_STATES)
            row.save(update_fields=["state", "synced_at"])


def refresh_from_odoo(request, pl, client):
    """Re-read the state of every saved MO and transfer, and re-run the system check on marked rows."""
    now = timezone.now()
    result = GenerateResult()
    with transaction.atomic():
        _refresh_mos(client, pl.mos.all(), now, result)
        _refresh_transfers(client, TransferRecord.objects.filter(mo__packing_list=pl), now, result)
        _recheck_marked_rows(pl, now)
        pl.refreshed_at = now
        pl.save(update_fields=["refreshed_at"])
        pl.update_status()
    log(request, Section.GENERATE, "Refreshed from Odoo", pl, new=f"{len(result.state_changes)} state changes")
    _log_state_changes(request, pl, result)
    return result


def _log_state_changes(request, pl, result):
    for row, old, new in result.state_changes:
        log(request, Section.GENERATE, "Odoo state changed", pl, target=row.reference, old=old, new=new)


def _apply_system_check(row, now):
    row.system_odoo_state = row.state
    row.system_status = (
        TransferRecord.SystemStatus.CONFIRMED if row.state == "done" else TransferRecord.SystemStatus.NOT_DONE
    )
    row.system_checked_at = now


def _recheck_marked_rows(pl, now):
    for row in pl.all_rows():
        if row.user_done and not row.final_confirmed:
            _apply_system_check(row, now)
            row.save(update_fields=["system_odoo_state", "system_status", "system_checked_at"])


# --- Step 3 ----------------------------------------------------------------------------------


def get_row(kind, pk):
    model = MORecord if kind == "mo" else TransferRecord
    return model.objects.select_related("final_confirmed_by", "user_done_by").get(pk=pk)


def _row_pl(row):
    return row.packing_list


def _read_state(client, row):
    model, fields = ("mrp.production", ["state"]) if row.KIND == "mo" else ("stock.picking", ["state"])
    data = client.read(model, [row.odoo_id], fields)
    return data[0]["state"] if data else None


def mark_done(request, row, client):
    """User marks a row done → the system immediately checks the state in Odoo."""
    if not request.user.can("mo_close.mark_done"):
        raise PermissionDenied
    if row.final_confirmed:
        raise ValidationError("This row already has final confirmation.")
    pl = _row_pl(row)
    now = timezone.now()
    row.user_done, row.user_done_at, row.user_done_by = True, now, request.user
    log(request, Section.MARK, "Marked done", pl, target=row.reference)
    try:
        state = _read_state(client, row)
    except OdooSessionExpired:
        raise
    except OdooError as exc:
        row.system_status, row.system_checked_at, row.system_odoo_state = row.SystemStatus.ERROR, now, ""
        row.save()
        log(request, Section.SYSTEM_CHECK, "System check failed", pl, target=row.reference, comment=str(exc))
        return row
    if state:
        row.state, row.synced_at = state, now
    _apply_system_check(row, now)
    row.save()
    log(
        request,
        Section.SYSTEM_CHECK,
        "System check",
        pl,
        target=row.reference,
        new=f"{row.get_system_status_display()} (Odoo: {row.system_state_label})",
    )
    return row


def unmark_done(request, row):
    if not request.user.can("mo_close.mark_done"):
        raise PermissionDenied
    if row.final_confirmed:
        raise ValidationError("This row already has final confirmation.")
    row.user_done, row.user_done_at, row.user_done_by = False, None, None
    row.system_status, row.system_checked_at, row.system_odoo_state = "", None, ""
    row.save()
    log(request, Section.MARK, "Mark done removed", _row_pl(row), target=row.reference)
    return row


def confirm_final(request, row, comment):
    """Checker / Supervisor final confirmation. A comment is required when the system did not confirm."""
    if not request.user.can("mo_close.confirm"):
        raise PermissionDenied
    if not row.user_done:
        raise ValidationError("The row must be marked done first.")
    comment = (comment or "").strip()
    if row.system_status != row.SystemStatus.CONFIRMED and not comment:
        raise ValidationError("Odoo has not confirmed this as Done — a comment is required.")
    row.final_confirmed, row.final_confirmed_at, row.final_confirmed_by = True, timezone.now(), request.user
    row.final_comment = comment
    row.save()
    pl = _row_pl(row)
    log(
        request,
        Section.CONFIRMATION,
        "Final confirmation" if row.system_status == row.SystemStatus.CONFIRMED else "Final confirmation (override)",
        pl,
        target=row.reference,
        new=row.get_system_status_display(),
        comment=comment,
    )
    pl.update_status()
    return row


def unconfirm_final(request, row, comment):
    if not request.user.can("mo_close.confirm"):
        raise PermissionDenied
    row.final_confirmed, row.final_confirmed_at, row.final_confirmed_by = False, None, None
    row.final_comment = ""
    row.save()
    pl = _row_pl(row)
    log(request, Section.CONFIRMATION, "Final confirmation removed", pl, target=row.reference, comment=comment)
    pl.update_status()
    return row


def _sf_team(team):
    if team not in MORecord.SF_TEAMS:
        raise ValidationError("Unknown SF column.")
    return MORecord.SF_TEAMS[team]


def sf_mark_done(request, mo, team):
    """SF-Innova / SF-Electrical checks an MO as done. Once checked, only an Admin can uncheck it."""
    label, module = _sf_team(team)
    if not request.user.can(module):
        raise PermissionDenied
    if getattr(mo, f"sf_{team}_done"):
        raise ValidationError(f"{label} is already checked done.")
    setattr(mo, f"sf_{team}_done", True)
    setattr(mo, f"sf_{team}_at", timezone.now())
    setattr(mo, f"sf_{team}_by", request.user)
    mo.save(update_fields=[f"sf_{team}_done", f"sf_{team}_at", f"sf_{team}_by"])
    log(request, Section.MARK, f"{label} done", mo.packing_list, target=mo.reference)
    return mo


def sf_unmark_done(request, mo, team, comment):
    """Admin unchecks SF done; the reason is required, logged and shown on the row. Users can then check again."""
    label, _ = _sf_team(team)
    if not request.user.can("mo_close.sf_undo"):
        raise PermissionDenied
    if not getattr(mo, f"sf_{team}_done"):
        raise ValidationError(f"{label} is not checked.")
    comment = (comment or "").strip()
    if not comment:
        raise ValidationError(f"A comment is required to uncheck {label}.")
    old = f"Done {timezone.localtime(getattr(mo, f'sf_{team}_at')):%d-%b-%Y %H:%M}"
    by = getattr(mo, f"sf_{team}_by")
    if by:
        old += f" by {by.display_name}"
    values = {
        "done": False,
        "at": None,
        "by": None,
        "undo_comment": comment,
        "undo_at": timezone.now(),
        "undo_by": request.user,
    }
    for name, value in values.items():
        setattr(mo, f"sf_{team}_{name}", value)
    mo.save(update_fields=[f"sf_{team}_{name}" for name in values])
    log(request, Section.MARK, f"{label} unchecked", mo.packing_list, target=mo.reference, old=old, comment=comment)
    return mo


# --- progress ---------------------------------------------------------------------------------


def progress_counts(packing_lists):
    """{record id: {"total", "marked", "system", "final"}} for MO + transfer rows."""
    from django.db.models import Count, Q

    counts = {pl.pk: {"total": 0, "marked": 0, "system": 0, "final": 0} for pl in packing_lists}
    ids = list(counts)
    aggregates = {
        "total": Count("id"),
        "marked": Count("id", filter=Q(user_done=True)),
        "system": Count("id", filter=Q(system_status="confirmed")),
        "final": Count("id", filter=Q(final_confirmed=True)),
    }
    for key, qs in (
        ("packing_list", MORecord.objects.filter(packing_list__in=ids)),
        ("mo__packing_list", TransferRecord.objects.filter(mo__packing_list__in=ids)),
    ):
        for row in qs.values(key).annotate(**aggregates):
            c = counts[row[key]]
            for k in aggregates:
                c[k] += row[k]
    for c in counts.values():
        c["open"] = c["total"] - c["marked"]
        c["percent"] = round(100 * c["final"] / c["total"]) if c["total"] else 0
    return counts

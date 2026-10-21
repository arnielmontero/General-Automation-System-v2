from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, FloatField, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Lower
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from core.audit import Section
from core.models import AuditLog
from core.odoo import OdooError, OdooSessionExpired, client_for
from core.permissions import module_required

from . import exports, report_exports, reports, services
from .forms import NewRecordForm, UploadForm
from .models import SYSTEM, MORecord, PackingDocument, PackingList, SalesOrder, TransferRecord, group_lines, norm_code
from .pdf_parser import PackingListParseError, parse_packing_list

ROW_FILTERS = {
    "all": "All",
    "open": "Not marked done",
    "waiting": "Waiting final confirmation",
    "flag": "Not confirmed by Odoo",
    "confirmed": "Confirmed done",
}


PLANTS = ["PL1", "PL2", "PL3"]
PLANT_SHOW = {"open": "Open records", "closed": "Closed records", "all": "All records"}
ROW_RELATED = ["user_done_by", "final_confirmed_by"]
MO_RELATED = ["packing_list", "sales_order", *ROW_RELATED, *MORecord.SF_RELATED]


def _row_q(f, prefix=""):
    """_row_matches as a database filter (prefix 'transfers__' for the MO's transfers)."""
    p = lambda **kw: Q(**{prefix + k: v for k, v in kw.items()})  # noqa: E731
    if f == "open":
        return p(user_done=False)
    if f == "waiting":
        return p(user_done=True, final_confirmed=False)
    if f == "flag":
        return p(user_done=True, final_confirmed=False) & ~p(system_status="confirmed")
    if f == "confirmed":
        return p(final_confirmed=True)
    return Q()


def _row_matches(row, f):
    if f == "open":
        return not row.user_done
    if f == "waiting":
        return row.user_done and not row.final_confirmed
    if f == "flag":
        return row.user_done and row.system_status != "confirmed" and not row.final_confirmed
    if f == "confirmed":
        return row.final_confirmed
    return True


def _record_logs(pl, limit=None):
    qs = AuditLog.objects.filter(system=SYSTEM, record_id=pl.pk)
    return qs[:limit] if limit else qs


# --- Dashboard -------------------------------------------------------------------------------


@module_required("mo_close.dashboard")
def dashboard(request):
    by_status = dict(PackingList.objects.values_list("status").annotate(n=Count("id")))
    rows_filter = {"packing_list__status": PackingList.Status.GENERATED}
    tr_filter = {"mo__packing_list__status": PackingList.Status.GENERATED}

    def count(q):
        return MORecord.objects.filter(q, **rows_filter).count() + TransferRecord.objects.filter(q, **tr_filter).count()

    kpis = {
        "records": sum(by_status.values()),
        "in_progress": by_status.get(PackingList.Status.GENERATED, 0),
        "to_generate": by_status.get(PackingList.Status.SAVED, 0),
        "closed": by_status.get(PackingList.Status.CLOSED, 0),
        "open_rows": count(Q(user_done=False)),
        "waiting_rows": count(Q(user_done=True, final_confirmed=False)),
        "flag_rows": count(Q(user_done=True, final_confirmed=False) & ~Q(system_status="confirmed")),
    }
    active = list(
        PackingList.objects.filter(status__in=[PackingList.Status.SAVED, PackingList.Status.GENERATED])
        .select_related("created_by")
        .prefetch_related("sales_orders")
        .order_by("-generated_at", "-created_at")[:10]
    )
    counts = services.progress_counts(active)
    for pl in active:
        pl.progress = counts[pl.pk]
    recent = AuditLog.objects.filter(system=SYSTEM)[:8]
    return render(request, "mo_close/dashboard.html", {"kpis": kpis, "active": active, "recent": recent})


# --- Step 1: records -------------------------------------------------------------------------


def _records_matching(q):
    """Ids of records whose parent or any child SO matches, or the Sold To / container / invoice of any PDF."""
    return PackingList.objects.filter(
        Q(so_number__icontains=q)
        | Q(sales_orders__so_number__icontains=q)
        | Q(sold_to__icontains=q)
        | Q(documents__sold_to__icontains=q)
        | Q(documents__container_no__icontains=q)
        | Q(documents__invoice_no__icontains=q)
    ).values("pk")


def _plant_counts():
    """{'PL1': n, …}: MOs of each plant in open records (shown on the tabs)."""
    open_mos = MORecord.objects.filter(packing_list__status=PackingList.Status.GENERATED)
    return open_mos.aggregate(**{plant: Count("id", filter=Q(reference__startswith=plant + "/")) for plant in PLANTS})


@module_required("mo_close.records")
def records(request):
    tab = request.GET.get("tab", "").upper()
    if tab in PLANTS:
        return _plant_records(request, tab)
    qs = PackingList.objects.select_related("created_by").prefetch_related("sales_orders")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    if q:
        qs = qs.filter(pk__in=_records_matching(q))
    if status in PackingList.Status.values:
        qs = qs.filter(status=status)
    qs = qs.annotate(
        line_count=Count("lines", filter=Q(lines__document__saved=True)), so_count=Count("sales_orders", distinct=True)
    ).order_by("-created_at")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    counts = services.progress_counts(page.object_list)
    for pl in page.object_list:
        pl.progress = counts[pl.pk]
    return render(
        request,
        "mo_close/records.html",
        {
            "page": page,
            "q": q,
            "status": status,
            "statuses": PackingList.Status.choices,
            "query": urlencode({"q": q, "status": status}),
            "plants": PLANTS,
            "plant_counts": _plant_counts(),
        },
    )


def _plant_records(request, plant):
    """Records > PL1 / PL2 / PL3: every MO of that plant (with its transfers) across the records."""
    show = request.GET.get("show", "open")
    if show not in PLANT_SHOW:
        show = "open"
    f = request.GET.get("f", "all")
    if f not in ROW_FILTERS:
        f = "all"
    q = request.GET.get("q", "").strip()

    qs = MORecord.objects.filter(reference__startswith=plant + "/").exclude(packing_list__status=PackingList.Status.DRAFT)
    if show == "open":
        qs = qs.exclude(packing_list__status=PackingList.Status.CLOSED)
    elif show == "closed":
        qs = qs.filter(packing_list__status=PackingList.Status.CLOSED)
    if q:
        qs = qs.filter(
            Q(packing_list__so_number__icontains=q)
            | Q(sales_order__so_number__icontains=q)
            | Q(reference__icontains=q)
            | Q(fg_code__icontains=q)
            | Q(fg_description__icontains=q)
            | Q(transfers__reference__icontains=q)
        )
    if f != "all":
        qs = qs.filter(_row_q(f) | _row_q(f, "transfers__"))
    qs = (
        qs.distinct()
        .select_related(*MO_RELATED)
        .prefetch_related("transfers__user_done_by", "transfers__final_confirmed_by")
        .order_by("-packing_list__created_at", "-on_packing_list", "fg_code", "reference")
    )
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    groups = []
    for mo in page.object_list:
        transfers = list(mo.transfers.all())
        groups.append({"mo": mo, "transfers": transfers if f == "all" else [t for t in transfers if _row_matches(t, f)]})

    base = {"tab": plant, "show": show, "f": f, "q": q}
    return render(
        request,
        "mo_close/records.html",
        {
            "plant": plant,
            "plants": PLANTS,
            "plant_counts": _plant_counts(),
            "page": page,
            "groups": groups,
            "q": q,
            "f": f,
            "filters": ROW_FILTERS,
            "show": show,
            "show_choices": PLANT_SHOW,
            "filter_urls": {key: "?" + urlencode({**base, "f": key}) for key in ROW_FILTERS},
            "query": urlencode(base),
            "link_so": True,
        },
    )


def _upload(request, form, record=None):
    """Read every PDF, place the SO in the tree (creating the record / child SOs) and add the PDFs for review.
    Returns the record, or None with the errors on the form."""
    so, parent_so, files = form.cleaned_data["so_number"], form.cleaned_data["parent_so"], form.cleaned_data["pdfs"]
    parsed = []
    for upload in files:
        try:
            parsed.append(parse_packing_list(upload))
        except PackingListParseError as exc:
            form.add_error("pdfs", f"{upload.name}: {exc}")
    if form.errors:
        return None
    try:
        node = services.place_so(request, so, parent_so, record)
    except ValidationError as exc:
        form.add_error("so_number", exc.messages)
        return None
    try:
        odoo_check = services.so_exists_in_odoo(client_for(request), so)
    except OdooSessionExpired:
        raise
    except OdooError:
        odoo_check = None
    for upload, result in zip(files, parsed):
        upload.seek(0)
        services.add_document(request, node, upload, result, odoo_check)
    return node.record


@module_required("mo_close.create")
def record_new(request):
    """New Record: a parent S0 with its packing list PDF. Existing SOs are uploaded to from Records."""
    form = NewRecordForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        so = form.cleaned_data["so_number"]
        upload = form.cleaned_data["pdf"]
        existing = services.find_so(so)
        if existing is not None:
            form.add_error(
                "so_number",
                f"{so} already exists (record {existing.record.so_number}). Go to Records, find it and click its SO "
                "number to upload more packing lists.",
            )
        else:
            try:
                parsed = parse_packing_list(upload)
            except PackingListParseError as exc:
                form.add_error("pdf", str(exc))
            else:
                upload.seek(0)
                try:
                    odoo_check = services.so_exists_in_odoo(client_for(request), so)
                except OdooSessionExpired:
                    raise
                except OdooError:
                    odoo_check = None
                try:
                    node = services.place_so(request, so)
                except ValidationError as exc:
                    form.add_error("so_number", exc.messages)
                else:
                    services.add_document(request, node, upload, parsed, odoo_check)
                    return redirect("mo_close:record_preview", pk=node.record_id)
    return render(request, "mo_close/record_new.html", {"form": form})


@module_required("mo_close.create")
def record_add(request, pk):
    """Upload packing lists to a record (Records → click the SO number): for an SO already in its tree, or a
    new child SO under one of them. ``?so=`` fills in the SO that was clicked."""
    pl = get_object_or_404(PackingList, pk=pk)
    initial = {"so_number": request.GET.get("so") or pl.so_number}
    form = UploadForm(request.POST or None, request.FILES or None, record=pl, initial=initial)
    if request.method == "POST" and form.is_valid():
        if _upload(request, form, record=pl) is not None:
            return redirect("mo_close:record_preview", pk=pl.pk)
    return render(request, "mo_close/record_add.html", {"form": form, "record": pl, "tree": pl.so_tree()})


@module_required("mo_close.create")
def record_preview(request, pk):
    """Review the uploaded packing lists of a record (one or more PDFs), then Save or Cancel them together."""
    pl = get_object_or_404(PackingList, pk=pk)
    drafts = list(pl.documents.filter(saved=False).select_related("sales_order__parent").prefetch_related("lines"))
    if not drafts:
        return redirect("mo_close:record_detail", pk=pk)
    if request.method == "POST":
        if request.POST.get("action") == "save":
            saved = services.save_drafts(request, pl)
            rows = sum(len(d.lines.all()) for d in saved)
            note = " Run Generate from Odoo to include them." if pl.generated_at else ""
            messages.success(request, f"{pl.so_number}: {len(saved)} packing list(s) saved with {rows} rows.{note}")
            return redirect("mo_close:record_detail", pk=pk)
        so = pl.so_number
        if services.discard_drafts(request, pl) is None:
            messages.info(request, f"Upload for {so} cancelled.")
            return redirect("mo_close:record_new")
        messages.info(request, f"Upload cancelled; {so} is unchanged.")
        return redirect("mo_close:record_detail", pk=pk)
    for doc in drafts:
        doc.groups = doc.grouped_lines()
    return render(request, "mo_close/record_preview.html", {"pl": pl, "drafts": drafts, "tree": pl.so_tree()})


def _tree_with_documents(pl):
    """The record's SO tree with each SO's saved packing lists (.docs) and its grouped lines (.groups)."""
    tree = pl.so_tree()
    docs = list(pl.documents.filter(saved=True).select_related("created_by").prefetch_related("lines"))
    for node in tree:
        node.docs = [d for d in docs if d.sales_order_id == node.pk]
        for doc in node.docs:
            doc.groups = doc.grouped_lines()
        node.groups = group_lines([line for d in node.docs for line in d.lines.all()])
    return tree


@module_required("mo_close.records")
def record_detail(request, pk):
    pl = get_object_or_404(PackingList.objects.select_related("created_by", "generated_by"), pk=pk)
    if pl.status == PackingList.Status.DRAFT:
        return redirect("mo_close:record_preview", pk=pk)
    f = request.GET.get("f", "all")
    if f not in ROW_FILTERS:
        f = "all"

    tree = _tree_with_documents(pl)
    position = {n.pk: i for i, n in enumerate(tree)}
    by_pk = {n.pk: n for n in tree}
    root_pk = tree[0].pk if tree else None
    mos = list(
        pl.mos.select_related(*MO_RELATED).prefetch_related(
            "transfers__user_done_by", "transfers__final_confirmed_by"
        )
    )
    # MOs by SO (parent first, children under it); within an SO the packing list items, then components.
    mos.sort(key=lambda mo: (position.get(mo.sales_order_id or root_pk, 0), not mo.on_packing_list))
    groups, last = [], (None, None)
    for mo in mos:
        transfers = list(mo.transfers.all())
        shown = [t for t in transfers if _row_matches(t, f)]
        if not (_row_matches(mo, f) or shown):
            continue
        node_pk = mo.sales_order_id or root_pk
        groups.append(
            {
                "mo": mo,
                "transfers": transfers if f == "all" else shown,
                "so_head": by_pk.get(node_pk) if len(tree) > 1 and node_pk != last[0] else None,
                "comp_head": not mo.on_packing_list and (node_pk != last[0] or last[1]),
            }
        )
        last = (node_pk, mo.on_packing_list)

    matched_codes = {norm_code(mo.fg_code) for mo in mos if mo.on_packing_list}
    unmatched = [(node, g) for node in tree for g in node.groups if g["norm"] not in matched_codes]
    counts = services.progress_counts([pl])[pl.pk]
    return render(
        request,
        "mo_close/record_detail.html",
        {
            "pl": pl,
            "tree": tree,
            "groups": groups,
            "line_count": sum(len(n.groups) for n in tree),
            "drafts": pl.documents.filter(saved=False).count(),
            "unmatched": unmatched if pl.generated_at else [],
            "source_fixes": [mo for mo in mos if mo.source_fix],
            "progress": counts,
            "f": f,
            "filters": ROW_FILTERS,
            "logs": _record_logs(pl, 200),
            "tab": request.GET.get("tab", "mos" if pl.generated_at else "lines"),
        },
    )


@module_required("mo_close.records")
def document_pdf(request, pk):
    doc = get_object_or_404(PackingDocument, pk=pk)
    if not doc.pdf:
        raise Http404
    return FileResponse(doc.pdf.open("rb"), content_type="application/pdf", filename=doc.pdf_name or f"{doc.pk}.pdf")


@require_POST
@module_required("mo_close.delete")
def document_delete(request, pk):
    doc = get_object_or_404(PackingDocument.objects.select_related("record", "sales_order"), pk=pk)
    pl = doc.record
    name = doc.pdf_name
    services.delete_document(request, doc)
    messages.success(request, f"Packing list {name} removed. Run Generate from Odoo to update the MO's.")
    return redirect(reverse("mo_close:record_detail", args=[pl.pk]) + "?tab=lines")


@require_POST
@module_required("mo_close.delete")
def so_remove(request, pk):
    """Records list (×): remove a child SO added by mistake and all its data, with a required reason."""
    node = get_object_or_404(SalesOrder.objects.select_related("record"), pk=pk)
    so = node.so_number
    try:
        summary = services.remove_child_so(request, node, request.POST.get("comment", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"{so} removed ({summary}).")
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("mo_close:records")


@require_POST
@module_required("mo_close.delete")
def record_delete(request, pk):
    pl = get_object_or_404(PackingList, pk=pk)
    so = pl.so_number
    services.delete_record(request, pl)
    messages.success(request, f"Record {so} deleted.")
    return redirect("mo_close:records")


# --- Step 2: generate / refresh --------------------------------------------------------------


@require_POST
@module_required("mo_close.generate")
def record_generate(request, pk):
    pl = get_object_or_404(PackingList, pk=pk)
    if pl.status == PackingList.Status.DRAFT:
        messages.error(request, "Save the record before generating.")
        return redirect("mo_close:record_preview", pk=pk)
    try:
        r = services.generate_from_odoo(request, pl, client_for(request))
    except OdooSessionExpired:
        raise
    except OdooError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Generated from Odoo: {r.mos_new} new MO(s), {r.transfers_new} new transfer(s); "
            f"{r.mos_updated} MO(s) and {r.transfers_updated} transfer(s) refreshed.",
        )
    return redirect("mo_close:record_detail", pk=pk)


@require_POST
@module_required("mo_close.generate")
def record_refresh(request, pk):
    pl = get_object_or_404(PackingList, pk=pk)
    try:
        r = services.refresh_from_odoo(request, pl, client_for(request))
    except OdooSessionExpired:
        raise
    except OdooError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Refreshed from Odoo: {len(r.state_changes)} state change(s).")
    return redirect("mo_close:record_detail", pk=pk)


# --- Step 3: mark done / confirmation ----------------------------------------------------------


@require_POST
@module_required("mo_close.records")
def row_action(request, kind, pk, action):
    if kind not in ("mo", "transfer"):
        raise Http404
    try:
        row = services.get_row(kind, pk)
    except (MORecord.DoesNotExist, TransferRecord.DoesNotExist):
        raise Http404
    error = ""
    try:
        if action == "mark":
            services.mark_done(request, row, client_for(request))
        elif action == "unmark":
            services.unmark_done(request, row)
        elif action == "confirm":
            services.confirm_final(request, row, request.POST.get("comment", ""))
        elif action == "unconfirm":
            services.unconfirm_final(request, row, request.POST.get("comment", ""))
        else:
            raise Http404
    except ValidationError as exc:
        error = " ".join(exc.messages)
    except PermissionDenied:
        error = "Your role does not allow this."

    return _group_response(request, row if kind == "mo" else row.mo, error, error_kind=kind, error_pk=pk)


@require_POST
@module_required("mo_close.records")
def sf_action(request, pk, team, action):
    """Check / uncheck SF-Innova or SF-Electrical done on an MO (asked with a Yes / No popup first)."""
    if team not in MORecord.SF_TEAMS or action not in ("check", "uncheck"):
        raise Http404
    mo = get_object_or_404(MORecord.objects.select_related(*MO_RELATED), pk=pk)
    error = ""
    try:
        if action == "check":
            services.sf_mark_done(request, mo, team)
        else:
            services.sf_unmark_done(request, mo, team, request.POST.get("comment", ""))
    except ValidationError as exc:
        error = " ".join(exc.messages)
    except PermissionDenied:
        error = "Your role does not allow this."
    return _group_response(request, mo, error, error_kind="sf", error_pk=team)


def _group_response(request, mo, error, **error_at):
    """The MO group (MO row + transfers) re-rendered after an action, or back to the record without HTMX."""
    mo = MORecord.objects.select_related(*MO_RELATED).get(pk=mo.pk)
    context = {
        "group": {"mo": mo, "transfers": list(mo.transfers.select_related(*ROW_RELATED))},
        "pl": mo.packing_list,
        "link_so": request.headers.get("HX-Current-URL", "").split("?")[0].rstrip("/").endswith("/records"),
        "error": error,
        **error_at,
    }
    if request.headers.get("HX-Request"):
        response = render(request, "mo_close/_mo_group.html", context)
        response["HX-Trigger"] = "progressChanged"
        return response
    if error:
        messages.error(request, error)
    return redirect("mo_close:record_detail", pk=mo.packing_list_id)


@module_required("mo_close.records")
def record_progress_badge(request, pk):
    """Small HTMX fragment: progress summary of one record (refreshed after each row action)."""
    pl = get_object_or_404(PackingList, pk=pk)
    return render(
        request, "mo_close/_progress_summary.html", {"pl": pl, "progress": services.progress_counts([pl])[pl.pk]}
    )


# --- Progress (Step 3 monitoring) -----------------------------------------------------------------


def _row_count(model, record_field, **filters):
    """Subquery: number of MO / transfer rows of each record."""
    sub = (
        model.objects.filter(**{record_field: OuterRef("pk")}, **filters)
        .order_by()
        .values(record_field)
        .annotate(c=Count("id"))
        .values("c")
    )
    return Coalesce(Subquery(sub, output_field=IntegerField()), 0)


# Sortable Progress columns → ORDER BY expression (ascending).
PROGRESS_SORTS = {
    "so": F("so_number"),
    "sold_to": Lower("sold_to"),
    "status": F("status_rank"),
    "rows": F("rows_total"),
    "refresh": F("refreshed_at"),
    "final": F("final_pct"),
}


@module_required("mo_close.progress")
def progress(request):
    show = request.GET.get("show", "open")
    qs = PackingList.objects.exclude(status=PackingList.Status.DRAFT).select_related("created_by").prefetch_related("sales_orders")
    if show == "open":
        qs = qs.filter(status__in=[PackingList.Status.SAVED, PackingList.Status.GENERATED])
    elif show == "closed":
        qs = qs.filter(status=PackingList.Status.CLOSED)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(pk__in=_records_matching(q))

    sort = request.GET.get("sort", "")
    direction = "asc" if request.GET.get("dir") == "asc" else "desc"
    if sort in PROGRESS_SORTS:
        qs = qs.annotate(
            status_rank=Case(
                When(status=PackingList.Status.SAVED, then=Value(1)),
                When(status=PackingList.Status.GENERATED, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            ),
            rows_total=_row_count(MORecord, "packing_list") + _row_count(TransferRecord, "mo__packing_list"),
            final_total=_row_count(MORecord, "packing_list", final_confirmed=True)
            + _row_count(TransferRecord, "mo__packing_list", final_confirmed=True),
        ).annotate(
            final_pct=Case(
                When(rows_total=0, then=Value(-1.0)),
                default=Cast("final_total", FloatField()) * 100.0 / F("rows_total"),
                output_field=FloatField(),
            )
        )
        expr = PROGRESS_SORTS[sort]
        order = expr.asc(nulls_first=True) if direction == "asc" else expr.desc(nulls_last=True)
        qs = qs.order_by(order, "so_number")
    else:
        sort = ""
        qs = qs.order_by("status", "-generated_at", "-created_at")

    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    counts = services.progress_counts(page.object_list)
    for pl in page.object_list:
        pl.progress = counts[pl.pk]

    base = {"show": show, "q": q}
    sort_urls = {
        key: "?" + urlencode({**base, "sort": key, "dir": "desc" if sort == key and direction == "asc" else "asc"})
        for key in PROGRESS_SORTS
    }
    return render(
        request,
        "mo_close/progress.html",
        {
            "page": page,
            "show": show,
            "q": q,
            "sort": sort,
            "dir": direction,
            "sort_urls": sort_urls,
            "query": urlencode({**base, "sort": sort, "dir": direction} if sort else base),
        },
    )


# --- Reports ----------------------------------------------------------------------------------


@module_required("mo_close.reports")
def reports_view(request):
    f = reports.ReportFilters.from_query(request.GET)
    data = reports.build(f)
    return render(
        request,
        "mo_close/reports.html",
        {
            **data,
            "groups": reports.GROUPS,
            "bases": reports.BASES,
            "statuses": reports.STATUSES,
            "presets": reports.PRESETS,
            "export_query": f.query(),
        },
    )


@module_required("mo_close.reports")
def reports_export(request, fmt):
    f = reports.ReportFilters.from_query(request.GET)
    data = reports.build(f)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        content, filename, ctype = report_exports.report_excel(data), f"MO-To-Be-Close_report_{stamp}.xlsx", XLSX
    elif fmt == "pdf":
        content, filename, ctype = report_exports.report_pdf(data), f"MO-To-Be-Close_report_{stamp}.pdf", "application/pdf"
    else:
        raise Http404
    services.audit.log(
        request, Section.EXPORT, f"Exported report {fmt.upper()}", system=SYSTEM, target=filename, comment=f.description
    )
    return _download(content, filename, ctype)


# --- Exports ----------------------------------------------------------------------------------


def _download(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@module_required("mo_close.export")
def record_export(request, pk, fmt):
    pl = get_object_or_404(PackingList.objects.select_related("created_by", "generated_by"), pk=pk)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        content, filename, ctype = exports.record_excel(pl), f"MO-To-Be-Close_{pl.so_number}_{stamp}.xlsx", XLSX
    elif fmt == "pdf":
        content, filename, ctype = exports.record_pdf(pl), f"MO-To-Be-Close_{pl.so_number}_{stamp}.pdf", "application/pdf"
    else:
        raise Http404
    services.log(request, Section.EXPORT, f"Exported {fmt.upper()}", pl, target=filename)
    return _download(content, filename, ctype)


@module_required("mo_close.export")
def progress_export(request, fmt):
    """All rows not yet confirmed done, across open records."""
    records = list(
        PackingList.objects.filter(status=PackingList.Status.GENERATED).order_by("so_number")
    )
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        content, filename, ctype = exports.open_items_excel(records), f"MO-To-Be-Close_open-items_{stamp}.xlsx", XLSX
    elif fmt == "pdf":
        content, filename, ctype = exports.open_items_pdf(records), f"MO-To-Be-Close_open-items_{stamp}.pdf", "application/pdf"
    else:
        raise Http404
    services.audit.log(request, Section.EXPORT, f"Exported open items {fmt.upper()}", system=SYSTEM, target=filename)
    return _download(content, filename, ctype)

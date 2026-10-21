"""Reports: records, rows and confirmations by date (per day / month / year), progress and SO."""

import datetime as dt
from collections import Counter, OrderedDict
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone
from django.utils.http import urlencode

from core.odoo import MO_STATES, PICKING_STATES

from .models import MORecord, PackingList, TransferRecord
from .services import progress_counts, so_search_terms

GROUPS = OrderedDict([("day", "Day"), ("month", "Month"), ("year", "Year")])
BASES = OrderedDict([("created", "Record created"), ("doc_date", "Packing list date")])
STATUSES = OrderedDict([("all", "All"), ("open", "Open (in progress)"), ("closed", "Closed")])
PRESETS = OrderedDict(
    [("today", "Today"), ("month", "This month"), ("year", "This year"), ("12m", "Last 12 months"), ("all", "All time")]
)

# Validated categorical palette (fixed order; colour follows the entity, never its rank).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
# Record status series for "SOs per period" — fixed slot per status.
RECORD_SERIES = [
    ("generated", "In progress", SERIES[0]),
    ("saved", "Not generated yet", SERIES[1]),
    ("closed", "Closed", SERIES[2]),
]
# Row tracking stages are states (good / bad), so they use the status palette, always with a label.
STAGES = [
    ("open", "Not marked done", "#c3c2b7"),
    ("waiting", "Waiting final confirmation", "#fab219"),
    ("flag", "Not confirmed by Odoo", "#d03b3b"),
    ("confirmed", "Confirmed done", "#0ca30c"),
]
MO_STATE_SERIES = [(k, MO_STATES[k], c) for k, c in zip(["confirmed", "progress", "to_close", "done", "cancel"], SERIES)]
PICKING_STATE_SERIES = [
    (k, PICKING_STATES[k], c) for k, c in zip(["waiting", "confirmed", "assigned", "done", "cancel"], SERIES)
]
MAX_DAY_PERIODS = 400


@dataclass
class ReportFilters:
    date_from: dt.date | None
    date_to: dt.date | None
    basis: str = "created"
    group: str = "month"
    status: str = "all"
    so: str = ""
    preset: str = ""

    @classmethod
    def from_query(cls, q):
        today = timezone.localdate()
        preset = q.get("preset", "")
        if not any(k in q for k in ("preset", "from", "to")):
            preset = "year"  # default view: this year by month
        date_from = _parse(q.get("from"))
        date_to = _parse(q.get("to"))
        group = q.get("group") if q.get("group") in GROUPS else None
        if preset == "today":
            date_from = date_to = today
            group = group or "day"
        elif preset == "month":
            date_from, date_to = today.replace(day=1), today
            group = group or "day"
        elif preset == "year":
            date_from, date_to = today.replace(month=1, day=1), today
            group = group or "month"
        elif preset == "12m":
            date_from, date_to = _add_months(today.replace(day=1), -11), today
            group = group or "month"
        elif preset == "all":
            date_from = date_to = None
            group = group or "month"
        else:
            preset = ""
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        return cls(
            date_from=date_from,
            date_to=date_to,
            basis=q.get("basis") if q.get("basis") in BASES else "created",
            group=group or "month",
            status=q.get("status") if q.get("status") in STATUSES else "all",
            so=(q.get("so") or "").strip(),
            preset=preset,
        )

    def query(self, **override):
        params = {
            "from": self.date_from.isoformat() if self.date_from else "",
            "to": self.date_to.isoformat() if self.date_to else "",
            "basis": self.basis,
            "group": self.group,
            "status": self.status,
            "so": self.so,
        }
        if self.preset == "all" and not self.date_from:
            params["preset"] = "all"
        params.update(override)
        return urlencode({k: v for k, v in params.items() if v})

    @property
    def description(self):
        if self.date_from and self.date_to:
            rng = f"{self.date_from:%d-%b-%Y} to {self.date_to:%d-%b-%Y}"
        elif self.date_from:
            rng = f"from {self.date_from:%d-%b-%Y}"
        elif self.date_to:
            rng = f"until {self.date_to:%d-%b-%Y}"
        else:
            rng = "All time"
        parts = [rng, f"by {BASES[self.basis].lower()}", f"per {GROUPS[self.group].lower()}", STATUSES[self.status]]
        if self.so:
            parts.append(f'SO contains "{self.so}"')
        return " · ".join(parts)


def _parse(value):
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _add_months(d, months):
    m = d.month - 1 + months
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def period_start(d, group):
    if group == "year":
        return d.replace(month=1, day=1)
    if group == "month":
        return d.replace(day=1)
    return d


def period_label(d, group):
    if group == "year":
        return f"{d:%Y}"
    if group == "month":
        return f"{d:%b %Y}"
    return f"{d:%d-%b-%Y}"


def period_short(d, group):
    """Compact axis label (the table keeps the full label)."""
    if group == "year":
        return f"{d:%Y}"
    if group == "month":
        return f"{d:%b %y}"
    return f"{d:%d %b}"


def _next_period(d, group):
    if group == "year":
        return d.replace(year=d.year + 1)
    if group == "month":
        return _add_months(d, 1)
    return d + dt.timedelta(days=1)


def _periods(first, last, group):
    """Continuous periods so empty ones show as 0 instead of disappearing."""
    out, cur, end = [], period_start(first, group), period_start(last, group)
    while cur <= end and len(out) < MAX_DAY_PERIODS:
        out.append(cur)
        cur = _next_period(cur, group)
    return out


def _record_date(pl, basis):
    if basis == "doc_date":
        return pl.doc_date
    return timezone.localtime(pl.created_at).date()


def _stage(row):
    if row["final_confirmed"]:
        return "confirmed"
    if row["user_done"]:
        return "waiting" if row["system_status"] == "confirmed" else "flag"
    return "open"


def build(f):
    qs = PackingList.objects.exclude(status=PackingList.Status.DRAFT).select_related("created_by").prefetch_related("sales_orders")
    if f.status == "open":
        qs = qs.filter(status__in=[PackingList.Status.SAVED, PackingList.Status.GENERATED])
    elif f.status == "closed":
        qs = qs.filter(status=PackingList.Status.CLOSED)
    if f.so:
        match = Q()
        for term in so_search_terms(f.so):
            match |= Q(so_number__icontains=term) | Q(sales_orders__so_number__icontains=term)
        qs = qs.filter(match).distinct()
    if f.basis == "doc_date":
        qs = qs.exclude(doc_date=None)
        if f.date_from:
            qs = qs.filter(doc_date__gte=f.date_from)
        if f.date_to:
            qs = qs.filter(doc_date__lte=f.date_to)
    else:
        if f.date_from:
            qs = qs.filter(created_at__date__gte=f.date_from)
        if f.date_to:
            qs = qs.filter(created_at__date__lte=f.date_to)
    records = list(qs.order_by("so_number"))
    ids = [pl.pk for pl in records]
    counts = progress_counts(records)

    fields = ["user_done", "final_confirmed", "system_status", "final_confirmed_at", "state"]
    mo_rows = list(MORecord.objects.filter(packing_list__in=ids).values(*fields))
    tr_rows = list(TransferRecord.objects.filter(mo__packing_list__in=ids).values(*fields, "type_code"))
    rows = mo_rows + tr_rows

    stage_counts = Counter(_stage(r) for r in rows)
    mo_states = Counter(r["state"] for r in mo_rows)
    tr_states = Counter(r["state"] for r in tr_rows)

    # --- per period -------------------------------------------------------------------------
    dates = [d for d in (_record_date(pl, f.basis) for pl in records) if d]
    confirm_dates = [timezone.localtime(r["final_confirmed_at"]).date() for r in rows if r["final_confirmed_at"]]
    first = f.date_from or min(dates + confirm_dates, default=None)
    last = f.date_to or max(dates + confirm_dates, default=None)
    periods = _periods(first, last, f.group) if first and last else []
    by_period = OrderedDict(
        (p, {"records": 0, "generated": 0, "saved": 0, "closed": 0, "rows": 0, "final": 0, "confirmations": 0})
        for p in periods
    )
    for pl in records:
        d = _record_date(pl, f.basis)
        key = period_start(d, f.group) if d else None
        if key not in by_period:
            continue
        b = by_period[key]
        b["records"] += 1
        b[pl.status] = b.get(pl.status, 0) + 1
        b["rows"] += counts[pl.pk]["total"]
        b["final"] += counts[pl.pk]["final"]
    for d in confirm_dates:
        key = period_start(d, f.group)
        if key in by_period:
            by_period[key]["confirmations"] += 1
    period_rows = [
        {"key": k, "label": period_label(k, f.group), "short": period_short(k, f.group), **v} for k, v in by_period.items()
    ]

    # --- per SO -------------------------------------------------------------------------------
    now = timezone.now()
    per_so = []
    for pl in records:
        c = counts[pl.pk]
        end = pl.closed_at or now
        per_so.append(
            {
                "pl": pl,
                "date": _record_date(pl, f.basis),
                "days_open": max((end - pl.created_at).days, 0),
                **c,
            }
        )
    closed = [s for s in per_so if s["pl"].status == PackingList.Status.CLOSED]

    total = len(rows)
    final = stage_counts["confirmed"]
    kpis = {
        "records": len(records),
        "in_progress": sum(1 for pl in records if pl.status == PackingList.Status.GENERATED),
        "not_generated": sum(1 for pl in records if pl.status == PackingList.Status.SAVED),
        "closed": len(closed),
        "mos": len(mo_rows),
        "transfers": len(tr_rows),
        "rows": total,
        "marked": sum(1 for r in rows if r["user_done"]),
        "odoo_done": sum(1 for r in rows if r["system_status"] == "confirmed"),
        "final": final,
        "percent": round(100 * final / total) if total else 0,
        "avg_days_to_close": round(sum(s["days_open"] for s in closed) / len(closed), 1) if closed else None,
    }

    def series(spec, counter):
        return [{"key": k, "label": label, "color": color, "value": counter.get(k, 0)} for k, label, color in spec]

    by_so_chart = sorted(per_so, key=lambda s: (s["percent"], s["pl"].so_number))
    return {
        "filters": f,
        "kpis": kpis,
        "periods": period_rows,
        "per_so": per_so,
        "stages": series(STAGES, stage_counts),
        "mo_states": series(MO_STATE_SERIES, mo_states),
        "transfer_states": series(PICKING_STATE_SERIES, tr_states),
        "chart": {
            "periods": {
                "labels": [p["short"] for p in period_rows],
                "full_labels": [p["label"] for p in period_rows],
                "series": [
                    {"label": label, "color": color, "data": [p.get(key, 0) for p in period_rows]}
                    for key, label, color in RECORD_SERIES
                ],
                "confirmations": [p["confirmations"] for p in period_rows],
            },
            "stages": series(STAGES, stage_counts),
            "mo_states": series(MO_STATE_SERIES, mo_states),
            "transfer_states": series(PICKING_STATE_SERIES, tr_states),
            "by_so": {
                "labels": [s["pl"].so_number for s in by_so_chart if s["total"]],
                "data": [s["percent"] for s in by_so_chart if s["total"]],
                "detail": [f'{s["final"]}/{s["total"]} rows' for s in by_so_chart if s["total"]],
            },
        },
    }

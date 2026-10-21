import re
from collections import OrderedDict
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.odoo import MO_STATES, PICKING_STATES

SYSTEM = "mo_close"


def norm_code(code):
    """Product code compared without spaces, dashes or case (PDF vs Odoo formatting)."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


_SO_PARTS = re.compile(r"^S(\d+)((?:-\d+)*)$")


def so_digits(so):
    """'S08027' → '8027', child SO 'S08027-1' → '8027-1' (without the S and leading zeros)."""
    m = _SO_PARTS.match(so)
    return f"{int(m.group(1))}{m.group(2)}" if m else so


def so_base_digits(so):
    """'S08027-1-2' → '8027' (the number of the S0 the child is named after)."""
    return so_digits(so).split("-", 1)[0]


def so_root(so):
    """'S04743-1-2' → 'S04743': the parent S0 a child SO is named after — the Sales Order as it is in Odoo."""
    m = _SO_PARTS.match(so)
    return f"S{m.group(1)}" if m else so


def so_parent_number(so):
    """'S08027-1-2' → 'S08027-1', 'S08027-1' → 'S08027', 'S08027' → '' (no parent in the name)."""
    return so.rsplit("-", 1)[0] if _SO_PARTS.match(so) and "-" in so else ""


def so_sort_key(so):
    """Natural order: S08027, S08027-1, S08027-2, S08027-10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", so)]


def so_source_pattern(so):
    """Finds the SO number in an MO Source however it was typed. The correct form is 'S0…' (S08027);
    SO8027, So8027, SS08027, S8027, O8027, 08027, 'SO# 08027' and a bare 8027 are auto-corrected to it — but
    never S080271, PO8027, a number that is part of a date / time (10/16/2026, 2026-08-07), or a bare number in
    parentheses, which in SAWO Sources is the customer code (UKRAINE(4300), Sauna Europe (11500))."""
    sep = r"[\s#.:-]*"
    base, *suffix = so_digits(so).split("-")
    # A child SO keeps its suffix ('S08027-1', 'S08027 - 1'); 'S08027-1' is never the parent S08027.
    number = re.escape(base) + "".join(r"\s?-\s?" + re.escape(n) for n in suffix)
    prefixed = r"(?<![A-Za-z0-9])(?:S{1,2}" + sep + r"[O0]?|[O0])" + sep + r"0*" + number + r"(?!\d)(?!-\d)"
    bare = r"(?<![A-Za-z0-9/.:(-])0*" + number + r"(?![\d/)]|[.:-]\d)"
    return re.compile(f"(?P<prefixed>{prefixed})|(?P<bare>{bare})", re.IGNORECASE)


# An SO written with a letter prefix (S08027, SO6863, SS08027, S 8027) — used to tell that a bare number next to
# it is not an SO but the customer code ('SO6863 - Australia - 8200').
_PREFIXED_SO = re.compile(r"(?<![A-Za-z0-9])(?:S{1,2}[\s#.:-]*[O0]?|O)[\s#.:-]*0*(\d{3,6})(?!\d)", re.IGNORECASE)
_MONTH = (
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?"
    r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
_DATE_BEFORE = re.compile(_MONTH + r"[\s.,]*(?:\d{1,2}[\s.,]*)?$|\d\s*,\s*$", re.IGNORECASE)


def _other_so_in(so, source):
    number = int(so_base_digits(so)) if so_base_digits(so).isdigit() else None
    return any(int(m.group(1)) != number for m in _PREFIXED_SO.finditer(source))


def _bare_is_so(so, source, m):
    """A bare number is the SO only when it is not a customer code, model code, quantity or year."""
    before, after = source[: m.start()], source[m.end() :]
    if re.search(r"\(\s*$", before) and re.match(r"\s*\)", after):
        return False  # alone in brackets: customer code, 'UKRAINE ( 4300 )'
    if re.match(r"[A-Za-z]{1,3}(?![A-Za-z])", after):
        return False  # model code or quantity: 'For 1414RS', '100pcs'
    if _DATE_BEFORE.search(before):
        return False  # year of a date: 'July 30, 2026', 'Jan.08,2026'
    return not _other_so_in(so, source)  # 'SO6863 - Australia - 8200': the SO is 6863, 8200 the customer code


def so_in_source(so, source, bare=True):
    """Every spelling of the SO found in an MO Source (['SO8027']), [] when it is not there. bare=False ignores
    bare numbers (no S / O prefix), for an SO whose number is also a customer code — see so_is_customer_code."""
    source = source or ""
    return [
        m.group(0)
        for m in so_source_pattern(so).finditer(source)
        if m.group("prefixed") or (bare and _bare_is_so(so, source, m))
    ]


def so_is_customer_code(so, sources):
    """True when the SO's number is a customer code in any of these MO Sources — alone in brackets
    ('UKRAINE(4300)', 'UKRAINE ( 4300 )') or bare next to another SO ('SO6863 - Australia - 8200'). Then a bare
    number in a Source ('Heater for UKRAINE 4300') is the customer, not the SO."""
    number = re.escape(so_digits(so))
    in_brackets = re.compile(r"\(\s*0*" + number + r"\s*\)")
    pattern = so_source_pattern(so)
    for source in sources:
        source = source or ""
        if in_brackets.search(source):
            return True
        if _other_so_in(so, source) and any(m.group("bare") for m in pattern.finditer(source)):
            return True
    return False


def so_fix_hint(wrong):
    """What is wrong in an SO spelling, in words — 'SO4743' and 'S04743' look the same in many fonts."""
    w = re.sub(r"[\s#.:-]", "", wrong)
    hints = []
    if "O" in w.upper():
        hints.append("letter O instead of zero")
    if not w.upper().startswith("S"):
        hints.append("S missing")
    elif w.upper().startswith("SS"):
        hints.append("double S")
    elif not re.match(r"S[O0]", w, re.IGNORECASE):
        hints.append("0 missing after the S")
    if w != w.upper():
        hints.append("lowercase")
    if w != wrong:
        hints.append("extra characters")
    return ", ".join(hints)


def so_source_fix(so, source):
    """The wrong spelling of the SO in an MO Source ('SO8027'), or '' when it is written correctly."""
    found = so_in_source(so, source)
    if not found or so in found:
        return ""
    return found[0]


def group_lines(lines):
    """Packing lines grouped by External Code with Qty summed (as shown on the record)."""
    groups = OrderedDict()
    for line in lines:
        key = norm_code(line.external_code) or f"__line{line.pk}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "code": line.external_code,
                "norm": norm_code(line.external_code),
                "description": line.description,
                "um": line.um,
                "qty": Decimal("0"),
                "rows": 0,
            }
        g["qty"] += line.qty or 0
        g["rows"] += 1
    return list(groups.values())


HEADER_FIELDS = ["sold_to", "tel", "fax", "attn", "doc_date_text", "doc_date", "invoice_no", "container_no", "seal_no", "destination"]
TOTAL_FIELDS = ["total_packages", "total_net_weight", "total_gross_weight", "total_cbm", "total_qty"]


class PackingHeader(models.Model):
    """Header information and Grand Total of a packing list PDF."""

    sold_to = models.TextField(blank=True)
    tel = models.CharField("Tel. No.", max_length=100, blank=True)
    fax = models.CharField("Fax No.", max_length=100, blank=True)
    attn = models.CharField("ATTN", max_length=200, blank=True)
    doc_date_text = models.CharField("Date", max_length=50, blank=True)
    doc_date = models.DateField(null=True, blank=True)
    invoice_no = models.CharField("Invoice No.", max_length=50, blank=True)
    container_no = models.CharField("Container No.", max_length=200, blank=True)
    seal_no = models.CharField("Seal No.", max_length=100, blank=True)
    destination = models.CharField(max_length=100, blank=True)

    # Grand total row from the PDF
    total_packages = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_net_weight = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    total_gross_weight = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    total_cbm = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    total_qty = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def sold_to_name(self):
        return self.sold_to.splitlines()[0] if self.sold_to else ""


class PackingList(PackingHeader):
    """One record per parent Sales Order (S0). It holds the SO's tree of child SOs (other Odoo SOs grouped
    under it, any depth) and their packing list PDFs; MOs are generated and tracked once for the whole tree.
    The header fields show the parent's first packing list (containers and totals: all of them)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SAVED = "saved", "Saved"
        GENERATED = "generated", "In progress"
        CLOSED = "closed", "Closed"

    so_number = models.CharField("SO", max_length=20, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    saved_at = models.DateTimeField(null=True, blank=True)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    refreshed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.so_number

    @property
    def audit_label(self):
        return self.so_number

    def grouped_lines(self):
        """Lines of every saved packing list of the record (all SOs), grouped by External Code."""
        return group_lines(self.lines.filter(document__saved=True))

    def so_tree(self):
        """[SalesOrder, …] parent first, each child right under its parent (depth-first), with .depth set."""
        nodes = list(self.sales_orders.all())
        by_pk = {node.pk: node for node in nodes}
        children = {}
        for node in nodes:
            if node.parent_id in by_pk:
                node.parent = by_pk[node.parent_id]  # no query per node for .parent / .path
            children.setdefault(node.parent_id, []).append(node)
        out = []

        def walk(parent_id, depth):
            for node in sorted(children.get(parent_id, []), key=lambda n: so_sort_key(n.so_number)):
                node.depth = depth
                out.append(node)
                walk(node.pk, depth + 1)

        walk(None, 0)
        return out

    def mo_placements(self, mos, tree=None):
        """Where each MO is listed: [(mo, SO node, qty)], parent SO first, each child under it, within an SO the
        packing list items then the components. A child named after its parent (S04743-1) is an additional packing
        list of the same Odoo Sales Order (S04743), whose MOs say S04743 in their Source: such an MO is listed under
        every SO of that S0 whose packing list has its product, with that SO's qty — one MO can serve both
        (MO qty 30 = 10 on S04743 + 20 on S04743-1). It is the same MO row, so ticks and confirmations are shared.
        An MO whose Source names the child itself, or a component, is listed under its own SO only."""
        tree = self.so_tree() if tree is None else tree
        if not tree:
            return [(mo, None, mo.qty) for mo in mos]
        position = {n.pk: i for i, n in enumerate(tree)}
        by_pk = {n.pk: n for n in tree}
        for n in tree:
            if not hasattr(n, "groups"):
                n.groups = n.grouped_lines()
        qty = {n.pk: {g["norm"]: g["qty"] for g in n.groups if g["norm"]} for n in tree}
        out = []
        for mo in mos:
            owner = by_pk.get(mo.sales_order_id) or tree[0]
            code = norm_code(mo.fg_code)
            places = [owner]
            if mo.on_packing_list and not (owner.so_number != so_root(owner.so_number) and so_in_source(owner.so_number, mo.origin)):
                root = so_root(owner.so_number)
                places = [n for n in tree if so_root(n.so_number) == root and code in qty[n.pk]] or [owner]
            out.extend((mo, n, qty[n.pk].get(code, mo.qty) if mo.on_packing_list else mo.qty) for n in places)
        out.sort(key=lambda p: (position[p[1].pk], not p[0].on_packing_list))  # stable: MO order kept within
        return out

    @property
    def child_sos(self):
        return self.so_tree()[1:]

    def ensure_root_so(self):
        """The record's own (parent) SO node, created when missing."""
        root, _ = SalesOrder.objects.get_or_create(
            record=self, parent=None, defaults={"so_number": self.so_number, "created_by_id": self.created_by_id}
        )
        return root

    def sync_header(self, save=True):
        """Header from the parent's first packing list; container numbers and totals from all of them."""
        docs = [d for d in self.documents.select_related("sales_order") if d.saved] or list(self.documents.all())
        if not docs:
            return
        docs.sort(key=lambda d: (d.sales_order.parent_id is not None, d.created_at, d.pk))
        for name in HEADER_FIELDS:
            setattr(self, name, getattr(docs[0], name))
        containers = list(OrderedDict.fromkeys(d.container_no for d in docs if d.container_no))
        self.container_no = ", ".join(containers)[:200]
        for name in TOTAL_FIELDS:
            values = [getattr(d, name) for d in docs if getattr(d, name) is not None]
            setattr(self, name, sum(values) if values else None)
        if save:
            self.save(update_fields=HEADER_FIELDS + TOTAL_FIELDS)

    def all_rows(self):
        """Every tracked row (MOs and their transfers)."""
        rows = []
        for mo in self.mos.all():
            rows.append(mo)
            rows.extend(mo.transfers.all())
        return rows

    def update_status(self, save=True):
        """In progress ↔ Closed: closed when every MO and transfer row has final confirmation."""
        if self.status not in (self.Status.GENERATED, self.Status.CLOSED):
            return
        rows = self.all_rows()
        if rows and all(r.final_confirmed for r in rows):
            if self.status != self.Status.CLOSED:
                self.status, self.closed_at = self.Status.CLOSED, timezone.now()
        else:
            self.status, self.closed_at = self.Status.GENERATED, None
        if save:
            self.save(update_fields=["status", "closed_at"])


class SalesOrder(models.Model):
    """One SO of a record: the record's own parent S0 (no parent) or a child SO under it, any depth.
    A child is another Odoo SO grouped under the parent — S04743-1, S04743-1-1, or any number (S08869)."""

    record = models.ForeignKey(PackingList, on_delete=models.CASCADE, related_name="sales_orders")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    so_number = models.CharField("SO", max_length=30, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.so_number

    @property
    def is_root(self):
        return self.parent_id is None

    def ancestors(self):
        """[parent, grandparent, …]."""
        out, node = [], self.parent
        while node is not None:
            out.append(node)
            node = node.parent
        return out

    @property
    def path(self):
        """'S04743 › S04743-1 › S04743-1-1'."""
        return " › ".join(n.so_number for n in [*reversed(self.ancestors()), self])

    def grouped_lines(self):
        """Lines of this SO's saved packing lists, grouped by External Code."""
        return group_lines(PackingLine.objects.filter(document__sales_order=self, document__saved=True))


class PackingDocument(PackingHeader):
    """One uploaded packing list PDF, for one SO of the record. Waits for review (saved=False) after upload."""

    record = models.ForeignKey(PackingList, on_delete=models.CASCADE, related_name="documents")
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="documents")
    saved = models.BooleanField(default=False)

    pdf = models.FileField(upload_to="packing_lists/%Y/%m/")
    pdf_name = models.CharField(max_length=255, blank=True)
    parse_warnings = models.JSONField(default=list, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.pdf_name or f"Packing list {self.pk}"

    def grouped_lines(self):
        return group_lines(self.lines.all())


class PackingLine(models.Model):
    """One row of the packing list table, saved as printed."""

    packing_list = models.ForeignKey(PackingList, on_delete=models.CASCADE, related_name="lines")
    document = models.ForeignKey(PackingDocument, null=True, blank=True, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveIntegerField()
    external_code = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=500, blank=True)
    um = models.CharField("UM", max_length=20, blank=True)
    packages = models.DecimalField("Number of Packages", max_digits=12, decimal_places=2, null=True, blank=True)
    net_weight = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    gross_weight = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    cbm = models.DecimalField("CBM", max_digits=12, decimal_places=3, null=True, blank=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["line_no"]


class TrackedRow(models.Model):
    """Odoo item followed until done: user marks done → system checks Odoo → Checker confirms (Step 3)."""

    class SystemStatus(models.TextChoices):
        NONE = "", "Not checked"
        CONFIRMED = "confirmed", "Done in Odoo"
        NOT_DONE = "not_done", "Not done in Odoo"
        ERROR = "error", "Could not check"

    odoo_id = models.PositiveIntegerField()
    reference = models.CharField(max_length=100)
    state = models.CharField("Odoo state", max_length=20, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    user_done = models.BooleanField(default=False)
    user_done_at = models.DateTimeField(null=True, blank=True)
    user_done_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    system_status = models.CharField(max_length=10, choices=SystemStatus.choices, blank=True, default="")
    system_checked_at = models.DateTimeField(null=True, blank=True)
    system_odoo_state = models.CharField(max_length=20, blank=True)

    final_confirmed = models.BooleanField(default=False)
    final_confirmed_at = models.DateTimeField(null=True, blank=True)
    final_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    final_comment = models.TextField(blank=True)

    STATE_LABELS = {}
    KIND = ""

    class Meta:
        abstract = True

    @property
    def state_label(self):
        return self.STATE_LABELS.get(self.state, self.state or "—")

    @property
    def system_state_label(self):
        return self.STATE_LABELS.get(self.system_odoo_state, self.system_odoo_state)

    @property
    def is_odoo_done(self):
        return self.state == "done"

    @property
    def tracking_stage(self):
        if self.final_confirmed:
            return "confirmed"
        if self.user_done:
            return "waiting"
        return "open"

    @property
    def row_progress(self):
        """Progress of this one row over the Step 3 steps: marked done → done in Odoo → final confirmation."""
        steps = [
            ("Marked done", self.user_done),
            ("Done in Odoo", self.system_status == self.SystemStatus.CONFIRMED or self.final_confirmed),
            ("Final confirmation", self.final_confirmed),
        ]
        done = sum(1 for _, ok in steps if ok)
        if self.final_confirmed:
            label = "Confirmed"
        elif self.user_done:
            label = "Waiting checker"
        else:
            label = "Not marked done"
        return {
            "done": done,
            "total": len(steps),
            "percent": round(100 * done / len(steps)),
            "label": label,
            "title": " · ".join(f"{'✓' if ok else '○'} {name}" for name, ok in steps),
        }


class MORecord(TrackedRow):
    """A Manufacturing Order of the SO (Step 2): matched to a packing line, or — when its product is not on
    the packing list — a component / sub-assembly made for the SO (fg_* then hold the MO's product)."""

    packing_list = models.ForeignKey(PackingList, on_delete=models.CASCADE, related_name="mos")
    # The SO of the record's tree that the MO Source names (the most specific one: S04743-1 over S04743).
    sales_order = models.ForeignKey(SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="mos")
    on_packing_list = models.BooleanField(default=True)
    fg_code = models.CharField("FG Code", max_length=100)
    fg_description = models.CharField("FG", max_length=500, blank=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    product_qty = models.DecimalField("MO Qty", max_digits=12, decimal_places=2, null=True, blank=True)
    product_label = models.CharField(max_length=500, blank=True)
    origin = models.CharField("Source", max_length=500, blank=True)

    # Semi-finished work done by SF-Innova / SF-Electrical (checked by Users, unchecked only by Admin with a reason)
    sf_innova_done = models.BooleanField("SF-Innova done", default=False)
    sf_innova_at = models.DateTimeField(null=True, blank=True)
    sf_innova_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sf_innova_undo_comment = models.TextField(blank=True)
    sf_innova_undo_at = models.DateTimeField(null=True, blank=True)
    sf_innova_undo_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sf_electrical_done = models.BooleanField("SF-Electrical done", default=False)
    sf_electrical_at = models.DateTimeField(null=True, blank=True)
    sf_electrical_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sf_electrical_undo_comment = models.TextField(blank=True)
    sf_electrical_undo_at = models.DateTimeField(null=True, blank=True)
    sf_electrical_undo_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    STATE_LABELS = MO_STATES
    KIND = "mo"
    # team code → (label, module that may check it)
    SF_TEAMS = {
        "innova": ("SF-Innova", "mo_close.sf_innova"),
        "electrical": ("SF-Electrical", "mo_close.sf_electrical"),
    }
    SF_RELATED = [f"sf_{team}_{suffix}" for team in SF_TEAMS for suffix in ("by", "undo_by")]

    class Meta:
        ordering = ["-on_packing_list", "fg_code", "reference"]
        constraints = [models.UniqueConstraint(fields=["packing_list", "odoo_id"], name="uniq_mo_per_record")]

    def __str__(self):
        return self.reference

    @property
    def plant(self):
        """'PL1/MO/36235' → 'PL1'."""
        return self.reference.split("/", 1)[0]

    def sf_mark(self, team):
        """One SF column of this MO as a dict (done, at, by, undo_comment, undo_at, undo_by)."""
        get = lambda name: getattr(self, f"sf_{team}_{name}")  # noqa: E731
        return {
            "team": team,
            "label": self.SF_TEAMS[team][0],
            "module": self.SF_TEAMS[team][1],
            "done": get("done"),
            "at": get("at"),
            "by": get("by"),
            "undo_comment": get("undo_comment"),
            "undo_at": get("undo_at"),
            "undo_by": get("undo_by"),
        }

    @property
    def sf_marks(self):
        return [self.sf_mark(team) for team in self.SF_TEAMS]

    @property
    def so_number(self):
        """The MO's own SO: a child SO of the record, or the record's parent SO."""
        return self.sales_order.so_number if self.sales_order_id else self.packing_list.so_number

    @property
    def source_so(self):
        """The SO the Odoo Source names: the MO's own SO, or — for an additional packing list named after its
        parent (S04743-1) — the parent S0 (S04743), the only one Odoo knows."""
        so = self.so_number
        return so if so_in_source(so, self.origin) else so_root(so)

    @property
    def source_fix(self):
        """Fix flag: the SO is written wrong in the Odoo Source (e.g. 'SO8027' instead of 'S08027')."""
        return so_source_fix(self.source_so, self.origin)

    @property
    def source_fix_hint(self):
        """'letter O instead of zero' for 'SO4743', … ('' when the Source is right)."""
        fix = self.source_fix
        return so_fix_hint(fix) if fix else ""


class TransferRecord(TrackedRow):
    """A transfer (PC / SFP …) of a matched MO that was not Done when generated (Step 2)."""

    mo = models.ForeignKey(MORecord, on_delete=models.CASCADE, related_name="transfers")
    type_code = models.CharField(max_length=20, blank=True)
    type_name = models.CharField(max_length=200, blank=True)
    scheduled_date = models.DateTimeField(null=True, blank=True)

    STATE_LABELS = PICKING_STATES
    KIND = "transfer"

    class Meta:
        ordering = ["reference"]
        constraints = [models.UniqueConstraint(fields=["mo", "odoo_id"], name="uniq_transfer_per_mo")]

    def __str__(self):
        return self.reference

    @property
    def packing_list(self):
        return self.mo.packing_list

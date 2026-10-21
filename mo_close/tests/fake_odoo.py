"""In-memory stand-in for Odoo, based on the S04743 screenshots (PL3/MO/06873 etc.)."""

import copy

from core.odoo import OdooSessionExpired


def _term(record, field, op, value):
    actual = record.get(field)
    if op == "=":
        return actual == value
    if op == "in":
        return actual in value
    if op == "ilike":
        return str(value).lower() in str(actual or "").lower()
    raise NotImplementedError(op)


def _match(record, domain):
    """Odoo domain in prefix notation: terms are ANDed unless joined by '|' / '&'."""
    stack = []
    for item in reversed(domain):
        if item == "|":
            stack.append(stack.pop() | stack.pop())
        elif item == "&":
            stack.append(stack.pop() & stack.pop())
        else:
            stack.append(_term(record, *item))
    return all(stack)


class FakeOdoo:
    def __init__(self):
        self.calls = []
        self.expired = False
        self.data = {
            "sale.order": [{"id": 1, "name": "S04743"}],
            "product.product": [
                {"id": 101, "default_code": "NRNS-60NS-BL-Z-C", "name": "Nordex S Black 3H.E 6,0kW"},
                {"id": 102, "default_code": "NRNS-90NS-BL-Z-C", "name": "Nordex S Black 3H.E 9,0kW"},
                {"id": 103, "default_code": "OTHER-CODE", "name": "Not on the packing list"},
                {"id": 104, "default_code": "2INN-IH23-001", "name": "Plastic Backing - Rectangular Innova Classic V2.0"},
            ],
            "mrp.production": [
                {"id": 1, "name": "PL3/MO/06873", "product_id": [101, "[NRNS-60NS-BL-Z-C] Nordex S Black 3H.E 6,0kW"],
                 "product_qty": 30.0, "state": "confirmed", "picking_ids": [201, 202, 203],
                 "origin": "Heater for S04743 - HOLLAND SAWO 3 - 1500 - target date 12/12/25"},
                {"id": 2, "name": "PL3/MO/07787", "product_id": [102, "[NRNS-90NS-BL-Z-C] Nordex S Black 3H.E 9,0kW"],
                 "product_qty": 50.0, "state": "confirmed", "picking_ids": [204],
                 "origin": "Heater for S04743 - HOLLAND SAWO 3 - 1500 - target date 12/12/25"},
                {"id": 3, "name": "PL3/MO/09999", "product_id": [103, "[OTHER-CODE] Not on the packing list"],
                 "product_qty": 5.0, "state": "confirmed", "picking_ids": [],
                 "origin": "Heater for S04743"},
                {"id": 4, "name": "PL3/MO/08888", "product_id": [101, "[NRNS-60NS-BL-Z-C] Other SO"],
                 "product_qty": 5.0, "state": "confirmed", "picking_ids": [],
                 "origin": "Heater for S047431"},
                {"id": 5, "name": "PL3/MO/06871", "product_id": [101, "[NRNS-60NS-BL-Z-C] Cancelled one"],
                 "product_qty": 5.0, "state": "cancel", "picking_ids": [],
                 "origin": "Heater for S04743"},
                # Component MO whose Source spells the SO as "SO4743" (like PL1/MO/34770-002 for SO8027).
                {"id": 6, "name": "PL1/MO/34770-002", "product_id": [104, "[2INN-IH23-001] Plastic Backing"],
                 "product_qty": 1732.0, "state": "progress", "picking_ids": [205],
                 "origin": "SO4743 -Sawo Sauna Europe B.V. - 11500 -TARGET 8/7/26 (for Container 13)"},
                {"id": 7, "name": "PL1/MO/34999", "product_id": [104, "[2INN-IH23-001] Other number"],
                 "product_qty": 1.0, "state": "confirmed", "picking_ids": [],
                 "origin": "PO4743 / S47430"},
                # Child SO S04743-1 (another Odoo SO under S04743) and a separate SO S08869 grouped under it.
                {"id": 8, "name": "PL1/MO/40001", "product_id": [101, "[NRNS-60NS-BL-Z-C] Child SO"],
                 "product_qty": 4.0, "state": "confirmed", "picking_ids": [],
                 "origin": "Heater for S04743-1 - HOLLAND SAWO 3"},
                {"id": 9, "name": "PL2/MO/50001", "product_id": [102, "[NRNS-90NS-BL-Z-C] Grouped SO"],
                 "product_qty": 2.0, "state": "confirmed", "picking_ids": [],
                 "origin": "S08869 - Costa Rica"},
            ],
            "stock.picking": [
                {"id": 201, "name": "PL3/SFP/04631", "state": "waiting", "picking_type_id": [7, "PL3: Store Finished Product"],
                 "scheduled_date": "2025-10-26 08:00:00"},
                {"id": 202, "name": "PL3/PC/17852", "state": "assigned", "picking_type_id": [8, "PL3: Pick Components"],
                 "scheduled_date": "2025-10-26 08:00:00"},
                {"id": 203, "name": "PL3/PC/05565", "state": "done", "picking_type_id": [8, "PL3: Pick Components"],
                 "scheduled_date": False},
                {"id": 204, "name": "PL3/PC/17855", "state": "confirmed", "picking_type_id": [8, "PL3: Pick Components"],
                 "scheduled_date": False},
                {"id": 205, "name": "PL1/SFP/16273", "state": "waiting", "picking_type_id": [9, "PL1: Store Finished Product"],
                 "scheduled_date": "2026-07-31 08:00:00"},
            ],
        }

    def set_state(self, model, odoo_id, state):
        for rec in self.data[model]:
            if rec["id"] == odoo_id:
                rec["state"] = state

    def _check(self, *call):
        self.calls.append(call)
        if self.expired:
            raise OdooSessionExpired("Your Odoo session has expired. Please log in again.")

    def search_read(self, model, domain, fields, order=None, limit=None):
        self._check("search_read", model, domain)
        rows = [r for r in self.data[model] if _match(r, domain)]
        return [{k: copy.deepcopy(r.get(k)) for k in ["id", *fields]} for r in rows]

    def read(self, model, ids, fields):
        self._check("read", model, ids)
        ids = set(ids)
        return [{k: copy.deepcopy(r.get(k)) for k in ["id", *fields]} for r in self.data[model] if r["id"] in ids]

    def search_count(self, model, domain):
        self._check("search_count", model, domain)
        return len([r for r in self.data[model] if _match(r, domain)])

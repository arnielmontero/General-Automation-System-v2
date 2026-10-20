"""Steps 1–3 end to end through the web views, with Odoo replaced by FakeOdoo."""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import AuditLog, Role, User
from core.odoo import OdooAuthError
from mo_close.models import MORecord, PackingList, TransferRecord

from .fake_odoo import FakeOdoo

SAMPLE = Path(__file__).parent / "fixtures" / "packing_list_S04743.pdf"


def sample_upload(name="packing.pdf"):
    return SimpleUploadedFile(name, SAMPLE.read_bytes(), content_type="application/pdf")


class LoginTests(TestCase):
    def test_login_page(self):
        self.assertContains(self.client.get(reverse("login")), "Odoo email")

    def test_pages_require_login(self):
        resp = self.client.get(reverse("mo_close:records"))
        self.assertRedirects(resp, reverse("login") + "?next=" + reverse("mo_close:records"))

    @mock.patch("core.backends.OdooClient")
    def test_login_with_odoo_account(self, client_cls):
        odoo = client_cls.return_value
        odoo.authenticate.return_value = {"uid": 42, "username": "Jane@Sawo.com", "name": "Jane Cruz"}
        odoo.session_id = "sid-123"
        with self.settings(ADMIN_ODOO_LOGINS=["admin@sawo.com"]):
            resp = self.client.post(reverse("login"), {"email": "jane@sawo.com", "password": "secret"})
        self.assertRedirects(resp, reverse("home"), fetch_redirect_response=False)
        user = User.objects.get(odoo_uid=42)
        self.assertEqual((user.username, user.first_name, user.last_name, user.role.name), ("jane@sawo.com", "Jane", "Cruz", "User"))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(self.client.session["odoo_session_id"], "sid-123")

    @mock.patch("core.backends.OdooClient")
    def test_admin_login_from_env(self, client_cls):
        client_cls.return_value.authenticate.return_value = {"uid": 7, "username": "boss@sawo.com", "name": "Boss"}
        client_cls.return_value.session_id = "sid-7"
        with self.settings(ADMIN_ODOO_LOGINS=["boss@sawo.com"]):
            self.client.post(reverse("login"), {"email": "boss@sawo.com", "password": "x"})
        self.assertEqual(User.objects.get(odoo_uid=7).role.name, "Admin")

    @mock.patch("core.backends.OdooClient")
    def test_wrong_password(self, client_cls):
        client_cls.return_value.authenticate.side_effect = OdooAuthError("Wrong email or password.")
        resp = self.client.post(reverse("login"), {"email": "jane@sawo.com", "password": "bad"})
        self.assertContains(resp, "Wrong email or password.")
        self.assertTrue(AuditLog.objects.filter(action="Login failed").exists())


class WorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = tempfile.mkdtemp()  # uploaded PDFs never land in the real media volume
        cls.media_override = override_settings(MEDIA_ROOT=cls.media)
        cls.media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.media_override.disable()
        shutil.rmtree(cls.media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.odoo = FakeOdoo()
        patcher = mock.patch("mo_close.views.client_for", return_value=self.odoo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create(username="user@sawo.com", first_name="Ana", role=Role.objects.get(name="User"))
        self.checker = User.objects.create(username="checker@sawo.com", first_name="Ben", role=Role.objects.get(name="Checker / Supervisor"))
        self.client.force_login(self.user)

    # --- helpers ---
    def create_record(self, so="4743"):
        resp = self.client.post(reverse("mo_close:record_new"), {"so_number": so, "pdf": sample_upload()})
        pl = PackingList.objects.get(so_number="S04743")
        self.assertRedirects(resp, reverse("mo_close:record_preview", args=[pl.pk]))
        return pl

    def save_and_generate(self):
        pl = self.create_record()
        self.client.post(reverse("mo_close:record_preview", args=[pl.pk]), {"action": "save"})
        self.client.post(reverse("mo_close:record_generate", args=[pl.pk]))
        pl.refresh_from_db()
        return pl

    def act(self, row, action, **data):
        kind = "mo" if isinstance(row, MORecord) else "transfer"
        return self.client.post(
            reverse("mo_close:row_action", args=[kind, row.pk, action]), data, HTTP_HX_REQUEST="true"
        )

    # --- Step 1 ---
    def test_step1_upload_preview_save(self):
        pl = self.create_record()
        self.assertEqual(pl.status, PackingList.Status.DRAFT)
        self.assertEqual(pl.lines.count(), 23)
        self.assertEqual(pl.container_no, "PACK0000856")
        self.assertEqual(pl.parse_warnings, [])
        resp = self.client.get(reverse("mo_close:record_preview", args=[pl.pk]))
        self.assertContains(resp, "Wellzijn BV")
        groups = pl.grouped_lines()
        self.assertEqual([(g["code"], int(g["qty"])) for g in groups], [
            ("NRNS-60NS-BL-Z-C", 10), ("NRNS-90NS-BL-Z-C", 10), ("NRNF-105NS-BL-Z", 10),
            ("NRNF-120NS-BL-Z", 5), ("NRNF-150NS-BL-Z", 10),
        ])
        self.client.post(reverse("mo_close:record_preview", args=[pl.pk]), {"action": "save"})
        pl.refresh_from_db()
        self.assertEqual(pl.status, PackingList.Status.SAVED)
        self.assertContains(self.client.get(reverse("mo_close:records")), "S04743")

    def test_step1_duplicate_and_mismatch(self):
        self.create_record()
        resp = self.client.post(reverse("mo_close:record_new"), {"so_number": "S04743", "pdf": sample_upload()})
        self.assertContains(resp, "already exists")
        resp = self.client.post(reverse("mo_close:record_new"), {"so_number": "S05000", "pdf": sample_upload()})
        pl = PackingList.objects.get(so_number="S05000")
        self.assertIn("The PDF Invoice No. is S04743, but this record is for S05000.", pl.parse_warnings)
        self.assertIn("Sales Order S05000 was not found in Odoo.", pl.parse_warnings)

    def test_step1_cancel_draft(self):
        pl = self.create_record()
        self.client.post(reverse("mo_close:record_preview", args=[pl.pk]), {"action": "cancel"})
        self.assertFalse(PackingList.objects.exists())

    # --- Step 2 ---
    def test_step2_generate(self):
        pl = self.save_and_generate()
        self.assertEqual(pl.status, PackingList.Status.GENERATED)
        mos = {m.reference: m for m in pl.mos.all()}
        # Other SO (S047431, PO4743 / S47430) and cancelled excluded; components of the SO included.
        self.assertEqual(set(mos), {"PL3/MO/06873", "PL3/MO/07787", "PL3/MO/09999", "PL1/MO/34770-002"})
        mo = mos["PL3/MO/06873"]
        self.assertTrue(mo.on_packing_list)
        self.assertEqual((mo.fg_code, int(mo.qty), int(mo.product_qty), mo.state_label), ("NRNS-60NS-BL-Z-C", 10, 30, "Confirmed"))
        comp = mos["PL1/MO/34770-002"]  # Source says "SO4743", product not on the packing list
        self.assertFalse(comp.on_packing_list)
        self.assertEqual((comp.fg_code, comp.fg_description, int(comp.qty), comp.state_label),
                         ("2INN-IH23-001", "Plastic Backing - Rectangular Innova Classic V2.0", 0, "In Progress"))
        self.assertEqual([t.reference for t in comp.transfers.all()], ["PL1/SFP/16273"])
        self.assertEqual((comp.source_fix, mo.source_fix), ("SO4743", ""))  # fix flag only on the wrong Source
        self.assertEqual([m.reference for m in pl.mos.all()][-2:], ["PL1/MO/34770-002", "PL3/MO/09999"])  # components last
        transfers = {t.reference: t.state_label for t in mo.transfers.all()}
        self.assertEqual(transfers, {"PL3/SFP/04631": "Waiting Another Operation", "PL3/PC/17852": "Ready"})  # Done skipped
        self.assertEqual(mos["PL3/MO/07787"].transfers.get().type_code, "PC")

        resp = self.client.get(reverse("mo_close:record_detail", args=[pl.pk]))
        self.assertContains(resp, "PL3/SFP/04631")
        self.assertContains(resp, "NRNF-105NS-BL-Z")  # in "No MO found"
        self.assertContains(resp, "Component · not on packing list")
        self.assertContains(resp, "Source fix: SO4743 → S04743")
        self.assertContains(resp, "PL1/SFP/16273")

    def test_so_in_source_spellings_and_fix_flag(self):
        from mo_close.models import so_source_fix, so_source_pattern

        pattern = so_source_pattern("S08027")
        for text in ["S08027 - SAWO", "CU for SO8027 -Sawo", "MS for So8027", "S8027", "O8027", "08027", "SO# 08027",
                     "SO 8027", "(S08027)", "s08027", "SS08027-UKRAINE", "8027", "CU for 8027 - Sawo"]:
            self.assertTrue(pattern.search(text), text)
        for text in ["S080271", "S08028", "PO8027", "ABS08027", "S18027", "108027", "80271", "8/8027", "8027/26",
                     "1.8027", "8027.5", "12:8027", "2026-8027", "8027-12", "SSS08027"]:
            self.assertFalse(pattern.search(text), text)
        # A bare number in parentheses is the customer code: UKRAINE(4300) is not SO S04300.
        self.assertFalse(so_source_pattern("S04300").search("SS06092-UKRAINE(4300) Quality pass"))
        self.assertFalse(so_source_pattern("S11500").search("Sauna Europe (11500)"))
        # A year inside a date is not SO S02026.
        self.assertFalse(so_source_pattern("S02026").search("Target Date = 10/16/2026"))
        self.assertFalse(so_source_pattern("S02026").search("2026-10-16 03:21"))
        # Fix flag: anything but the exact "S08027" is flagged; correct Sources are not.
        self.assertEqual(so_source_fix("S08027", "S08027 - SAWO SAUNA EUROPE"), "")
        self.assertEqual(so_source_fix("S08027", "CU for SO8027 -Sawo"), "SO8027")
        self.assertEqual(so_source_fix("S08027", "CU for So8027"), "So8027")
        self.assertEqual(so_source_fix("S08027", "S8027"), "S8027")
        self.assertEqual(so_source_fix("S08027", "for 08027"), "08027")
        self.assertEqual(so_source_fix("S08027", "s08027"), "s08027")
        self.assertEqual(so_source_fix("S08027", "CU for 8027 - Sawo"), "8027")
        self.assertEqual(so_source_fix("S08027", "SS08027-UKRAINE"), "SS08027")

    def test_so_bare_number_is_not_a_customer_code_model_or_year(self):
        from mo_close.models import so_in_source, so_is_customer_code

        # Taken as the SO: a bare number that is the only order reference in the Source.
        for so, text in [("S05270", "5270 SAWO EUROPE"), ("S09164", "prio 800 for the SSE 9164, Completion 10/16"),
                         ("S08643", "THAILAND  8643Consolidated SF + buffer"), ("S04793", "( KOREA 4793 )")]:
            self.assertTrue(so_in_source(so, text), text)
        # Not the SO: customer code alone in brackets or next to another SO, model code / quantity, year of a date.
        for so, text in [("S04300", "S06092 - UKRAINE ( 4300 ) = 03/13"),
                         ("S08200", "HP for SO6863 - Australia - 8200 - Target Date: 6-5-26"),
                         ("S01500", "Heater for S04752 - HOLLAND SAWO 2 - 1500 - target date 11/25/25"),
                         ("S01414", "EC for Local - Target Date: 7-24-26(For 1414RS)"),
                         ("S00100", "prio 100pcs Consolidated SF + buffer"),
                         ("S02026", "Target Date: July 30, 2026"), ("S02026", "Target date Jan.08,2026 by air")]:
            self.assertEqual(so_in_source(so, text), [], text)
        # Prefixed spellings are kept whatever else is in the Source.
        self.assertEqual(so_in_source("S08200", "HP for SO8200 - USA - 00000"), ["SO8200"])
        # Once the number is a customer code anywhere in the search results, bare numbers are ignored for the SO.
        sources = ["S06092 - UKRAINE ( 4300 ) = 03/13", "Heater for UKRAINE 4300 - target date 1/9/26", "S04300 - X"]
        self.assertTrue(so_is_customer_code("S04300", sources))
        self.assertFalse(so_is_customer_code("S05270", ["5270 SAWO EUROPE", "SO 5270 Sauna Europe"]))
        self.assertEqual([s for s in sources if so_in_source("S04300", s, bare=False)], ["S04300 - X"])

    def test_step2_generate_again_keeps_tracking(self):
        pl = self.save_and_generate()
        mo = pl.mos.get(reference="PL3/MO/06873")
        self.act(mo, "mark")
        self.odoo.set_state("stock.picking", 201, "assigned")
        self.client.post(reverse("mo_close:record_generate", args=[pl.pk]))
        mo.refresh_from_db()
        self.assertTrue(mo.user_done)
        self.assertEqual(TransferRecord.objects.get(reference="PL3/SFP/04631").state, "assigned")
        self.assertEqual(MORecord.objects.count(), 4)
        self.assertTrue(AuditLog.objects.filter(action="Odoo state changed", target="PL3/SFP/04631").exists())

    # --- Step 3 ---
    def test_step3_mark_done_not_done_in_odoo_needs_comment(self):
        pl = self.save_and_generate()
        mo = pl.mos.get(reference="PL3/MO/06873")
        resp = self.act(mo, "mark")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Odoo: Confirmed")
        mo.refresh_from_db()
        self.assertTrue(mo.user_done)
        self.assertEqual(mo.user_done_by, self.user)
        self.assertEqual(mo.system_status, "not_done")

        # A plain user cannot confirm.
        resp = self.act(mo, "confirm", comment="ok")
        self.assertContains(resp, "Your role does not allow this.")
        mo.refresh_from_db()
        self.assertFalse(mo.final_confirmed)

        self.client.force_login(self.checker)
        resp = self.act(mo, "confirm")
        self.assertContains(resp, "a comment is required")
        self.act(mo, "confirm", comment="Checked physically in warehouse")
        mo.refresh_from_db()
        self.assertTrue(mo.final_confirmed)
        self.assertEqual(mo.final_confirmed_by, self.checker)
        log = AuditLog.objects.get(action="Final confirmation (override)")
        self.assertEqual(log.comment, "Checked physically in warehouse")

    def test_step3_done_in_odoo_then_record_closes(self):
        pl = self.save_and_generate()
        for model, ids in (("mrp.production", [1, 2, 3, 6]), ("stock.picking", [201, 202, 204, 205])):
            for i in ids:
                self.odoo.set_state(model, i, "done")
        rows = pl.all_rows()
        self.assertEqual(len(rows), 8)  # component MOs (not on the packing list) must be closed too
        for row in rows:
            self.act(row, "mark")
        self.client.force_login(self.checker)
        for row in pl.all_rows():
            self.assertEqual(row.system_status, "confirmed")
            self.act(row, "confirm")
        pl.refresh_from_db()
        self.assertEqual(pl.status, PackingList.Status.CLOSED)

        row = pl.mos.first()
        self.act(row, "unconfirm")
        pl.refresh_from_db()
        self.assertEqual(pl.status, PackingList.Status.GENERATED)

    def test_step3_refresh_rechecks_marked_rows(self):
        pl = self.save_and_generate()
        tr = TransferRecord.objects.get(reference="PL3/PC/17852")
        self.act(tr, "mark")
        tr.refresh_from_db()
        self.assertEqual(tr.system_status, "not_done")
        self.odoo.set_state("stock.picking", 202, "done")
        self.client.post(reverse("mo_close:record_refresh", args=[pl.pk]))
        tr.refresh_from_db()
        self.assertEqual(tr.system_status, "confirmed")

    def test_unmark(self):
        pl = self.save_and_generate()
        mo = pl.mos.first()
        self.act(mo, "mark")
        self.act(mo, "unmark")
        mo.refresh_from_db()
        self.assertFalse(mo.user_done)
        self.assertEqual(mo.system_status, "")

    def test_session_expired_redirects_to_login(self):
        pl = self.save_and_generate()
        self.odoo.expired = True
        resp = self.act(pl.mos.first(), "mark")
        self.assertEqual(resp["HX-Redirect"], reverse("login"))

    # --- Reports / pages ---
    def test_exports_and_pages(self):
        pl = self.save_and_generate()
        self.act(pl.mos.first(), "mark")
        for fmt, magic in (("xlsx", b"PK"), ("pdf", b"%PDF")):
            resp = self.client.get(reverse("mo_close:record_export", args=[pl.pk, fmt]))
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.content.startswith(magic))
            resp = self.client.get(reverse("mo_close:progress_export", args=[fmt]))
            self.assertTrue(resp.content.startswith(magic))
        for name in ("mo_close:dashboard", "mo_close:records", "mo_close:progress"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("mo_close:record_pdf", args=[pl.pk])).status_code, 200)

    # --- Reports ---
    def _second_record(self):
        return PackingList.objects.create(
            so_number="S00001", status=PackingList.Status.SAVED, sold_to="Aardvark Oy\nFinland",
            created_by=self.user, pdf="packing_lists/none.pdf",
        )

    def test_reports_numbers(self):
        from mo_close import reports

        pl = self.save_and_generate()
        self._second_record()
        tr = TransferRecord.objects.get(reference="PL3/PC/17852")
        self.odoo.set_state("stock.picking", 202, "done")
        self.act(tr, "mark")
        self.act(pl.mos.get(reference="PL3/MO/06873"), "mark")
        self.client.force_login(self.checker)
        self.act(tr, "confirm")

        data = reports.build(reports.ReportFilters.from_query({"preset": "all"}))
        k = data["kpis"]
        self.assertEqual((k["records"], k["in_progress"], k["not_generated"], k["rows"], k["mos"], k["transfers"]), (2, 1, 1, 8, 4, 4))
        self.assertEqual((k["marked"], k["odoo_done"], k["final"], k["percent"]), (2, 1, 1, 12))
        self.assertEqual({s["key"]: s["value"] for s in data["stages"]}, {"open": 6, "waiting": 0, "flag": 1, "confirmed": 1})
        self.assertEqual({s["key"]: s["value"] for s in data["mo_states"]}["confirmed"], 3)
        self.assertEqual(sum(p["records"] for p in data["periods"]), 2)
        self.assertEqual(sum(p["confirmations"] for p in data["periods"]), 1)

        data = reports.build(reports.ReportFilters.from_query({"preset": "all", "status": "open", "so": "4743"}))
        self.assertEqual([s["pl"].so_number for s in data["per_so"]], ["S04743"])
        data = reports.build(reports.ReportFilters.from_query({"preset": "all", "basis": "doc_date"}))
        self.assertEqual(data["kpis"]["records"], 1)  # S00001 has no packing list date
        data = reports.build(reports.ReportFilters.from_query({"from": "2001-01-01", "to": "2001-12-31", "group": "day"}))
        self.assertEqual((data["kpis"]["records"], len(data["periods"])), (0, 365))

    def test_reports_page_and_downloads(self):
        self.save_and_generate()
        resp = self.client.get(reverse("mo_close:reports"))
        self.assertContains(resp, "SO records per month")
        self.assertContains(resp, 'id="report-data"')
        for preset in ("today", "month", "year", "12m", "all"):
            self.assertEqual(self.client.get(reverse("mo_close:reports"), {"preset": preset}).status_code, 200)
        for group in ("day", "month", "year"):
            self.assertContains(self.client.get(reverse("mo_close:reports"), {"preset": "all", "group": group}), "S04743")
        for fmt, magic in (("xlsx", b"PK"), ("pdf", b"%PDF")):
            resp = self.client.get(reverse("mo_close:reports_export", args=[fmt]), {"preset": "all", "group": "day"})
            self.assertTrue(resp.content.startswith(magic), fmt)
        empty = self.client.get(reverse("mo_close:reports_export", args=["pdf"]), {"from": "2001-01-01", "to": "2001-01-31"})
        self.assertTrue(empty.content.startswith(b"%PDF"))
        self.assertTrue(AuditLog.objects.filter(action="Exported report PDF").exists())

    def test_reports_permission(self):
        self.client.force_login(User.objects.create(username="v@sawo.com", role=Role.objects.create(name="Viewer", modules=["mo_close.records"])))
        self.assertEqual(self.client.get(reverse("mo_close:reports")).status_code, 403)

    # --- Progress sorting ---
    def test_progress_sort(self):
        pl = self.save_and_generate()
        other = self._second_record()
        mo = pl.mos.first()
        self.act(mo, "mark")

        def order(**params):
            resp = self.client.get(reverse("mo_close:progress"), {"show": "all", **params})
            return [p.so_number for p in resp.context["page"].object_list]

        self.assertEqual(order(sort="so", dir="asc"), ["S00001", "S04743"])
        self.assertEqual(order(sort="so", dir="desc"), ["S04743", "S00001"])
        self.assertEqual(order(sort="sold_to", dir="asc"), ["S00001", "S04743"])  # Aardvark before Wellzijn
        self.assertEqual(order(sort="status", dir="asc"), ["S00001", "S04743"])  # not generated before in progress
        self.assertEqual(order(sort="rows", dir="desc"), ["S04743", "S00001"])
        self.assertEqual(order(sort="refresh", dir="desc"), ["S04743", "S00001"])
        self.assertEqual(order(sort="final", dir="asc"), ["S00001", "S04743"])
        resp = self.client.get(reverse("mo_close:progress"), {"show": "all", "sort": "rows", "dir": "asc"})
        self.assertContains(resp, 'aria-sort="ascending"')
        self.assertContains(resp, "sort=rows&amp;dir=desc")  # clicking again flips the direction
        self.assertEqual(order(sort="bogus"), order())  # unknown sort → default order
        self.assertTrue(other.pk)

    def test_admin_pages(self):
        self.assertEqual(self.client.get(reverse("users")).status_code, 403)
        admin = User.objects.create(username="admin@sawo.com", role=Role.admin())
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("logs")).status_code, 200)
        checker_role = Role.objects.get(name="Checker / Supervisor")
        self.client.post(reverse("user_update", args=[self.user.pk]), {"role": checker_role.pk, "active": "1"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, checker_role)

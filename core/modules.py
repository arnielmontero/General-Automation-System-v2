"""Modules (permissions) a role can be given. Each module is a feature of the system that a role can tick.

To add a module for a new system: add it here and protect its views with ``module_required("<code>")``.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    code: str
    label: str
    description: str
    group: str


MODULES = [
    Module("mo_close.dashboard", "Dashboard", "KPI cards, records in progress, recent activity", "MO's To Be Close"),
    Module("mo_close.records", "Records — view", "Records list, record detail, packing list, logs of a record", "MO's To Be Close"),
    Module("mo_close.create", "New Record", "Create a record by SO and upload the packing list PDF", "MO's To Be Close"),
    Module("mo_close.generate", "Generate / Refresh from Odoo", "Collect MOs and transfers from Odoo, refresh states", "MO's To Be Close"),
    Module("mo_close.mark_done", "Mark done", "Tick MO / transfer rows as done (system checks Odoo)", "MO's To Be Close"),
    Module("mo_close.sf_innova", "SF-Innova done", "Check SF-Innova done on an MO (cannot uncheck)", "MO's To Be Close"),
    Module("mo_close.sf_electrical", "SF-Electrical done", "Check SF-Electrical done on an MO (cannot uncheck)", "MO's To Be Close"),
    Module("mo_close.sf_undo", "Uncheck SF done", "Uncheck SF-Innova / SF-Electrical with a required reason (Admin)", "MO's To Be Close"),
    Module("mo_close.confirm","Final confirmation", "Checker / Supervisor final confirmation of rows", "MO's To Be Close"),
    Module("mo_close.progress", "Progress", "Progress monitoring of all records", "MO's To Be Close"),
    Module("mo_close.reports", "Reports", "Reports & charts by date, progress and SO (per day / month / year)", "MO's To Be Close"),
    Module("mo_close.export", "Export reports", "Download Excel / PDF reports", "MO's To Be Close"),
    Module("mo_close.delete", "Delete records", "Permanently delete a record and its tracking", "MO's To Be Close"),
    Module("admin.users", "Users", "Assign roles to users, enable / disable users", "Administration"),
    Module("admin.roles", "Roles & Permissions", "Create roles and tick their modules", "Administration"),
    Module("admin.logs", "Logs", "Activity log of the whole system", "Administration"),
]

BY_CODE = {m.code: m for m in MODULES}
ALL_CODES = [m.code for m in MODULES]


def grouped(modules=MODULES):
    """[(group name, [Module, …]), …] in declaration order."""
    groups = {}
    for m in modules:
        groups.setdefault(m.group, []).append(m)
    return list(groups.items())


def label(code):
    m = BY_CODE.get(code)
    return m.label if m else code

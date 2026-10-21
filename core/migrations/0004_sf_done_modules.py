"""Give the new SF-Innova / SF-Electrical done modules to the built-in Checker / Supervisor and User roles.
Unchecking (mo_close.sf_undo) stays with Admin, which has every module."""

from django.db import migrations

CODES = ["mo_close.sf_innova", "mo_close.sf_electrical"]
ROLES = ["Checker / Supervisor", "User"]


def add(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    for role in Role.objects.filter(name__in=ROLES, is_system=True):
        missing = [c for c in CODES if c not in role.modules]
        if missing:
            role.modules = [*role.modules, *missing]
            role.save(update_fields=["modules"])


def remove(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    for role in Role.objects.filter(name__in=ROLES, is_system=True):
        role.modules = [c for c in role.modules if c not in CODES]
        role.save(update_fields=["modules"])


class Migration(migrations.Migration):
    dependencies = [("core", "0003_reports_module")]
    operations = [migrations.RunPython(add, remove)]

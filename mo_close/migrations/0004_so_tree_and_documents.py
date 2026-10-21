"""Records become a parent SO with a tree of child SOs and many packing list PDFs: every existing record gets
its root SO, and its one PDF becomes a packing list document of that SO (lines and MOs follow)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


HEADER = ["sold_to", "tel", "fax", "attn", "doc_date_text", "doc_date", "invoice_no", "container_no", "seal_no", "destination",
          "total_packages", "total_net_weight", "total_gross_weight", "total_cbm", "total_qty"]


def forwards(apps, schema_editor):
    PackingList = apps.get_model("mo_close", "PackingList")
    SalesOrder = apps.get_model("mo_close", "SalesOrder")
    PackingDocument = apps.get_model("mo_close", "PackingDocument")
    for pl in PackingList.objects.all():
        root = SalesOrder.objects.create(record=pl, so_number=pl.so_number, created_by_id=pl.created_by_id)
        SalesOrder.objects.filter(pk=root.pk).update(created_at=pl.created_at)
        doc = PackingDocument.objects.create(
            record=pl,
            sales_order=root,
            saved=pl.status != "draft",
            pdf=pl.pdf.name,
            pdf_name=pl.pdf_name,
            parse_warnings=pl.parse_warnings,
            created_by_id=pl.created_by_id,
            saved_at=pl.saved_at,
            **{name: getattr(pl, name) for name in HEADER},
        )
        PackingDocument.objects.filter(pk=doc.pk).update(created_at=pl.created_at)
        pl.lines.update(document=doc)
        pl.mos.update(sales_order=root)


def backwards(apps, schema_editor):
    PackingList = apps.get_model("mo_close", "PackingList")
    for pl in PackingList.objects.all():
        doc = pl.documents.order_by("created_at", "id").first()
        if doc:
            pl.pdf, pl.pdf_name, pl.parse_warnings = doc.pdf.name, doc.pdf_name, doc.parse_warnings
            pl.save(update_fields=["pdf", "pdf_name", "parse_warnings"])


class Migration(migrations.Migration):

    dependencies = [
        ('mo_close', '0003_mo_sf_done'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PackingDocument',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sold_to', models.TextField(blank=True)),
                ('tel', models.CharField(blank=True, max_length=100, verbose_name='Tel. No.')),
                ('fax', models.CharField(blank=True, max_length=100, verbose_name='Fax No.')),
                ('attn', models.CharField(blank=True, max_length=200, verbose_name='ATTN')),
                ('doc_date_text', models.CharField(blank=True, max_length=50, verbose_name='Date')),
                ('doc_date', models.DateField(blank=True, null=True)),
                ('invoice_no', models.CharField(blank=True, max_length=50, verbose_name='Invoice No.')),
                ('container_no', models.CharField(blank=True, max_length=200, verbose_name='Container No.')),
                ('seal_no', models.CharField(blank=True, max_length=100, verbose_name='Seal No.')),
                ('destination', models.CharField(blank=True, max_length=100)),
                ('total_packages', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('total_net_weight', models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
                ('total_gross_weight', models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
                ('total_cbm', models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ('total_qty', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('saved', models.BooleanField(default=False)),
                ('pdf', models.FileField(upload_to='packing_lists/%Y/%m/')),
                ('pdf_name', models.CharField(blank=True, max_length=255)),
                ('parse_warnings', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('saved_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('record', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='mo_close.packinglist')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddField(
            model_name='packingline',
            name='document',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='mo_close.packingdocument'),
        ),
        migrations.CreateModel(
            name='SalesOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('so_number', models.CharField(max_length=30, unique=True, verbose_name='SO')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='mo_close.salesorder')),
                ('record', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sales_orders', to='mo_close.packinglist')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.AddField(
            model_name='packingdocument',
            name='sales_order',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='mo_close.salesorder'),
        ),
        migrations.AddField(
            model_name='morecord',
            name='sales_order',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='mos', to='mo_close.salesorder'),
        ),
        migrations.RunPython(forwards, backwards),
    ]

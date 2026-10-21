from django import forms
from django.conf import settings

from .services import normalize_so, validate_so

MAX_PDFS = 20


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Several files in one input; cleans to a list."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data] if data else [single(None, initial)]
        return [single(data, initial)]


def _check_pdf(pdf):
    if not pdf.name.lower().endswith(".pdf"):
        raise forms.ValidationError(f"{pdf.name}: please upload a PDF file.")
    if pdf.size > settings.MAX_PDF_UPLOAD_MB * 1024 * 1024:
        raise forms.ValidationError(f"{pdf.name}: the PDF is larger than {settings.MAX_PDF_UPLOAD_MB} MB.")
    head = pdf.read(5)
    pdf.seek(0)
    if head != b"%PDF-":
        raise forms.ValidationError(f"{pdf.name}: this file is not a valid PDF.")


class NewRecordForm(forms.Form):
    """New Record: a parent S0 and its packing list PDF. Child SOs and more PDFs are added from the record."""

    so_number = forms.CharField(
        label="Sales Order (SO)",
        max_length=20,
        widget=forms.TextInput(attrs={"placeholder": "e.g. S04743", "class": "form-control", "autofocus": True}),
    )
    pdf = forms.FileField(
        label="Packing list PDF",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,.pdf", "class": "form-control"}),
    )

    def clean_so_number(self):
        so = normalize_so(self.cleaned_data["so_number"])
        validate_so(so)
        if "-" in so:
            raise forms.ValidationError(
                f"{so} is a child SO. Go to Records, find the parent SO and click its number to upload it there."
            )
        return so

    def clean_pdf(self):
        pdf = self.cleaned_data["pdf"]
        _check_pdf(pdf)
        return pdf


class UploadForm(forms.Form):
    """Upload packing lists to a record: for an SO of its tree, or a new child SO, with one or more PDFs."""

    so_number = forms.CharField(
        label="Sales Order (SO)",
        max_length=30,
        widget=forms.TextInput(attrs={"placeholder": "e.g. S04743 or S04743-1", "class": "form-control", "autofocus": True}),
    )
    parent_so = forms.CharField(
        label="Parent SO (optional)",
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. S04743", "class": "form-control"}),
    )
    pdfs = MultipleFileField(
        label="Packing list PDFs",
        widget=MultipleFileInput(attrs={"accept": "application/pdf,.pdf", "class": "form-control"}),
    )

    def __init__(self, *args, record, **kwargs):
        super().__init__(*args, **kwargs)
        self.record = record
        choices = [("", "— by the SO number, else under " + record.so_number + " —")]
        choices += [(n.so_number, "   " * n.depth + n.so_number) for n in record.so_tree()]
        self.fields["parent_so"] = forms.ChoiceField(
            label="Parent SO (for a new child SO)",
            choices=choices,
            required=False,
            widget=forms.Select(attrs={"class": "form-select"}),
        )
        self.fields["so_number"].widget.attrs["list"] = "record-sos"

    def clean_so_number(self):
        so = normalize_so(self.cleaned_data["so_number"])
        validate_so(so)
        return so

    def clean_parent_so(self):
        parent = normalize_so(self.cleaned_data.get("parent_so") or "")
        if parent:
            validate_so(parent)
        return parent

    def clean_pdfs(self):
        pdfs = self.cleaned_data["pdfs"]
        if len(pdfs) > MAX_PDFS:
            raise forms.ValidationError(f"Upload at most {MAX_PDFS} PDFs at a time.")
        errors = []
        for pdf in pdfs:
            try:
                _check_pdf(pdf)
            except forms.ValidationError as exc:
                errors.extend(exc.messages)
        if errors:
            raise forms.ValidationError(errors)
        return pdfs

    def clean(self):
        data = super().clean()
        if data.get("so_number") and data.get("so_number") == data.get("parent_so"):
            self.add_error("parent_so", "An SO cannot be its own parent.")
        return data

from django.urls import path

from . import views

app_name = "mo_close"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("records/", views.records, name="records"),
    path("records/new/", views.record_new, name="record_new"),
    path("records/<int:pk>/", views.record_detail, name="record_detail"),
    path("records/<int:pk>/preview/", views.record_preview, name="record_preview"),
    path("records/<int:pk>/pdf/", views.record_pdf, name="record_pdf"),
    path("records/<int:pk>/delete/", views.record_delete, name="record_delete"),
    path("records/<int:pk>/generate/", views.record_generate, name="record_generate"),
    path("records/<int:pk>/refresh/", views.record_refresh, name="record_refresh"),
    path("records/<int:pk>/progress/", views.record_progress_badge, name="record_progress"),
    path("records/<int:pk>/export/<str:fmt>/", views.record_export, name="record_export"),
    path("rows/<str:kind>/<int:pk>/<str:action>/", views.row_action, name="row_action"),
    path("mos/<int:pk>/sf/<str:team>/<str:action>/", views.sf_action, name="sf_action"),
    path("progress/", views.progress, name="progress"),
    path("progress/export/<str:fmt>/", views.progress_export, name="progress_export"),
    path("reports/", views.reports_view, name="reports"),
    path("reports/export/<str:fmt>/", views.reports_export, name="reports_export"),
]

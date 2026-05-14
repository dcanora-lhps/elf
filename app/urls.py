from django.urls import path

from . import sync, views

urlpatterns = [
    # Santa sync endpoints (no trailing slash — avoids 301-on-POST from the Santa client).
    path("preflight/<str:machine_id>", sync.preflight, name="santa-preflight"),
    path("eventupload/<str:machine_id>", sync.eventupload, name="santa-eventupload"),
    path("ruledownload/<str:machine_id>", sync.ruledownload, name="santa-ruledownload"),
    path("postflight/<str:machine_id>", sync.postflight, name="santa-postflight"),
    # UI.
    path("", views.dashboard, name="dashboard"),
    path("rules/", views.RuleListView.as_view(), name="rule-list"),
    path("rules/new/", views.RuleCreateView.as_view(), name="rule-create"),
    path("rules/<int:pk>/edit/", views.RuleUpdateView.as_view(), name="rule-edit"),
    path("rules/<int:pk>/delete/", views.RuleDeleteView.as_view(), name="rule-delete"),
    path("events/", views.EventListView.as_view(), name="event-list"),
    path("events/aggregate/", views.event_aggregate, name="event-aggregate"),
    path("events/export.csv", views.event_export_csv, name="event-export-csv"),
    path("events/<int:pk>/", views.EventDetailView.as_view(), name="event-detail"),
    path("unknown/", views.UnknownMachineListView.as_view(), name="unknown-list"),
    path("machines/", views.machine_list, name="machine-list"),
    path("machines/<path:machine_id>/", views.machine_detail, name="machine-detail"),
    path("settings/", views.server_settings_view, name="server-settings"),
    path("settings/toggle-monitor-only/", views.toggle_monitor_only, name="toggle-monitor-only"),
]

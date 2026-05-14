import csv
import json
from datetime import timedelta
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import (
    SORT_KEYS,
    EventFilterForm,
    MachinePolicyForm,
    RuleFilterForm,
    RuleForm,
    ServerSettingsForm,
)
from .mobileconfig import AUDIENCE_LABELS, build_mobileconfig
from .models import (
    Audience,
    AuxiliaryEvent,
    Event,
    MachinePolicy,
    Rule,
    RuleType,
    ServerSettings,
    SyncSession,
    UnknownMachine,
)


@login_required
def dashboard(request):
    now = timezone.now()
    last_day = now - timedelta(days=1)

    top_blocked = list(
        Event.objects.filter(received_at__gte=last_day, decision__startswith="BLOCK_")
        .values("team_id", "signing_id", "file_bundle_id", "file_name")
        .annotate(count=Count("id"), last_seen=Max("received_at"))
        .order_by("-count", "-last_seen")[:10]
    )

    ctx = {
        "rule_count": Rule.objects.count(),
        "rules_teachers": Rule.objects.filter(applies_to_teachers=True).count(),
        "rules_middle_school": Rule.objects.filter(applies_to_middle_school=True).count(),
        "rules_upper_school": Rule.objects.filter(applies_to_upper_school=True).count(),
        "events_24h": Event.objects.filter(received_at__gte=last_day).count(),
        "blocks_24h": Event.objects.filter(
            received_at__gte=last_day, decision__startswith="BLOCK_"
        ).count(),
        "unknown_count": UnknownMachine.objects.count(),
        "in_flight_sessions": SyncSession.objects.filter(completed_at__isnull=True).count(),
        "server_settings": ServerSettings.get(),
        "top_blocked": top_blocked,
    }
    return render(request, "dashboard.html", ctx)


@login_required
def server_settings_view(request):
    obj = ServerSettings.get()
    if request.method == "POST":
        form = ServerSettingsForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Server settings saved.")
            return redirect("server-settings")
    else:
        form = ServerSettingsForm(instance=obj)
    return render(request, "server_settings.html", {"form": form, "settings": obj})


@login_required
@require_POST
def toggle_monitor_only(request):
    obj = ServerSettings.get()
    obj.monitor_only = not obj.monitor_only
    obj.save()
    messages.success(
        request,
        f"Monitor-only mode {'enabled' if obj.monitor_only else 'disabled'}.",
    )
    return redirect(request.POST.get("next") or "dashboard")


@login_required
def mobileconfig_download(request, audience):
    if audience not in AUDIENCE_LABELS:
        return HttpResponse("Unknown audience.", status=404)
    body, _ = build_mobileconfig(audience)
    resp = HttpResponse(body, content_type="application/x-apple-aspen-config")
    resp["Content-Disposition"] = f'attachment; filename="santa-{audience}.mobileconfig"'
    return resp


RULE_LIST_SORTS = {
    "-updated_at",
    "updated_at",
    "rule_type",
    "-rule_type",
    "policy",
    "-policy",
}


class RuleListView(LoginRequiredMixin, ListView):
    model = Rule
    template_name = "rules/list.html"
    context_object_name = "rules"
    paginate_by = 50

    def get_queryset(self):
        qs = Rule.objects.all()
        self.filter_form = RuleFilterForm(self.request.GET or None)
        if self.filter_form.is_valid():
            d = self.filter_form.cleaned_data
            if d.get("q"):
                qs = qs.filter(
                    Q(identifier__icontains=d["q"])
                    | Q(custom_msg__icontains=d["q"])
                    | Q(notification_app_name__icontains=d["q"])
                    | Q(comment__icontains=d["q"])
                )
            if d.get("rule_type"):
                qs = qs.filter(rule_type=d["rule_type"])
            if d.get("policy"):
                qs = qs.filter(policy=d["policy"])
            aud = d.get("audience")
            if aud == "middle_school":
                qs = qs.filter(applies_to_middle_school=True)
            elif aud == "upper_school":
                qs = qs.filter(applies_to_upper_school=True)
            elif aud == "teachers":
                qs = qs.filter(applies_to_teachers=True)
            elif aud == "any_student":
                qs = qs.filter(
                    Q(applies_to_middle_school=True) | Q(applies_to_upper_school=True)
                )
            elif aud == "all":
                qs = qs.filter(
                    applies_to_middle_school=True,
                    applies_to_upper_school=True,
                    applies_to_teachers=True,
                )
        sort = self.request.GET.get("sort") or "-updated_at"
        if sort not in RULE_LIST_SORTS:
            sort = "-updated_at"
        self.current_sort = sort
        return qs.order_by(sort, "-id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        ctx["querystring"] = self.request.GET.urlencode()
        ctx["current_sort"] = getattr(self, "current_sort", "-updated_at")
        filters_only = self.request.GET.copy()
        filters_only.pop("sort", None)
        filters_only.pop("page", None)
        ctx["filters_querystring"] = filters_only.urlencode()
        return ctx


_BACK_TO_ALLOWLIST = {"/events/", "/events/aggregate/", "/rules/"}


def _safe_back_to(request, candidate):
    """Validate a redirect target points back to a list page in this app.

    Accepts only same-host URLs whose path is exactly /events/, /events/aggregate/,
    or /rules/. The query string is preserved. Returns the candidate string when
    safe, else None.
    """
    if not candidate:
        return None
    if not url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=False
    ):
        return None
    path = urlparse(candidate).path or ""
    if path not in _BACK_TO_ALLOWLIST:
        return None
    return candidate


class _BackToMixin:
    """Captures a return URL from the referrer (GET) or hidden field (POST) so
    that saving a rule sends the user back to wherever they came from."""

    def _back_to(self):
        return _safe_back_to(
            self.request,
            self.request.POST.get("_back_to")
            or self.request.GET.get("_back_to")
            or self.request.META.get("HTTP_REFERER", ""),
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["back_to"] = self._back_to() or ""
        return ctx

    def get_success_url(self):
        back = _safe_back_to(self.request, self.request.POST.get("_back_to", ""))
        return back or str(self.success_url)


class RuleCreateView(_BackToMixin, LoginRequiredMixin, CreateView):
    model = Rule
    form_class = RuleForm
    template_name = "rules/form.html"
    success_url = reverse_lazy("rule-list")

    def get_initial(self):
        initial = super().get_initial()
        identifier = (self.request.GET.get("identifier") or "").strip()
        rule_type = (self.request.GET.get("rule_type") or "").strip()
        if identifier:
            initial["identifier"] = identifier
        if rule_type in {c.value for c in RuleType}:
            initial["rule_type"] = rule_type
        return initial


class RuleUpdateView(_BackToMixin, LoginRequiredMixin, UpdateView):
    model = Rule
    form_class = RuleForm
    template_name = "rules/form.html"
    success_url = reverse_lazy("rule-list")


class RuleDeleteView(LoginRequiredMixin, DeleteView):
    model = Rule
    template_name = "rules/confirm_delete.html"
    success_url = reverse_lazy("rule-list")


def _apply_event_filters(qs, filter_form, *, extended_search=False):
    """Apply EventFilterForm cleaned_data to an Event queryset. When extended_search,
    also searches file_bundle_id (used by the aggregate view).
    """
    if not filter_form.is_valid():
        return qs
    d = filter_form.cleaned_data
    if d.get("audience"):
        qs = qs.filter(audience=d["audience"])
    if d.get("decision"):
        qs = qs.filter(decision=d["decision"])
    if d.get("only_blocks"):
        qs = qs.filter(decision__startswith="BLOCK_")
    if d.get("machine_id"):
        qs = qs.filter(machine_id__icontains=d["machine_id"])
    if d.get("date_from"):
        qs = qs.filter(received_at__date__gte=d["date_from"])
    if d.get("date_to"):
        qs = qs.filter(received_at__date__lte=d["date_to"])
    if d.get("q"):
        q = d["q"]
        text_q = (
            Q(file_name__icontains=q)
            | Q(file_sha256__icontains=q)
            | Q(signing_id__icontains=q)
            | Q(team_id__icontains=q)
            | Q(cdhash__icontains=q)
            | Q(file_path__icontains=q)
        )
        if extended_search:
            text_q |= Q(file_bundle_id__icontains=q)
        qs = qs.filter(text_q)
    if not d.get("show_covered"):
        covered = _covered_by_rule_q()
        if covered:
            qs = qs.exclude(covered)
    return qs


class EventListView(LoginRequiredMixin, ListView):
    model = Event
    template_name = "events/list.html"
    context_object_name = "events"
    paginate_by = 100

    def get_queryset(self):
        qs = Event.objects.all()
        self.filter_form = EventFilterForm(self.request.GET)
        qs = _apply_event_filters(qs, self.filter_form)
        sort = "-received_at"
        if self.filter_form.is_valid():
            requested_sort = self.filter_form.cleaned_data.get("sort") or ""
            if requested_sort in SORT_KEYS:
                sort = requested_sort
        return qs.order_by(sort)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        ctx["querystring"] = self.request.GET.urlencode()
        return ctx


_RULE_TYPE_TO_EVENT_FIELD = {
    "BINARY": "file_sha256",
    "SIGNINGID": "signing_id",
    "TEAMID": "team_id",
    "CDHASH": "cdhash",
}


def _covered_by_rule_q():
    """Build a Q matching events whose (rule_type, identifier, audience) is covered by an existing Rule.

    BINARY/SIGNINGID/TEAMID/CDHASH only — CERTIFICATE rules are skipped because the leaf cert
    SHA lives inside the event's JSON signing_chain and isn't queryable with a plain field lookup.
    """
    covered = Q()
    for r in Rule.objects.all().only(
        "rule_type",
        "identifier",
        "applies_to_middle_school",
        "applies_to_upper_school",
        "applies_to_teachers",
    ):
        field = _RULE_TYPE_TO_EVENT_FIELD.get(r.rule_type)
        if not field or not r.identifier:
            continue
        audiences = []
        if r.applies_to_middle_school:
            audiences.append(Audience.MIDDLE_SCHOOL)
        if r.applies_to_upper_school:
            audiences.append(Audience.UPPER_SCHOOL)
        if r.applies_to_teachers:
            audiences.append(Audience.TEACHER)
        if not audiences:
            continue
        covered |= Q(**{field: r.identifier, "audience__in": audiences})
    return covered


AGGREGATE_SORTS = {
    "-count": ("-count", "-last_seen"),
    "count": ("count", "-last_seen"),
    "file_name": ("file_name", "team_id"),
    "-file_name": ("-file_name", "team_id"),
    "team_id": ("team_id", "file_name"),
    "-team_id": ("-team_id", "file_name"),
    "-last_seen": ("-last_seen",),
    "last_seen": ("last_seen",),
}


@login_required
def event_aggregate(request):
    """Group events by app-identity and show counts."""
    qs = Event.objects.all()
    filter_form = EventFilterForm(request.GET)
    qs = _apply_event_filters(qs, filter_form, extended_search=True)

    sort_param = request.GET.get("sort") or "-count"
    if sort_param not in AGGREGATE_SORTS:
        sort_param = "-count"
    order_by = AGGREGATE_SORTS[sort_param]

    aggregated = (
        qs.values("team_id", "signing_id", "file_bundle_id", "file_name")
        .annotate(
            count=Count("id"),
            blocks=Count("id", filter=Q(decision__startswith="BLOCK_")),
            first_seen=Min("received_at"),
            last_seen=Max("received_at"),
        )
        .order_by(*order_by)
    )

    paginator = Paginator(aggregated, 100)
    page_obj = paginator.get_page(request.GET.get("page"))

    filters_only = request.GET.copy()
    filters_only.pop("sort", None)
    filters_only.pop("page", None)

    return render(
        request,
        "events/aggregate.html",
        {
            "filter_form": filter_form,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,
            "groups": page_obj.object_list,
            "querystring": request.GET.urlencode(),
            "filters_querystring": filters_only.urlencode(),
            "current_sort": sort_param,
            "total_events": qs.count(),
            "total_groups": paginator.count,
        },
    )


class EventDetailView(LoginRequiredMixin, DetailView):
    model = Event
    template_name = "events/detail.html"
    context_object_name = "event"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["raw_pretty"] = json.dumps(self.object.raw, indent=2, sort_keys=True, default=str)
        return ctx


class UnknownMachineListView(LoginRequiredMixin, ListView):
    model = UnknownMachine
    template_name = "unknown/list.html"
    context_object_name = "machines"
    paginate_by = 100
    queryset = UnknownMachine.objects.order_by("-last_seen")


MACHINE_LIST_SORTS = {
    "-last_seen": ("-last_seen",),
    "last_seen": ("last_seen",),
    "-first_seen": ("-first_seen",),
    "first_seen": ("first_seen",),
    "-sync_count": ("-sync_count", "-last_seen"),
    "sync_count": ("sync_count", "-last_seen"),
    "machine_id": ("machine_id",),
    "-machine_id": ("-machine_id",),
}

_ACTIVE_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@login_required
def machine_list(request):
    """Aggregate SyncSession rows by machine_id to show a roster of machines and last check-in."""
    qs = SyncSession.objects.all()

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(machine_id__icontains=q)
            | Q(machine_owner__icontains=q)
            | Q(primary_user__icontains=q)
            | Q(hostname__icontains=q)
            | Q(serial_num__icontains=q)
        )

    audience = (request.GET.get("audience") or "").strip()
    if audience in {c.value for c in Audience}:
        qs = qs.filter(audience=audience)

    active = (request.GET.get("active") or "").strip()
    if active in _ACTIVE_WINDOWS:
        qs = qs.filter(started_at__gte=timezone.now() - _ACTIVE_WINDOWS[active])

    sort = request.GET.get("sort") or "-last_seen"
    if sort not in MACHINE_LIST_SORTS:
        sort = "-last_seen"

    aggregated = (
        qs.values("machine_id")
        .annotate(
            sync_count=Count("id"),
            last_seen=Max("started_at"),
            first_seen=Min("started_at"),
            last_completed=Max("completed_at"),
        )
        .order_by(*MACHINE_LIST_SORTS[sort])
    )

    paginator = Paginator(aggregated, 100)
    page_obj = paginator.get_page(request.GET.get("page"))

    last_per_machine = {}
    for s in (
        SyncSession.objects.filter(machine_id__in=[r["machine_id"] for r in page_obj.object_list])
        .order_by("machine_id", "-started_at")
        .only(
            "machine_id", "audience", "sync_type", "client_mode", "started_at", "completed_at",
            "machine_owner", "primary_user", "hostname", "serial_num", "os_version", "santa_version",
        )
    ):
        last_per_machine.setdefault(s.machine_id, s)

    rows = []
    for r in page_obj.object_list:
        last = last_per_machine.get(r["machine_id"])
        rows.append({**r, "last": last})

    filters_only = request.GET.copy()
    filters_only.pop("sort", None)
    filters_only.pop("page", None)

    return render(
        request,
        "machines/list.html",
        {
            "rows": rows,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,
            "querystring": request.GET.urlencode(),
            "filters_querystring": filters_only.urlencode(),
            "current_sort": sort,
            "q": q,
            "selected_audience": audience,
            "selected_active": active,
            "audience_choices": Audience.choices,
            "active_choices": [("1h", "Last hour"), ("24h", "Last 24h"), ("7d", "Last 7 days"), ("30d", "Last 30 days")],
            "total_machines": paginator.count,
        },
    )


@login_required
def machine_detail(request, machine_id):
    """Recent sync history for a single machine, plus per-machine override form."""
    policy = MachinePolicy.objects.filter(machine_id=machine_id).first()

    if request.method == "POST":
        if request.POST.get("action") == "clear_override":
            if policy:
                policy.delete()
                messages.success(request, "Per-machine override cleared.")
            return redirect("machine-detail", machine_id=machine_id)
        form = MachinePolicyForm(request.POST, instance=policy)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.machine_id = machine_id
            obj.set_by = request.user if request.user.is_authenticated else None
            obj.save()
            messages.success(request, "Override saved.")
            return redirect("machine-detail", machine_id=machine_id)
    else:
        form = MachinePolicyForm(instance=policy)

    sessions = list(
        SyncSession.objects.filter(machine_id=machine_id).order_by("-started_at")[:50]
    )
    summary = None
    current = None
    if sessions:
        summary = SyncSession.objects.filter(machine_id=machine_id).aggregate(
            sync_count=Count("id"),
            first_seen=Min("started_at"),
            last_seen=Max("started_at"),
            last_completed=Max("completed_at"),
        )
        current = sessions[0]
    recent_events = list(
        Event.objects.filter(machine_id=machine_id).order_by("-received_at")[:25]
    )
    server_settings = ServerSettings.get()
    if server_settings.monitor_only:
        effective_mode = "MONITOR (monitor-only is ON)"
    elif policy and policy.client_mode:
        effective_mode = f"{policy.client_mode} (per-machine override)"
    else:
        effective_mode = f"{server_settings.default_client_mode} (global default)"

    return render(
        request,
        "machines/detail.html",
        {
            "machine_id": machine_id,
            "sessions": sessions,
            "summary": summary,
            "current": current,
            "recent_events": recent_events,
            "policy": policy,
            "policy_form": form,
            "effective_mode": effective_mode,
            "server_settings": server_settings,
        },
    )


_CSV_COLUMNS = (
    "received_at",
    "audience",
    "machine_id",
    "decision",
    "executing_user",
    "file_name",
    "file_path",
    "file_sha256",
    "file_bundle_id",
    "file_bundle_name",
    "file_bundle_version_string",
    "signing_id",
    "team_id",
    "cdhash",
    "signing_status",
    "static_rule",
    "execution_time",
    "parent_name",
    "pid",
    "ppid",
)


class _Echo:
    """File-like object that just returns the written value (for csv.writer streaming)."""

    def write(self, value):
        return value


@login_required
def event_export_csv(request):
    """Stream filtered events as CSV, respecting EventFilterForm + ordering query params."""
    qs = Event.objects.all()
    filter_form = EventFilterForm(request.GET)
    qs = _apply_event_filters(qs, filter_form)

    sort = "-received_at"
    if filter_form.is_valid():
        requested_sort = filter_form.cleaned_data.get("sort") or ""
        if requested_sort in SORT_KEYS:
            sort = requested_sort
    qs = qs.order_by(sort).values_list(*_CSV_COLUMNS).iterator(chunk_size=500)

    writer = csv.writer(_Echo())

    def row_iter():
        yield writer.writerow(_CSV_COLUMNS)
        for row in qs:
            yield writer.writerow(row)

    response = StreamingHttpResponse(row_iter(), content_type="text/csv")
    filename = f"elf-events-{timezone.now():%Y%m%d-%H%M%S}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import SORT_KEYS, EventFilterForm, RuleFilterForm, RuleForm, ServerSettingsForm
from .mobileconfig import AUDIENCE_LABELS, build_mobileconfig
from .models import (
    Audience,
    AuxiliaryEvent,
    Event,
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
    ctx = {
        "rule_count": Rule.objects.count(),
        "rules_teachers": Rule.objects.filter(applies_to_teachers=True).count(),
        "rules_students": Rule.objects.filter(applies_to_students=True).count(),
        "rules_both": Rule.objects.filter(
            applies_to_teachers=True, applies_to_students=True
        ).count(),
        "events_24h": Event.objects.filter(received_at__gte=last_day).count(),
        "blocks_24h": Event.objects.filter(
            received_at__gte=last_day, decision__startswith="BLOCK_"
        ).count(),
        "unknown_count": UnknownMachine.objects.count(),
        "in_flight_sessions": SyncSession.objects.filter(completed_at__isnull=True).count(),
        "server_settings": ServerSettings.get(),
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
            if aud == "students":
                qs = qs.filter(applies_to_students=True)
            elif aud == "teachers":
                qs = qs.filter(applies_to_teachers=True)
            elif aud == "both":
                qs = qs.filter(applies_to_students=True, applies_to_teachers=True)
        return qs.order_by("-updated_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        ctx["querystring"] = self.request.GET.urlencode()
        return ctx


class RuleCreateView(LoginRequiredMixin, CreateView):
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


class RuleUpdateView(LoginRequiredMixin, UpdateView):
    model = Rule
    form_class = RuleForm
    template_name = "rules/form.html"
    success_url = reverse_lazy("rule-list")


class RuleDeleteView(LoginRequiredMixin, DeleteView):
    model = Rule
    template_name = "rules/confirm_delete.html"
    success_url = reverse_lazy("rule-list")


class EventListView(LoginRequiredMixin, ListView):
    model = Event
    template_name = "events/list.html"
    context_object_name = "events"
    paginate_by = 100

    def get_queryset(self):
        qs = Event.objects.all()
        self.filter_form = EventFilterForm(self.request.GET or None)
        sort = "-received_at"
        if self.filter_form.is_valid():
            d = self.filter_form.cleaned_data
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
                qs = qs.filter(
                    Q(file_name__icontains=q)
                    | Q(file_sha256__icontains=q)
                    | Q(signing_id__icontains=q)
                    | Q(team_id__icontains=q)
                    | Q(cdhash__icontains=q)
                    | Q(file_path__icontains=q)
                )
            if d.get("hide_covered"):
                covered = _covered_by_rule_q()
                if covered:
                    qs = qs.exclude(covered)
            requested_sort = d.get("sort") or ""
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
        "rule_type", "identifier", "applies_to_students", "applies_to_teachers"
    ):
        field = _RULE_TYPE_TO_EVENT_FIELD.get(r.rule_type)
        if not field or not r.identifier:
            continue
        audiences = []
        if r.applies_to_students:
            audiences.append(Audience.STUDENT)
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
    filter_form = EventFilterForm(request.GET or None)
    if filter_form.is_valid():
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
            qs = qs.filter(
                Q(file_name__icontains=q)
                | Q(file_sha256__icontains=q)
                | Q(signing_id__icontains=q)
                | Q(team_id__icontains=q)
                | Q(cdhash__icontains=q)
                | Q(file_path__icontains=q)
                | Q(file_bundle_id__icontains=q)
            )
        if d.get("hide_covered"):
            covered = _covered_by_rule_q()
            if covered:
                qs = qs.exclude(covered)

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

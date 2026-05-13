import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import SORT_KEYS, EventFilterForm, RuleFilterForm, RuleForm, ServerSettingsForm
from .mobileconfig import AUDIENCE_LABELS, build_mobileconfig
from .models import Audience, AuxiliaryEvent, Event, Rule, ServerSettings, SyncSession, UnknownMachine


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
            requested_sort = d.get("sort") or ""
            if requested_sort in SORT_KEYS:
                sort = requested_sort
        return qs.order_by(sort)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        ctx["querystring"] = self.request.GET.urlencode()
        return ctx


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

from django import forms

from .models import Audience, MachinePolicy, Policy, Rule, RuleType, ServerSettings


class ServerSettingsForm(forms.ModelForm):
    class Meta:
        model = ServerSettings
        fields = [
            "default_client_mode",
            "monitor_only",
            "mobileconfig_client_mode",
            "organization",
            "payload_identifier_prefix",
            "allowed_path_regex",
            "event_detail_url",
            "event_detail_text",
            "unknown_block_message",
            "banned_block_message",
            "mode_notification_monitor",
            "mode_notification_lockdown",
        ]
        widgets = {
            "allowed_path_regex": forms.TextInput(attrs={"size": 60}),
            "event_detail_url": forms.TextInput(attrs={"size": 60}),
            "event_detail_text": forms.TextInput(attrs={"size": 40}),
            "unknown_block_message": forms.Textarea(attrs={"rows": 3, "cols": 60}),
            "banned_block_message": forms.Textarea(attrs={"rows": 3, "cols": 60}),
            "mode_notification_monitor": forms.Textarea(attrs={"rows": 2, "cols": 60}),
            "mode_notification_lockdown": forms.Textarea(attrs={"rows": 2, "cols": 60}),
        }

    # Section grouping for the settings template.
    SECTIONS = (
        ("Sync", ("default_client_mode", "monitor_only", "allowed_path_regex")),
        ("Profile (mobileconfig)", ("mobileconfig_client_mode", "organization", "payload_identifier_prefix")),
        (
            "Block dialog defaults",
            (
                "event_detail_url",
                "event_detail_text",
                "unknown_block_message",
                "banned_block_message",
                "mode_notification_monitor",
                "mode_notification_lockdown",
            ),
        ),
    )

    def sectioned_fields(self):
        for title, names in self.SECTIONS:
            yield title, [self[name] for name in names]


class MachinePolicyForm(forms.ModelForm):
    """Per-machine override. machine_id is set by the view, not the user."""

    class Meta:
        model = MachinePolicy
        fields = ["client_mode", "notes"]
        widgets = {
            "notes": forms.TextInput(attrs={"size": 60, "placeholder": "Why this override exists"}),
        }
        labels = {
            "client_mode": "Client mode override",
        }

    def clean_allowed_path_regex(self):
        import re

        value = self.cleaned_data.get("allowed_path_regex", "") or ""
        if value:
            try:
                re.compile(value)
            except re.error as e:
                raise forms.ValidationError(f"Invalid regex: {e}")
        return value


class RuleForm(forms.ModelForm):
    class Meta:
        model = Rule
        fields = [
            "identifier",
            "rule_type",
            "policy",
            "custom_msg",
            "custom_url",
            "notification_app_name",
            "cel_expr",
            "applies_to_middle_school",
            "applies_to_upper_school",
            "applies_to_teachers",
            "comment",
        ]
        widgets = {
            "cel_expr": forms.Textarea(attrs={"rows": 3}),
            "comment": forms.Textarea(attrs={"rows": 3}),
            "custom_msg": forms.TextInput(attrs={"size": 60}),
            "custom_url": forms.TextInput(attrs={"size": 60}),
            "identifier": forms.TextInput(attrs={"size": 60}),
        }

    # Visual sections for the form template. Each tuple: (section_title, [field_names]).
    SECTIONS = (
        ("Match", ("identifier", "rule_type", "policy")),
        ("Block dialog", ("custom_msg", "custom_url", "notification_app_name")),
        ("CEL expression", ("cel_expr",)),
        (
            "Audience",
            ("applies_to_middle_school", "applies_to_upper_school", "applies_to_teachers"),
        ),
        ("Notes", ("comment",)),
    )

    def sectioned_fields(self):
        """Yield (section_title, [BoundField, ...]) for template rendering."""
        for title, names in self.SECTIONS:
            yield title, [self[name] for name in names]

    def clean(self):
        cleaned = super().clean()
        if not (
            cleaned.get("applies_to_middle_school")
            or cleaned.get("applies_to_upper_school")
            or cleaned.get("applies_to_teachers")
        ):
            raise forms.ValidationError(
                "Rule must apply to at least one audience (Middle School, Upper School, or Teachers)."
            )
        if cleaned.get("policy") == Policy.CEL and not cleaned.get("cel_expr"):
            self.add_error("cel_expr", "Required when policy is CEL.")
        return cleaned


SORT_CHOICES = [
    ("-received_at", "Received (newest)"),
    ("received_at", "Received (oldest)"),
    ("decision", "Decision"),
    ("audience", "Audience"),
    ("machine_id", "Machine ID"),
    ("file_name", "File name"),
]
SORT_KEYS = {k for k, _ in SORT_CHOICES}


class EventFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    audience = forms.ChoiceField(
        required=False,
        choices=[("", "Any")] + list(Audience.choices),
    )
    decision = forms.CharField(required=False)
    only_blocks = forms.BooleanField(required=False, label="Only blocks")
    machine_id = forms.CharField(required=False)
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    show_covered = forms.BooleanField(
        required=False,
        label="Show events covered by a rule (off by default)",
    )
    sort = forms.ChoiceField(required=False, choices=[("", "")] + SORT_CHOICES)


RULE_AUDIENCE_CHOICES = [
    ("", "Any"),
    ("middle_school", "Middle School"),
    ("upper_school", "Upper School"),
    ("teachers", "Teachers"),
    ("any_student", "Any student (MS or US)"),
    ("all", "All three audiences"),
]


class RuleFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    rule_type = forms.ChoiceField(
        required=False, choices=[("", "Any")] + list(RuleType.choices)
    )
    policy = forms.ChoiceField(
        required=False, choices=[("", "Any")] + list(Policy.choices)
    )
    audience = forms.ChoiceField(required=False, choices=RULE_AUDIENCE_CHOICES)

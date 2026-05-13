from django import forms

from .models import Audience, Policy, Rule, RuleType, ServerSettings


class ServerSettingsForm(forms.ModelForm):
    class Meta:
        model = ServerSettings
        fields = [
            "monitor_only",
            "mobileconfig_client_mode",
            "organization",
            "payload_identifier_prefix",
            "allowed_path_regex",
        ]
        widgets = {
            "allowed_path_regex": forms.TextInput(attrs={"size": 60}),
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
            "applies_to_students",
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

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("applies_to_students") or cleaned.get("applies_to_teachers")):
            raise forms.ValidationError(
                "Rule must apply to at least one of students or teachers."
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


class RuleFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    rule_type = forms.ChoiceField(
        required=False, choices=[("", "Any")] + list(RuleType.choices)
    )
    policy = forms.ChoiceField(
        required=False, choices=[("", "Any")] + list(Policy.choices)
    )
    audience = forms.ChoiceField(
        required=False,
        choices=[("", "Any"), ("students", "Students"), ("teachers", "Teachers"), ("both", "Both")],
    )

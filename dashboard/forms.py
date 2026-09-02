"""Forms for the dashboard pages."""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from .models import AIModel


class SignupForm(forms.Form):
    """Multi-step account sign-up: name → email/password → birthdate → where-found-us.
    Email is the login id; birthdate + source are stored on the user's Profile."""

    SOURCE_CHOICES = [
        ("", "Pasirinkite…"),
        ("search", "Paieškos sistema"),
        ("friend", "Draugas ar kolega"),
        ("social", "Socialiniai tinklai"),
        ("ad", "Reklama"),
        ("other", "Kita"),
    ]

    # The autocomplete tokens are what make a browser's password manager recognise this as a sign-up
    # form: "username" + two "new-password" fields is the pattern Chrome needs before it will offer a
    # generated strong password (and save the credential afterwards).
    name = forms.CharField(label="Kaip mums į jus kreiptis?", max_length=150,
                           widget=forms.TextInput(attrs={"autocomplete": "name"}))
    email = forms.EmailField(label="El. paštas",
                             widget=forms.EmailInput(attrs={"autocomplete": "username"}))
    password1 = forms.CharField(label="Slaptažodis",
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Pakartokite slaptažodį",
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    birthdate = forms.DateField(label="Gimimo data",
                                widget=forms.DateInput(attrs={"type": "date", "autocomplete": "bday"}))
    source = forms.ChoiceField(label="Kur mus radote?", choices=SOURCE_CHOICES)

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        User = get_user_model()
        if User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Šis el. paštas jau užregistruotas.")
        return email

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Slaptažodžiai nesutampa.")
        elif p1:
            try:
                validate_password(p1)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self):
        data = self.cleaned_data
        User = get_user_model()
        user = User.objects.create_user(username=data["email"], email=data["email"], password=data["password1"])
        user.first_name = data["name"]
        user.save(update_fields=["first_name"])
        from .models import Profile

        Profile.objects.create(user=user, birthdate=data.get("birthdate"), source=data.get("source", ""))
        return user


class AIModelForm(forms.ModelForm):
    """Create or edit an AI model command/configuration."""

    class Meta:
        model = AIModel
        fields = [
            "name",
            "model_type",
            "execution_command",
            "is_active",
            "execution_order",
            "timeout_seconds",
            "max_tokens",
            "temperature",
            "api_config",
            "extra_config",
        ]
        widgets = {
            "execution_command": forms.Textarea(attrs={"rows": 4, "placeholder": "ollama run llama3"}),
            "api_config": forms.Textarea(attrs={"rows": 4}),
            "extra_config": forms.Textarea(attrs={"rows": 4}),
        }
        labels = {
            "name": "Pavadinimas",
            "model_type": "Modelio tipas",
            "execution_command": "Vykdymo komanda",
            "is_active": "Aktyvus",
            "execution_order": "Vykdymo eilė",
            "timeout_seconds": "Laiko limitas sekundėmis",
            "max_tokens": "Maksimalūs tokenai",
            "temperature": "Temperatūra",
            "api_config": "API konfigūracija",
            "extra_config": "Papildoma konfigūracija",
        }

    def clean_execution_command(self) -> str:
        command = self.cleaned_data["execution_command"].strip()
        model_type = self.cleaned_data.get("model_type")
        if model_type in {AIModel.ModelType.LOCAL, AIModel.ModelType.CUSTOM_LOCAL} and not command:
            raise forms.ValidationError("Lokaliems modeliams būtina vykdymo komanda.")
        return command

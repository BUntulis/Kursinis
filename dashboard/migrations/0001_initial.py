"""Pradinė dashboard modelių migracija."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Sukuria `Job` ir `JobLog` lenteles."""

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=80)),
                ("label", models.CharField(max_length=200)),
                ("command", models.JSONField(default=list)),
                ("params", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("queued", "Laukia"), ("running", "Vykdomas"), ("succeeded", "Pavyko"), ("failed", "Nepavyko"), ("cancelled", "Atšauktas")], default="queued", max_length=20)),
                ("pid", models.IntegerField(blank=True, null=True)),
                ("return_code", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="JobLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stream", models.CharField(default="stdout", max_length=20)),
                ("text", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="logs", to="dashboard.job")),
            ],
            options={"ordering": ["id"]},
        ),
    ]

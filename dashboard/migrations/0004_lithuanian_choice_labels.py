"""Update stored model field choices to Lithuanian labels."""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Choice labels only; stored values remain unchanged."""

    dependencies = [
        ("dashboard", "0003_lithuanian_platform"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aimodel",
            name="model_type",
            field=models.CharField(
                choices=[("local", "Lokalus"), ("api", "API"), ("custom_local", "Individualus lokalus")],
                default="local",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="benchmarkrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Laukia"),
                    ("running", "Vykdoma"),
                    ("succeeded", "Baigta"),
                    ("failed", "Nepavyko"),
                    ("cancelled", "Atšaukta"),
                ],
                default="queued",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="modelrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Laukia"),
                    ("running", "Vykdoma"),
                    ("succeeded", "Baigta"),
                    ("failed", "Nepavyko"),
                    ("cancelled", "Atšaukta"),
                ],
                default="queued",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="commandlog",
            name="kind",
            field=models.CharField(
                choices=[("model", "Generavimas"), ("test", "Testai"), ("preview", "Peržiūra"), ("system", "Sistema")],
                default="system",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="previewserver",
            name="status",
            field=models.CharField(
                choices=[("stopped", "Sustabdyta"), ("running", "Vykdoma"), ("failed", "Nepavyko")],
                default="stopped",
                max_length=20,
            ),
        ),
    ]

"""Add structured benchmark metadata fields."""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Stores summaries, test plans, and scoring rubrics with prompts/runs."""

    dependencies = [
        ("dashboard", "0004_lithuanian_choice_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="prompttemplate",
            name="expected_implementation_summary",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="prompttemplate",
            name="unit_test_plan",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="prompttemplate",
            name="scoring_criteria",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="benchmarktemplate",
            name="expected_implementation_summary",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="benchmarktemplate",
            name="unit_test_plan",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="benchmarktemplate",
            name="scoring_criteria",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="benchmarkrun",
            name="expected_implementation_summary",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="benchmarkrun",
            name="unit_test_plan",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="benchmarkrun",
            name="scoring_criteria",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

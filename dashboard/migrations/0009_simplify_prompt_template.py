"""Simplify PromptTemplate down to name, prompt, and connected test_suites.

Authored explicitly (rather than via the interactive makemigrations rename
prompt) so that existing rows keep their data: ``title`` is renamed to ``name``
instead of being dropped and recreated.
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0008_commandlog_agent_generatedfile_content_and_more"),
    ]

    operations = [
        # Drop the old ordering (it referenced sort_order) before that field is removed.
        migrations.AlterModelOptions(
            name="prompttemplate",
            options={},
        ),
        # Preserve data: title -> name, then widen and drop the unique constraint.
        migrations.RenameField(
            model_name="prompttemplate",
            old_name="title",
            new_name="name",
        ),
        migrations.AlterField(
            model_name="prompttemplate",
            name="name",
            field=models.CharField(max_length=200),
        ),
        migrations.RemoveField(model_name="prompttemplate", name="slug"),
        migrations.RemoveField(model_name="prompttemplate", name="description"),
        migrations.RemoveField(model_name="prompttemplate", name="category"),
        migrations.RemoveField(model_name="prompttemplate", name="expected_implementation_summary"),
        migrations.RemoveField(model_name="prompttemplate", name="unit_test_plan"),
        migrations.RemoveField(model_name="prompttemplate", name="scoring_criteria"),
        migrations.RemoveField(model_name="prompttemplate", name="is_active"),
        migrations.RemoveField(model_name="prompttemplate", name="sort_order"),
        migrations.RemoveField(model_name="prompttemplate", name="created_at"),
        migrations.RemoveField(model_name="prompttemplate", name="updated_at"),
        migrations.AddField(
            model_name="prompttemplate",
            name="test_suites",
            field=models.ManyToManyField(
                blank=True, related_name="prompt_templates", to="dashboard.testsuite"
            ),
        ),
    ]

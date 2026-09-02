"""Seed dashboard starters from YAML benchmark tasks."""
from __future__ import annotations

from pathlib import Path

import yaml
from django.db import migrations


def seed_yaml_benchmark_tasks(apps, schema_editor):
    """Create one prompt, suite, and runnable template per YAML task."""
    AIModel = apps.get_model("dashboard", "AIModel")
    PromptTemplate = apps.get_model("dashboard", "PromptTemplate")
    TestSuite = apps.get_model("dashboard", "TestSuite")
    BenchmarkTemplate = apps.get_model("dashboard", "BenchmarkTemplate")

    root = Path(__file__).resolve().parents[2]
    task_dir = root / "benchmark" / "tasks"
    active_models = list(AIModel.objects.filter(is_active=True).order_by("execution_order", "name")[:4])

    for index, path in enumerate(sorted(task_dir.glob("*.yaml")), start=1):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        task_id = data["id"]
        prompt, _ = PromptTemplate.objects.get_or_create(
            slug=task_id,
            defaults={
                "title": data["title"],
                "description": data.get("expected_implementation_summary", ""),
                "category": data.get("category", "django"),
                "prompt": data["description"],
                "expected_implementation_summary": data.get("expected_implementation_summary", ""),
                "unit_test_plan": data.get("unit_test_plan") or [],
                "scoring_criteria": data.get("scoring_criteria") or {},
                "is_active": True,
                "sort_order": index * 10,
            },
        )
        suite, _ = TestSuite.objects.get_or_create(
            slug=f"{task_id}-testai",
            defaults={
                "name": f"{data['title']} testai",
                "category": data.get("category", "django"),
                "description": f"Referenciniai pytest testai užduočiai {task_id}.",
                "test_content": data["reference_tests"],
                "default_test_directory": "tests",
                "is_active": True,
                "sort_order": index * 10,
            },
        )
        template, _ = BenchmarkTemplate.objects.get_or_create(
            slug=f"{task_id}-sablonas",
            defaults={
                "name": f"{task_id} šablonas",
                "description": f"Vykdymo šablonas užduočiai „{data['title']}“.",
                "prompt_template": prompt,
                "prompt": prompt.prompt,
                "expected_implementation_summary": data.get("expected_implementation_summary", ""),
                "unit_test_plan": data.get("unit_test_plan") or [],
                "scoring_criteria": data.get("scoring_criteria") or {},
                "test_directory": "tests",
                "is_active": True,
            },
        )
        template.selected_models.set(active_models)
        template.test_suites.set([suite])


class Migration(migrations.Migration):
    """Adds YAML-backed benchmark starter records for fresh installs."""

    dependencies = [
        ("dashboard", "0005_structured_benchmark_metadata"),
    ]

    operations = [
        migrations.RunPython(seed_yaml_benchmark_tasks, migrations.RunPython.noop),
    ]

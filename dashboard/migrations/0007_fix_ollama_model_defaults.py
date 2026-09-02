"""Fix dashboard model defaults to installed Ollama tags."""
from __future__ import annotations

from django.db import migrations


DEFAULT_MODELS = [
    ("Qwen2.5 Coder 7B Instruct", "local", True, 10, "ollama run qwen2.5-coder:7b-instruct"),
    ("DeepSeek Coder", "local", True, 20, "ollama run deepseek-coder:latest"),
    ("Llama 3.1 8B", "local", True, 30, "ollama run llama3.1:8b"),
    ("Qwen2.5 Coder 14B", "local", True, 40, "ollama run qwen2.5-coder:14b"),
    ("Local Custom Model", "custom_local", False, 50, '"{prompt_file}"'),
    ("GPT-5.5", "api", False, 60, ""),
    ("Claude", "api", False, 70, ""),
    ("Gemini", "api", False, 80, ""),
]

UNSAFE_DEFAULT_MODEL_NAMES = ["Llama 3", "Mistral", "DeepSeek", "Gemma"]


def fix_ollama_model_defaults(apps, schema_editor):
    """Create installed-model records and remove unsafe defaults from templates."""
    AIModel = apps.get_model("dashboard", "AIModel")
    BenchmarkTemplate = apps.get_model("dashboard", "BenchmarkTemplate")

    active_default_names = []
    for name, model_type, is_active, order, command in DEFAULT_MODELS:
        model, _ = AIModel.objects.get_or_create(
            name=name,
            defaults={
                "model_type": model_type,
                "execution_command": command,
                "is_active": is_active,
                "execution_order": order,
                "timeout_seconds": 300,
                "temperature": 0.2,
            },
        )
        model.model_type = model_type
        model.execution_command = command
        model.is_active = is_active
        model.execution_order = order
        model.save(update_fields=["model_type", "execution_command", "is_active", "execution_order", "updated_at"])
        if is_active:
            active_default_names.append(name)

    AIModel.objects.filter(name__in=UNSAFE_DEFAULT_MODEL_NAMES).update(is_active=False)
    default_models = list(
        AIModel.objects.filter(name__in=active_default_names, is_active=True).order_by("execution_order", "name")
    )
    for template in BenchmarkTemplate.objects.all():
        template.selected_models.set(default_models[:4])


class Migration(migrations.Migration):
    """Applies safer model defaults to existing dashboard databases."""

    dependencies = [
        ("dashboard", "0006_seed_yaml_benchmark_tasks"),
    ]

    operations = [
        migrations.RunPython(fix_ollama_model_defaults, migrations.RunPython.noop),
    ]

"""Starter data: the default local AI models the dashboard ships with."""
from __future__ import annotations

from django.utils.text import slugify

from dashboard.models import AIModel


MODELS = [
    ("Qwen2.5 Coder 7B Instruct", AIModel.ModelType.LOCAL, True, 10, "ollama run qwen2.5-coder:7b-instruct"),
    ("DeepSeek Coder", AIModel.ModelType.LOCAL, True, 20, "ollama run deepseek-coder:latest"),
    ("Llama 3.1 8B", AIModel.ModelType.LOCAL, True, 30, "ollama run llama3.1:8b"),
    ("Qwen2.5 Coder 14B", AIModel.ModelType.LOCAL, True, 40, "ollama run qwen2.5-coder:14b"),
    ("Local Custom Model", AIModel.ModelType.CUSTOM_LOCAL, False, 50, '"{prompt_file}"'),
    ("GPT-5.5", AIModel.ModelType.API, False, 60, ""),
    ("Claude", AIModel.ModelType.API, False, 70, ""),
    ("Gemini", AIModel.ModelType.API, False, 80, ""),
]

UNSAFE_DEFAULT_MODEL_NAMES = {"Llama 3", "Mistral", "DeepSeek", "Gemma"}


def seed_all(overwrite: bool = False) -> dict[str, int]:
    """Create the starter AI models."""
    counts = {"models": 0}
    for name, model_type, is_active, order, command in MODELS:
        obj, created = AIModel.objects.get_or_create(
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
        if overwrite and not created:
            obj.model_type = model_type
            obj.execution_command = command
            obj.is_active = is_active
            obj.execution_order = order
            obj.save(update_fields=["model_type", "execution_command", "is_active", "execution_order", "updated_at"])
        counts["models"] += int(created)

    AIModel.objects.filter(name__in=UNSAFE_DEFAULT_MODEL_NAMES).update(is_active=False)
    return counts


def unique_slug(model, value: str, field: str = "slug") -> str:
    """Return a unique slug for clone operations."""
    base = slugify(value) or "kopija"
    slug = base
    suffix = 2
    while model.objects.filter(**{field: slug}).exists():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug

"""Lithuanian platform schema, metrics, templates, and starter data."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def seed_starter_data(apps, schema_editor):
    AIModel = apps.get_model("dashboard", "AIModel")
    PromptTemplate = apps.get_model("dashboard", "PromptTemplate")
    TestSuite = apps.get_model("dashboard", "TestSuite")
    BenchmarkTemplate = apps.get_model("dashboard", "BenchmarkTemplate")

    models = [
        ("Llama 3", "local", True, 10, "ollama run llama3"),
        ("Mistral", "local", True, 20, "ollama run mistral"),
        ("DeepSeek", "local", True, 30, "ollama run deepseek-coder"),
        ("Gemma", "local", True, 40, "ollama run gemma"),
        ("Local Custom Model", "custom_local", True, 50, "ollama run {model_name}"),
        ("GPT-5.5", "api", False, 60, ""),
        ("Claude", "api", False, 70, ""),
        ("Gemini", "api", False, 80, ""),
    ]
    model_objects = []
    for name, model_type, active, order, command in models:
        model, _ = AIModel.objects.get_or_create(
            name=name,
            defaults={
                "model_type": model_type,
                "execution_command": command,
                "is_active": active,
                "execution_order": order,
                "timeout_seconds": 300,
                "temperature": 0.2,
            },
        )
        model_objects.append(model)

    prompts = [
        (
            "Django CRUD aplikacija",
            "django-crud-aplikacija",
            "CRUD",
            "Sugeneruoti pilną Django CRUD aplikaciją.",
            "Sukurk pilną Django CRUD aplikaciją su modeliu, formomis, URL, šablonais ir testais.",
        ),
        (
            "Django tinklaraštis",
            "django-tinklarastis",
            "Auth",
            "Sugeneruoti tinklaraščio sistemą su autentifikacija.",
            "Sukurk Django tinklaraščio sistemą su autentifikacija, įrašais, komentarais ir teisėmis.",
        ),
        (
            "Django e. prekyba",
            "django-e-prekyba",
            "E-commerce",
            "Sugeneruoti paprastą e. prekybos aplikaciją.",
            "Sukurk Django e. prekybos aplikaciją su produktais, krepšeliu, užsakymais ir paskyromis.",
        ),
        (
            "Django REST API",
            "django-rest-api",
            "API",
            "Sugeneruoti REST API su autentifikacija.",
            "Sukurk Django REST Framework API su autentifikacija, CRUD endpointais, serializatoriais ir teisėmis.",
        ),
        (
            "Django valdymo skydelis",
            "django-valdymo-skydelis",
            "Dashboard",
            "Sugeneruoti administravimo skydelį su diagramomis.",
            "Sukurk Django valdymo skydelį su kortelėmis, lentelėmis, diagramomis ir filtrais.",
        ),
    ]
    prompt_objects = []
    for index, (title, slug, category, description, prompt) in enumerate(prompts, start=1):
        obj, _ = PromptTemplate.objects.get_or_create(
            slug=slug,
            defaults={
                "title": title,
                "category": category,
                "description": description,
                "prompt": prompt,
                "is_active": True,
                "sort_order": index * 10,
            },
        )
        prompt_objects.append(obj)

    suites = [
        (
            "Bazinis Django patikrinimas",
            "bazinis-django-patikrinimas",
            "bazinis",
            "Tikrina, ar projektas turi bazinius Django failus.",
            "import pathlib\n\n\ndef test_manage_py_exists():\n    assert pathlib.Path('manage.py').exists()\n",
        ),
        (
            "CRUD validacija",
            "crud-validacija",
            "crud",
            "Tikrina CRUD struktūros požymius.",
            "import pathlib\n\n\ndef test_crud_keywords_exist():\n    text='\\n'.join(p.read_text(encoding='utf-8', errors='ignore') for p in pathlib.Path('.').rglob('*.py'))\n    assert any(w in text.lower() for w in ['create','update','delete','crud'])\n",
        ),
        (
            "Autentifikacijos validacija",
            "autentifikacijos-validacija",
            "auth",
            "Tikrina autentifikacijos požymius.",
            "import pathlib\n\n\ndef test_auth_keywords_exist():\n    text='\\n'.join(p.read_text(encoding='utf-8', errors='ignore') for p in pathlib.Path('.').rglob('*.py'))\n    assert any(w in text.lower() for w in ['login','logout','auth','permission'])\n",
        ),
        (
            "API validacija",
            "api-validacija",
            "api",
            "Tikrina API požymius.",
            "import pathlib\n\n\ndef test_api_keywords_exist():\n    text='\\n'.join(p.read_text(encoding='utf-8', errors='ignore') for p in pathlib.Path('.').rglob('*.py'))\n    assert any(w in text.lower() for w in ['serializer','api','viewset','rest_framework','status'])\n",
        ),
    ]
    suite_objects = []
    for index, (name, slug, category, description, content) in enumerate(suites, start=1):
        obj, _ = TestSuite.objects.get_or_create(
            slug=slug,
            defaults={
                "name": name,
                "category": category,
                "description": description,
                "test_content": content,
                "default_test_directory": "tests",
                "is_active": True,
                "sort_order": index * 10,
            },
        )
        suite_objects.append(obj)

    active_models = [model for model in model_objects if model.is_active][:4]
    for prompt in prompt_objects:
        template, _ = BenchmarkTemplate.objects.get_or_create(
            slug=f"{prompt.slug}-sablonas",
            defaults={
                "name": f"{prompt.title} šablonas",
                "description": f"Greitas vykdymo šablonas užklausai „{prompt.title}“.",
                "prompt_template": prompt,
                "prompt": prompt.prompt,
                "test_directory": "tests",
                "is_active": True,
            },
        )
        template.selected_models.set(active_models)
        template.test_suites.set(suite_objects)


class Migration(migrations.Migration):
    """Adds Lithuanian platform records and metrics."""

    dependencies = [
        ("dashboard", "0002_benchmarking_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="PromptTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=180, unique=True)),
                ("slug", models.SlugField(max_length=180, unique=True)),
                ("description", models.TextField(blank=True)),
                ("category", models.CharField(default="django", max_length=80)),
                ("prompt", models.TextField()),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["sort_order", "title"]},
        ),
        migrations.CreateModel(
            name="TestSuite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=180, unique=True)),
                ("slug", models.SlugField(max_length=180, unique=True)),
                ("category", models.CharField(default="bendras", max_length=80)),
                ("description", models.TextField(blank=True)),
                ("test_content", models.TextField()),
                ("default_command", models.TextField(blank=True)),
                ("default_test_directory", models.CharField(default="tests", max_length=240)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="BenchmarkTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=180, unique=True)),
                ("slug", models.SlugField(max_length=180, unique=True)),
                ("description", models.TextField(blank=True)),
                ("prompt", models.TextField()),
                ("test_command", models.TextField(blank=True)),
                ("test_directory", models.CharField(default="tests", max_length=240)),
                ("settings", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "prompt_template",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="benchmark_templates",
                        to="dashboard.prompttemplate",
                    ),
                ),
                ("selected_models", models.ManyToManyField(blank=True, related_name="benchmark_templates", to="dashboard.aimodel")),
                ("test_suites", models.ManyToManyField(blank=True, related_name="benchmark_templates", to="dashboard.testsuite")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="benchmarkrun",
            name="benchmark_template",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="benchmark_runs", to="dashboard.benchmarktemplate"),
        ),
        migrations.AddField(
            model_name="benchmarkrun",
            name="prompt_template",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="benchmark_runs", to="dashboard.prompttemplate"),
        ),
        migrations.AddField(model_name="benchmarkrun", name="test_suites", field=models.ManyToManyField(blank=True, related_name="benchmark_runs", to="dashboard.testsuite")),
        migrations.AddField(model_name="benchmarkrun", name="selected_models_snapshot", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="benchmarkrun", name="selected_test_suites_snapshot", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="benchmarkrun", name="total_files", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="total_directories", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="total_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="python_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="html_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="css_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="javascript_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="warning_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="error_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="benchmarkrun", name="report_summary", field=models.TextField(blank=True)),
        migrations.AddField(model_name="modelrun", name="waiting_seconds", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="generation_duration_seconds", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="testing_duration_seconds", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="preview_startup_duration_seconds", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="cpu_percent", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="ram_mb", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="peak_ram_mb", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="modelrun", name="disk_usage_bytes", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="directory_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="file_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="total_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="python_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="html_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="css_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="javascript_lines", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="warning_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="error_count", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="modelrun", name="ranking_score", field=models.FloatField(default=0.0)),
        migrations.AddField(model_name="commandlog", name="cpu_percent", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="commandlog", name="ram_mb", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="commandlog", name="peak_ram_mb", field=models.FloatField(blank=True, null=True)),
        migrations.CreateModel(
            name="TestSuiteResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("suite_name", models.CharField(max_length=180)),
                ("command", models.JSONField(blank=True, default=list)),
                ("stdout", models.TextField(blank=True)),
                ("stderr", models.TextField(blank=True)),
                ("exit_code", models.IntegerField(blank=True, null=True)),
                ("total", models.PositiveIntegerField(default=0)),
                ("passed", models.PositiveIntegerField(default=0)),
                ("failed", models.PositiveIntegerField(default=0)),
                ("skipped", models.PositiveIntegerField(default=0)),
                ("duration_seconds", models.FloatField(blank=True, null=True)),
                ("failure_details", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("model_run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="test_suite_results", to="dashboard.modelrun")),
                ("test_suite", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="results", to="dashboard.testsuite")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.RunPython(seed_starter_data, migrations.RunPython.noop),
    ]

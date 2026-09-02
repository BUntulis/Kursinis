"""Add benchmark domain models."""
from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Creates database-backed benchmark orchestration tables."""

    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIModel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=160, unique=True)),
                (
                    "model_type",
                    models.CharField(
                        choices=[("local", "Local"), ("api", "API"), ("custom_local", "Custom local")],
                        default="local",
                        max_length=30,
                    ),
                ),
                ("execution_command", models.TextField(blank=True)),
                ("api_config", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("execution_order", models.PositiveIntegerField(default=100)),
                ("timeout_seconds", models.PositiveIntegerField(default=300)),
                ("max_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("temperature", models.FloatField(default=0.2)),
                ("extra_config", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["execution_order", "name"]},
        ),
        migrations.CreateModel(
            name="BenchmarkRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(blank=True, max_length=180)),
                ("prompt", models.TextField()),
                ("reference_tests", models.TextField(blank=True)),
                ("test_command", models.TextField(blank=True)),
                ("test_directory", models.CharField(default="tests", max_length=240)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("workspace_root", models.CharField(blank=True, max_length=600)),
                ("total_tests", models.PositiveIntegerField(default=0)),
                ("passed_tests", models.PositiveIntegerField(default=0)),
                ("failed_tests", models.PositiveIntegerField(default=0)),
                ("skipped_tests", models.PositiveIntegerField(default=0)),
                ("success_rate", models.FloatField(default=0.0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="benchmark_runs",
                        to="dashboard.job",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ModelRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("model_name", models.CharField(max_length=160)),
                ("model_type", models.CharField(blank=True, max_length=30)),
                ("execution_order", models.PositiveIntegerField(default=100)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("workspace_path", models.CharField(blank=True, max_length=600)),
                ("command_snapshot", models.JSONField(blank=True, default=list)),
                ("duration_seconds", models.FloatField(blank=True, null=True)),
                ("return_code", models.IntegerField(blank=True, null=True)),
                ("error_summary", models.TextField(blank=True)),
                ("raw_response_path", models.CharField(blank=True, max_length=600)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "ai_model",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="model_runs",
                        to="dashboard.aimodel",
                    ),
                ),
                (
                    "benchmark",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="model_runs",
                        to="dashboard.benchmarkrun",
                    ),
                ),
            ],
            options={"ordering": ["execution_order", "id"]},
        ),
        migrations.CreateModel(
            name="CommandLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[("model", "Model"), ("test", "Test"), ("preview", "Preview"), ("system", "System")],
                        default="system",
                        max_length=20,
                    ),
                ),
                ("command", models.JSONField(blank=True, default=list)),
                ("cwd", models.CharField(blank=True, max_length=600)),
                ("stdout", models.TextField(blank=True)),
                ("stderr", models.TextField(blank=True)),
                ("exit_code", models.IntegerField(blank=True, null=True)),
                ("duration_seconds", models.FloatField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "model_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="command_logs",
                        to="dashboard.modelrun",
                    ),
                ),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="GeneratedFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("relative_path", models.CharField(max_length=600)),
                ("file_type", models.CharField(blank=True, max_length=60)),
                ("size_bytes", models.PositiveIntegerField(default=0)),
                ("sha256", models.CharField(blank=True, max_length=64)),
                ("modified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "model_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="generated_files",
                        to="dashboard.modelrun",
                    ),
                ),
            ],
            options={"ordering": ["relative_path"]},
        ),
        migrations.CreateModel(
            name="PreviewServer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[("stopped", "Stopped"), ("running", "Running"), ("failed", "Failed")],
                        default="stopped",
                        max_length=20,
                    ),
                ),
                ("port", models.PositiveIntegerField(blank=True, null=True)),
                ("url", models.URLField(blank=True)),
                ("pid", models.IntegerField(blank=True, null=True)),
                ("command", models.JSONField(blank=True, default=list)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("stopped_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "model_run",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="preview",
                        to="dashboard.modelrun",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TestResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
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
                (
                    "model_run",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="test_result",
                        to="dashboard.modelrun",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="generatedfile",
            constraint=models.UniqueConstraint(
                fields=("model_run", "relative_path"), name="unique_generated_file_per_run"
            ),
        ),
    ]

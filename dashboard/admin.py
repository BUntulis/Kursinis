"""Dashboard modelių registracija Django admin sąsajai."""
from __future__ import annotations

from django.contrib import admin

from .models import AIModel
from .models import Chat
from .models import CommandLog
from .models import GeneratedFile
from .models import Job
from .models import JobLog
from .models import ModelRun
from .models import PendingInteraction
from .models import PreviewServer
from .models import Project
from .models import TestSuiteResult
from .models import TestResult


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "origin", "is_loose", "created_at")
    list_filter = ("is_loose", "origin")
    search_fields = ("name", "slug")
    readonly_fields = ("sandbox_path", "created_at", "updated_at")

    @staticmethod
    def _wipe(path):
        if not path:
            return
        try:
            from dashboard.services.project_sandbox import remove_sandbox_dir

            remove_sandbox_dir(path)
        except Exception:
            pass

    def delete_model(self, request, obj):
        # Admin delete bypasses Project.delete(); remove the sandbox dir here too.
        path = obj.sandbox_path
        super().delete_model(request, obj)
        self._wipe(path)

    def delete_queryset(self, request, queryset):
        # The bulk "Delete selected" action also bypasses Project.delete().
        paths = list(queryset.values_list("sandbox_path", flat=True))
        super().delete_queryset(request, queryset)
        for path in paths:
            self._wipe(path)


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "title", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("title",)


@admin.register(PendingInteraction)
class PendingInteractionAdmin(admin.ModelAdmin):
    list_display = ("id", "model_run", "kind", "status", "created_at")
    list_filter = ("kind", "status")


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin):
    """Admin configuration for benchmark model definitions."""

    list_display = ("id", "name", "model_type", "is_active", "execution_order", "timeout_seconds")
    list_filter = ("model_type", "is_active")
    search_fields = ("name", "execution_command")


@admin.register(ModelRun)
class ModelRunAdmin(admin.ModelAdmin):
    """Admin configuration for individual model/chat-turn executions."""

    list_display = ("id", "chat", "model_name", "status", "return_code", "duration_seconds")
    list_filter = ("status", "model_type")
    search_fields = ("model_name", "workspace_path", "error_summary")


@admin.register(CommandLog)
class CommandLogAdmin(admin.ModelAdmin):
    """Admin configuration for command logs."""

    list_display = ("id", "model_run", "kind", "exit_code", "duration_seconds", "peak_ram_mb", "created_at")
    list_filter = ("kind", "exit_code")
    search_fields = ("stdout", "stderr", "cwd")


@admin.register(GeneratedFile)
class GeneratedFileAdmin(admin.ModelAdmin):
    """Admin configuration for generated files."""

    list_display = ("id", "model_run", "relative_path", "file_type", "size_bytes")
    list_filter = ("file_type",)
    search_fields = ("relative_path", "sha256")


@admin.register(TestResult)
class TestResultAdmin(admin.ModelAdmin):
    """Admin configuration for test results."""

    list_display = ("id", "model_run", "passed", "failed", "skipped", "total", "exit_code")
    list_filter = ("exit_code",)
    search_fields = ("stdout", "stderr")


@admin.register(TestSuiteResult)
class TestSuiteResultAdmin(admin.ModelAdmin):
    """Admin configuration for per-suite test results."""

    list_display = ("id", "model_run", "suite_name", "passed", "failed", "skipped", "total", "exit_code")
    list_filter = ("suite_name", "exit_code")
    search_fields = ("suite_name", "stdout", "stderr")


@admin.register(PreviewServer)
class PreviewServerAdmin(admin.ModelAdmin):
    """Admin configuration for preview servers."""

    list_display = ("id", "model_run", "status", "port", "pid", "url")
    list_filter = ("status",)
    search_fields = ("url", "error_message")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    """Admin konfigūracija `Job` modeliui."""

    list_display = ("id", "kind", "label", "status", "pid", "return_code", "created_at")
    list_filter = ("status", "kind")
    search_fields = ("label", "kind")


@admin.register(JobLog)
class JobLogAdmin(admin.ModelAdmin):
    """Admin konfigūracija `JobLog` modeliui."""

    list_display = ("id", "job", "stream", "created_at")
    list_filter = ("stream",)
    search_fields = ("text",)

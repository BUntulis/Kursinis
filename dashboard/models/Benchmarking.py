"""Benchmarking domain models for the dashboard app."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from .Job import Job


class RunStatus(models.TextChoices):
    """Shared status values for benchmark entities."""

    QUEUED = "queued", "Laukia"
    RUNNING = "running", "Vykdoma"
    SUCCEEDED = "succeeded", "Baigta"
    FAILED = "failed", "Nepavyko"
    CANCELLED = "cancelled", "Atšaukta"


class AIModel(models.Model):
    """Configurable AI model or local command used in benchmarks."""

    class ModelType(models.TextChoices):
        LOCAL = "local", "Lokalus"
        API = "api", "API"
        CUSTOM_LOCAL = "custom_local", "Individualus lokalus"

    name = models.CharField(max_length=160, unique=True)
    model_type = models.CharField(max_length=30, choices=ModelType.choices, default=ModelType.LOCAL)
    execution_command = models.TextField(blank=True)
    api_config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    execution_order = models.PositiveIntegerField(default=100)
    timeout_seconds = models.PositiveIntegerField(default=300)
    max_tokens = models.PositiveIntegerField(null=True, blank=True)
    temperature = models.FloatField(default=0.2)
    extra_config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["execution_order", "name"]

    @property
    def slug(self) -> str:
        value = slugify(self.name) or f"model-{self.pk or 'new'}"
        return value.replace("-", "_")

    def __str__(self) -> str:
        return self.name


class ModelRun(models.Model):
    """A project-chat "turn": one agent execution against a project sandbox."""

    chat = models.ForeignKey("Chat", null=True, blank=True, related_name="turns", on_delete=models.CASCADE)
    #: The user's message that triggered this turn (project-chat mode only).
    user_prompt = models.TextField(blank=True)
    ai_model = models.ForeignKey(AIModel, null=True, blank=True, related_name="model_runs", on_delete=models.SET_NULL)
    model_name = models.CharField(max_length=160)
    model_type = models.CharField(max_length=30, blank=True)
    execution_order = models.PositiveIntegerField(default=100)
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.QUEUED)
    workspace_path = models.CharField(max_length=600, blank=True)
    command_snapshot = models.JSONField(default=list, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    waiting_seconds = models.FloatField(null=True, blank=True)
    generation_duration_seconds = models.FloatField(null=True, blank=True)
    testing_duration_seconds = models.FloatField(null=True, blank=True)
    preview_startup_duration_seconds = models.FloatField(null=True, blank=True)
    cpu_percent = models.FloatField(null=True, blank=True)
    ram_mb = models.FloatField(null=True, blank=True)
    peak_ram_mb = models.FloatField(null=True, blank=True)
    disk_usage_bytes = models.PositiveIntegerField(default=0)
    directory_count = models.PositiveIntegerField(default=0)
    file_count = models.PositiveIntegerField(default=0)
    total_lines = models.PositiveIntegerField(default=0)
    python_lines = models.PositiveIntegerField(default=0)
    html_lines = models.PositiveIntegerField(default=0)
    css_lines = models.PositiveIntegerField(default=0)
    javascript_lines = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    ranking_score = models.FloatField(default=0.0)
    return_code = models.IntegerField(null=True, blank=True)
    error_summary = models.TextField(blank=True)
    raw_response_path = models.CharField(max_length=600, blank=True)
    # --- autonomous-mode resource telemetry (peaks/averages over the run) ---
    gpu_percent_avg = models.FloatField(null=True, blank=True)
    gpu_percent_peak = models.FloatField(null=True, blank=True)
    vram_used_mb_peak = models.FloatField(null=True, blank=True)
    vram_total_mb = models.FloatField(null=True, blank=True)
    gpu_temp_c_peak = models.FloatField(null=True, blank=True)
    cpu_temp_c_peak = models.FloatField(null=True, blank=True)
    disk_free_gb = models.FloatField(null=True, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    tokens_per_second = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["execution_order", "id"]

    def mark_running(self) -> None:
        """Guarded QUEUED→RUNNING transition: a Stop that landed first (status already CANCELLED)
        must not be overwritten back to RUNNING — that would orphan the turn as a zombie."""
        now = timezone.now()
        updated = type(self).objects.filter(id=self.id, status=RunStatus.QUEUED).update(
            status=RunStatus.RUNNING, started_at=now, updated_at=now,
        )
        if updated:
            self.status = RunStatus.RUNNING
            self.started_at = now
        else:
            self.refresh_from_db(fields=["status", "started_at"])

    def mark_finished(self, status: str, return_code: int | None = None, error_summary: str = "") -> None:
        self.status = status
        self.return_code = return_code
        self.error_summary = error_summary
        self.finished_at = timezone.now()
        if self.started_at:
            self.duration_seconds = (self.finished_at - self.started_at).total_seconds()
        self.save(
            update_fields=[
                "status",
                "return_code",
                "error_summary",
                "finished_at",
                "duration_seconds",
                "updated_at",
            ]
        )

    def __str__(self) -> str:
        return f"{self.model_name} (chat {self.chat_id})"


class CommandLog(models.Model):
    """A command executed inside a model workspace."""

    class Kind(models.TextChoices):
        MODEL = "model", "Generavimas"
        TEST = "test", "Testai"
        PREVIEW = "preview", "Peržiūra"
        SYSTEM = "system", "Sistema"

    model_run = models.ForeignKey(ModelRun, related_name="command_logs", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SYSTEM)
    #: Optional agent-node identity (planner / retriever / coder / executor / critic).
    #: The chat UI (app.js -> AGENT_BY_NAME) renders a dedicated bubble per node when set.
    agent = models.CharField(max_length=30, blank=True)
    command = models.JSONField(default=list, blank=True)
    cwd = models.CharField(max_length=600, blank=True)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    cpu_percent = models.FloatField(null=True, blank=True)
    ram_mb = models.FloatField(null=True, blank=True)
    peak_ram_mb = models.FloatField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    @property
    def display_stdout(self) -> str:
        from dashboard.services.commands import strip_terminal_control

        return strip_terminal_control(self.stdout)

    @property
    def display_stderr(self) -> str:
        from dashboard.services.commands import strip_terminal_control

        return strip_terminal_control(self.stderr)

    def __str__(self) -> str:
        return f"{self.kind} #{self.pk}"


class GeneratedFile(models.Model):
    """Indexed generated file inside a model workspace."""

    model_run = models.ForeignKey(ModelRun, related_name="generated_files", on_delete=models.CASCADE)
    relative_path = models.CharField(max_length=600)
    file_type = models.CharField(max_length=60, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    #: Full text of the most recent version of this file (the agent overwrites it
    #: each iteration). Empty for binary files; mirrors the on-disk workspace copy.
    content = models.TextField(blank=True)
    #: Agent iteration that last produced this file (1-based).
    iteration = models.PositiveIntegerField(default=1)
    modified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["relative_path"]
        constraints = [
            models.UniqueConstraint(fields=["model_run", "relative_path"], name="unique_generated_file_per_run")
        ]

    def __str__(self) -> str:
        return self.relative_path


class TestResult(models.Model):
    """Stored result of the predefined test command for a model run."""

    model_run = models.OneToOneField(ModelRun, related_name="test_result", on_delete=models.CASCADE)
    command = models.JSONField(default=list, blank=True)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    total = models.PositiveIntegerField(default=0)
    passed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    duration_seconds = models.FloatField(null=True, blank=True)
    failure_details = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.model_run_id}: {self.passed}/{self.total}"


class TestSuiteResult(models.Model):
    """Per-suite result for a model run."""

    model_run = models.ForeignKey(ModelRun, related_name="test_suite_results", on_delete=models.CASCADE)
    suite_name = models.CharField(max_length=180)
    command = models.JSONField(default=list, blank=True)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    total = models.PositiveIntegerField(default=0)
    passed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    duration_seconds = models.FloatField(null=True, blank=True)
    failure_details = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.suite_name}: {self.passed}/{self.total}"


class PreviewServer(models.Model):
    """Local preview process for a generated Django project."""

    class Status(models.TextChoices):
        STOPPED = "stopped", "Sustabdyta"
        RUNNING = "running", "Vykdoma"
        FAILED = "failed", "Nepavyko"

    # Either a benchmark model_run OR a project (project-chat preview is sandbox-scoped:
    # one runserver per project sandbox, shared across chat turns).
    model_run = models.OneToOneField(ModelRun, null=True, blank=True, related_name="preview", on_delete=models.CASCADE)
    project = models.OneToOneField("Project", null=True, blank=True, related_name="preview", on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.STOPPED)
    port = models.PositiveIntegerField(null=True, blank=True)
    url = models.URLField(blank=True)
    pid = models.IntegerField(null=True, blank=True)
    command = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.model_run_id}: {self.status}"


class ResourceSample(models.Model):
    """One periodic system-resource sample captured during a model run.

    Written by ``dashboard.services.resource_monitor.ResourceMonitor`` (a daemon
    thread) when autonomous monitoring is enabled. The live charts on the run page
    read these via ``model_run_resources_api``.
    """

    model_run = models.ForeignKey(ModelRun, related_name="resource_samples", on_delete=models.CASCADE)
    elapsed_seconds = models.FloatField(default=0.0)
    cpu_percent = models.FloatField(null=True, blank=True)
    ram_used_mb = models.FloatField(null=True, blank=True)
    ram_total_mb = models.FloatField(null=True, blank=True)
    gpu_percent = models.FloatField(null=True, blank=True)
    vram_used_mb = models.FloatField(null=True, blank=True)
    vram_total_mb = models.FloatField(null=True, blank=True)
    gpu_temp_c = models.FloatField(null=True, blank=True)
    cpu_temp_c = models.FloatField(null=True, blank=True)
    disk_free_gb = models.FloatField(null=True, blank=True)
    tokens_per_second = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"sample #{self.pk} run={self.model_run_id} t={self.elapsed_seconds:.1f}s"


class Project(models.Model):
    """A user project with a persistent sandbox directory + a chat history.

    The sandbox (``sandbox_path``) is where the agent reads/writes/runs the Django
    project; ``ensure_runnable_project`` / ``db_inspector`` / ``PreviewService`` all
    operate on it. Deleting a project removes its sandbox dir from disk.
    """

    class Origin(models.TextChoices):
        NEW = "new", "Naujas"
        EXISTING = "existing", "Esamas"

    name = models.CharField(max_length=180)
    slug = models.SlugField(max_length=190, unique=True)
    #: Absolute path to the project's sandbox directory (under BASE_DIR/project_sandboxes/).
    sandbox_path = models.CharField(max_length=600, blank=True)
    origin = models.CharField(max_length=20, choices=Origin.choices, default=Origin.NEW)
    #: A "loose" project backs a standalone chat ("New chat") — it owns a sandbox so the chat can
    #: build fully, but it is hidden from the Projektai grid (it's not a real, named project). A loose
    #: project holds exactly one work chat; moving that chat into a real project removes the loose one.
    is_loose = models.BooleanField(default=False)
    #: Project-level defaults (model / effort / thinking / pursue_goal / auto_approve).
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        """Delete the project (cascading chats/turns/logs) then wipe its sandbox dir."""
        path = self.sandbox_path
        super().delete(*args, **kwargs)
        if path:
            try:
                from dashboard.services.project_sandbox import remove_sandbox_dir

                remove_sandbox_dir(path)
            except Exception:
                pass


class Chat(models.Model):
    """A multi-turn conversation inside a project. Each turn is a ``ModelRun``."""

    project = models.ForeignKey(Project, related_name="chats", on_delete=models.CASCADE)
    title = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.QUEUED)
    #: Per-chat settings: model, effort (low|medium|high), thinking, pursue_goal, auto_approve.
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title or f"Chat #{self.pk}"


class PendingInteraction(models.Model):
    """A question or approval the agent is waiting on the user to answer.

    The agent thread creates one of these (status=pending) and blocks until the user
    responds via the chat UI, which flips it to answered/denied and records the answer.
    """

    class Kind(models.TextChoices):
        QUESTION = "question", "Klausimas"
        APPROVAL = "approval", "Patvirtinimas"

    class Status(models.TextChoices):
        PENDING = "pending", "Laukia"
        ANSWERED = "answered", "Atsakyta"
        DENIED = "denied", "Atmesta"

    model_run = models.ForeignKey(ModelRun, related_name="interactions", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.QUESTION)
    #: Question text or the approval rationale.
    prompt = models.TextField(blank=True)
    #: For questions: ``[{"key": "A", "label": "..."}, ...]`` (the UI also offers "Other").
    options = models.JSONField(default=list, blank=True)
    #: For approvals: the tool + target/command the agent wants to run.
    command = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    #: The user's answer — an option key, free text, or "approved"/"denied".
    answer = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.kind} #{self.pk} ({self.status})"


class Profile(models.Model):
    """Extra sign-up details collected by the multi-step wizard (birthdate, how they found us)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    birthdate = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Profile<{self.user_id}>"

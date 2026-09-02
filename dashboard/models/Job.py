"""Ilgai trunkančio darbo modelis."""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class Job(models.Model):
    """
    Kam skirtas:
    Saugoti Web arba CLI paleisto proceso būseną.

    Tikslas:
    Leisti vartotojui matyti pipeline veiksmų eigą, rezultatą ir komandą.

    Argumentai:
    Django ORM laukus pildo per modelio konstruktorių arba `objects.create()`.

    Grąžinama:
    `Job` modelio įrašas duomenų bazėje.

    Panaudojimo pavyzdžiai:
    ```python
    job = Job.objects.create(kind="run_agent", label="Agent 001")
    ```
    """

    class Status(models.TextChoices):
        """Leistinos darbo būsenos."""

        QUEUED = "queued", "Laukia"
        RUNNING = "running", "Vykdomas"
        SUCCEEDED = "succeeded", "Pavyko"
        FAILED = "failed", "Nepavyko"
        CANCELLED = "cancelled", "Atšauktas"

    #: Darbo tipas, pvz. `run_agent`, `pytest` arba `finetune`.
    kind = models.CharField(max_length=80)

    #: Vartotojui rodoma darbo antraštė.
    label = models.CharField(max_length=200)

    #: Pilna vykdoma komanda kaip JSON masyvas.
    command = models.JSONField(default=list)

    #: Papildomi paleidimo argumentai arba metaduomenys.
    params = models.JSONField(default=dict, blank=True)

    #: Esama darbo būsena.
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)

    #: Subprocess proceso ID, jei darbas jau paleistas.
    pid = models.IntegerField(null=True, blank=True)

    #: Proceso grįžimo kodas, kai darbas baigtas.
    return_code = models.IntegerField(null=True, blank=True)

    #: Sukūrimo data.
    created_at = models.DateTimeField(auto_now_add=True)

    #: Paskutinio atnaujinimo data.
    updated_at = models.DateTimeField(auto_now=True)

    #: Vykdymo pradžios data.
    started_at = models.DateTimeField(null=True, blank=True)

    #: Vykdymo pabaigos data.
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Django ORM metaduomenys."""

        ordering = ["-created_at"]

    def mark_running(self, pid: int) -> None:
        """
        Kam skirtas:
        Pažymėti darbą kaip vykdomą.

        Tikslas:
        Vienoje vietoje atnaujinti statusą, PID ir pradžios laiką.

        Argumentai:
        `pid` yra subprocess proceso ID.

        Grąžinama:
        `None`.

        Panaudojimo pavyzdžiai:
        ```python
        job.mark_running(proc.pid)
        ```
        """
        self.status = self.Status.RUNNING
        self.pid = pid
        self.started_at = timezone.now()
        self.save(update_fields=["status", "pid", "started_at", "updated_at"])

    def mark_finished(self, return_code: int) -> None:
        """
        Kam skirtas:
        Pažymėti darbą kaip baigtą.

        Tikslas:
        Iš return code išvesti galutinę būseną ir pabaigos laiką.

        Argumentai:
        `return_code` yra subprocess grįžimo kodas.

        Grąžinama:
        `None`.

        Panaudojimo pavyzdžiai:
        ```python
        job.mark_finished(0)
        ```
        """
        self.return_code = return_code
        self.status = self.Status.SUCCEEDED if return_code == 0 else self.Status.FAILED
        self.finished_at = timezone.now()
        self.save(update_fields=["return_code", "status", "finished_at", "updated_at"])

    def __str__(self) -> str:
        """Grąžina žmogui suprantamą darbo pavadinimą."""
        return f"{self.label} ({self.status})"

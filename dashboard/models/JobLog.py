"""Darbo logų modelis."""
from __future__ import annotations

from django.db import models

from .Job import Job


class JobLog(models.Model):
    """
    Kam skirtas:
    Saugoti vieną proceso logų eilutę.

    Tikslas:
    Leisti React sąsajai gyvai pollinti proceso išvestį.

    Argumentai:
    Django ORM laukus pildo per `objects.create()`.

    Grąžinama:
    `JobLog` modelio įrašas duomenų bazėje.

    Panaudojimo pavyzdžiai:
    ```python
    JobLog.objects.create(job=job, stream="stdout", text="Pradedama")
    ```
    """

    #: Darbas, kuriam priklauso logų eilutė.
    job = models.ForeignKey(Job, related_name="logs", on_delete=models.CASCADE)

    #: Logų srautas: `stdout`, `stderr` arba `system`.
    stream = models.CharField(max_length=20, default="stdout")

    #: Eilutės tekstas.
    text = models.TextField()

    #: Sukūrimo data.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Django ORM metaduomenys."""

        ordering = ["id"]

    def __str__(self) -> str:
        """Grąžina trumpą logų eilutės reprezentaciją."""
        return f"{self.job_id}:{self.stream}:{self.text[:40]}"

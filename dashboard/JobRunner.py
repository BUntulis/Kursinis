"""Subprocess darbų paleidimo servisas."""
from __future__ import annotations

import os
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any

from django.db import close_old_connections
from django.utils import timezone

from .JobCommandBuilder import JobCommandBuilder
from .models import Job, JobLog


class JobRunner:
    """
    Kam skirtas:
    Paleisti ilgai trunkančius projekto veiksmus fone ir saugoti jų logus DB.

    Tikslas:
    Web sąsajai suteikti gyvą procesų eigą be papildomo queue serverio.

    Argumentai:
    Konstruktorius priima projekto šaknį ir komandų konstruktorių.

    Grąžinama:
    `JobRunner` instancija.

    Panaudojimo pavyzdžiai:
    ```python
    job = JobRunner().start("run_agent", {"task": "001_blog_model"})
    ```
    """

    #: Projekto šaknis.
    root: Path

    #: Komandų konstruktorius.
    command_builder: JobCommandBuilder

    def __init__(
        self,
        root: Path | None = None,
        command_builder: JobCommandBuilder | None = None,
    ) -> None:
        """Inicializuoja fono darbų runner'į."""
        self.root = root or Path(__file__).resolve().parent.parent
        self.command_builder = command_builder or JobCommandBuilder(self.root)

    def start(self, action: str, payload: dict[str, Any]) -> Job:
        """
        Kam skirtas:
        Sukurti DB įrašą ir pradėti veiksmą fone.

        Tikslas:
        Leisti Web API iškart grąžinti `job_id`, kol procesas tęsiasi atskirame thread'e.

        Argumentai:
        `action` yra veiksmų vardas, o `payload` yra jo parametrai.

        Grąžinama:
        Sukurtą `Job` objektą.

        Panaudojimo pavyzdžiai:
        ```python
        job = runner.start("pytest", {"target": "tests"})
        ```
        """
        label, command, params = self.command_builder.build(action, payload)
        job = Job.objects.create(kind=action, label=label, command=command, params=params)
        JobLog.objects.create(job=job, stream="system", text=f"$ {' '.join(command)}")
        thread = threading.Thread(target=self._run_job, args=(job.id,), daemon=True)
        thread.start()
        return job

    def cancel(self, job: Job) -> Job:
        """
        Kam skirtas:
        Atšaukti vykdomą darbą pagal jo PID.

        Tikslas:
        Suteikti Web sąsajai ir CLI kontrolę ilgai trunkantiems procesams.

        Argumentai:
        `job` yra atšaukiamas DB įrašas.

        Grąžinama:
        Atnaujintą `Job` objektą.

        Panaudojimo pavyzdžiai:
        ```python
        runner.cancel(job)
        ```
        """
        if job.pid and job.status == Job.Status.RUNNING:
            if platform.system().lower().startswith("win"):
                subprocess.run(["taskkill", "/PID", str(job.pid), "/T", "/F"], check=False)
            else:
                os.kill(job.pid, 15)
            JobLog.objects.create(job=job, stream="system", text="Darbas atšauktas vartotojo.")
        job.status = Job.Status.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at", "updated_at"])
        return job

    def _run_job(self, job_id: int) -> None:
        """
        Kam skirtas:
        Vykdyti konkretų DB darbą atskirame thread'e.

        Tikslas:
        Stream'inti subprocess išvestį į `JobLog` įrašus.

        Argumentai:
        `job_id` yra vykdomo darbo ID.

        Grąžinama:
        `None`.

        Panaudojimo pavyzdžiai:
        ```python
        runner._run_job(job.id)
        ```
        """
        close_old_connections()
        job = Job.objects.get(id=job_id)
        try:
            process = subprocess.Popen(
                job.command,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            job.mark_running(process.pid)
            assert process.stdout is not None
            for line in process.stdout:
                JobLog.objects.create(job=job, stream="stdout", text=line.rstrip("\n"))
            return_code = process.wait()
            job.mark_finished(return_code)
            JobLog.objects.create(job=job, stream="system", text=f"Procesas baigtas: {return_code}")
        except Exception as exc:
            JobLog.objects.create(job=job, stream="system", text=f"Klaida: {exc}")
            job.status = Job.Status.FAILED
            job.return_code = -1
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "return_code", "finished_at", "updated_at"])
        finally:
            close_old_connections()


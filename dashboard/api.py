"""Dashboard JSON API view funkcijos."""
from __future__ import annotations

import json
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .JobRunner import JobRunner
from .ResultsStore import ResultsStore
from .TaskStore import TaskStore
from .models import Job


def index(request: HttpRequest):
    """Grąžina React SPA HTML puslapį."""
    return render(request, "dashboard/index.html")


@require_http_methods(["GET"])
def overview_api(request: HttpRequest) -> JsonResponse:
    """Grąžina pagrindinę dashboard suvestinę."""
    tasks = TaskStore().list()
    runs = ResultsStore().list_runs()
    jobs = [serialize_job(job) for job in Job.objects.all()[:8]]
    return JsonResponse(
        {
            "tasks_total": len(tasks),
            "runs_total": len(runs),
            "jobs": jobs,
            "latest_runs": runs[-5:],
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def tasks_api(request: HttpRequest) -> JsonResponse:
    """Valdo benchmark užduočių kolekciją."""
    store = TaskStore()
    if request.method == "GET":
        return JsonResponse({"tasks": store.list()})
    try:
        task = store.save(_json_body(request))
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"task": task}, status=201)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
def task_detail_api(request: HttpRequest, task_id: str) -> JsonResponse:
    """Valdo vieną benchmark užduotį."""
    store = TaskStore()
    try:
        if request.method == "GET":
            return JsonResponse({"task": store.get(task_id)})
        if request.method == "DELETE":
            store.delete(task_id)
            return JsonResponse({"deleted": task_id})
        task = store.save(_json_body(request), original_id=task_id)
        return JsonResponse({"task": task})
    except FileNotFoundError as exc:
        return JsonResponse({"error": str(exc)}, status=404)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@require_http_methods(["GET"])
def results_api(request: HttpRequest) -> JsonResponse:
    """Grąžina benchmark rezultatų ir ataskaitų duomenis."""
    store = ResultsStore()
    return JsonResponse({"runs": store.list_runs(), "reports": store.reports()})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def jobs_api(request: HttpRequest) -> JsonResponse:
    """Grąžina darbus arba paleidžia naują fono darbą."""
    if request.method == "GET":
        limit = int(request.GET.get("limit", "25"))
        return JsonResponse({"jobs": [serialize_job(job) for job in Job.objects.all()[:limit]]})

    body = _json_body(request)
    action = body.get("action")
    payload = body.get("payload", {})
    if not action:
        return JsonResponse({"error": "Trūksta `action`."}, status=400)
    try:
        job = JobRunner().start(action, payload)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"job": serialize_job(job, include_logs=True)}, status=201)


@require_http_methods(["GET"])
def job_detail_api(request: HttpRequest, job_id: int) -> JsonResponse:
    """Grąžina vieno darbo būseną ir logus."""
    job = get_object_or_404(Job, id=job_id)
    after = request.GET.get("after")
    return JsonResponse({"job": serialize_job(job, include_logs=True, after_id=int(after) if after else None)})


@csrf_exempt
@require_http_methods(["POST"])
def job_cancel_api(request: HttpRequest, job_id: int) -> JsonResponse:
    """Atšaukia vykdomą darbą."""
    job = get_object_or_404(Job, id=job_id)
    job = JobRunner().cancel(job)
    return JsonResponse({"job": serialize_job(job, include_logs=True)})


def serialize_job(job: Job, include_logs: bool = False, after_id: int | None = None) -> dict[str, Any]:
    """
    Kam skirtas:
    Paversti `Job` modelį JSON draugišku žodynu.

    Tikslas:
    API atsakymuose turėti vienodą darbo reprezentaciją.

    Argumentai:
    `job` yra modelio įrašas, `include_logs` nurodo ar įtraukti logus, o `after_id` filtruoja tik naujesnes eilutes.

    Grąžinama:
    JSON serializuojamą žodyną.

    Panaudojimo pavyzdžiai:
    ```python
    data = serialize_job(job, include_logs=True)
    ```
    """
    data: dict[str, Any] = {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "command": job.command,
        "params": job.params,
        "status": job.status,
        "pid": job.pid,
        "return_code": job.return_code,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if include_logs:
        logs = job.logs.all()
        if after_id is not None:
            logs = logs.filter(id__gt=after_id)
        data["logs"] = [
            {
                "id": log.id,
                "stream": log.stream,
                "text": log.text,
                "created_at": log.created_at.isoformat(),
            }
            for log in logs[:1000]
        ]
    return data


def _json_body(request: HttpRequest) -> dict[str, Any]:
    """Saugiai perskaito JSON request body."""
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))

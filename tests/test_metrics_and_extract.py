"""Sanity testai pass@k ir code extraction'ui — neužkliuvo nuo jokių LLM/ChromaDB."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent.extract import extract_files  # noqa: E402
from src.eval.metrics import aggregate_pass_at_k, pass_at_k  # noqa: E402
from src.eval.metrics import TaskResult  # noqa: E402


def test_pass_at_k_all_passed():
    assert pass_at_k(n=5, c=5, k=1) == 1.0


def test_pass_at_k_none_passed():
    assert pass_at_k(n=5, c=0, k=1) == 0.0


def test_pass_at_k_partial():
    # n=2, c=1, k=1 → 1 - C(1,1)/C(2,1) = 1 - 1/2 = 0.5
    assert abs(pass_at_k(2, 1, 1) - 0.5) < 1e-9


def test_aggregate():
    results = [
        TaskResult("a", attempts=1, successes=1),
        TaskResult("b", attempts=1, successes=0),
    ]
    assert aggregate_pass_at_k(results, k=1) == 0.5


def test_extract_files_with_path_in_block():
    text = """\
Štai kodas:

```python
# blog/models.py
from django.db import models

class Post(models.Model):
    title = models.CharField(max_length=200)
```

Ir testai:

```python
# blog/tests.py
def test_x():
    assert True
```
"""
    files = extract_files(text)
    assert "blog/models.py" in files
    assert "blog/tests.py" in files
    assert "class Post" in files["blog/models.py"]
    assert "def test_x" in files["blog/tests.py"]


def test_extract_files_with_file_header():
    text = """\
```
# File: app/views.py
def index(request):
    pass
```
"""
    files = extract_files(text)
    assert "app/views.py" in files
    assert "def index" in files["app/views.py"]

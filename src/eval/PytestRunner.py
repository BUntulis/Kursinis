"""Pytest paleidimo klasė."""
from __future__ import annotations

import re
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Pattern

from .SandboxResult import SandboxResult


class PytestRunner:
    """
    **Kam skirtas:** Paleidžia pytest procesą ir parenka jo suvestinę į struktūruotą rezultatą.

    **Tikslas:** Atskirti subprocess vykdymo ir pytest išvesties analizės logiką nuo likusio sandbox kodo.

    **Argumentai:** Konstruktorius priima vykdymo timeout'ą sekundėmis.

    **Grąžinama:** `PytestRunner` instanciją, galinčią vykdyti `run()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    runner = PytestRunner(timeout_sec=60)
    result = runner.run(Path("tmp/project"))
    ```
    """

    #: **Kam skirtas:** Saugo regex'ą praeitų testų skaičiui aptikti.
    #: **Tikslas:** Iš pytest suvestinės ištraukti `passed` reikšmę.
    SUMMARY_PASS_RE: ClassVar[Pattern[str]] = re.compile(r"(\d+)\s+passed")

    #: **Kam skirtas:** Saugo regex'ą nepraeitų testų skaičiui aptikti.
    #: **Tikslas:** Iš pytest suvestinės ištraukti `failed` reikšmę.
    SUMMARY_FAIL_RE: ClassVar[Pattern[str]] = re.compile(r"(\d+)\s+failed")

    #: **Kam skirtas:** Saugo regex'ą klaidų skaičiui aptikti.
    #: **Tikslas:** Iš pytest suvestinės ištraukti `error` reikšmę.
    SUMMARY_ERR_RE: ClassVar[Pattern[str]] = re.compile(r"(\d+)\s+error")

    #: **Kam skirtas:** Saugo vieno vykdymo timeout'ą sekundėmis.
    #: **Tikslas:** Apsaugoti sandbox paleidimus nuo užstrigusių testų.
    timeout_sec: int

    def __init__(self, timeout_sec: int) -> None:
        """
        **Kam skirtas:** Inicializuoja pytest runner'į su timeout'u.

        **Tikslas:** Nustatyti, kiek daugiausia laiko galima skirti vienam testų paleidimui.

        **Argumentai:** `timeout_sec` yra maksimalus vykdymo laikas sekundėmis.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        runner = PytestRunner(timeout_sec=30)
        ```
        """
        self.timeout_sec = timeout_sec

    def run(self, cwd: Path) -> SandboxResult:
        """
        **Kam skirtas:** Paleidžia pytest nurodytame kataloge.

        **Tikslas:** Grąžinti standartizuotą `SandboxResult` objektą su visais svarbiausiais vykdymo laukais.

        **Argumentai:** `cwd` yra laikino Django projekto katalogas.

        **Grąžinama:** `SandboxResult` objektą su testų baigtimi ir logais.

        **Panaudojimo pavyzdžiai:**
        ```python
        result = runner.run(Path("tmp/project"))
        ```
        """
        env = os.environ.copy()
        # The sandbox project ships its own ``pytest.ini`` with
        # ``DJANGO_SETTINGS_MODULE = settings``. If this runner is invoked from
        # inside a configured Django process (e.g. the dashboard orchestrator),
        # the inherited ``DJANGO_SETTINGS_MODULE=webapp.settings`` would override
        # the ini and load the real project settings, which do not contain the
        # generated app -> "Model ... doesn't declare an explicit app_label".
        # Drop the inherited settings/addopts so the scaffolded project stands alone.
        env.pop("DJANGO_SETTINGS_MODULE", None)
        env.pop("PYTEST_ADDOPTS", None)
        root = Path(__file__).resolve().parents[2]
        local_site_packages = root / "Lib" / "site-packages"
        pythonpath_parts = [str(root)]
        if local_site_packages.exists():
            pythonpath_parts.append(str(local_site_packages))
        if env.get("PYTHONPATH"):
            pythonpath_parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

        # Run ONLY the benchmark's reference tests when present. The model frequently writes
        # its own tests.py / test_*.py (often broken — e.g. a root ``tests.py`` doing a
        # relative import) which would otherwise abort collection of the whole session and
        # score correct code 0/0. Targeting the reference file explicitly ignores stray tests.
        pytest_args = [sys.executable, "-m", "pytest", "-v", "--tb=short"]
        if (Path(cwd) / "test_reference.py").exists():
            pytest_args.append("test_reference.py")

        try:
            proc = subprocess.run(
                pytest_args,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                passed=False,
                returncode=-1,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\n[TIMEOUT]",
            )

        passed, failed, errors = self._parse_summary(proc.stdout)
        return SandboxResult(
            passed=(proc.returncode == 0 and passed > 0),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            tests_run=passed + failed + errors,
            tests_passed=passed,
            tests_failed=failed,
            tests_errors=errors,
        )

    @classmethod
    def _parse_summary(cls, stdout: str) -> tuple[int, int, int]:
        """
        **Kam skirtas:** Ištraukia pytest suvestinės skaičius iš `stdout`.

        **Tikslas:** Paversti tekstinę pytest išvestį į skaitines testų metrikas.

        **Argumentai:** `stdout` yra visas pytest standartinės išvesties tekstas.

        **Grąžinama:** `tuple[int, int, int]` formatu `(passed, failed, errors)`.

        **Panaudojimo pavyzdžiai:**
        ```python
        passed, failed, errors = PytestRunner._parse_summary(stdout)
        ```
        """
        passed = failed = errors = 0
        for line in reversed(stdout.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            match_pass = cls.SUMMARY_PASS_RE.search(stripped)
            match_fail = cls.SUMMARY_FAIL_RE.search(stripped)
            match_err = cls.SUMMARY_ERR_RE.search(stripped)
            if match_pass or match_fail or match_err:
                if match_pass:
                    passed = int(match_pass.group(1))
                if match_fail:
                    failed = int(match_fail.group(1))
                if match_err:
                    errors = int(match_err.group(1))
                break
        return passed, failed, errors

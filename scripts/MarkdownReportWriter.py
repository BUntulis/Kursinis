"""Markdown ataskaitos rašymo klasė."""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from scripts.TaskMetadata import TaskMetadata


class MarkdownReportWriter:
    """
    **Kam skirtas:** Sugeneruoja žmonėms skirtą markdown ataskaitą iš benchmark rezultatų.

    **Tikslas:** Suformuoti bendrą suvestinę, grupines lenteles ir detalų rezultatų vaizdą viename `.md` faile.

    **Argumentai:** Klasė papildomų konstruktoriaus argumentų nenaudoja.

    **Grąžinama:** `MarkdownReportWriter` instanciją, galinčią vykdyti `write()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    MarkdownReportWriter().write(Path("results/report.md"), runs, meta)
    ```
    """

    #: **Kam skirtas:** Saugo numatytą sudėtingumų tvarką.
    #: **Tikslas:** Užtikrinti nuoseklų lentelių eiliškumą.
    DIFFICULTIES: ClassVar[list[str]] = ["easy", "medium", "hard"]

    def write(self, path: Path, runs: dict[str, dict], meta: TaskMetadata) -> None:
        """
        **Kam skirtas:** Sugeneruoja ir įrašo pilną markdown ataskaitą.

        **Tikslas:** Vienu iškvietimu sukurti žmogui skaitomą rezultatų santrauką.

        **Argumentai:** `path` nurodo `.md` failą, `runs` yra rezultatų žemėlapis, o `meta` pateikia užduočių metaduomenis.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        writer.write(Path("results/report.md"), runs, meta)
        ```
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        lines.append("# Eksperimentų rezultatai\n")
        lines.extend(self._summary_section(runs))
        lines.extend(self._by_category_section(runs, meta))
        lines.extend(self._by_difficulty_section(runs, meta))
        lines.extend(self._per_task_section(runs, meta))
        path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _summary_section(runs: dict[str, dict]) -> list[str]:
        """
        **Kam skirtas:** Sugeneruoja bendrą suvestinės sekciją markdown ataskaitai.

        **Tikslas:** Pateikti pagrindines kiekvieno run'o metrikas vienoje lentelėje.

        **Argumentai:** `runs` yra visų eksperimentų rezultatų žemėlapis.

        **Grąžinama:** `list[str]` su markdown eilučių sąrašu.

        **Panaudojimo pavyzdžiai:**
        ```python
        lines = MarkdownReportWriter._summary_section(runs)
        ```
        """
        out = ["## Bendra suvestinė\n"]
        out.append("| Setup | Pass@1 | Praėjo / Iš viso | Vid. wall (s) | Vid. tokens (in/out) | LLM iškvietimai (vid.) |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for run_name, data in runs.items():
            results = data.get("results", [])
            total = len(results)
            if not total:
                continue
            passed = sum(1 for result in results if result.get("passed"))
            avg_wall = sum(
                result.get("wall_time_sec", result.get("generation_time_sec", 0))
                for result in results
            ) / total
            avg_in = sum(result.get("prompt_tokens", 0) for result in results) / total
            avg_out = sum(result.get("completion_tokens", 0) for result in results) / total
            avg_calls = sum(result.get("llm_calls", 1) for result in results) / total
            out.append(
                f"| `{run_name}` | {passed / total:.1%} | {passed}/{total} | "
                f"{avg_wall:.1f} | {avg_in:.0f} / {avg_out:.0f} | {avg_calls:.1f} |"
            )
        return out

    @staticmethod
    def _by_category_section(runs: dict[str, dict], meta: TaskMetadata) -> list[str]:
        """
        **Kam skirtas:** Sugeneruoja `pass@1` lentelę pagal kategorijas.

        **Tikslas:** Leisti greitai pamatyti, kuriose srityse kuris modelis sekasi geriau.

        **Argumentai:** `runs` yra rezultatų žemėlapis, o `meta` pateikia kategorijų priskyrimą.

        **Grąžinama:** `list[str]` su markdown eilutėmis.

        **Panaudojimo pavyzdžiai:**
        ```python
        lines = MarkdownReportWriter._by_category_section(runs, meta)
        ```
        """
        out = ["\n## Pass@1 pagal kategoriją\n"]
        categories = sorted(set(meta.by_category.values()))
        return MarkdownReportWriter._group_table(out, runs, meta.by_category, categories, "Kategorija")

    @classmethod
    def _by_difficulty_section(
        cls,
        runs: dict[str, dict],
        meta: TaskMetadata,
    ) -> list[str]:
        """
        **Kam skirtas:** Sugeneruoja `pass@1` lentelę pagal sudėtingumus.

        **Tikslas:** Palyginti modelių našumą skirtingo sudėtingumo užduotyse.

        **Argumentai:** `runs` yra rezultatų žemėlapis, o `meta` pateikia sudėtingumų priskyrimą.

        **Grąžinama:** `list[str]` su markdown eilutėmis.

        **Panaudojimo pavyzdžiai:**
        ```python
        lines = MarkdownReportWriter._by_difficulty_section(runs, meta)
        ```
        """
        out = ["\n## Pass@1 pagal sudėtingumą\n"]
        return cls._group_table(out, runs, meta.by_difficulty, cls.DIFFICULTIES, "Sudėtingumas")

    @staticmethod
    def _group_table(
        out: list[str],
        runs: dict[str, dict],
        group_map: dict[str, str],
        groups: list[str],
        header_label: str,
    ) -> list[str]:
        """
        **Kam skirtas:** Sugeneruoja bendrą grupuotą markdown lentelę.

        **Tikslas:** Išvengti pasikartojančio kodo kategorijų ir sudėtingumų lentelėms.

        **Argumentai:** `out` yra pradinis eilučių sąrašas, `runs` yra rezultatų žemėlapis, `group_map` susieja užduotis su grupėmis, `groups` pateikia eiliškumą, o `header_label` nurodo pirmo stulpelio pavadinimą.

        **Grąžinama:** `list[str]` su papildytomis markdown eilutėmis.

        **Panaudojimo pavyzdžiai:**
        ```python
        lines = MarkdownReportWriter._group_table(out, runs, group_map, groups, "Kategorija")
        ```
        """
        headers = [header_label] + list(runs.keys())
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "---|" * len(headers))
        for group in groups:
            row = [group]
            for _run_name, data in runs.items():
                group_results = [
                    result
                    for result in data.get("results", [])
                    if group_map.get(result.get("task_id", "")) == group
                ]
                if group_results:
                    passed = sum(1 for result in group_results if result.get("passed"))
                    row.append(f"{passed}/{len(group_results)}")
                else:
                    row.append("-")
            out.append("| " + " | ".join(row) + " |")
        return out

    @staticmethod
    def _per_task_section(runs: dict[str, dict], meta: TaskMetadata) -> list[str]:
        """
        **Kam skirtas:** Sugeneruoja detalią lentelę pagal kiekvieną užduotį.

        **Tikslas:** Leisti greitai matyti, kuris run'as praėjo ar nepraėjo konkrečią benchmark užduotį.

        **Argumentai:** `runs` yra rezultatų žemėlapis, o `meta` pateikia pilną užduočių sąrašą.

        **Grąžinama:** `list[str]` su markdown eilutėmis.

        **Panaudojimo pavyzdžiai:**
        ```python
        lines = MarkdownReportWriter._per_task_section(runs, meta)
        ```
        """
        out = ["\n## Detalūs rezultatai pagal užduotį\n"]
        headers = ["Task", "Kategorija"] + list(runs.keys())
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "---|" * len(headers))
        for task in meta.tasks:
            row = [task.id, task.category]
            for _run_name, data in runs.items():
                match = next(
                    (result for result in data.get("results", []) if result.get("task_id") == task.id),
                    None,
                )
                if match is None:
                    row.append("-")
                else:
                    row.append("✓" if match.get("passed") else "✗")
            out.append("| " + " | ".join(row) + " |")
        return out

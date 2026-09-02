"""Benchmark užduoties duomenų klasė."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_SCORING_CRITERIA: dict[str, int] = {
    "Funkcinis korektiskumas": 30,
    "Modeliu ir duomenu bazes dizainas": 15,
    "Validacija ir krastiniai atvejai": 15,
    "Autentifikacija ir teises": 10,
    "API arba vaizdu elgsena": 10,
    "Suderinamumas su testais": 10,
    "Kodo kokybe ir palaikomumas": 5,
    "Saugumas ir atsparumas": 5,
}


@dataclass
class BenchmarkTask:
    """
    **Kam skirtas:** Saugo vienos benchmark užduoties duomenis.

    **Tikslas:** Pateikti vieningą struktūrą, kurią gali naudoti loader'is, agentas, baseline scenarijai ir sandbox'as.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `BenchmarkTask` instancija su užduoties metaduomenimis ir testais.

    **Panaudojimo pavyzdžiai:**
    ```python
    task = BenchmarkTask(
        id="001_blog_model",
        title="Blog modelis",
        difficulty="easy",
        category="models",
        description="Sukurk modelį",
        expected_files=["blog/models.py"],
        reference_tests="def test_example(): ..."
    )
    ```
    """

    #: **Kam skirtas:** Saugo užduoties identifikatorių.
    #: **Tikslas:** Leisti vienareikšmiškai atpažinti užduotį rezultatuose ir paieškoje.
    id: str

    #: **Kam skirtas:** Saugo trumpą užduoties pavadinimą.
    #: **Tikslas:** Rodyti žmonėms suprantamą užduoties antraštę CLI ir ataskaitose.
    title: str

    #: **Kam skirtas:** Saugo sudėtingumo lygį.
    #: **Tikslas:** Leisti grupuoti rezultatus pagal užduoties sudėtingumą.
    difficulty: str

    #: **Kam skirtas:** Saugo kategoriją.
    #: **Tikslas:** Leisti analizuoti rezultatus pagal teminę sritį.
    category: str

    #: **Kam skirtas:** Saugo pilną užduoties aprašą.
    #: **Tikslas:** Pateikti agentui ir baseline modeliui užduoties reikalavimus.
    description: str

    #: **Kam skirtas:** Saugo tikėtinų failų sąrašą.
    #: **Tikslas:** Padėti modeliui ir ataskaitoms žinoti, kokius failus reikia sugeneruoti.
    expected_files: list[str]

    #: **Kam skirtas:** Saugo referencinius testus.
    #: **Tikslas:** Leisti sandbox'ui patikrinti sugeneruoto sprendimo teisingumą.
    reference_tests: str

    #: **Kam skirtas:** Saugo glaustą teisingo sprendimo santrauką.
    #: **Tikslas:** Leisti ataskaitoms ir dashboard'ui atskirti vertinimo lūkesčius nuo modelio užklausos.
    expected_implementation_summary: str = ""

    #: **Kam skirtas:** Saugo planuojamų testų sąrašą.
    #: **Tikslas:** Dokumentuoti, kokias elgsenas turi dengti referenciniai testai.
    unit_test_plan: list[str] = field(default_factory=list)

    #: **Kam skirtas:** Saugo 100 taškų vertinimo rubriką.
    #: **Tikslas:** Standartizuoti rankinį ir automatinį benchmark rezultatų vertinimą.
    scoring_criteria: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SCORING_CRITERIA))

    @classmethod
    def from_yaml(cls, path: Path) -> "BenchmarkTask":
        """
        **Kam skirtas:** Sukuria `BenchmarkTask` objektą iš YAML failo.

        **Tikslas:** Konvertuoti benchmark failą iš disko į tipizuotą Python objektą.

        **Argumentai:** `path` nurodo YAML failą, kuriame yra užduoties aprašas.

        **Grąžinama:** `BenchmarkTask` instanciją, užpildytą pagal failo turinį.

        **Panaudojimo pavyzdžiai:**
        ```python
        task = BenchmarkTask.from_yaml(Path("benchmark/tasks/001_blog_model.yaml"))
        ```
        """
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            id=data["id"],
            title=data["title"],
            difficulty=data.get("difficulty", "unknown"),
            category=data.get("category", "general"),
            description=data["description"],
            expected_files=list(data.get("expected_files", [])),
            reference_tests=data["reference_tests"],
            expected_implementation_summary=data.get("expected_implementation_summary", ""),
            unit_test_plan=list(data.get("unit_test_plan") or []),
            scoring_criteria=dict(data.get("scoring_criteria") or DEFAULT_SCORING_CRITERIA),
        )

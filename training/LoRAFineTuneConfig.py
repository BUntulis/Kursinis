"""QLoRA treniravimo konfigūracijos klasė."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LoRAFineTuneConfig:
    """
    **Kam skirtas:** Saugo visus LoRA treniravimo parametrus viename objekte.

    **Tikslas:** Pateikti aiškų, tipizuotą ir lengvai perduodamą API `LoRATrainer` klasei bei CLI sluoksniui.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `LoRAFineTuneConfig` instancija su treniravimo ir LoRA parametrais.

    **Panaudojimo pavyzdžiai:**
    ```python
    from training.LoRAFineTuneConfig import LoRAFineTuneConfig

    config = LoRAFineTuneConfig(epochs=2, batch_size=4)
    print(config.output)
    ```
    """

    #: **Kam skirtas:** Nurodo instrukcijų dataset JSONL failą.
    #: **Tikslas:** Leisti `LoRATrainer` klasei žinoti, iš kur krauti treniravimo duomenis.
    dataset: Path = Path("data/django_instructions.jsonl")

    #: **Kam skirtas:** Saugo bazinio Hugging Face modelio identifikatorių.
    #: **Tikslas:** Nurodyti, kurį modelį kvantizuoti ir adaptuoti su LoRA sluoksniu.
    base_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"

    #: **Kam skirtas:** Nurodo katalogą, kuriame bus išsaugotas adapteris.
    #: **Tikslas:** Centralizuoti išėjimo vietą po treniravimo.
    output: Path = Path("models/qwen-django-lora")

    #: **Kam skirtas:** Saugo epokų skaičių.
    #: **Tikslas:** Valdyti, kiek kartų modelis pereis per visą dataset'ą.
    epochs: int = 1

    #: **Kam skirtas:** Saugo vienos GPU partijos dydį.
    #: **Tikslas:** Valdyti VRAM apkrovą ir vieno žingsnio duomenų kiekį.
    batch_size: int = 2

    #: **Kam skirtas:** Saugo gradientų akumuliacijos žingsnių skaičių.
    #: **Tikslas:** Emuliuoti didesnį efektyvų `batch size` ribotos GPU atminties sąlygomis.
    grad_accum_steps: int = 8

    #: **Kam skirtas:** Saugo mokymosi žingsnio dydį.
    #: **Tikslas:** Valdyti svorių atnaujinimo agresyvumą treniravimo metu.
    learning_rate: float = 2e-4

    #: **Kam skirtas:** Nurodo maksimalų sekos ilgį.
    #: **Tikslas:** Apriboti vieno pavyzdžio tokenų kiekį treniravimo metu.
    max_seq_len: int = 2048

    #: **Kam skirtas:** Saugo pseudoatsitiktinumo sėklą.
    #: **Tikslas:** Padidinti eksperimentų atkuriamumą.
    seed: int = 42

    #: **Kam skirtas:** Saugo LoRA žemo rango matricos dydį.
    #: **Tikslas:** Valdyti adapterio talpą ir parametrų kiekį.
    lora_r: int = 16

    #: **Kam skirtas:** Saugo LoRA mastelio koeficientą.
    #: **Tikslas:** Nustatyti adapterio atnaujinimų stiprumą.
    lora_alpha: int = 32

    #: **Kam skirtas:** Saugo LoRA dropout reikšmę.
    #: **Tikslas:** Reguliarizuoti adapterio mokymą ir mažinti persimokymo riziką.
    lora_dropout: float = 0.05

    #: **Kam skirtas:** Saugo tikslinių transformerių modulių pavadinimus.
    #: **Tikslas:** Nurodyti, kuriuose svoriuose turi būti taikomas LoRA adapteris.
    lora_target_modules: list[str] = field(
        default_factory=lambda: [
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ]
    )

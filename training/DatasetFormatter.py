"""Dataset formatavimo klasė Qwen stiliaus pokalbiui."""
from __future__ import annotations


class DatasetFormatter:
    """
    **Kam skirtas:** Konvertuoja instrukcijų dataset įrašus į Qwen pokalbio formatą.

    **Tikslas:** Paruošti `instruction/input/output` struktūrą taip, kad ją tiesiogiai galėtų naudoti SFT treniravimas.

    **Argumentai:** Klasė dirba su `dict` tipo įrašais, perduodamais į `format()` metodą.

    **Grąžinama:** `DatasetFormatter` instancija, galinti paversti įrašus į `{"text": ...}` formatą.

    **Panaudojimo pavyzdžiai:**
    ```python
    from training.DatasetFormatter import DatasetFormatter

    formatter = DatasetFormatter()
    payload = formatter.format(
        {"instruction": "Sukurk modelį", "input": "", "output": "class Book: ..."}
    )
    ```
    """

    #: **Kam skirtas:** Saugo sisteminį prompt'ą, įdedamą į kiekvieną įrašą.
    #: **Tikslas:** Užtikrinti vienodą modelio rolės apibrėžimą visame treniravimo rinkinyje.
    SYSTEM_PROMPT = "You are a senior Django engineer."

    #: **Kam skirtas:** Saugo Qwen pokalbio šabloną.
    #: **Tikslas:** Vienodai sukomponuoti `system`, `user` ir `assistant` pranešimus viename tekste.
    TEMPLATE = (
        "<|im_start|>system\n{system}<|im_end|>\n"
        "<|im_start|>user\n{user}<|im_end|>\n"
        "<|im_start|>assistant\n{assistant}<|im_end|>"
    )

    def format(self, example: dict) -> dict:
        """
        **Kam skirtas:** Paverčia vieną dataset įrašą į SFT treniravimo tekstinę eilutę.

        **Tikslas:** Sukurti `{"text": ...}` struktūrą, kurią tiesiogiai priima `trl.SFTTrainer`.

        **Argumentai:** `example` yra žodynas su raktais `instruction`, pasirenkamu `input` ir `output`.

        **Grąžinama:** `dict` su vienu raktu `text`, kuriame yra pilnas pokalbio tekstas.

        **Panaudojimo pavyzdžiai:**
        ```python
        formatter = DatasetFormatter()
        record = formatter.format(
            {
                "instruction": "Sukurk serializerį",
                "input": "Modelis: Book",
                "output": "class BookSerializer(...): ..."
            }
        )
        print(record["text"])
        ```
        """
        instruction = example["instruction"]
        extra_input = example.get("input") or ""
        user = instruction if not extra_input else f"{instruction}\n\n{extra_input}"
        return {
            "text": self.TEMPLATE.format(
                system=self.SYSTEM_PROMPT,
                user=user,
                assistant=example["output"],
            )
        }

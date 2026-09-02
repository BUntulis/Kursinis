"""QLoRA treniravimo klasė."""
from __future__ import annotations

from training.DatasetFormatter import DatasetFormatter
from training.LoRAFineTuneConfig import LoRAFineTuneConfig


class LoRATrainer:
    """
    **Kam skirtas:** Vykdo pilną QLoRA treniravimo eigą nuo dataset užkrovimo iki adapterio išsaugojimo.

    **Tikslas:** Sukoncentruoti modelio, tokenizer'io, dataset ir `trl.SFTTrainer` orkestraciją vienoje klasėje.

    **Argumentai:** Konstruktorius priima `LoRAFineTuneConfig` ir pasirenkamą `DatasetFormatter`.

    **Grąžinama:** `LoRATrainer` instancija, kuri gali pradėti treniravimą per `train()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    from training.LoRAFineTuneConfig import LoRAFineTuneConfig
    from training.LoRATrainer import LoRATrainer

    trainer = LoRATrainer(LoRAFineTuneConfig())
    # trainer.train()
    ```
    """

    #: **Kam skirtas:** Saugo treniravimo konfigūraciją.
    #: **Tikslas:** Leisti visiems treniravimo žingsniams naudoti vieną parametrų šaltinį.
    config: LoRAFineTuneConfig

    #: **Kam skirtas:** Saugo dataset formatuotoją.
    #: **Tikslas:** Atskirti duomenų pavertimo logiką nuo modelio treniravimo logikos.
    formatter: DatasetFormatter

    def __init__(
        self,
        config: LoRAFineTuneConfig,
        formatter: DatasetFormatter | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja trenerį su konfigūracija ir formatavimo strategija.

        **Tikslas:** Paruošti objektą taip, kad `train()` metodas galėtų dirbti su pilna priklausomybių būsena.

        **Argumentai:** `config` nurodo treniravimo parametrus, o `formatter` leidžia įšvirkšti alternatyvų dataset formatuotoją.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        trainer = LoRATrainer(config)
        trainer = LoRATrainer(config, formatter=DatasetFormatter())
        ```
        """
        self.config = config
        self.formatter = formatter or DatasetFormatter()

    def train(self) -> None:
        """
        **Kam skirtas:** Paleidžia visą QLoRA treniravimo pipeline'ą.

        **Tikslas:** Užkrauti dataset'ą, modelį, tokenizer'į, LoRA parametrus ir išsaugoti ištreniruotą adapterį.

        **Argumentai:** Metodas papildomų argumentų nepriima, nes naudoja konstruktoriuje perduotą būseną.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        trainer = LoRATrainer(config)
        trainer.train()
        ```
        """
        import torch

        if not torch.cuda.is_available():
            raise SystemExit("CUDA nepasiekiama. Šis skriptas skirtas HPC GPU node'ui.")

        from datasets import load_dataset
        from peft import LoraConfig, prepare_model_for_kbit_training
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from trl import SFTConfig, SFTTrainer

        cfg = self.config

        print(f"==> Įkraunamas dataset: {cfg.dataset}")
        ds = load_dataset("json", data_files=str(cfg.dataset), split="train")
        ds = ds.map(self.formatter.format, remove_columns=ds.column_names)
        print(f"   pavyzdžių: {len(ds)}")

        bnb = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

        print(f"==> Įkraunamas modelis: {cfg.base_model}")
        tokenizer = AutoTokenizer.from_pretrained(cfg.base_model, trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        model = AutoModelForCausalLM.from_pretrained(
            cfg.base_model,
            quantization_config=bnb,
            device_map="auto",
            trust_remote_code=True,
        )
        model = prepare_model_for_kbit_training(model)

        lora_cfg = LoraConfig(
            r=cfg.lora_r,
            lora_alpha=cfg.lora_alpha,
            lora_dropout=cfg.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=cfg.lora_target_modules,
        )

        sft_cfg = SFTConfig(
            output_dir=str(cfg.output),
            num_train_epochs=cfg.epochs,
            per_device_train_batch_size=cfg.batch_size,
            gradient_accumulation_steps=cfg.grad_accum_steps,
            learning_rate=cfg.learning_rate,
            max_seq_length=cfg.max_seq_len,
            logging_steps=20,
            save_steps=200,
            save_total_limit=2,
            bf16=True,
            warmup_ratio=0.03,
            lr_scheduler_type="cosine",
            seed=cfg.seed,
            report_to="none",
        )

        trainer = SFTTrainer(
            model=model,
            args=sft_cfg,
            train_dataset=ds,
            peft_config=lora_cfg,
            tokenizer=tokenizer,
        )

        print("==> Pradedamas treniravimas")
        trainer.train()

        print(f"==> Saugomas adapteris: {cfg.output}")
        trainer.save_model(str(cfg.output))
        tokenizer.save_pretrained(str(cfg.output))

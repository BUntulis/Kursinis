"""QLoRA fine-tuning skriptas Qwen2.5-Coder-7B modeliui ant Django dataset'o.

NEPALEISTI lokaliai (Intel Arc — be CUDA). Skirta VU MIF Klevas HPC GPU node'ui
(žr. hpc/finetune.slurm).

Klasių struktūra:
    LoRAFineTuneConfig — visa konfigūracija viename objekte (override per CLI).
    DatasetFormatter   — konvertuoja `{instruction,input,output}` į Qwen chat formatą.
    LoRATrainer        — pilnas pipeline'as: load model + dataset → train → save.

Naudojimas (HPC):
    python training/lora_finetune.py --dataset data/django_instructions.jsonl
"""
from __future__ import annotations

import argparse
from pathlib import Path

from training.DatasetFormatter import DatasetFormatter
from training.LoRAFineTuneConfig import LoRAFineTuneConfig
from training.LoRATrainer import LoRATrainer


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=LoRAFineTuneConfig.dataset)
    p.add_argument("--base", default=LoRAFineTuneConfig.base_model)
    p.add_argument("--output", type=Path, default=LoRAFineTuneConfig.output)
    p.add_argument("--epochs", type=int, default=LoRAFineTuneConfig.epochs)
    p.add_argument("--batch", type=int, default=LoRAFineTuneConfig.batch_size)
    p.add_argument("--grad-accum", type=int, default=LoRAFineTuneConfig.grad_accum_steps)
    p.add_argument("--lr", type=float, default=LoRAFineTuneConfig.learning_rate)
    p.add_argument("--max-seq-len", type=int, default=LoRAFineTuneConfig.max_seq_len)
    p.add_argument("--lora-r", type=int, default=LoRAFineTuneConfig.lora_r)
    p.add_argument("--lora-alpha", type=int, default=LoRAFineTuneConfig.lora_alpha)
    p.add_argument("--lora-dropout", type=float, default=LoRAFineTuneConfig.lora_dropout)
    p.add_argument("--seed", type=int, default=LoRAFineTuneConfig.seed)
    return p


def _config_from_args(args: argparse.Namespace) -> LoRAFineTuneConfig:
    return LoRAFineTuneConfig(
        dataset=args.dataset,
        base_model=args.base,
        output=args.output,
        epochs=args.epochs,
        batch_size=args.batch,
        grad_accum_steps=args.grad_accum,
        learning_rate=args.lr,
        max_seq_len=args.max_seq_len,
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        seed=args.seed,
    )


def main() -> int:
    args = _build_argparser().parse_args()
    config = _config_from_args(args)
    LoRATrainer(config, formatter=DatasetFormatter()).train()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/bin/bash
#SBATCH --job-name=qwen-django-lora
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=06:00:00
#SBATCH --output=logs/finetune_%j.out
#SBATCH --error=logs/finetune_%j.err

set -e
mkdir -p logs

module load python/3.11 cuda/12.1 || true
source .venv-hpc/bin/activate

# Treniravimo deps (atskira nuo pagrindinių requirements.txt)
pip install -q -r requirements-train.txt

# 1) Jei dataset'o dar nėra — sukloduoti repos ir paruošti
if [ ! -f data/django_instructions.jsonl ]; then
    python -m src.scraper.github_scraper --output data/raw
    python -m src.dataset.build_dataset \
        --input data/raw \
        --output data/django_instructions.jsonl
fi

# 2) Fine-tune QLoRA
python training/lora_finetune.py \
    --dataset data/django_instructions.jsonl \
    --base Qwen/Qwen2.5-Coder-7B-Instruct \
    --output models/qwen-django-lora \
    --epochs 1 --batch 2 --grad-accum 8

echo "==> Done. Adapteris: models/qwen-django-lora"

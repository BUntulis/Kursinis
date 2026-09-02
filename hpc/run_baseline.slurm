#!/bin/bash
# VU MIF HPC (Klevas) — submission skriptas baseline'ui ant didelio modelio (A100).
#
# Pateikimas:
#   sbatch hpc/run_baseline.slurm
#
# Modeliai, kurie reikalauja A100 (40GB+):
#   qwen2.5-coder:32b, qwen2.5-coder:14b-q4_K_M

#SBATCH --job-name=kursinis-baseline
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=02:00:00
#SBATCH --output=logs/baseline_%j.out
#SBATCH --error=logs/baseline_%j.err

set -euo pipefail

# Pakoreguok pagal savo HPC modulių pavadinimus
module load python/3.11 || true
module load cuda/12.1 || true

cd "$SLURM_SUBMIT_DIR"

# Aktyvuoti venv (sukurtas iš anksto: python -m venv .venv-hpc)
source .venv-hpc/bin/activate

# Ollama paleidimas fone (jei naudojamas lokaliam inference)
export OLLAMA_HOST=127.0.0.1:11434
nohup ollama serve > logs/ollama_${SLURM_JOB_ID}.log 2>&1 &
OLLAMA_PID=$!
sleep 10

# Modelio pull (saugu — jeigu jau yra, neperatsisiųs)
ollama pull "${OLLAMA_MODEL:-qwen2.5-coder:14b-instruct}"

# Paleidžiam benchmark
python scripts/run_baseline.py \
    --tasks all \
    --model "${OLLAMA_MODEL:-qwen2.5-coder:14b-instruct}" \
    --output "results/hpc_baseline_${OLLAMA_MODEL//[:\/]/_}.json"

# Sustabdom Ollama
kill $OLLAMA_PID || true

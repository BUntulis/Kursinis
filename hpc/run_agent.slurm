#!/bin/bash
# VU MIF HPC — agentinio pipeline'o paleidimas.

#SBATCH --job-name=kursinis-agent
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=03:00:00
#SBATCH --output=logs/agent_%j.out
#SBATCH --error=logs/agent_%j.err

set -euo pipefail

module load python/3.11 || true
module load cuda/12.1 || true

cd "$SLURM_SUBMIT_DIR"
source .venv-hpc/bin/activate

export OLLAMA_HOST=127.0.0.1:11434
nohup ollama serve > logs/ollama_${SLURM_JOB_ID}.log 2>&1 &
OLLAMA_PID=$!
sleep 10

ollama pull "${OLLAMA_MODEL:-qwen2.5-coder:14b-instruct}"
ollama pull nomic-embed-text || true

# RAG indexas (jeigu dar nesukurtas)
if [ ! -d ".chroma" ]; then
    if [ ! -d "django-src" ]; then
        git clone --depth=1 https://github.com/django/django.git django-src
    fi
    python -m src.rag.indexer --source django-src/docs --output .chroma
fi

# Agentas
python scripts/run_agent.py \
    --tasks all \
    --max-iter 3 \
    --tag "qwen14b_hpc" \
    --output "results/hpc_agent_${OLLAMA_MODEL//[:\/]/_}.json"

# Be RAG palyginimui
python scripts/run_agent.py \
    --tasks all \
    --max-iter 3 \
    --no-rag \
    --tag "qwen14b_hpc" \
    --output "results/hpc_agent_norag_${OLLAMA_MODEL//[:\/]/_}.json"

kill $OLLAMA_PID || true

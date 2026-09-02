# HPC (VU MIF Klevas) paleidimo instrukcijos

## Vienkartinė paruošimo procedūra

```bash
# Prisijunk per SSH (po prieigos suteikimo)
ssh <vartotojas>@<klevas-host>

# Klonuok projektą
git clone <tavo-repo-url> kursinis
cd kursinis

# Sukurk Python venv
module load python/3.11
python -m venv .venv-hpc
source .venv-hpc/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Įdiek Ollama (vartotojo prefix'e, be sudo)
curl -fsSL https://ollama.com/install.sh | OLLAMA_INSTALL_DIR=$HOME/.local/bin sh

# Sukurk logų direktoriją
mkdir -p logs results
```

## Job'ų pateikimas

```bash
# Baseline (vienas LLM call per užduotį)
OLLAMA_MODEL=qwen2.5-coder:14b-instruct sbatch hpc/run_baseline.slurm

# Agentinis pipeline'as
OLLAMA_MODEL=qwen2.5-coder:14b-instruct sbatch hpc/run_agent.slurm

# Status
squeue -u $USER
sacct -j <job-id> --format=JobID,State,Elapsed,MaxRSS,Reqgres
```

## Resursų prašymo komentarai

| Modelis | VRAM (Q4) | Rekomenduojamas GPU | Walltime |
|---|---|---|---|
| qwen2.5-coder:7b-instruct | ~5 GB | V100 / A100 | 1 h |
| qwen2.5-coder:14b-instruct | ~9 GB | V100 / A100 | 2 h |
| qwen2.5-coder:32b-instruct | ~20 GB | A100 40GB | 3 h |

## Patikrink savo kvotą prieš submit'ą

```bash
sacctmgr show assoc user=$USER format=Account,Cluster,Partition,GrpTRES
sinfo -p gpu -o "%P %a %l %D %t %N %G"   # GPU mazgų statusas
```

## Pastabos

- Skriptai naudoja `module load` direktyvas — pakoreguok pagal tikslius modulių
  pavadinimus VU klasteryje (pvz. gali būti `Python/3.11.4-GCCcore-12.3.0`).
- Jei klasteryje GPU nėra įdiegtas CUDA per modulį, Ollama vis tiek veiks ant CPU,
  tik lėčiau.
- `--time` reikšmę pakoreguok pagal kvotą — per ilgi job'ai gali nepatekti į queue.

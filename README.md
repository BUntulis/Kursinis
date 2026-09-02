# Kursinis

Sistema, kuri pati kuria Django web aplikacijas. Parašai, ko nori, o ji sugeneruoja kodą, paleidžia testus ir pataiso klaidas.

Viskas veikia savo kompiuteryje su nemokamais modeliais per Ollama, todėl nieko mokėti nereikia ir duomenys niekur nesiunčiami.

## Ko reikia

- Python 3.11
- [Ollama](https://ollama.com) su bent vienu kodo modeliu, pvz. `ollama pull qwen2.5-coder:7b-instruct`

## Paleidimas

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Atsidaryk http://127.0.0.1:8000/

## Testai

```bash
python -m pytest tests/
python manage.py test dashboard
```

## Kas kur

- `src/agent/` — agentas (LangGraph)
- `src/llm/` — Ollama ir OpenAI
- `src/rag/` — Django dokumentacijos paieška
- `src/eval/` — sugeneruoto kodo testavimas
- `dashboard/` — sąsaja naršyklėje
- `benchmark/tasks/` — 12 užduočių modeliams palyginti

VU MIF kursinis darbas.

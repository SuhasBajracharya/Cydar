# Cydar — AI-Assisted Web Recon & Vulnerability Triage

> My Final Year Project (FYP).

An autonomous security agent for web targets: point it at a domain you own / are
authorised to test, and it runs scoped recon (nmap, gobuster, subdomain/vhost
enum, HTTP/DNS checks, detection-only probes), proposes each tool call for
approval, and compiles an evidence-grounded report (`confirmed` findings with
pasted tool output + `leads` with manual verify steps).

## Layout

- `backend/` — Django 6.1 project (`config.settings`, app package `config/`).
  - `targets/` — Target / AgentRun / Finding / Scan models, `/api/` JSON views,
    admin registration.
  - `agent/` — Plain package (not a Django app): provider registry, LLM client,
    system prompt, ReAct loop (`loop.py`).
  - `recon/` — Plain package: local nmap/gobuster runner via subprocess
    (no shell) + scope-checked recon tools.
- `frontend/` — React 19 + Vite 8 SPA (plain JSX, `src/main.jsx` entry),
  Tailwind v4, session-cookie + CSRF fetch wrapper (`src/api.js`).

Two independent scaffolds — no root build/lint/test runner. Work in one side
at a time.

## Prerequisites

- Python 3.x with `venv`
- Node 18+ / npm
- Optional: `nmap`, `gobuster` (for local recon presets), `ollama` (for local
  models, e.g. `ollama pull qwen3:8b`)

## Setup

1. Configure secrets — copy the example and fill in keys:

   ```bash
   cp .env.example .env
   ```

   One `*_API_KEY` per hosted provider (`DEEPSEEK_`, `OPENROUTER_`,
   `AGENTROUTER_`, `NVIDIA_`, `OPENCODE_` for Zen). Ollama needs no key
   (`OLLAMA_BASE_URL` / `OLLAMA_MODEL` to override defaults). Never commit
   the real `.env`.

2. Backend:

   ```bash
   cd backend
   ./venv/bin/pip install -r requirements.txt
   ./venv/bin/python manage.py migrate
   ./venv/bin/python manage.py runserver
   ```

   Always run from `backend/` so `config.settings` resolves, using the venv
   directly (it is not on PATH). DB is SQLite at `backend/db.sqlite3`
   (`DEBUG=True`, dev `SECRET_KEY`).

3. Frontend (separate terminal):

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

   `/api` proxies to Django at `127.0.0.1:8000` in dev.

## Useful commands

| Where      | Command                                 |
| ---------- | --------------------------------------- |
| `backend/` | `./venv/bin/python manage.py migrate`   |
| `backend/` | `./venv/bin/python manage.py runserver` |
| `backend/` | `./venv/bin/python manage.py test`      |
| `frontend/`| `npm run dev` / `build` / `preview`     |
| `frontend/`| `npm run lint` (oxlint, not ESLint)     |

## How it works (short)

- **Targets** have an Edit form including an *Allow insecure TLS* checkbox
  (lab/self-signed hosts only) and session-auth slots (bearer/header/cookie +
  check URL, one per host; values are write-only, injected server-side).
- **Local recon**: `POST /api/targets/<id>/scans/` runs one preset
  synchronously (`nmap-fast`, `nmap-services`, `nmap-scripts`,
  `gobuster-common`).
- **Autonomous agent**: ReAct loop over scope-checked, level-gated,
  detection-only tools. `POST …/agent/start/` (defaults: 25 steps / 30 min),
  modes `semi` (approve/reject + guidance per call) and `auto`, live SSE at
  `…/agent/<run>/events/`, stop via `POST …/agent/<run>/stop/`, audit via
  `GET …/agent/<run>/actions/`, reports via `GET …/reports/`.
- **Report grounding**: `confirmed` findings must quote a 40+ char verbatim
  chunk of recorded tool output; evidenceless items are rejected back to the
  model.

# val-agent

Lite Python web app that runs an uploaded document through multiple LLM validators
in parallel and records every step in a hash-chained audit log.

See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the design methodology
(deterministic / versioned / immutable) that the implementation is held against.

## Audit posture

Each validation captures the exact inputs, the exact outputs, and a tamper-evident
hash chain so a reviewer can prove the system followed a structured standard:

| Property | How it is enforced |
|---|---|
| Reproducibility | `temperature=0` on every adapter; pinned model IDs in `config.yaml` |
| Versioned rubric | `rubric.yaml` carries `id`, `version`, and a recognized standard reference (NIST AI RMF by default) |
| Versioned prompt | `prompt_template_version` in `config.yaml`, recorded with every result |
| Integrity | SHA-256 of the uploaded file and of the rubric file, both stored per submission |
| Tamper-evidence | Each `validations` row stores `prev_chain_hash` + `chain_hash` over a canonical JSON of the result; recompute via `GET /audit/verify` |
| Multi-model consensus | N models run independently; per-criterion votes preserved; disagreement surfaced rather than silently averaged |
| Structured output | Every model returns the same JSON schema; non-conforming responses are coerced to `missing` so they can't masquerade as `pass` |

## Swapping models

Models are pluggable via `adapters.py` and `config.yaml`. To enable a new
provider, set `enabled: true` on the entry and export the relevant API key:

| Provider | Env var |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `google` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| `mock` | (none — offline stub) |

To add a new provider: subclass `ModelAdapter`, implement `_call(prompt) -> str`,
register it in `PROVIDERS`, and add an entry in `config.yaml`.

## Run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...   # plus any others enabled in config.yaml
uvicorn app:app --reload
```

Open http://localhost:8000 and upload a UTF-8 text file.

## Endpoints

- `GET  /` — upload UI
- `POST /validate` — run all enabled adapters, return consensus
- `GET  /audit/{id}` — full record for a submission (file hash, rubric hash, every model response, chain hashes)
- `GET  /audit/verify` — recompute the entire chain and report tampering

## Limits (lite build)

- 2 MiB upload cap, UTF-8 text only.
- Audit DB is local SQLite (`audit.db`). For production, point at a managed DB and add row-level WORM storage / off-site signing.

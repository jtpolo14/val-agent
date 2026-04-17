"""FastAPI entry point. Run: uvicorn app:app --reload"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile

load_dotenv()
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import audit
from adapters import env_status, load_adapters
from validate import result_to_dict, run_validation

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.yaml"
RUBRIC_PATH = BASE / "rubric.yaml"
MAX_BYTES = 2 * 1024 * 1024  # 2 MiB cap for the lite version

app = FastAPI(title="val-agent", version="0.1.0")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _load_yaml(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return yaml.safe_load(raw), audit.sha256_bytes(raw)


@app.on_event("startup")
def _startup() -> None:
    audit.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    config, _ = _load_yaml(CONFIG_PATH)
    rubric, _ = _load_yaml(RUBRIC_PATH)
    enabled = [m for m in config["models"] if m.get("enabled")]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "models": enabled,
            "rubric": rubric,
            "env": env_status(),
            "history": audit.list_submissions(20),
        },
    )


@app.post("/validate")
async def validate_endpoint(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"file exceeds {MAX_BYTES} byte limit")
    try:
        document = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "only UTF-8 text files are supported in the lite build")

    config, _ = _load_yaml(CONFIG_PATH)
    rubric, rubric_sha = _load_yaml(RUBRIC_PATH)
    adapters = load_adapters(config)

    results, consensus = run_validation(adapters, rubric, document)

    file_sha = hashlib.sha256(data).hexdigest()
    submission_id = audit.record_submission(
        file_name=file.filename or "unnamed",
        file_size=len(data),
        file_sha256=file_sha,
        rubric=rubric,
        rubric_sha256=rubric_sha,
        consensus=consensus,
    )
    chain_hashes = []
    for r in results:
        chain_hashes.append(audit.record_validation(submission_id, result_to_dict(r)))

    return JSONResponse(
        {
            "submission_id": submission_id,
            "file_sha256": file_sha,
            "rubric_id": rubric["id"],
            "rubric_version": rubric["version"],
            "rubric_sha256": rubric_sha,
            "consensus": consensus,
            "audit_chain_tip": chain_hashes[-1] if chain_hashes else None,
        }
    )


@app.get("/audit/{submission_id}")
def audit_view(submission_id: int):
    record = audit.get_submission(submission_id)
    if record is None:
        raise HTTPException(404, "submission not found")
    return record


@app.get("/audit/verify")
def audit_verify():
    ok, rows, err = audit.verify_chain()
    return {"ok": ok, "rows_checked": rows, "error": err}

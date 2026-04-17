"""Append-only, hash-chained audit log. Each row's chain_hash incorporates the
prior row's chain_hash, making silent edits detectable. Verify with verify_chain()."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(__file__).parent / "audit.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at REAL NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_sha256 TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    rubric_sha256 TEXT NOT NULL,
    consensus_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    adapter_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    error TEXT,
    raw_response TEXT NOT NULL,
    verdicts_json TEXT NOT NULL,
    prev_chain_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL,
    FOREIGN KEY (submission_id) REFERENCES submissions(id)
);

CREATE INDEX IF NOT EXISTS idx_validations_submission ON validations(submission_id);
CREATE INDEX IF NOT EXISTS idx_submissions_hash ON submissions(file_sha256);
"""

GENESIS_HASH = "0" * 64


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _last_chain_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT chain_hash FROM validations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["chain_hash"] if row else GENESIS_HASH


def _compute_chain_hash(prev: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + canonical).encode("utf-8")).hexdigest()


def record_submission(
    file_name: str,
    file_size: int,
    file_sha256: str,
    rubric: dict,
    rubric_sha256: str,
    consensus: dict,
) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO submissions
               (submitted_at, file_name, file_size, file_sha256,
                rubric_id, rubric_version, rubric_sha256, consensus_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                file_name,
                file_size,
                file_sha256,
                rubric["id"],
                rubric["version"],
                rubric_sha256,
                json.dumps(consensus, sort_keys=True),
            ),
        )
        return cur.lastrowid


def record_validation(submission_id: int, result: dict) -> str:
    with _conn() as c:
        prev = _last_chain_hash(c)
        payload = {
            "submission_id": submission_id,
            "adapter_id": result["adapter_id"],
            "provider": result["provider"],
            "model": result["model"],
            "prompt_template_version": result["prompt_template_version"],
            "latency_ms": result["latency_ms"],
            "error": result.get("error"),
            "raw_response_sha256": sha256_bytes(result["raw_response"].encode("utf-8")),
            "verdicts": result["verdicts"],
        }
        chain = _compute_chain_hash(prev, payload)
        c.execute(
            """INSERT INTO validations
               (submission_id, recorded_at, adapter_id, provider, model,
                prompt_template_version, latency_ms, error, raw_response,
                verdicts_json, prev_chain_hash, chain_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                submission_id,
                time.time(),
                result["adapter_id"],
                result["provider"],
                result["model"],
                result["prompt_template_version"],
                result["latency_ms"],
                result.get("error"),
                result["raw_response"],
                json.dumps(result["verdicts"], sort_keys=True),
                prev,
                chain,
            ),
        )
        return chain


def verify_chain() -> tuple[bool, int, str | None]:
    """Recompute the chain end-to-end. Returns (ok, rows_checked, first_bad_id)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, submission_id, adapter_id, provider, model, "
            "prompt_template_version, latency_ms, error, raw_response, "
            "verdicts_json, prev_chain_hash, chain_hash "
            "FROM validations ORDER BY id ASC"
        ).fetchall()
    prev = GENESIS_HASH
    for row in rows:
        if row["prev_chain_hash"] != prev:
            return False, row["id"], f"row {row['id']} prev_chain_hash mismatch"
        payload = {
            "submission_id": row["submission_id"],
            "adapter_id": row["adapter_id"],
            "provider": row["provider"],
            "model": row["model"],
            "prompt_template_version": row["prompt_template_version"],
            "latency_ms": row["latency_ms"],
            "error": row["error"],
            "raw_response_sha256": sha256_bytes(row["raw_response"].encode("utf-8")),
            "verdicts": json.loads(row["verdicts_json"]),
        }
        expected = _compute_chain_hash(prev, payload)
        if expected != row["chain_hash"]:
            return False, len(rows), f"row {row['id']} chain_hash mismatch"
        prev = row["chain_hash"]
    return True, len(rows), None


def get_submission(submission_id: int) -> dict | None:
    with _conn() as c:
        sub = c.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if sub is None:
            return None
        vals = c.execute(
            "SELECT * FROM validations WHERE submission_id = ? ORDER BY id ASC",
            (submission_id,),
        ).fetchall()
    return {
        "submission": dict(sub),
        "validations": [dict(v) for v in vals],
    }


def list_submissions(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, submitted_at, file_name, file_sha256, rubric_version "
            "FROM submissions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

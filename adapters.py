"""Pluggable model adapters. Add a provider by subclassing ModelAdapter and
registering it in PROVIDERS. config.yaml selects which to instantiate."""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CriterionVerdict:
    criterion_id: str
    verdict: str
    confidence: float
    evidence: str


@dataclass
class ValidationResult:
    adapter_id: str
    provider: str
    model: str
    prompt_template_version: str
    rubric_version: str
    verdicts: list[CriterionVerdict]
    raw_response: str
    latency_ms: int
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


PROMPT_TEMPLATE = """You are an independent validator running under an audited
multi-model review process. Evaluate the supplied document against EVERY criterion
in the rubric below. You MUST respond with a single JSON object and nothing else.

Rubric standard: {standard_name} ({standard_ref})
Rubric version: {rubric_version}

Criteria:
{criteria_block}

Required JSON shape:
{{
  "verdicts": [
    {{
      "criterion_id": "<id from rubric>",
      "verdict": "pass" | "fail" | "not_applicable",
      "confidence": <number 0.0-1.0>,
      "evidence": "<short citation or quote from the document, max 240 chars>"
    }}
  ]
}}

Rules:
- Return one entry per criterion, in rubric order.
- Use "not_applicable" only when the criterion genuinely does not apply.
- "evidence" must reference the document (quote or paraphrase). Empty string only for not_applicable.
- Do not include prose outside the JSON object.

--- DOCUMENT START ---
{document}
--- DOCUMENT END ---"""


def build_prompt(document: str, rubric: dict) -> str:
    criteria_block = "\n".join(
        f"- {c['id']}: {c['description']} [standard_ref: {c.get('standard_ref', 'n/a')}]"
        for c in rubric["criteria"]
    )
    return PROMPT_TEMPLATE.format(
        standard_name=rubric["standard"]["name"],
        standard_ref=rubric["standard"]["reference"],
        rubric_version=rubric["version"],
        criteria_block=criteria_block,
        document=document,
    )


def parse_response(raw: str, rubric: dict) -> list[CriterionVerdict]:
    """Tolerantly extract JSON from a model response and coerce to verdicts."""
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in response")
    payload = json.loads(text[start : end + 1])

    by_id = {v["criterion_id"]: v for v in payload.get("verdicts", [])}
    verdicts = []
    for c in rubric["criteria"]:
        v = by_id.get(c["id"])
        if v is None:
            verdicts.append(CriterionVerdict(c["id"], "missing", 0.0, ""))
            continue
        verdict = str(v.get("verdict", "missing")).lower()
        if verdict not in {"pass", "fail", "not_applicable"}:
            verdict = "missing"
        try:
            conf = float(v.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        verdicts.append(
            CriterionVerdict(
                criterion_id=c["id"],
                verdict=verdict,
                confidence=max(0.0, min(1.0, conf)),
                evidence=str(v.get("evidence", ""))[:240],
            )
        )
    return verdicts


class ModelAdapter(ABC):
    provider: str = ""

    def __init__(self, adapter_id: str, model: str, prompt_template_version: str):
        self.adapter_id = adapter_id
        self.model = model
        self.prompt_template_version = prompt_template_version

    @abstractmethod
    def _call(self, prompt: str) -> str:
        ...

    def validate(self, document: str, rubric: dict) -> ValidationResult:
        prompt = build_prompt(document, rubric)
        started = time.perf_counter()
        error = None
        raw = ""
        verdicts: list[CriterionVerdict] = []
        try:
            raw = self._call(prompt)
            verdicts = parse_response(raw, rubric)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ValidationResult(
            adapter_id=self.adapter_id,
            provider=self.provider,
            model=self.model,
            prompt_template_version=self.prompt_template_version,
            rubric_version=rubric["version"],
            verdicts=verdicts,
            raw_response=raw,
            latency_ms=latency_ms,
            error=error,
        )


class AnthropicAdapter(ModelAdapter):
    provider = "anthropic"

    def _call(self, prompt: str) -> str:
        from anthropic import Anthropic, BadRequestError

        client = Anthropic()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            msg = client.messages.create(**kwargs)
        except BadRequestError as exc:
            if "temperature" in str(exc).lower():
                kwargs.pop("temperature", None)
                msg = client.messages.create(**kwargs)
            else:
                raise
        return "".join(block.text for block in msg.content if block.type == "text")


class OpenAIAdapter(ModelAdapter):
    provider = "openai"

    def _call(self, prompt: str) -> str:
        from openai import OpenAI

        client = OpenAI()
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""


class GoogleAdapter(ModelAdapter):
    """Google Gemini via the google-genai SDK. Reads GOOGLE_API_KEY or GEMINI_API_KEY."""

    provider = "google"

    def _call(self, prompt: str) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client()
        resp = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        return resp.text or ""


class MockAdapter(ModelAdapter):
    """Deterministic stub for offline runs and tests."""

    provider = "mock"

    def _call(self, prompt: str) -> str:
        verdicts = []
        for line in prompt.splitlines():
            if line.startswith("- ") and ":" in line:
                cid = line[2:].split(":", 1)[0].strip()
                verdicts.append(
                    {
                        "criterion_id": cid,
                        "verdict": "pass",
                        "confidence": 0.5,
                        "evidence": "mock adapter — no real review performed",
                    }
                )
        return json.dumps({"verdicts": verdicts})


PROVIDERS: dict[str, type[ModelAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "google": GoogleAdapter,
    "mock": MockAdapter,
}


def load_adapters(config: dict) -> list[ModelAdapter]:
    template_version = config.get("prompt_template_version", "0")
    adapters: list[ModelAdapter] = []
    for entry in config.get("models", []):
        if not entry.get("enabled", False):
            continue
        provider = entry["provider"]
        cls = PROVIDERS.get(provider)
        if cls is None:
            raise ValueError(f"unknown provider '{provider}' in config.yaml")
        adapters.append(cls(entry["id"], entry["model"], template_version))
    if not adapters:
        raise ValueError("no enabled models in config.yaml")
    return adapters


def env_status() -> dict[str, bool]:
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "google": bool(
            os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        ),
    }

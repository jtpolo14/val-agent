# Methodology

This document captures the design methodology that shaped val-agent. It is the
"why" behind the code — if the implementation drifts, the goals on this page
are what a reviewer should hold it against.

## Guiding principle

> For audit-worthiness, the core pattern is **deterministic, versioned, immutable**.

Hash the uploaded file (SHA-256), pin model IDs and set `temperature=0`, version
the prompt and rubric, force each model to return structured JSON against that
rubric, and append every input/output (file hash, model version, prompt
version, raw response, parsed verdict, timestamp) to a tamper-evident log.

Run three or more models independently and record agreement/disagreement
rather than collapsing to a single answer — **divergence is itself an audit
signal**. Map the rubric to a recognized framework (NIST AI RMF or
ISO/IEC 42001) so reviewers see a named standard, not a bespoke checklist.

## Main tradeoff

Stricter determinism (`temperature=0`, rigid schema) trades some model nuance
for reproducibility. For audit contexts this is usually the right call, but it
is worth confirming for the specific domain: if the rubric requires open-ended
judgment (legal reasoning, creative fit), rigid JSON may under-serve the task.
In that case relax the schema on individual criteria rather than the system as
a whole, and log the relaxation explicitly.

## The three pillars

### 1. Deterministic

- `temperature=0` on every adapter where the provider still accepts it;
  providers that have deprecated the parameter are called without it and that
  fact is recorded (the prompt and model id are still pinned).
- Model IDs pinned to specific versions in `config.yaml`, not "latest".
- Prompt template versioned (`prompt_template_version` in `config.yaml`) and
  written into every row of the audit log.
- Structured JSON output: each model must return one verdict per criterion in
  a fixed shape. Non-conforming responses are coerced to `missing` so a broken
  response cannot masquerade as `pass`.

### 2. Versioned

- `rubric.yaml` carries `id` and `version` and names the standard it maps to
  (NIST AI RMF by default; swap for ISO/IEC 42001, SOC 2 CC, or a
  domain-specific framework).
- Each criterion carries a `standard_ref` pointing at the named clause in the
  framework so a reviewer can trace verdict → rubric item → standard.
- Rubric file bytes are hashed on every request and the SHA-256 is stored per
  submission. If the rubric file changes, new submissions record the new hash;
  old submissions still prove what rubric judged them.

### 3. Immutable (tamper-evident)

- Every model run is appended to a `validations` table. Each row stores
  `prev_chain_hash` and a freshly computed `chain_hash` over the canonical
  JSON of the result (adapter id, provider, model, prompt template version,
  verdicts, SHA-256 of the raw response).
- Editing or deleting any row breaks the chain at that point and every row
  after it. `GET /audit/verify` recomputes the chain from genesis and reports
  the first row that does not match.
- The raw model response is stored verbatim alongside the parsed verdicts so
  a reviewer can re-parse and check for interpretation drift.

## Multi-model consensus

Three or more independent models is the sweet spot:

- **One model** — no cross-check. A single hallucination passes silently.
- **Two models** — disagreement is detectable but not resolvable. Every
  disagreement becomes a human-review task.
- **Three+ models** — majority voting becomes meaningful; the rate of
  non-unanimous rulings is itself a health metric on the rubric.

Consensus logic never averages confidences or collapses disagreement. Each
criterion preserves every model's vote, confidence, and evidence. The
`unanimous` flag and the per-submission `any_disagreement` flag surface
divergence so a reviewer can focus attention where models disagreed.

## Mapping to external frameworks

| Framework | Where it shows up in val-agent |
|---|---|
| **NIST AI RMF (AI 100-1)** | Default `rubric.yaml` maps each criterion to a GOVERN/MEASURE/MANAGE function. |
| **ISO/IEC 42001** | Swap the `standard` block in `rubric.yaml`; criteria should reference clauses 6–9 (planning, support, operation, performance evaluation). |
| **SOC 2 (CC series)** | Use when the concern is control evidence rather than content quality; add CC7.x references to each criterion. |
| **Domain-specific (HIPAA, FINRA, GDPR Art. 22)** | Replace the generic criteria with domain checks; keep the same schema so the audit plumbing is unchanged. |

Reviewers should be able to walk from an audit row to a criterion to a named
clause in a framework they already recognize. That chain is the argument for
"we followed a structured standard."

## What this methodology deliberately does not do

- It does not prove the models are correct. It proves the system asked them
  the same question, recorded their answers verbatim, and stored the answers
  immutably.
- It does not replace human review. It narrows human attention to the rows
  where models disagreed or flagged failures.
- It does not defend against a compromised service host. For that, export the
  audit log off-box to WORM storage and sign the chain tip with an external
  key on a schedule.

## Minimum bar for "audit-worthy"

A submission is audit-worthy when, given only the submission id, a reviewer
can recover:

1. The exact bytes that were uploaded (via file SHA-256 + archived original).
2. The exact rubric version that judged them (via rubric SHA-256).
3. The exact prompt template that was sent (via `prompt_template_version`).
4. The exact model version(s) that responded (via pinned model ids).
5. Every model's verbatim response.
6. Every model's parsed verdict and the consensus that was reported.
7. Proof that none of the above has been altered since recording (via
   `GET /audit/verify`).

All seven are enforced by the current implementation. If any one of them
becomes impossible to reconstruct, the system has fallen out of compliance
with this methodology.

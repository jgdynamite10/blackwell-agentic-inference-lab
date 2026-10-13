# D-0035: Complete-catalog qualification and the Akamai baseline

Status: study-entry aggregate is 120 of 200 (0.60) under D-0036. A live
W7 freeze preserved before that decision remains a failed study entry
under the prior 0.70 rule. No qualified baseline has been executed.
Passing source tests does not establish that a later freeze will pass.

## The versioned change

W7 is a standalone candidate on workload **2.9.0**, using the existing
`workflow-controller-v3` support matcher and no `evidence-refs` treatment.
It publishes hypotheses for all **ten public templates and thirty candidate
IDs**. W5/W6 remain development-only, including their holdout/freeze refusal.
Historical W1–W6 identity-closure files stay unchanged. The six development
metadata blocks retain their previous compiled content; the new coverage
does not tune those claims from the official W5/W6 failures.
W7 repairs coverage and runner bindings with the same agent policy; it
does not establish a model-quality gain. Live freeze may still fail the
0.60 aggregate (120 of 200) or 0.40 per-scenario floor. Decision D-0036
lowered only the aggregate count. A live freeze preserved before that
decision remains a failed study entry under the 0.70 rule then in force.

| Binding | W7 contract |
| --- | --- |
| Evidence contract | 2.0.0, `schemas/evidence-contract-v2.schema.json` |
| Public metadata | 2.0.0, `workload/public_metadata/v2/` |
| Finding and hypothesis grammar | Existing public-evidence schema 1.0.0 |
| Normalization policy | 2.0.0 |
| Controller | `workflow-controller-v3`; support algorithm unchanged |
| System prompt | Existing `SYSTEM_PROMPT_V241` bytes |
| Generation | Temperature 0.2, top-p 0.95, 1,024 max tokens, model seed `20260906`, reasoning enabled |
| Serving and infrastructure | Existing pinned model, BF16, vLLM 0.27.1/container, one Blackwell GPU plus firewall, provider-native, `ca-central` |
| Execution limits | 12 turns, one native model call per turn, concurrency 1 for qualification, `RETRIES=0` |
| Evaluation scope | `public-catalog-pre-exposed-holdout`; `blind_generalization_evidence=false` |

The W7 identity binds the evidence schema, compiled public metadata,
normalization policy, controller closure, renderer closure, system prompt,
and native tool contract. Recompute it from the final reviewed source. Do
not assign a proposed digest or reuse the W5/W6 identity in a W7 config.

The new v2 renderer compiles the identical JSON schema once per schema
content rather than once per tool call. Schema validation and matching
behavior stay the same; source identity and CPU overhead change. This
renderer behavior is part of W7's new measured surface, not a performance
improvement inferred from a live result.

The qualified baseline uses W7's **1,024-token cap for both interactive
and batch-heavy** cells. This is an opt-in, versioned change to the
measurement instrument: the historical batch-heavy profile/MVL path
retains its 4,096-token cap, and MVL-F stays diagnostic. Context, returned
log/metric windows, timeouts, SLOs, three cells, and sample counts remain
unchanged. Do not relabel historical observations as this instrument.

The controller checks the hypothesis selected by the model. Eligible
findings must cover every declared assertion with the matching subject,
scope, condition, state, polarity, certainty, witness type, and role.
Mentions, denied assertions, ambiguity, and symptoms alone remain
insufficient. The controller does not select a diagnosis. Pattern coverage
does not establish a unique cause; the unchanged evaluator still decides
whether the diagnosis, remediation, and investigation are correct.

## Exposure and the benchmark task

New annotation authoring inspected public candidate IDs, raw logs, and
returned health fields of `elevated-latency-001`, `pod-failures-001`,
`storage-latency-001`, and `rate-limiting-001`. These four public templates
cannot support a claim of blind generalization. Earlier answer-bearing
exposure of two templates remains recorded in
[D-0034 holdout exposure](d-0034-holdout-exposure.md).

Annotation rules and claims are authored from public fields, not accepted
diagnoses or evaluator predicates. Validation tests may consult the
existing evaluator and accepted answers **after authoring** to check
reachability and rejection. That does not assert that answer-bearing
material has never been visible. Runtime matching and public rendering
must not read correctness labels. Official qualification and holdout
outcomes must not be used to revise candidate wording.

W7 exposes declared hypothesis patterns and structured findings to the
agent. The task becomes investigating and selecting among explicit public
hypotheses while meeting the unchanged raw-evidence and correctness
requirements. Report W7 under its new workload and identity. Do not pool
its observations with W1–W6 or describe this catalog as an unseen set.

## Finish criteria

The bounded target is **one qualified Akamai baseline**, not completion of
the three-provider charter. AWS, GCP, NVFP4, TensorRT-LLM, NIM,
controlled-resource comparison, and Dynamo remain later work.

| Step | Frozen schedule | Required result |
| --- | --- | --- |
| Diagnostic canary | 10 development-only tasks; schedule seed `20261007` | At least 4 passes; diagnostic only |
| Development | 20 tasks; no warm-up | At least 8 passes, quality 0.40 |
| Holdout-labelled public split | 20 tasks; no warm-up | At least 10 passes, quality 0.50 |
| Freeze / study entry | 200 balanced measured tasks plus one separate 200-task warm-up | At least 120 measured passes, quality 0.60 overall, at least 0.40 each scenario, and all structural gates |
| Qualified baseline | Interactive/1, batch-heavy/4, batch-heavy/8 | Each cell: one 200-task warm-up plus three measured repetitions of 200; 1,800 measured observations total |
| Session closure | Immutable external archive and exact recorded resources | Artifacts verified, deletion confirmed, orphan report clear |

Freeze retains valid-native-call rate at least 0.99, invalid-tool-name
rate zero, invalid-argument and request-inference-error rates at most
0.01 each, zero timeouts, interactive TTFT p95 at most 2,500 ms,
interactive task latency p95 at most 60,000 ms, and verified provenance
and artifacts. Passing development does not establish freeze success.
The separate production-like target stays 0.90 overall / 0.80 per
scenario; it is reported separately rather than substituted for study entry.

W7 reads persisted TTFT from the existing per-turn fields using the same
per-task minimum used by the existing in-memory metric path. This fixes
W7 persisted-record extraction only; thresholds and historical metrics
stay unchanged. Unavailable timing does not establish a passing gate.

Stop after the first failed stage. Do not rerun official qualification to
select a favorable result, alter floors, select previously passing tasks,
or revise hypotheses using holdout or official qualification outcomes.
Preserve a failed attempt and its artifacts as a failed attempt.

## Cursor local runbook

Offline implementation does not authorize new billable resources or
inference. [AGENTS.md](../AGENTS.md) sections 1, 3, and 7 require exact
local owner approvals for the new plan and each live config. The former
W5/W6 resources were deleted; their approvals, ledger, and control cannot
authorize this session. Every variable below must be populated from
reviewed source or a real local approval, not inferred from this document.

### 1. Reviewed source and external environment

After review and required CI, fetch the approved implementation commit.
Set `BWLAB_APPROVED_COMMIT` to that actual SHA. Use a clean pinned checkout
on the workstation and GPU host. No future SHA is assigned here.

```bash
git fetch origin
git switch --detach "$BWLAB_APPROVED_COMMIT"
git status --porcelain
export BWLAB_QUAL_ENV_DIR="$HOME/.local/share/blackwell-lab/qual-env-d0035"
export BWLAB_QUAL_PYTHON=python3.12
scripts/bootstrap_qual_env.sh
BWLAB_CLI="$BWLAB_QUAL_ENV_DIR/bin/blackwell-cloud"
"$BWLAB_CLI" readiness
```

Any status output is a dirty-tree blocker. `LAB_RESULTS_DIR` must resolve
outside the checkout. Recompute W1–W7 identities and the W7 evidence
contract; confirm W1–W6 match their published values. Capture the valid
environment marker, pinned packages, and RFC 3339 format checker report.

### 2. Fresh resource session on the workstation

Set `BWLAB_NEW_RUN_TAG` to an unused tag, for example
`w7-akamai-20261012a` only if never initialized. Never recycle an aborted,
empty, completed, or deleted tag. Use existing local authentication for
a sanitized read-only check of entitlement, `ca-central`, this exact GPU
plan, and current price. Advertised availability is not capacity proof;
there is no fallback region, automatic retry, or alternate plan.

Prepare external Terraform inputs for exactly the pinned GPU instance
and firewall, approved operator SSH key/management CIDR, and six-hour TTL.
Keep inputs, plans, state, and provider facts outside Git. Estimate spend
from the fresh quote and retain the six-hour / $25 ceiling.

```bash
"$BWLAB_CLI" init --run-tag "$BWLAB_NEW_RUN_TAG"
"$BWLAB_CLI" plan --run-tag "$BWLAB_NEW_RUN_TAG"
```

Return the saved plan's sanitized scope, actual SHA-256, freshness, spend,
and exact phrase for owner review. After the owner supplies that phrase:

```bash
"$BWLAB_CLI" apply --run-tag "$BWLAB_NEW_RUN_TAG" --approve "$BWLAB_OWNER_APPLY_APPROVAL"
"$BWLAB_CLI" reconcile --run-tag "$BWLAB_NEW_RUN_TAG"
```

Require both typed resource identities in `ca-central`, no missing or
untracked resources, and clean reconciliation. Prepare an identity-verified
teardown plan early. Power-off and the idle watchdog do not stop billing.

### 3. One canary, then sequential qualification on the GPU host

Use the existing pinned bootstrap. Verify model aggregate, container,
driver/CUDA/GPU facts, and loopback health. Transfer the approved checkout
and new external configs, then bootstrap its isolated environment as in
step 1. Execute on the GPU host: laptop loopback is not the GPU server,
and an SSH tunnel alone does not supply host-local provenance.

Keep the original execution ledger snapshot read-only and separate from
the mutable operator ledger. Retain the host's evolving private session
record; freeze it in the archive after execution. Do not alter archived
hashes after reconciliation. Prepare canary, development, holdout, and freeze
configs bound to the actual candidate identity, evidence contract, commit, pins,
generation, and this session. D-0037 uses candidate W8 and workload 2.10.0.
D-0038 uses candidate W9 and workload 2.11.0;
the W7 commands below remain the historical contract. Use the implementation's frozen schedules.

```bash
"$BWLAB_CLI" canary-agent --run-tag "$BWLAB_NEW_RUN_TAG" \
  --run-label "$BWLAB_CANARY_LABEL" --candidate W7 \
  --config "$BWLAB_CANARY_CONFIG" --validate-only
```

Require environment `ok: true`, expected source/identity/contract, ten
development-only tasks, and no client or endpoint contact. Return the hash
of the actual config bytes and exact phrase. After separate owner approval:

```bash
"$BWLAB_CLI" canary-agent --run-tag "$BWLAB_NEW_RUN_TAG" \
  --run-label "$BWLAB_CANARY_LABEL" --candidate W7 \
  --config "$BWLAB_CANARY_CONFIG" --approve "$BWLAB_OWNER_CANARY_APPROVAL"
```

After it passes, run **development, then holdout, then freeze**, one at a
time. For each stage set `BWLAB_STAGE`, `BWLAB_STAGE_LABEL`, and
`BWLAB_STAGE_CONFIG` to its reviewed config. Validate, return the actual
digest/phrase, obtain that stage's separate approval, and execute once.
These are checkpoints, not an unattended loop or authorization to start
the next stage automatically.

```bash
"$BWLAB_CLI" qualify-agent --run-tag "$BWLAB_NEW_RUN_TAG" \
  --run-label "$BWLAB_STAGE_LABEL" --candidate W7 --stage "$BWLAB_STAGE" \
  --config "$BWLAB_STAGE_CONFIG" --validate-only

"$BWLAB_CLI" qualify-agent --run-tag "$BWLAB_NEW_RUN_TAG" \
  --run-label "$BWLAB_STAGE_LABEL" --candidate W7 --stage "$BWLAB_STAGE" \
  --config "$BWLAB_STAGE_CONFIG" --approve "$BWLAB_OWNER_STAGE_APPROVAL"
```

Verify each persisted result and receipt before proceeding. W7 is
standalone; a W5/W6 control cannot replace its gates.
Each W7 qualification receipt binds the SHA-256 of every result, manifest,
measured-observation, and warm-up artifact present in that stage, plus the
run tag, canonical commit, and stable resource-identity digest. Harmless
operator-ledger byte updates do not change that resource-session identity;
different resources or session do. Preserve the original ledger hash for
audit without replacing the active operator ledger.

### 4. Baseline bound to the successful freeze

Generate the external baseline config only after the bound freeze passes.
Set workflow `qualified-baseline`, candidate `W7`, `W8`, or `W9`, the matching
workload (`2.9.0`, `2.10.0`, or `2.11.0`), that candidate's generation and evidence
contract, the approved commit, and this session. Its
`qualification_binding` has exactly two fields: `run_label` (the freeze
command's original, unsuffixed label) and `receipt_sha256` (the actual
hash of that exact persisted freeze receipt).

The command authenticates that receipt, complete artifact hashes,
observations, re-evaluated gates, source binding, and resource session
before a client exists. The historical `mvl-baseline` stays pinned to
workload 2.3.0 and is not this command. Validate-only needs the real
successful freeze artifacts and reconciled ledger:

The generated run specification must carry all six proof fields:
`candidate_id`, `candidate_identity_sha256`, `receipt_sha256`,
`result_sha256`, `manifest_sha256`, and `study_entry_passed`. A direct
specification with only a passing boolean is refused. This generated proof
is distinct from the external config's two-field `qualification_binding`.

```bash
"$BWLAB_CLI" qualified-baseline --run-tag "$BWLAB_NEW_RUN_TAG" \
  --run-label "$BWLAB_BASELINE_LABEL" --config "$BWLAB_BASELINE_CONFIG" \
  --validate-only
```

Review the actual config digest, qualification proof, counts, and remaining
budget. Execution requires a real, timezone-aware provisioning timestamp,
refuses a projection beyond the remaining six-hour approved session, and
clamps every model-request deadline to its remaining time. The six-hour
clock starts at provisioning, not at benchmark launch. After separate
owner approval of this baseline config:

```bash
"$BWLAB_CLI" qualified-baseline --run-tag "$BWLAB_NEW_RUN_TAG" \
  --run-label "$BWLAB_BASELINE_LABEL" --config "$BWLAB_BASELINE_CONFIG" \
  --approve "$BWLAB_OWNER_BASELINE_APPROVAL"
```

### 5. Verify, archive, and stop billing

Verify `canary-runs`, `qualification-runs`, and `qualified-baseline-runs`
separately, without `--allow-empty`. For example:

```bash
"$BWLAB_CLI" verify-results --subdirectory qualification-runs
"$BWLAB_CLI" verify-results --subdirectory qualified-baseline-runs
```

Archive configs, original ledger/session, observations, manifests, results,
receipts, failure records, and hashes outside Git before deletion. Verify
the archive under its preserved execution context without replacing the
operator ledger. On the workstation, generate a fresh identity-verified
destroy plan, review its exact targets/digest/freshness, and obtain the
owner's exact deletion phrase:

```bash
"$BWLAB_CLI" teardown-plan --run-tag "$BWLAB_NEW_RUN_TAG"
"$BWLAB_CLI" destroy --run-tag "$BWLAB_NEW_RUN_TAG" --approve "$BWLAB_OWNER_DESTROY_APPROVAL"
"$BWLAB_CLI" orphan-report --run-tag "$BWLAB_NEW_RUN_TAG"
```

Completion requires deletion confirmation, not just destroy exit status.
Report latency, throughput, quality, successful-completion economics, and
limitations from verified observations. Keep p50/p95 primary; p99 stays
exploratory/suppressed below its sample requirement. Genuine results and
infrastructure facts stay private. Publishing exact sanitized files is a
separate owner decision.

## Acceptance tests for review

- Full metadata coverage, IDs/order, grounding, compiled-overlay equality,
  unchanged six development blocks, and no runtime correctness-label reads.
- Supported patterns pass; mentions, symptom-only evidence, denial,
  ambiguity, wrong subject/scope/state, and incomplete witness sets fail.
  All ten templates have an evaluator-valid path within twelve turns.
- W1–W6 identities/closure bytes stay unchanged; W5/W6 still refuse
  holdout/freeze. Missing or mutated W7 contract bindings are refused.
- Persisted W7 TTFT agrees with the existing object path; absent or
  over-limit timing fails the applicable gate. Historical metrics and
  threshold constants stay unchanged.
- Frozen canary/stage schedules and structural gates remain enforced;
  validate-only constructs no client and contacts no inference endpoint.
- Qualified baseline refuses absent/failed/tampered freeze, missing
  observations, wrong candidate/identity/commit/contract/generation/session,
  or receipt digest before client creation. A verified successful freeze
  admits exactly three cells and the declared sample counts. Receipts bind
  every artifact; ledger byte updates preserve the stable resource proof,
  while different resources do not. Direct specs require all six fields.
- Both new measurement profiles execute and record a 1,024-token cap;
  historical batch-heavy behavior stays 4,096. Cached v2 schema validation
  accepts/rejects the same payloads and invalidates on changed content.
  Absent provisioning timestamps and expired session budgets are refused;
  model-request deadlines are clamped to the remaining approved session.
- Private custody, six-hour / $25 budget, exact saved-plan approvals,
  freshness, reconciliation, confirmed deletion, and orphan checks remain.
  Ruff, schemas, affected tests, required CI, and existing Python 3.10/3.12
  suites pass before release.

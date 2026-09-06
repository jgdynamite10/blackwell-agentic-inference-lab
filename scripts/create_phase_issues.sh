#!/usr/bin/env bash
# Create GitHub milestones and tracking issues for Phases 2-8 in the
# canonical repository only. It writes nothing anywhere else and includes no
# credentials, account identifiers, or benchmark data.
#
# The Phase 1 hosted working environment had a READ-ONLY GitHub CLI, so these
# could not be created automatically. Run this once LOCALLY from a machine
# where `gh` has write access to the repository:
#
#   ./scripts/create_phase_issues.sh
#
# Idempotency: safe to re-run. Milestones and issues are looked up by exact
# title first and skipped if they already exist (closed issues with matching
# titles also count as existing and are not recreated).
set -euo pipefail

REPO="jgdynamite10/blackwell-agentic-inference-lab"

declare -a TITLES=(
  "Phase 2: Synthetic workload and evaluator"
  "Phase 3: Akamai Cloud baseline"
  "Phase 4: NVIDIA optimization"
  "Phase 5: Google Cloud portability"
  "Phase 6: AWS portability"
  "Phase 7: Analysis and report preparation"
  "Phase 8: Optional Dynamo extension"
)

declare -a BODIES=(
"Implement the Cloud Operations Agent, simulated tools, deterministic incident scenarios, task-success evaluator, and local benchmark runner without requiring a GPU. Scope: docs/roadmap.md (Phase 2). Requires explicit owner authorization before work begins (AGENTS.md §7)."
"Run one validated model and serving configuration on one Akamai RTX PRO 6000 Blackwell GPU using two workload profiles and concurrency levels 1, 4, and 8; freeze the baseline. Scope: docs/roadmap.md (Phase 3). Requires explicit owner authorization and per-instance provisioning approval (AGENTS.md §1, §7)."
"Add approved precision (BF16/NVFP4) and serving-path (vLLM/TensorRT-LLM/NIM) comparisons, operational telemetry (DCGM, Prometheus, Grafana), and selected Nsight profiling. Scope: docs/roadmap.md (Phase 4). Requires explicit owner authorization (AGENTS.md §7)."
"Reproduce the frozen baseline on a Google Cloud G4 single-GPU configuration (g4-standard-48) while documenting every material environmental difference. Scope: docs/roadmap.md (Phase 5). Requires explicit owner authorization (AGENTS.md §7)."
"Reproduce the frozen baseline on a single-GPU Amazon EC2 G7e configuration (size selected during feasibility analysis) while documenting every material environmental difference. Scope: docs/roadmap.md (Phase 6). AWS mutation is prohibited until this phase is explicitly authorized (AGENTS.md §1.5)."
"Calculate outcome-level performance and economic measures across Akamai Cloud, Google Cloud, and AWS; document limitations; sanitize approved results; prepare the technical report. Scope: docs/roadmap.md (Phase 7). Genuine results remain external and private; every result release requires separate explicit owner approval identifying the exact files and scope, per docs/publication-governance.md."
"Investigate multi-GPU or disaggregated prefill/decode serving with NVIDIA Dynamo only if single-GPU findings justify the additional complexity and cost. Scope: docs/roadmap.md (Phase 8). Optional; may be skipped. Requires explicit owner authorization (AGENTS.md §7)."
)

milestone_exists() {
  gh api "repos/${REPO}/milestones?state=all" --paginate \
    --jq '.[].title' 2>/dev/null | grep -Fxq "$1"
}

issue_exists() {
  gh issue list --repo "${REPO}" --state all --limit 200 \
    --json title --jq '.[].title' 2>/dev/null | grep -Fxq "$1"
}

for i in "${!TITLES[@]}"; do
  title="${TITLES[$i]}"
  body="${BODIES[$i]}"

  if milestone_exists "${title}"; then
    echo "Milestone exists, skipping: ${title}"
  else
    echo "Creating milestone: ${title}"
    gh api "repos/${REPO}/milestones" -f "title=${title}" \
      -f "description=${body}" >/dev/null
  fi

  if issue_exists "${title}"; then
    echo "Issue exists, skipping: ${title}"
  else
    echo "Creating issue: ${title}"
    gh issue create --repo "${REPO}" --title "${title}" --body "${body}" \
      --milestone "${title}" \
      || gh issue create --repo "${REPO}" --title "${title}" --body "${body}"
  fi
done

echo "Done. Review milestones and issues in the GitHub UI."

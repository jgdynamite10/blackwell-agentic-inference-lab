#!/usr/bin/env bash
# Create GitHub milestones and tracking issues for Phases 2-8.
#
# The Phase 1 working environment had a READ-ONLY GitHub CLI, so these could
# not be created automatically. Run this once from a machine where `gh` has
# write access to the repository:
#
#   ./scripts/create_phase_issues.sh
#
# Idempotency: milestone creation fails harmlessly if the milestone exists;
# delete duplicate issues manually if you run this twice.
set -euo pipefail

REPO="jgdynamite10/blackwell-agentic-inference-lab"

declare -a TITLES=(
  "Phase 2: Synthetic workload and evaluator"
  "Phase 3: Akamai Cloud baseline"
  "Phase 4: NVIDIA optimization"
  "Phase 5: Google Cloud portability"
  "Phase 6: AWS portability"
  "Phase 7: Analysis and publication"
  "Phase 8: Optional Dynamo extension"
)

declare -a BODIES=(
"Implement the Cloud Operations Agent, simulated tools, deterministic incident scenarios, task-success evaluator, and local benchmark runner without requiring a GPU. Scope: docs/roadmap.md (Phase 2). Requires explicit owner authorization before work begins (AGENTS.md §5)."
"Run one validated model and serving configuration on one Akamai RTX PRO 6000 Blackwell GPU using two workload profiles and concurrency levels 1, 4, and 8; freeze the baseline. Scope: docs/roadmap.md (Phase 3). Requires explicit owner authorization and per-instance provisioning approval (AGENTS.md §1, §5)."
"Add approved precision (BF16/NVFP4) and serving-path (vLLM/TensorRT-LLM/NIM) comparisons, operational telemetry (DCGM, Prometheus, Grafana), and selected Nsight profiling. Scope: docs/roadmap.md (Phase 4). Requires explicit owner authorization (AGENTS.md §5)."
"Reproduce the frozen baseline on a Google Cloud G4 single-GPU configuration (g4-standard-48) while documenting every material environmental difference. Scope: docs/roadmap.md (Phase 5). Requires explicit owner authorization (AGENTS.md §5)."
"Reproduce the frozen baseline on a single-GPU Amazon EC2 G7e configuration (size selected during feasibility analysis) while documenting every material environmental difference. Scope: docs/roadmap.md (Phase 6). AWS mutation is prohibited until this phase is explicitly authorized (AGENTS.md §1.5)."
"Calculate outcome-level performance and economic measures across Akamai Cloud, Google Cloud, and AWS; document limitations; sanitize approved results; prepare the public technical report. Scope: docs/roadmap.md (Phase 7). Publication requires the process in docs/publication-governance.md."
"Investigate multi-GPU or disaggregated prefill/decode serving with NVIDIA Dynamo only if single-GPU findings justify the additional complexity and cost. Scope: docs/roadmap.md (Phase 8). Optional; may be skipped."
)

for i in "${!TITLES[@]}"; do
  title="${TITLES[$i]}"
  body="${BODIES[$i]}"
  echo "Creating milestone: ${title}"
  gh api "repos/${REPO}/milestones" -f "title=${title}" \
    -f "description=${body}" >/dev/null || echo "  (milestone may already exist)"
  echo "Creating issue: ${title}"
  gh issue create --repo "${REPO}" --title "${title}" --body "${body}" \
    --milestone "${title}" || gh issue create --repo "${REPO}" \
    --title "${title}" --body "${body}"
done

echo "Done. Review milestones and issues in the GitHub UI."

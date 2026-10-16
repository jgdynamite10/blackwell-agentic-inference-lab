#!/usr/bin/env python3
"""One AWS NIM BF16 cell on the already-running D-0047 host.

Workload 2.13.0, workflow-controller-v3, interactive, concurrency 1,
200 measured tasks, and one separate 200-task warmup. Generation is
temperature 0.2, top_p 0.95, seed 20260906. The schedule seed is
20261028. This is not qualify-agent.

The served model name is the id the server lists. Results stay under
the directory passed as the only argument, which must be outside the
repository.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from blackwell_lab.cloud import realbench
from blackwell_lab.cloud.telemetry import (
    GpuSamplerThread,
    collect_gpu_facts,
    collect_host_facts,
    observe_container_cuda_version,
)
from blackwell_lab.engines.components.nim import (
    ADOPTED_CONTAINER_DIGEST,
    ADOPTED_MODEL_ARTIFACT_HASH,
    ADOPTED_MODEL_REVISION,
    ADOPTED_RUNTIME_PROFILE_ID,
    SUPPORTED_MODEL_ARTIFACT,
)
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.scenarios import catalog

_SCHEDULE_SEED = 20261028
_RUN_LABEL = "aws-nim-20261016a"
_INSTANCE_TYPE = "g7e.4xlarge"
_LIST_PRICE = 3.99816
_PRICE_DATE = "2026-10-14"


def _served_model_name() -> str:
    with urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=30) as response:
        payload = json.load(response)
    ids = [item["id"] for item in payload.get("data", []) if item.get("id")]
    if len(ids) != 1:
        raise SystemExit(f"expected one served model, found {len(ids)}")
    return ids[0]


def _component_passes(document: dict) -> dict[str, int]:
    counts = {"diagnosis": 0, "remediation": 0, "evidence": 0}
    keys = {
        "diagnosis": "diagnosis_component",
        "remediation": "remediation_component",
        "evidence": "evidence_component",
    }
    for row in document["observations"]:
        evaluation = row["evaluation"]
        for name, field in keys.items():
            if evaluation[field] == 1:
                counts[name] += 1
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: aws_nim_bf16_cell.py RESULTS_DIR", file=sys.stderr)
        return 2
    results = Path(argv[1]).resolve()
    served = _served_model_name()
    print(json.dumps({"served_model": served, "runtime_profile": ADOPTED_RUNTIME_PROFILE_ID}))
    spec = realbench.RealRunSpec(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type=_INSTANCE_TYPE,
        region="us-east-1",
        list_price_usd_per_hour=_LIST_PRICE,
        price_source_date=_PRICE_DATE,
        provider="aws",
        model={
            "artifact": SUPPORTED_MODEL_ARTIFACT,
            "revision": ADOPTED_MODEL_REVISION,
            "artifact_hash": ADOPTED_MODEL_ARTIFACT_HASH,
            "precision": "bf16",
        },
        engine="nim",
        engine_version="2.0.13",
        container_digest=ADOPTED_CONTAINER_DIGEST,
        container_cuda_runtime_version=observe_container_cuda_version("bwlab-nim"),
        repetitions=1,
        warmup_passes=1,
        tasks_per_repetition=200,
        seed=_SCHEDULE_SEED,
        run_label=_RUN_LABEL,
        generation=GenerationSettings(
            temperature=0.2,
            top_p=0.95,
            max_tokens=1024,
            seed=20260906,
            reasoning_mode=True,
            workload_version="2.13.0",
        ),
        template_ids=tuple(catalog()),
        artifact_family="real-runs",
        workload_version="2.13.0",
        controller="workflow-controller-v3",
    )
    host = {
        **collect_host_facts(
            storage_description="400 GB gp3 root volume",
            network_description="default VPC, no inbound",
            virtualization="nitro",
        ),
        **collect_gpu_facts(),
    }
    records = realbench.run_real_cell(
        spec,
        OpenAICompatibleClient("http://127.0.0.1:8000/v1", served),
        host=host,
        sampler_factory=GpuSamplerThread,
        results_dir=results,
    )
    result = records[0].result
    measured_name = result["observations"]["measured_file"]
    measured = json.loads((results / "real-runs" / _RUN_LABEL / measured_name).read_text())
    tasks = result["tasks"]
    summary = {
        "diagnostic": True,
        "protocol": "w11-freeze-shape",
        "provider": "aws",
        "precision": "bf16",
        "engine": "nim",
        "served_model": served,
        "runtime_profile": ADOPTED_RUNTIME_PROFILE_ID,
        "workload": "2.13.0",
        "profile": "interactive",
        "concurrency": 1,
        "measured_tasks": tasks["attempted"],
        "warmup_tasks": result["observations"]["warmup_count"],
        "succeeded": tasks["succeeded"],
        "quality_failed": tasks["quality_failed"],
        "errored": tasks["errored"],
        "mean_quality": tasks["mean_quality_score"],
        "slo_attaining": result["slo"]["slo_attaining_tasks"],
        "component_passes": _component_passes(measured),
        "records": len(records),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

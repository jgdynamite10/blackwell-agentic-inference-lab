#!/usr/bin/env python3
"""D-0046 Akamai vLLM NVFP4 cell, same W11 shape as the BF16 freeze.

One real-runs cell. It is not qualify-agent, not a qualification receipt,
and not the 36-cell Phase 4 matrix. The schedule seed is 20261021.

Results stay under the directory passed as the only argument, which must
be outside the repository. Copy that directory off the host before the
instance is deleted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from blackwell_lab.cloud import realbench
from blackwell_lab.cloud.telemetry import (
    GpuSamplerThread,
    collect_gpu_facts,
    collect_host_facts,
    observe_container_cuda_version,
)
from blackwell_lab.engines.components.vllm_nvfp4 import MODEL_ARTIFACT_HASH
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.scenarios import catalog

_CONTAINER_DIGEST = (
    "docker.io/vllm/vllm-openai@sha256:"
    "c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
)
_SCHEDULE_SEED = 20261021
_RUN_LABEL = "akamai-w11-nvfp4-20261015a"
_SERVED_MODEL_NAME = "nemotron-3.5-lightning-30b-a3b-nvfp4"


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
        print("usage: akamai_w11_nvfp4_cell.py RESULTS_DIR", file=sys.stderr)
        return 2
    results = Path(argv[1]).resolve()
    templates = tuple(catalog())
    spec = realbench.RealRunSpec(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type="g3-gpu-rtxpro6000-blackwell-1",
        region="ca-central",
        list_price_usd_per_hour=3.0,
        price_source_date="2026-09-18",
        provider="akamai",
        model={
            "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4",
            "revision": "bee7596271d1495f6992ae224aefde4410e816b8",
            "artifact_hash": MODEL_ARTIFACT_HASH,
            "precision": "nvfp4",
        },
        engine="vllm",
        engine_version="0.27.1",
        container_digest=_CONTAINER_DIGEST,
        container_cuda_runtime_version=observe_container_cuda_version("bwlab-vllm"),
        repetitions=1,
        warmup_passes=1,
        tasks_per_repetition=200,
        seed=_SCHEDULE_SEED,
        run_label=_RUN_LABEL,
        generation=GenerationSettings(
            temperature=0.2,
            top_p=0.95,
            seed=20260906,
            reasoning_mode=True,
            workload_version="2.13.0",
        ),
        template_ids=templates,
        artifact_family="real-runs",
        workload_version="2.13.0",
        controller="workflow-controller-v3",
    )
    host = {
        **collect_host_facts(
            storage_description="Linode plan root disk, no extra volume",
            network_description="public SSH from the management CIDR only, serving on loopback",
        ),
        **collect_gpu_facts(),
    }
    client = OpenAICompatibleClient(
        "http://127.0.0.1:8000/v1",
        _SERVED_MODEL_NAME,
    )
    records = realbench.run_real_cell(
        spec,
        client,
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
        "precision": "nvfp4",
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

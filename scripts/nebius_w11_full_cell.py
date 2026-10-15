#!/usr/bin/env python3
"""D-0045 Nebius replication of the finished Akamai W11 freeze cell.

This is a real-runs diagnostic on one Nebius RTX PRO 6000. It is not
qualify-agent, not a qualified-baseline receipt, and not the 12-cell
comparative matrix. Qualification stays Akamai-ledger and ca-central gated.

The shape matches the 2026-10-14 Akamai W11 freeze and the D-0044 AWS
cell: workload 2.13.0, workflow-controller-v3, interactive, concurrency 1,
one measured pass of 200 tasks, and one separate 200-task warmup.
Generation is temperature 0.2, top_p 0.95, seed 20260906. The schedule
seed is 20261024. The dirty nebius-w11-20261015a cell used 20261020 and
is not reused. This draw is not the Akamai freeze draw and not the AWS
schedule seed 20261018.

Results stay under the directory passed as the only argument, which must
be outside the repository. Copy that directory off the host before teardown.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from blackwell_lab.cloud import realbench
from blackwell_lab.cloud.telemetry import (
    GpuSamplerThread,
    collect_gpu_facts,
    collect_host_facts,
    observe_container_cuda_version,
)
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.scenarios import catalog

_MODEL_ARTIFACT_HASH = "sha256:5d6a435f3e0faf95dd4a610c66fefd9c4ccbe350af950216dab9fec6f3d3da0d"
_CONTAINER_DIGEST = (
    "docker.io/vllm/vllm-openai@sha256:"
    "c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
)
_SCHEDULE_SEED = 20261024
_RUN_LABEL = "nebius-w11-20261015b"
_INSTANCE_TYPE = "gpu-rtx6000-a:1gpu-24vcpu-218gb"
_LIST_PRICE = 1.80
_PRICE_DATE = "2026-10-15"


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
        print("usage: nebius_w11_full_cell.py RESULTS_DIR", file=sys.stderr)
        return 2
    results = Path(argv[1]).resolve()
    instance_type = os.environ.get("BWLAB_INSTANCE_TYPE", _INSTANCE_TYPE)
    list_price = float(os.environ.get("BWLAB_LIST_PRICE", str(_LIST_PRICE)))
    templates = tuple(catalog())
    spec = realbench.RealRunSpec(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type=instance_type,
        region="eu-south1",
        list_price_usd_per_hour=list_price,
        price_source_date=_PRICE_DATE,
        provider="nebius",
        model={
            "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
            "revision": "a9904d24bcc1d289a1950fa9d2b978c47cf903b9",
            "artifact_hash": _MODEL_ARTIFACT_HASH,
            "precision": "bf16",
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
            storage_description="400 GiB network SSD boot disk",
            network_description="security group with outbound only, plus optional operator SSH",
            virtualization="nebius-compute",
        ),
        **collect_gpu_facts(),
    }
    client = OpenAICompatibleClient(
        "http://127.0.0.1:8000/v1",
        "nemotron-3.5-lightning-30b-a3b-bf16",
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
        "provider": "nebius",
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

#!/usr/bin/env python3
"""D-0043 private W11 diagnostic. Not a qualification or a comparison.

Runs on the GPU host, against loopback serving only. Ten public-catalog
tasks, workload 2.13.0, concurrency 1. The instance type and list price
come from the environment so a capacity substitution stays honest.
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
)
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.scenarios import catalog


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: aws_w11_diagnostic_cell.py RESULTS_DIR", file=sys.stderr)
        return 2
    results = Path(argv[1]).resolve()
    instance_type = os.environ["BWLAB_INSTANCE_TYPE"]
    list_price = float(os.environ["BWLAB_LIST_PRICE"])
    templates = tuple(catalog())
    spec = realbench.RealRunSpec(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type=instance_type,
        region="us-east-1",
        list_price_usd_per_hour=list_price,
        price_source_date="2026-10-14",
        model={
            "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
            "revision": "a9904d24bcc1d289a1950fa9d2b978c47cf903b9",
            "artifact_hash": (
                "sha256:5d6a435f3e0faf95dd4a610c66fefd9c4ccbe350af950216dab9fec6f3d3da0d"
            ),
            "precision": "bf16",
        },
        engine="vllm",
        engine_version="0.27.1",
        container_digest=(
            "docker.io/vllm/vllm-openai@sha256:"
            "c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
        ),
        container_cuda_runtime_version="13.0",
        repetitions=1,
        warmup_passes=0,
        tasks_per_repetition=10,
        seed=20261016,
        run_label="aws-w11-20261014a",
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
            storage_description="400 GB gp3 root volume",
            network_description="default VPC, no inbound",
            virtualization="nitro",
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
    print(json.dumps({"diagnostic": True, "tasks": 10, "records": len(records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

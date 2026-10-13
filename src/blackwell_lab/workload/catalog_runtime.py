"""Additive workload 2.9.0 dispatch; historical evidence closures stay intact."""

from __future__ import annotations

from blackwell_lab.workload import evidence
from blackwell_lab.workload.public_metadata import catalog_v2
from blackwell_lab.workload.tools import SimulatedToolbox, ToolResult
from blackwell_lab.workload.validation import ConfigError

WORKLOAD_VERSION = "2.9.0"
CATALOG_WORKLOAD_VERSIONS = frozenset({WORKLOAD_VERSION, "2.10.0", "2.11.0"})


def controller_for_workload(workload_version: str | None) -> str | None:
    if workload_version in CATALOG_WORKLOAD_VERSIONS:
        return catalog_v2.CONTROLLER_ID
    return evidence.controller_for_workload(workload_version)


def require_controller_binding(workload_version: str | None, controller: str | None) -> str | None:
    expected = controller_for_workload(workload_version)
    if controller != expected:
        raise ConfigError("workload/controller binding mismatch")
    return expected


def workload_treatments(workload_version: str | None) -> tuple[str, ...]:
    if workload_version in CATALOG_WORKLOAD_VERSIONS:
        return ()
    return evidence.workload_treatments(workload_version)


def manifest_workload_binding(workload_version: str | None) -> dict:
    if workload_version in CATALOG_WORKLOAD_VERSIONS:
        return catalog_v2.manifest_workload_binding(workload_version)
    from blackwell_lab.workload.public_metadata import manifest_workload_binding as historical

    return historical(workload_version)


def require_manifest_binding(workload: dict) -> None:
    if workload.get("version") in CATALOG_WORKLOAD_VERSIONS:
        catalog_v2.require_manifest_binding(workload)
        return
    from blackwell_lab.workload.public_metadata import require_manifest_binding as historical

    historical(workload)


def build_controller(workload_version: str, context: str):
    if workload_version not in CATALOG_WORKLOAD_VERSIONS:
        raise ConfigError("the catalog runtime requires workload 2.9.0, 2.10.0, or 2.11.0")
    from blackwell_lab.workload.workflow_v3 import StructuredEvidenceWorkflowController

    # Validate this catalog's contract, rather than invoking the development
    # factory and its unrelated v1 metadata preflight.
    catalog_v2.require_evidence_contract(catalog_v2.evidence_contract())
    return StructuredEvidenceWorkflowController(context=context, treatments=())


class CatalogToolbox(SimulatedToolbox):
    """Unchanged raw toolbox responses, rendered with the complete catalog."""

    def execute(
        self, name: str, arguments: dict, workload_version: str | None = None
    ) -> ToolResult:
        if workload_version not in CATALOG_WORKLOAD_VERSIONS:
            raise ConfigError("the catalog toolbox requires workload 2.9.0, 2.10.0, or 2.11.0")
        # 2.7.0 has the same terminal argument schema and does not render v1.
        # Avoid invoking the development-only renderer for a holdout template.
        result = super().execute(name, arguments, workload_version="2.7.0")
        return ToolResult(
            tool=result.tool,
            payload=catalog_v2.render_tool_payload(self._scenario, name, result.payload),
            simulated_latency_ms=result.simulated_latency_ms,
        )

"""Compatibility exports for shared CPU flow definitions."""

from flows.model_specs import registry

CpuFlow = registry.model("CpuFlow")
CpuFlowState = registry.namespace["CpuFlowState"]

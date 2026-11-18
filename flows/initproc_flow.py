"""Compatibility exports for the first-process flow defined in InitProcFlow.spec."""

from flows.model_specs import registry

InitProcFlow = registry.model("InitProcFlow")
Privilege = registry.namespace["Privilege"]

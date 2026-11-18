"""Compatibility export for the BP flow defined in BpInitFlow.spec."""

from flows.bp_specs import registry

BpInitFlow = registry.model("BpInitFlow")

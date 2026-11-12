"""Named protection dependencies follow the current global object graph."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from framework.contention import DOMAINS, ContentionVector
from framework.engine import System, TaskLocalEnv, protected_by
from framework.sync_primitives import GuardSpinLock
from global_vars import GlobalVars, gv
from kernel.io import IO


@pytest.fixture
def model() -> Iterator[GlobalVars]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


@pytest.mark.parametrize(
    ("target_name", "action"), [("console", "_sync_putc"), ("uart", "_putc_sync")]
)
def test_named_dependency_matches_the_exact_instance(
    model: GlobalVars, target_name: str, action: str
) -> None:
    target = getattr(model, target_name)
    other_io = IO()
    assert other_io == model.io
    assert other_io is not model.io
    env = TaskLocalEnv(cv=ContentionVector.ones())

    with GuardSpinLock(env.cv, model.io):
        target.acquire(env, action)

    for wrong_target in (other_io, IO):
        with (
            GuardSpinLock(env.cv, wrong_target),
            pytest.raises(AssertionError, match="Contention invariant violated"),
        ):
            target.acquire(env, action)


@pytest.mark.parametrize("domain", DOMAINS)
def test_named_dependency_does_not_bypass_contention_counts(
    model: GlobalVars, domain: str
) -> None:
    env = TaskLocalEnv(cv=ContentionVector.ones())
    env.cv.protect(model.io, *DOMAINS)
    model.console.acquire(env, "_sync_putc")

    env.cv.unprotect(model.io, domain)

    assert model.console.violated_domains(env, "_sync_putc") == [domain]


def test_method_dependency_overrides_inherited_class_declaration(
    model: GlobalVars,
) -> None:
    @protected_by("io")
    class Base(System):
        def _inherited(self, env: TaskLocalEnv) -> None:
            pass

    class Derived(Base):
        @protected_by("console")
        def _overridden(self, env: TaskLocalEnv) -> None:
            pass

    target = Derived()
    env = TaskLocalEnv(cv=ContentionVector.ones())

    with GuardSpinLock(env.cv, model.io):
        assert target.check_invariant(env, "_inherited")
        assert not target.check_invariant(env, "_overridden")
    with GuardSpinLock(env.cv, model.console):
        assert not target.check_invariant(env, "_inherited")
        assert target.check_invariant(env, "_overridden")


def test_dotted_dependency_follows_replaced_instances(model: Any) -> None:
    @protected_by("holder.lock")
    class Dependent(System):
        def _action(self, env: TaskLocalEnv) -> None:
            pass

    holder = SimpleNamespace(lock=model.io)
    model.holder = holder
    target = Dependent()
    env = TaskLocalEnv(cv=ContentionVector.ones())
    old_lock = holder.lock

    with GuardSpinLock(env.cv, old_lock):
        assert target.check_invariant(env, "_action")
        holder.lock = IO()
        assert not target.check_invariant(env, "_action")
    with GuardSpinLock(env.cv, holder.lock):
        assert target.check_invariant(env, "_action")


@pytest.mark.parametrize("path", ["missing_target", "io.missing_target"])
def test_missing_dependency_names_raise_at_check_time(
    model: GlobalVars, path: str
) -> None:
    class Dependent(System):
        @protected_by(path)
        def _action(self, env: TaskLocalEnv) -> None:
            pass

    with pytest.raises(AttributeError, match="missing_target"):
        Dependent().acquire(TaskLocalEnv(), "_action")


@pytest.mark.parametrize("path", ["", " ", ".io", "io.", "io..lock"])
def test_malformed_dependency_names_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="nonempty dotted object path"):
        protected_by(path)


@pytest.mark.parametrize("target", [None, IO, IO()])
def test_dependency_declarations_require_names(target: Any) -> None:
    with pytest.raises(TypeError, match="object path string"):
        protected_by(target)


def test_snapshot_dependencies_match_restored_protection_stacks(
    model: Any, tmp_path: Path
) -> None:
    old_io = model.io
    env = TaskLocalEnv(cv=ContentionVector.ones())
    env.cv.protect(model.io, *DOMAINS)
    model.locked_env = env
    path = tmp_path / "dependencies.snap"
    model.save_snapshot(path, checkpoint="dependencies")

    model.load_snapshot(path, expected_checkpoint="dependencies")

    restored_env = model.locked_env
    assert model.io is not old_io
    assert restored_env is not env
    assert all(restored_env.cv.stacks[domain][-1] is model.io for domain in DOMAINS)
    model.console.acquire(restored_env, "_sync_putc")
    env = TaskLocalEnv(cv=ContentionVector.ones())
    with GuardSpinLock(env.cv, old_io):
        assert not model.console.check_invariant(env, "_sync_putc")
    with GuardSpinLock(env.cv, model.io):
        model.console.acquire(env, "_sync_putc")


def test_printk_protects_the_console_and_uart_with_competitors_enabled(
    model: GlobalVars,
) -> None:
    env = TaskLocalEnv(cv=ContentionVector.ones())
    before = (
        tuple(getattr(env.cv, domain) for domain in DOMAINS),
        {domain: list(stack) for domain, stack in env.cv.stacks.items()},
    )

    System().drive(env, "io", "printk")

    assert tuple(getattr(env.cv, domain) for domain in DOMAINS) == before[0]
    assert env.cv.stacks == before[1]
    assert env.depth == 0
    assert env.signal_queues == []

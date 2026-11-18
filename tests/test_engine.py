"""Native Signal dispatch stays independent of spec source-line recording."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from framework import snapshot_paths
from framework.engine import StepPoint, System, TaskLocalEnv, exit_from


@dataclass
class ExampleSystem(System):
    value: int = 0
    releases: int = 0
    fail_release: bool = False

    def release(self, env: TaskLocalEnv, action: str):
        self.releases += 1
        if self.fail_release:
            raise RuntimeError("release failed")

    def _step(self, env: TaskLocalEnv, amount: int = 1):
        self.value += amount
        return self.value

    def public(self, env: TaskLocalEnv):
        return self.drive(env, "self", "_step", amount=3)

    def _queued(self, env: TaskLocalEnv):
        self.emit(env, "self", "_step")
        return self.value

    def _fail(self, env: TaskLocalEnv):
        self.drive(env, "self", "_step")
        raise RuntimeError("action failed")

    def _stop(self, env: TaskLocalEnv):
        exit_from("example", "stopped")


@dataclass
class ExampleRoot:
    system: ExampleSystem = field(default_factory=ExampleSystem)


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = ExampleRoot()
    module = ModuleType("global_vars")
    vars(module).update(gv=root, MODEL_VERSION=1)
    monkeypatch.setitem(sys.modules, "global_vars", module)
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    return root


@pytest.mark.parametrize(
    "action,expected", [("_step", 1), ("public", 3), ("_queued", 0)]
)
def test_native_methods_do_not_create_spec_points(
    root: ExampleRoot, tmp_path: Path, action: str, expected: int
):
    env = TaskLocalEnv(name="example")
    assert System().drive(env, "system", action) == expected
    assert root.system.value == (1 if action == "_queued" else expected)
    assert not list(tmp_path.iterdir())
    assert env.depth == 0 and not env.signal_queues


def test_self_target_uses_the_signal_source(root: ExampleRoot):
    other = ExampleSystem()
    assert other.drive(TaskLocalEnv(name="example"), "self", "public") == 3
    assert other.value == 3
    assert root.system.value == 0


def test_nested_failure_releases_every_entered_method(root: ExampleRoot):
    env = TaskLocalEnv(name="example")
    with pytest.raises(RuntimeError, match="action failed"):
        System().drive(env, "system", "_fail")
    assert root.system.value == 1
    assert root.system.releases == 2
    assert env.depth == 0 and not env.signal_queues


def test_release_failure_cleans_invocation_queue(root: ExampleRoot):
    root.system.fail_release = True
    env = TaskLocalEnv(name="example")
    with pytest.raises(RuntimeError, match="release failed"):
        System().drive(env, "system", "_step")
    assert root.system.value == 1
    assert root.system.releases == 1
    assert env.depth == 0 and not env.signal_queues


def test_acquire_failure_does_not_enter_or_release_method(root: ExampleRoot):
    env = TaskLocalEnv(name="example")
    env.cv.expose("remote_tasks")
    with pytest.raises(AssertionError, match="Contention invariant"):
        System().drive(env, "system", "_step")
    assert root.system.value == 0
    assert root.system.releases == 0
    assert env.depth == 0 and not env.signal_queues


def test_step_point_propagates_and_cleans_environment(root: ExampleRoot):
    env = TaskLocalEnv(name="example")
    with pytest.raises(StepPoint) as error:
        System().drive(env, "system", "_stop")
    assert (error.value.point, error.value.state) == ("example", "stopped")
    assert root.system.releases == 1
    assert env.depth == 0 and not env.signal_queues


def test_environments_do_not_share_cv_or_queues():
    first, second = TaskLocalEnv(name="first"), TaskLocalEnv(name="second")
    first.cv.expose("local_irq")
    assert first.cv is not second.cv
    assert first.signal_queues is not second.signal_queues
    assert second.cv.local_irq == 0

"""Spec parsing and interpretation; model files serve only as language fixtures."""

import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from framework import snapshot_paths
from framework.contracts import ContractViolation
from framework.engine import System, TaskLocalEnv
from framework.snapshot import load_snapshot
from framework.spec import SpecError, SpecRegistry, parse_spec

PROC_TABLE_SPEC = Path(__file__).resolve().parents[1] / "kernel" / "ProcTable.spec"


@dataclass
class SpecRoot:
    table: Any = None
    worker: Any = None
    data: list[int] = field(default_factory=list)


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SpecRoot:
    root = SpecRoot()
    module = ModuleType("global_vars")
    vars(module).update(gv=root, MODEL_VERSION=1)
    monkeypatch.setitem(sys.modules, "global_vars", module)
    monkeypatch.setattr(snapshot_paths, "SNAPSHOT_DIR", tmp_path)
    return root


def write_spec(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / f"{name}.spec"
    path.write_text(textwrap.dedent(source).lstrip())
    return path


@pytest.mark.parametrize("items", [[], [10], [10, 20, 30]])
def test_original_proc_table_spec_returns_selected_item(
    root: SpecRoot, tmp_path: Path, items: list[int]
):
    registry = SpecRegistry([PROC_TABLE_SPEC])
    root.table = registry.model("ProcTable")()
    root.table.table.extend(items)
    env = TaskLocalEnv(name="example")

    result = System().drive(env, "table", "_pick_runnable")

    assert result == (items[0] if items else None)
    assert root.table.table == items[1:]
    restored = SpecRoot()
    load_snapshot(
        restored,
        tmp_path / "example_0.0.snap",
        model_version=1,
        expected_checkpoint="example_0.0",
    )
    assert restored.table.table == items[1:]
    assert env.depth == 0
    assert not env.signal_queues


def test_property_defaults_are_independent():
    registry = SpecRegistry([PROC_TABLE_SPEC])
    first = registry.model("ProcTable")()
    second = registry.model("ProcTable")()
    first.table.append(1)
    assert second.table == []


def test_nested_drives_assign_results_before_continuing(root: SpecRoot, tmp_path: Path):
    path = write_spec(
        tmp_path,
        "SpecWorker",
        """
        Prop value: int = 0

        Def start() -> result:
            Actions:
                result = drive("self", "_outer", amount=4)
                self.value += result

        Def _outer(amount) -> result:
            Actions:
                result = drive("self", "_inner", amount=amount)
                result += 2

        Def _inner(amount) -> result:
            Actions:
                self.value += amount
                result = self.value
    """,
    )
    root.worker = SpecRegistry([path]).model("SpecWorker")()
    env = TaskLocalEnv(name="example")

    result = System().drive(env, "worker", "start")

    assert result == 6
    assert root.worker.value == 10
    assert env.snapshot_position == 2
    assert env.depth == 0


def test_guards_and_kwargs_protect_nested_actions(root: SpecRoot, tmp_path: Path):
    path = write_spec(
        tmp_path,
        "ProtectedSpec",
        """
        Prop value: int = 0

        Def start(**kwargs):
            Actions:
                with GuardSpinLock(env.cv, self):
                    drive("self", "_change", **kwargs)

        Def _change(**kwargs):
            ProtectedBy worker
            Actions:
                self.value += kwargs["amount"]
    """,
    )
    root.worker = SpecRegistry([path]).model("ProtectedSpec")()
    env = TaskLocalEnv(name="example")
    env.cv.expose("remote_irq", "remote_tasks")
    before = repr(env.cv)

    System().drive(env, "worker", "start", amount=3)

    assert root.worker.value == 3
    assert repr(env.cv) == before
    assert env.cv.stacks["remote_tasks"] == []


@pytest.mark.parametrize(
    "source,exception,match",
    [
        (
            "Requires:\n    False\nActions:\n    pass",
            ContractViolation,
            "requires failed",
        ),
        (
            "Requires:\n    True\nActions:\n    pass\nRequires:\n    True\nActions:\n    pass",
            ValueError,
            "overlap",
        ),
        ("Requires:\n    1\nActions:\n    pass", TypeError, "bool"),
        (
            "Actions:\n    pass\nEnsures:\n    False",
            ContractViolation,
            "ensures failed",
        ),
    ],
)
def test_invalid_contracts_fail_at_spec_location(
    root: SpecRoot, tmp_path: Path, source: str, exception: type[Exception], match: str
):
    source = "Def _test():\n" + textwrap.indent(source, "    ")
    path = write_spec(tmp_path, "ContractSpec", source)
    root.worker = SpecRegistry([path]).model("ContractSpec")()
    env = TaskLocalEnv(name="example")

    with pytest.raises(exception, match=match):
        System().drive(env, "worker", "_test")

    assert env.depth == 0
    assert not env.signal_queues
    assert (tmp_path / "example_0.0.snap").exists()


def test_selected_ensures_branch_is_checked(root: SpecRoot, tmp_path: Path):
    path = write_spec(
        tmp_path,
        "ChoiceSpec",
        """
        Def _choose(value) -> result:
            Requires:
                value == 0
            Actions:
                result = 10
            Ensures:
                result == 10

            Requires:
                value > 0
            Actions:
                result = 20
            Ensures:
                result == 20
    """,
    )
    root.worker = SpecRegistry([path]).model("ChoiceSpec")()
    assert (
        System().drive(TaskLocalEnv(name="first"), "worker", "_choose", value=0) == 10
    )
    assert (
        System().drive(TaskLocalEnv(name="second"), "worker", "_choose", value=1) == 20
    )


def test_exception_unwinds_guards_and_nested_frames(root: SpecRoot, tmp_path: Path):
    path = write_spec(
        tmp_path,
        "FailureSpec",
        """
        Def _outer():
            Actions:
                with GuardSpinLock(env.cv, self):
                    drive("self", "_inner")

        Def _inner():
            Actions:
                assert False, "inner failed"
    """,
    )
    root.worker = SpecRegistry([path]).model("FailureSpec")()
    env = TaskLocalEnv(name="example")
    before = repr(env.cv)

    with pytest.raises(AssertionError, match="inner failed"):
        System().drive(env, "worker", "_outer")

    assert repr(env.cv) == before
    assert env.depth == 0
    assert env.snapshot_position == 2
    assert not env.signal_queues


def test_conditional_and_direct_return(root: SpecRoot, tmp_path: Path):
    path = write_spec(
        tmp_path,
        "ReturnSpec",
        """
        Def start(value=0):
            Actions:
                if value:
                    return drive("self", "_value", value=value)
                else:
                    return 0

        Def _value(value):
            Actions:
                return value + 1
    """,
    )
    root.worker = SpecRegistry([path]).model("ReturnSpec")()
    assert System().drive(TaskLocalEnv(name="first"), "worker", "start", value=3) == 4
    assert System().drive(TaskLocalEnv(name="second"), "worker", "start") == 0


@pytest.mark.parametrize(
    "source",
    [
        "Def broken:\n    Actions:\n        pass",
        "Def start():\n    Actions:\n        while True:\n            pass",
        "Prop value: int\nProp value: int",
        "Def start():\n    Unknown:\n        True",
        'Def start():\n    Actions:\n        return drive("self", "start") + 1',
    ],
)
def test_unsupported_syntax_reports_file(root: SpecRoot, tmp_path: Path, source: str):
    path = write_spec(tmp_path, "InvalidSpec", source)
    with pytest.raises(SpecError, match="InvalidSpec.spec:"):
        parse_spec(path)


def test_return_releases_guard_before_ensures(root: SpecRoot, tmp_path: Path):
    path = write_spec(
        tmp_path,
        "GuardReturnSpec",
        """
        Def _check():
            Actions:
                with GuardSpinLock(env.cv, self):
                    return 3
            Ensures:
                env.cv.local_irq == 0 and env.cv.remote_tasks == 0
    """,
    )
    root.worker = SpecRegistry([path]).model("GuardReturnSpec")()
    env = TaskLocalEnv(name="example")
    assert System().drive(env, "worker", "_check") == 3
    assert env.cv.local_irq == 0
    assert env.cv.remote_tasks == 0


def test_inheritance_enums_and_visibility(root: SpecRoot, tmp_path: Path):
    base = write_spec(
        tmp_path,
        "BaseSpec",
        """
        Enum SpecState:
            READY = "ready"

        Visibility CPUSCOPE_CV
        Prop state: SpecState = SpecState.READY

        Def _read() -> result:
            Actions:
                result = self.state
    """,
    )
    derived = write_spec(
        tmp_path,
        "DerivedSpec",
        """
        Extends BaseSpec
        Prop values: list[int]
    """,
    )
    registry = SpecRegistry([derived, base])
    root.worker = registry.model("DerivedSpec")()
    env = TaskLocalEnv(name="example")
    env.cv.expose("remote_irq", "remote_tasks")
    assert (
        System().drive(env, "worker", "_read") is registry.namespace["SpecState"].READY
    )
    assert root.worker.values == []


def test_cyclic_inheritance_is_rejected(tmp_path: Path):
    first = write_spec(tmp_path, "FirstSpec", "Extends SecondSpec")
    second = write_spec(tmp_path, "SecondSpec", "Extends FirstSpec")
    with pytest.raises(SpecError, match="cyclic"):
        SpecRegistry([first, second])

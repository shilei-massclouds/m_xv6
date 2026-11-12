"""Boot origins select checkpoints and create independent task environments."""

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flows.bp_init_flow import BpInitFlow
from framework import snapshot_paths
from framework.contention import DOMAINS
from framework.engine import TaskLocalEnv
from framework.snapshot import SnapshotCompatibilityError, SnapshotError, save_snapshot
from framework.sync_primitives import LocalMultiTasks
from global_vars import MODEL_VERSION, GlobalVars, gv
from kernel.proc import NextRunnable, Proc, ProcState, SchedState, Scheduler
from origins.ap_init_origin import ApInitOrigin
from origins.bp_init_origin import BpInitOrigin

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def model() -> Iterator[Any]:
    previous_state = gv.__dict__
    gv.__dict__ = vars(GlobalVars())
    try:
        yield gv
    finally:
        gv.__dict__ = previous_state


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    destination = tmp_path / "repository"
    shutil.copytree(
        REPO_ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".agents",
            ".codex",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "*.snap",
            ".*.tmp",
        ),
    )
    return destination


def _run_main(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Run from outside the repository to verify paths are anchored to its files.
    return subprocess.run(
        [sys.executable, str(repository / "main.py"), *args],
        cwd=repository.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def _levels(env: TaskLocalEnv) -> tuple[int, ...]:
    return tuple(getattr(env.cv, domain) for domain in DOMAINS)


@pytest.mark.parametrize(
    ("next_runnable", "expected_state"),
    [
        (NextRunnable.INIT_PROC, SchedState.SWITCHED),
        (NextRunnable.NONE, SchedState.WFI),
    ],
)
def test_bp_records_initialized_state_before_scheduling(
    model: Any, next_runnable: NextRunnable, expected_state: SchedState
) -> None:
    model.proc_table.next_runnable = next_runnable
    original_root = model
    original_proc = model.initproc
    assert model.initproc.state is ProcState.UNUSED
    assert model.childproc.state is ProcState.UNUSED
    assert model.bp_init_flow.hartid == 0
    assert model.ap_init_flow.hartid == 1

    BpInitOrigin().start()

    assert model is original_root
    assert model.initproc is original_proc
    assert model.initproc.state is ProcState.RUNNABLE
    assert model.bp_scheduler.state is expected_state
    assert model.ap_scheduler.state is SchedState.RUNNING
    initialized = GlobalVars()
    metadata = initialized.load_snapshot(
        snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH,
        expected_checkpoint="bp_initialized",
    )
    assert metadata["model_version"] == MODEL_VERSION
    assert initialized.initproc.state is ProcState.RUNNABLE
    assert initialized.childproc.state is ProcState.UNUSED
    assert initialized.bp_scheduler.state is SchedState.RUNNING
    assert initialized.ap_scheduler.state is SchedState.RUNNING
    assert set(snapshot_paths.SNAPSHOT_DIR.iterdir()) == {
        snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH
    }


def test_bp_snapshot_is_recorded_before_competitors_even_when_start_fails(
    model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = BpInitOrigin()
    events: list[str] = []
    original_save = GlobalVars.save_snapshot
    original_init = Proc._init
    original_enable = LocalMultiTasks.enable

    def init(self: Proc, env: TaskLocalEnv) -> None:
        original_init(self, env)
        events.append("initproc initialized")

    def save(self: GlobalVars, path: Any, *, checkpoint: str) -> Any:
        if checkpoint == "bp_initialized":
            assert model.initproc.state is ProcState.RUNNABLE
            assert _levels(origin.env) == (0, 0, 0, 0)
            events.append("snapshot")
        return original_save(self, path, checkpoint=checkpoint)

    def enable(self: LocalMultiTasks, cv: Any) -> None:
        assert snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH.is_file()
        events.append("enable competitors")
        original_enable(self, cv)

    def schedule(self: Scheduler, env: TaskLocalEnv) -> None:
        assert _levels(env) == (0, 0, 1, 1)
        raise RuntimeError("scheduler stopped")

    monkeypatch.setattr(Proc, "_init", init)
    monkeypatch.setattr(GlobalVars, "save_snapshot", save)
    monkeypatch.setattr(LocalMultiTasks, "enable", enable)
    monkeypatch.setattr(Scheduler, "schedule", schedule)

    with pytest.raises(RuntimeError, match="scheduler stopped"):
        origin.start()

    assert events == ["initproc initialized", "snapshot", "enable competitors"]
    restored = GlobalVars()
    restored.load_snapshot(
        snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH,
        expected_checkpoint="bp_initialized",
    )
    assert restored.initproc.state is ProcState.RUNNABLE
    assert restored.childproc.state is ProcState.UNUSED
    assert origin.env.depth == 0
    assert origin.env.signal_queues == []


@pytest.mark.parametrize("origin_type", [BpInitOrigin, ApInitOrigin])
def test_origin_instances_have_independent_local_environments(origin_type: Any) -> None:
    first = origin_type()
    second = origin_type()
    assert first.env is not second.env
    assert first.env.cv is not second.env.cv
    assert first.env.signal_queues is not second.env.signal_queues

    first.env.depth = 42
    first.env.cv.remote_tasks = 99

    assert second.env.depth == 0
    assert _levels(second.env) == (0, 0, 0, 0)
    assert second.env.signal_queues == []


@pytest.mark.parametrize(
    ("next_runnable", "expected_state"),
    [
        (NextRunnable.INIT_PROC, SchedState.SWITCHED),
        (NextRunnable.NONE, SchedState.WFI),
    ],
)
def test_ap_restores_bp_state_and_schedules_its_own_cpu(
    model: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    next_runnable: NextRunnable,
    expected_state: SchedState,
) -> None:
    model.proc_table.next_runnable = next_runnable
    bp = BpInitOrigin()
    bp.start()
    assert model.bp_scheduler.state is expected_state
    assert model.ap_scheduler.state is SchedState.RUNNING
    bp.env.depth = 8
    model.initproc.state = ProcState.UNUSED
    model.later = True
    original_bytes = snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH.read_bytes()

    def no_bp(*args: Any, **kwargs: Any) -> None:
        pytest.fail("AP must not run BP initialization")

    monkeypatch.setattr(BpInitFlow, "start", no_bp)
    capsys.readouterr()
    ap = ApInitOrigin()
    assert _levels(ap.env) == (0, 0, 0, 0)
    ap.start()

    assert model.initproc.state is ProcState.RUNNABLE
    assert model.childproc.state is ProcState.UNUSED
    assert model.bp_scheduler.state is SchedState.RUNNING
    assert model.ap_scheduler.state is expected_state
    assert model.bp_scheduler is not model.ap_scheduler
    assert not hasattr(model, "later")
    assert model.ap_init_flow.hartid == 1
    assert ap.env is not bp.env
    assert _levels(ap.env) == (0, 0, 1, 1)
    assert ap.env.depth == 0
    assert ap.env.signal_queues == []
    output = capsys.readouterr().out
    assert output.splitlines()[:2] == [
        "ApInitOrigin():",
        "    start -> ap_init_flow",
    ]
    assert "schedule -> ap_scheduler" in output
    assert "schedule -> bp_scheduler" not in output
    assert snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH.read_bytes() == original_bytes


def test_ap_does_not_adopt_an_environment_reachable_from_the_snapshot(
    model: Any,
) -> None:
    source: Any = GlobalVars()
    source.initproc.state = ProcState.RUNNABLE
    source.bp_environment = TaskLocalEnv(depth=7)
    source.save_snapshot(
        snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH, checkpoint="bp_initialized"
    )
    origin = ApInitOrigin()

    origin.start()

    assert model.bp_environment.depth == 7
    assert origin.env is not model.bp_environment
    assert origin.env.cv is not model.bp_environment.cv
    assert _levels(origin.env) == (0, 0, 1, 1)
    assert origin.env.depth == 0
    assert set(snapshot_paths.SNAPSHOT_DIR.iterdir()) == {
        snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH
    }


@pytest.mark.parametrize("failure", ["corrupt", "old_model", "wrong_checkpoint"])
def test_ap_rejects_bad_existing_files_without_overwriting_or_changing_globals(
    model: Any, failure: str
) -> None:
    path = snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH
    checkpoint = snapshot_paths.BP_INITIALIZED_CHECKPOINT
    source = GlobalVars()
    if failure == "corrupt":
        path.write_bytes(b"broken snapshot")
    elif failure == "old_model":
        save_snapshot(source, path, checkpoint=checkpoint, model_version=1)
    else:
        source.save_snapshot(path, checkpoint="wrong")
    original_bytes = path.read_bytes()
    model.marker = "live"
    current_state = vars(model)

    with pytest.raises(SnapshotError):
        ApInitOrigin().start()

    assert vars(model) is current_state
    assert model.marker == "live"
    assert path.read_bytes() == original_bytes
    assert list(path.parent.iterdir()) == [path]


def test_ap_missing_checkpoint_does_not_create_files_or_run_bp(
    model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_bp(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a missing AP checkpoint must not trigger BP")

    monkeypatch.setattr(BpInitFlow, "start", no_bp)
    current_state = vars(model)

    with pytest.raises(SnapshotError, match="bp_initialized.snap"):
        ApInitOrigin().start()

    assert vars(model) is current_state
    assert list(snapshot_paths.SNAPSHOT_DIR.iterdir()) == []


def test_ap_rejects_version_two_snapshot_with_old_fields(model: Any) -> None:
    source = GlobalVars()
    source.initproc.state = ProcState.RUNNABLE
    old_state = vars(source)
    old_state["scheduler"] = old_state.pop("bp_scheduler")
    old_state["current_hart"] = old_state.pop("bp_hart")
    del old_state["ap_scheduler"]
    del old_state["ap_hart"]
    path = snapshot_paths.BP_INITIALIZED_SNAPSHOT_PATH
    save_snapshot(source, path, checkpoint="bp_initialized", model_version=2)
    original_bytes = path.read_bytes()
    current_state = vars(model)
    current_objects = (
        model.bp_scheduler,
        model.ap_scheduler,
        model.bp_hart,
        model.ap_hart,
    )

    with pytest.raises(SnapshotCompatibilityError, match="model_version"):
        ApInitOrigin().start()

    assert vars(model) is current_state
    for name, current in zip(
        ("bp_scheduler", "ap_scheduler", "bp_hart", "ap_hart"), current_objects
    ):
        assert getattr(model, name) is current
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("args", [(), ("bp_init_origin",)])
def test_cli_defaults_to_bp_and_accepts_explicit_bp(
    repository: Path, args: tuple[str, ...]
) -> None:
    first = _run_main(repository, *args)

    assert first.returncode == 0, first.stderr
    assert "start -> bp_init_flow" in first.stdout
    assert "schedule -> bp_scheduler" in first.stdout
    directory = repository / "snapshots"
    initialized_path = directory / "bp_initialized.snap"
    restored = GlobalVars()
    metadata = restored.load_snapshot(
        initialized_path, expected_checkpoint="bp_initialized"
    )
    assert metadata["model_version"] == MODEL_VERSION
    assert restored.initproc.state is ProcState.RUNNABLE
    assert {path.name for path in directory.glob("*.snap")} == {"bp_initialized.snap"}
    assert not (repository.parent / "snapshots").exists()

    second = _run_main(repository, *args)

    assert second.returncode == 0, second.stderr
    restored.load_snapshot(initialized_path, expected_checkpoint="bp_initialized")
    assert restored.initproc.state is ProcState.RUNNABLE


def test_independent_processes_run_bp_then_direct_ap(repository: Path) -> None:
    bp = _run_main(repository)
    assert bp.returncode == 0, bp.stderr
    directory = repository / "snapshots"
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    ap = _run_main(repository, "ap_init_origin")

    assert ap.returncode == 0, ap.stderr
    assert ap.stdout.splitlines()[:2] == [
        "ApInitOrigin():",
        "    start -> ap_init_flow",
    ]
    assert "schedule -> ap_scheduler" in ap.stdout
    assert "schedule -> bp_scheduler" not in ap.stdout
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before

    # Inspect the AP's loaded state in another fresh interpreter, through main.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import main
from flows.ap_init_flow import ApInitFlow
from flows.bp_init_flow import BpInitFlow
from global_vars import gv
from kernel.proc import ProcState, SchedState

original_root = gv
original_start = ApInitFlow.start

def no_bp(*args, **kwargs):
    raise AssertionError('AP unexpectedly ran BP')

def inspect_ap(self, env, **kwargs):
    assert gv is original_root
    assert self is gv.ap_init_flow
    assert self.hartid == 1
    assert gv.initproc.state is ProcState.RUNNABLE
    assert gv.childproc.state is ProcState.UNUSED
    assert gv.bp_scheduler is not gv.ap_scheduler
    assert gv.bp_scheduler.state is SchedState.RUNNING
    assert gv.ap_scheduler.state is SchedState.RUNNING
    assert (env.cv.local_irq, env.cv.local_tasks,
            env.cv.remote_irq, env.cv.remote_tasks) == (0, 0, 0, 0)
    original_start(self, env, **kwargs)
    assert gv.bp_scheduler.state is SchedState.RUNNING
    assert gv.ap_scheduler.state is SchedState.SWITCHED
    assert (env.cv.local_irq, env.cv.local_tasks,
            env.cv.remote_irq, env.cv.remote_tasks) == (0, 0, 1, 1)
    print('AP state verified')

BpInitFlow.start = no_bp
ApInitFlow.start = inspect_ap
sys.argv = ['main.py', 'ap_init_origin']
main.main()
""",
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "AP state verified" in probe.stdout


@pytest.mark.parametrize(
    "args", [("unknown",), ("bp_init_origin", "extra"), ("--hart", "2")]
)
def test_cli_rejects_unknown_origins_and_extra_arguments(
    repository: Path, args: tuple[str, ...]
) -> None:
    result = _run_main(repository, *args)

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "bp_init_origin" in result.stderr
    assert "ap_init_origin" in result.stderr
    assert result.stdout == ""
    assert {path.name for path in (repository / "snapshots").iterdir()} == {"README.md"}


def test_cli_ap_reports_missing_checkpoint_without_running_bp(repository: Path) -> None:
    result = _run_main(repository, "ap_init_origin")

    assert result.returncode == 1
    assert "cannot load snapshot" in result.stderr
    assert "bp_initialized.snap" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert {path.name for path in (repository / "snapshots").iterdir()} == {"README.md"}

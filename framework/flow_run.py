"""I/O records and history for spec derivations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from framework import snapshot_paths
from framework.engine import FlowPaused, TaskLocalEnv
from framework.input_encoding import InputEncoder
from framework.snapshot import (
    SnapshotCompatibilityError,
    SnapshotError,
    save_output_snapshot,
)
from framework.spec import ActionSite, Specification

RECORD_VERSION = 1
_ROOT = Path(__file__).resolve().parents[1]
_CURRENT: ContextVar[FlowRun | None] = ContextVar("spec_flow_run", default=None)
_INPUT_BATCH: ContextVar[dict[Path, _BufferedHistory] | None] = ContextVar(
    "spec_input_batch", default=None
)


@dataclass(frozen=True)
class RunPosition:
    branch: int
    input_position: int
    output_position: int
    pending: frozenset[str]
    records_digest: str


@dataclass(frozen=True)
class Fork:
    base: str
    candidate: Path
    pending: frozenset[str]


type CaptureInput = Callable[[str, bytes], None]
type InputRecords = list[dict[str, Any]]


@dataclass
class _BufferedHistory:
    flow: str
    model_version: int
    directory_fd: int
    records: InputRecords | None = None
    fingerprints: dict[str, str] = field(default_factory=dict)
    disk_digest: str | None = None
    dirty: bool = False


@contextmanager
def buffer_input_records() -> Iterator[None]:
    """Share I point history across runs, publishing it on context exit."""
    if _INPUT_BATCH.get() is not None or _CURRENT.get() is not None:
        raise RuntimeError("input record batches must enclose their FlowRun contexts")
    histories: dict[Path, _BufferedHistory] = {}
    token = _INPUT_BATCH.set(histories)
    try:
        yield
    finally:
        _INPUT_BATCH.reset(token)
        # Keep each history locked until its export. Try every history even
        # when execution or another export fails.
        with ExitStack() as exports:
            for path, history in histories.items():
                exports.callback(os.close, history.directory_fd)
                if history.dirty and history.records is not None:
                    exports.callback(
                        _write_records,
                        path,
                        history.flow,
                        history.model_version,
                        history.records,
                        history.fingerprints,
                    )


def _lock_directory(directory: Path, flow: str) -> int:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as error:
        os.close(fd)
        if isinstance(error, BlockingIOError):
            raise SnapshotError(f"flow {flow!r} is already running") from error
        raise
    return fd


def _write_records(
    path: Path,
    flow: str,
    model_version: int,
    records: InputRecords,
    fingerprints: dict[str, str],
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".records.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(
                {
                    "format_version": RECORD_VERSION,
                    "model_version": model_version,
                    "flow": flow,
                    "spec_fingerprints": fingerprints,
                    "points": records,
                },
                stream,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception as error:
        raise SnapshotError(f"cannot write I point records {path}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def require_run(env: TaskLocalEnv) -> FlowRun:
    run = _CURRENT.get()
    if run is None or not run.active:
        raise RuntimeError("spec execution requires an active FlowRun context")
    if env is not run.env:
        raise RuntimeError("spec environment does not belong to the active FlowRun")
    if run.failed:
        raise RuntimeError(
            "FlowRun has failed; start a new run after cleaning its records"
        )
    return run


class FlowRun:
    def __init__(
        self,
        root: object,
        env: TaskLocalEnv,
        model_version: int,
        *,
        history: tuple[Path, RunPosition, str] | None = None,
        fork: Fork | None = None,
        capture_input: CaptureInput | None = None,
        capture_all_inputs: bool = False,
    ):
        if history is not None and fork is not None:
            raise ValueError("resume and fork are separate entry points")
        self.root = root
        self.env = env
        self.model_version = model_version
        self.branch = 0
        self.input_position = 0
        self.output_position = 0
        self.latest_output = ""
        self.records: InputRecords = []
        self.spec_fingerprints: dict[str, str] = {}
        self.active = False
        self.failed = False
        self._token: Token[FlowRun | None] | None = None
        self._entered = False
        self._pending: set[str] = set()
        self._records_path = snapshot_paths.input_records_path(env.name)
        self._history = history
        self.fork = fork
        self.capture_input = capture_input
        self.capture_all_inputs = capture_all_inputs
        self._directory_fd: int | None = None
        self._buffered: _BufferedHistory | None = None

    def _lock_history(self, directory: Path) -> None:
        batch = _INPUT_BATCH.get()
        if batch is None:
            self._directory_fd = _lock_directory(directory, self.env.name)
            return
        path = self._records_path.resolve()
        history = batch.get(path)
        if history is None:
            history = _BufferedHistory(
                self.env.name,
                self.model_version,
                _lock_directory(directory, self.env.name),
            )
            batch[path] = history
        elif history.model_version != self.model_version:
            raise SnapshotCompatibilityError(
                "incompatible model version in input batch"
            )
        self._buffered = history

    def __enter__(self) -> Self:
        if self._entered or _CURRENT.get() is not None:
            raise RuntimeError("FlowRun contexts cannot be reused or nested")
        self._entered = True
        inputs = self._records_path.parent
        outputs = snapshot_paths.flow_directory(self.env.name) / "outputs"
        directory = inputs.parent
        if self._history is None and self.fork is None:
            directory.mkdir(parents=True, exist_ok=True)
        try:
            self._lock_history(directory)
            if self._history is not None:
                self._restore_history(*self._history)
            elif self.fork is not None:
                self._start_fork(self.fork)
            else:
                if any(
                    folder.exists() and any(folder.iterdir())
                    for folder in (inputs, outputs)
                ):
                    raise SnapshotError(
                        f"flow {self.env.name!r} already has records or outputs; run make clean_snap for a fresh run"
                    )
                inputs.mkdir(exist_ok=True)
                outputs.mkdir(exist_ok=True)
                self._save_output(None)
                if self._buffered is None:
                    self._persist(self.records, self.spec_fingerprints)
                else:
                    self._buffered.records = self.records
                    self._buffered.fingerprints = self.spec_fingerprints
                    self._buffered.dirty = True
        except BaseException:
            if self._directory_fd is not None:
                os.close(self._directory_fd)
            self.failed = True
            raise
        self.active = True
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.failed = self.failed or (
            exc_type is not None and not isinstance(exc_value, FlowPaused)
        )
        self.active = False
        assert self._token is not None
        _CURRENT.reset(self._token)
        if self._directory_fd is not None:
            os.close(self._directory_fd)
        self._pending.clear()

    def _load_records(self) -> str | None:
        cached = self._buffered
        if cached is not None and cached.records is not None:
            self.records = cached.records
            self.spec_fingerprints = cached.fingerprints
            return cached.disk_digest
        try:
            data = self._records_path.read_bytes()
            document = json.loads(data)
            if (
                document["format_version"] != RECORD_VERSION
                or document["model_version"] != self.model_version
                or document["flow"] != self.env.name
            ):
                raise ValueError("incompatible I point record header")
            self.records = document["points"]
            self.spec_fingerprints = document["spec_fingerprints"]
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise SnapshotCompatibilityError(
                f"invalid history for {self.env.name}: {error}"
            ) from error
        digest = hashlib.sha256(data).hexdigest()
        if cached is not None:
            cached.records = self.records
            cached.fingerprints = self.spec_fingerprints
            cached.disk_digest = digest
        return digest

    def _branch_records(self, branch: int) -> list[dict[str, Any]]:
        prefix = f"{self.env.name}_{branch}."
        return [record for record in self.records if record["name"].startswith(prefix)]

    @staticmethod
    def _digest(records: list[dict[str, Any]]) -> str:
        return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()

    def _start_fork(self, fork: Fork) -> None:
        self._load_records()
        names = {record["name"] for record in self.records}
        if fork.base not in names or not fork.pending <= names:
            raise SnapshotCompatibilityError("fork refers to a missing I point")
        self.branch = 1 + max(
            int(name.rsplit("_", 1)[1].split(".")[0]) for name in names
        )
        self.latest_output = (
            fork.candidate.resolve()
            .relative_to(snapshot_paths.SNAPSHOT_DIR.resolve())
            .as_posix()
        )
        self._pending = set(fork.pending)

    def _restore_history(self, path: Path, position: RunPosition, point: str) -> None:
        flow = self.env.name
        last = snapshot_paths.snapshot_path(
            flow, position.output_position - 1, position.branch
        )
        if path.resolve() != last.resolve():
            raise SnapshotCompatibilityError(
                "checkpoint must be in its flow's output history"
            )
        try:
            outputs = {
                entry.name
                for entry in snapshot_paths.branch_outputs(flow, position.branch)
            }
        except OSError as error:
            raise SnapshotError(f"cannot read history for {flow}: {error}") from error
        expected = {
            snapshot_paths.snapshot_path(flow, i, position.branch).name
            for i in range(position.output_position)
        }
        disk_digest = self._load_records()
        records = self._branch_records(position.branch)
        digests = {self._digest(records), disk_digest}
        if position.records_digest not in digests or outputs != expected:
            raise SnapshotCompatibilityError(
                "checkpoint is not the latest break in an unchanged branch history"
            )
        try:
            if (
                len(records) != position.input_position
                or records[-1]["name"] != point
                or records[-1]["operation"]["kind"] != "break"
                or point in position.pending
                or not position.pending <= {record["name"] for record in self.records}
            ):
                raise ValueError("I point records do not match the checkpoint")
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise SnapshotCompatibilityError(
                f"invalid history for {flow}: {error}"
            ) from error
        self.branch = position.branch
        self.input_position = position.input_position
        self.output_position = position.output_position
        self._pending = set(position.pending)
        self.latest_output = last.relative_to(snapshot_paths.SNAPSHOT_DIR).as_posix()

    def _persist(self, records: InputRecords, fingerprints: dict[str, str]) -> None:
        _write_records(
            self._records_path, self.env.name, self.model_version, records, fingerprints
        )

    def begin(
        self,
        specification: Specification,
        method: str,
        contract_branch: int,
        site: ActionSite,
        kind: str,
        values: dict[str, Any],
    ) -> str:
        require_run(self.env)
        try:
            path = specification.path.resolve()
            filename = (
                path.relative_to(_ROOT).as_posix()
                if path.is_relative_to(_ROOT)
                else str(path)
            )
            fingerprint = specification.fingerprint
            previous = self.spec_fingerprints.get(filename, fingerprint)
            if previous != fingerprint:
                raise ValueError(f"spec changed within the run: {filename}")
            name = f"{self.env.name}_{self.branch}.{self.input_position}"
            base = (
                self.fork.base
                if self.fork is not None and self.input_position == 0
                else None
            )
            record = {
                "name": name,
                "base": base,
                "input_snapshot": self.latest_output,
                "location": {
                    "file": filename,
                    "method": method,
                    "branch": contract_branch,
                    "line": site.line,
                },
                "operation": {"kind": kind, "text": site.text},
                "input": InputEncoder(self.root, self.env).fields(values),
            }
            if self._buffered is None:
                records = [*self.records, record]
                fingerprints = {**self.spec_fingerprints, filename: fingerprint}
                self._persist(records, fingerprints)
                self.records = records
                self.spec_fingerprints = fingerprints
            else:
                self.records.append(record)
                self.spec_fingerprints[filename] = fingerprint
                self._buffered.dirty = True
                self._buffered.disk_digest = None
        except BaseException:
            self.failed = True
            raise
        self.input_position += 1
        self._pending.add(name)
        return name

    def position_after_pause(self, name: str) -> RunPosition:
        return RunPosition(
            self.branch,
            self.input_position,
            self.output_position + 1,
            frozenset(self._pending - {name}),
            self._digest(self._branch_records(self.branch)),
        )

    def pending_before(self, point: str) -> frozenset[str]:
        return frozenset(self._pending - {point})

    def complete(self, name: str, *, continuation: object | None = None) -> Path:
        require_run(self.env)
        if name not in self._pending:
            raise RuntimeError(f"I point is not pending: {name}")
        try:
            path = self._save_output(name, continuation=continuation)
        except BaseException:
            self.failed = True
            raise
        self._pending.remove(name)
        return path

    def _save_output(
        self, input_point: str | None, *, continuation: object | None = None
    ) -> Path:
        path = snapshot_paths.snapshot_path(
            self.env.name, self.output_position, self.branch
        )
        save_output_snapshot(
            self.root,
            path,
            model_version=self.model_version,
            input_point=input_point,
            continuation=continuation,
        )
        self.latest_output = path.relative_to(snapshot_paths.SNAPSHOT_DIR).as_posix()
        self.output_position += 1
        return path

"""I/O records and history for spec derivations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from framework import snapshot_paths
from framework.engine import FlowPaused, TaskLocalEnv
from framework.input_encoding import InputEncoder
from framework.snapshot import SnapshotCompatibilityError, SnapshotError, save_snapshot
from framework.spec import ActionSite, Specification

RECORD_VERSION = 1
_ROOT = Path(__file__).resolve().parents[1]
_CURRENT: ContextVar[FlowRun | None] = ContextVar("spec_flow_run", default=None)


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
        self.records: list[dict[str, Any]] = []
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
        self._directory_fd: int | None = None

    def __enter__(self) -> Self:
        if self._entered or _CURRENT.get() is not None:
            raise RuntimeError("FlowRun contexts cannot be reused or nested")
        self._entered = True
        inputs = self._records_path.parent
        outputs = snapshot_paths.flow_directory(self.env.name) / "outputs"
        directory = inputs.parent
        if self._history is None and self.fork is None:
            directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SnapshotError(
                    f"flow {self.env.name!r} is already running"
                ) from error
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
                self._write_records(self.records, self.spec_fingerprints)
        except BaseException:
            os.close(fd)
            self.failed = True
            raise
        self._directory_fd = fd
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
        assert self._directory_fd is not None
        os.close(self._directory_fd)
        self._pending.clear()

    def _load_records(self) -> bytes:
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
        return data

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
                for entry in last.parent.glob(f"{flow}_{position.branch}.*.snap")
            }
        except OSError as error:
            raise SnapshotError(f"cannot read history for {flow}: {error}") from error
        expected = {
            snapshot_paths.snapshot_path(flow, i, position.branch).name
            for i in range(position.output_position)
        }
        data = self._load_records()
        records = self._branch_records(position.branch)
        digests = {self._digest(records), hashlib.sha256(data).hexdigest()}
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

    def _write_records(
        self, records: list[dict[str, Any]], fingerprints: dict[str, str]
    ) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._records_path.parent,
                prefix=".records.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(
                    {
                        "format_version": RECORD_VERSION,
                        "model_version": self.model_version,
                        "flow": self.env.name,
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
            os.replace(temporary_path, self._records_path)
        except Exception as error:
            raise SnapshotError(
                f"cannot write I point records {self._records_path}: {error}"
            ) from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

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
            fingerprints = dict(self.spec_fingerprints)
            previous = fingerprints.setdefault(filename, specification.fingerprint)
            if previous != specification.fingerprint:
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
            records = [*self.records, record]
            self._write_records(records, fingerprints)
        except BaseException:
            self.failed = True
            raise
        self.records = records
        self.spec_fingerprints = fingerprints
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
        if path.exists():
            raise SnapshotError(f"refusing to overwrite output snapshot {path}")
        save_snapshot(
            self.root,
            path,
            checkpoint=path.stem,
            model_version=self.model_version,
            input_point=input_point,
            continuation=continuation,
        )
        self.latest_output = path.relative_to(snapshot_paths.SNAPSHOT_DIR).as_posix()
        self.output_position += 1
        return path

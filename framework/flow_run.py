"""First-run I/O records for operations written in spec Actions sections."""

from __future__ import annotations

import json
import os
import tempfile
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Self

from framework import snapshot_paths
from framework.engine import TaskLocalEnv
from framework.input_encoding import InputEncoder
from framework.snapshot import SnapshotError, save_snapshot
from framework.spec import ActionSite, Specification

RECORD_VERSION = 1
_ROOT = Path(__file__).resolve().parents[1]
_CURRENT: ContextVar[FlowRun | None] = ContextVar("spec_flow_run", default=None)


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
    def __init__(self, root: object, env: TaskLocalEnv, model_version: int):
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

    def __enter__(self) -> Self:
        if self._entered or _CURRENT.get() is not None:
            raise RuntimeError("FlowRun contexts cannot be reused or nested")
        self._entered = True
        inputs = self._records_path.parent
        outputs = snapshot_paths.flow_directory(self.env.name) / "outputs"
        if any(
            directory.exists() and any(directory.iterdir())
            for directory in (inputs, outputs)
        ):
            raise SnapshotError(
                f"flow {self.env.name!r} already has records or outputs; run make clean_snap first; replay is not implemented"
            )
        inputs.mkdir(parents=True, exist_ok=True)
        outputs.mkdir(parents=True, exist_ok=True)
        try:
            self._save_output(None)
            self._write_records(self.records, self.spec_fingerprints)
        except BaseException:
            self.failed = True
            raise
        self.active = True
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.failed = self.failed or exc_type is not None
        self.active = False
        assert self._token is not None
        _CURRENT.reset(self._token)
        self._pending.clear()

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
            record = {
                "name": name,
                "base": None,
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

    def complete(self, name: str) -> None:
        require_run(self.env)
        if name not in self._pending:
            raise RuntimeError(f"I point is not pending: {name}")
        try:
            self._save_output(name)
        except BaseException:
            self.failed = True
            raise
        self._pending.remove(name)

    def _save_output(self, input_point: str | None) -> None:
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
        )
        self.latest_output = path.relative_to(snapshot_paths.SNAPSHOT_DIR).as_posix()
        self.output_position += 1

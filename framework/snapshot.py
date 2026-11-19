"""Versioned, atomic file snapshots of an instance's Pickle object graph."""

from __future__ import annotations

import copyreg
import hashlib
import json
import os
import pickle
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BufferedRandom, BytesIO
from pathlib import Path
from types import MethodType
from typing import Any, BinaryIO, NotRequired, TypedDict, cast

from framework.engine import TaskLocalEnv
from framework.input_encoding import InputEncoder

MAGIC = b"M_XV6_SNAPSHOT\n"
FORMAT_VERSION = 1
CONTINUATION_FORMAT_VERSION = 2
_MAX_HEADER_BYTES = 64 * 1024
_ROOT_ID = "snapshot_root"
_INDEX_NAME = "index.json"
_INDEX_VERSION = 1
_BREAK_PACK_NAME = "breaks.pack"


class SnapshotMetadata(TypedDict):
    format_version: int
    model_version: int
    checkpoint: str
    created_at: str
    payload_sha256: str
    input_point: NotRequired[str]
    notified: NotRequired[list[str]]
    waits_for: NotRequired[list[str]]
    gv_sha256: NotRequired[str]


class SnapshotError(Exception):
    """A snapshot could not be saved or loaded."""


class SnapshotFormatError(SnapshotError):
    """A snapshot is malformed or its payload is damaged."""


class SnapshotCompatibilityError(SnapshotError):
    """A snapshot's version or checkpoint does not match the caller."""


@dataclass
class _BreakPack:
    published_size: int
    pending: bytearray = field(default_factory=bytearray)

    def append(self, payload: bytes) -> dict[str, int]:
        offset = self.published_size + len(self.pending)
        self.pending.extend(payload)
        return {"offset": offset, "length": len(payload)}


@dataclass
class _OutputBatch:
    pack_breaks: bool
    indexes: dict[Path, dict[str, Any]] = field(default_factory=dict)
    dirty: set[Path] = field(default_factory=set)
    checked_bodies: set[Path] = field(default_factory=set)
    packs: dict[Path, _BreakPack] = field(default_factory=dict)


_OUTPUT_BATCH: ContextVar[_OutputBatch | None] = ContextVar(
    "snapshot_output_batch", default=None
)


@contextmanager
def deduplicate_outputs(*, pack_breaks: bool = False) -> Iterator[None]:
    """Publish indexed outputs on exit, optionally packing break checkpoints."""
    if _OUTPUT_BATCH.get() is not None:
        raise RuntimeError("output snapshot batches cannot be nested")
    batch = _OutputBatch(pack_breaks)
    token = _OUTPUT_BATCH.set(batch)
    try:
        yield
    finally:
        _OUTPUT_BATCH.reset(token)
        with ExitStack() as exports:
            for index_path in batch.dirty:
                pack = batch.packs.get(index_path.parent / _BREAK_PACK_NAME)
                exports.callback(
                    _publish_outputs, index_path, batch.indexes[index_path], pack
                )


def _write_atomic(path: Path, contents: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".snapshot.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise SnapshotError(f"cannot write {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_outputs(
    path: Path, entries: dict[str, Any], pack: _BreakPack | None
) -> None:
    if pack is not None:
        pack_path = path.parent / _BREAK_PACK_NAME
        try:
            prefix = pack_path.read_bytes()
        except FileNotFoundError:
            prefix = b""
        except OSError as error:
            raise SnapshotError(f"cannot read {pack_path}: {error}") from error
        if len(prefix) != pack.published_size:
            raise SnapshotError(f"break pack changed during derivation: {pack_path}")
        # Preserve the old offsets so the prior index can still be read until
        # its replacement is published, including after a failed index write.
        _write_atomic(pack_path, prefix + pack.pending)
    document = {"version": _INDEX_VERSION, "snapshots": entries}
    _write_atomic(
        path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    )


def _index_entries(directory: Path) -> dict[str, Any]:
    path = directory.resolve() / _INDEX_NAME
    batch = _OUTPUT_BATCH.get()
    if batch is not None and path in batch.indexes:
        return batch.indexes[path]
    try:
        document = json.loads(path.read_bytes())
    except FileNotFoundError:
        entries = {}
    except (ValueError, RecursionError) as error:
        raise SnapshotFormatError(f"invalid output index {path}: {error}") from error
    except OSError as error:
        raise SnapshotError(f"cannot read output index {path}: {error}") from error
    else:
        if (
            not isinstance(document, dict)
            or type(document.get("version")) is not int
            or document["version"] != _INDEX_VERSION
            or not isinstance(document.get("snapshots"), dict)
        ):
            raise SnapshotFormatError(f"invalid output index {path}")
        entries = document["snapshots"]
        if any(
            Path(name).name != name or not name.endswith(".snap") for name in entries
        ):
            raise SnapshotFormatError(f"invalid logical snapshot name in {path}")
    if batch is not None:
        batch.indexes[path] = entries
    return entries


def indexed_output_names(directory: Path) -> set[str]:
    return set(_index_entries(directory))


def _body_path(directory: Path, digest: str) -> Path:
    # A FlowRun output directory is <snapshot root>/<flow>/outputs.
    return directory.resolve().parents[1] / ".objects" / f"{digest}.snap-body"


def _append_break(batch: _OutputBatch, path: Path, payload: bytes) -> dict[str, int]:
    pack = batch.packs.get(path)
    if pack is None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        pack = batch.packs[path] = _BreakPack(size)
    return pack.append(payload)


def _read_packed_break(path: Path, span: Any) -> BytesIO:
    if not isinstance(span, dict):
        raise SnapshotFormatError("invalid break pack range")
    offset, length = span.get("offset"), span.get("length")
    if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
        raise SnapshotFormatError("invalid break pack offset or length")
    batch = _OUTPUT_BATCH.get()
    pack = batch.packs.get(path) if batch is not None else None
    if pack is not None and offset >= pack.published_size:
        start = offset - pack.published_size
        if start + length > len(pack.pending):
            raise SnapshotFormatError("break pack range exceeds buffered data")
        return BytesIO(pack.pending[start : start + length])
    with path.open("rb") as stream:
        if offset + length > os.fstat(stream.fileno()).st_size:
            raise SnapshotFormatError("break pack range exceeds file size")
        stream.seek(offset)
        payload = stream.read(length)
    if len(payload) != length:
        raise SnapshotFormatError("truncated break pack payload")
    return BytesIO(payload)


def _bind_method(function: Any, owner: object) -> MethodType:
    # Unlike Pickle's getattr-based reduction, this also restores methods
    # attached dynamically under a name different from their function name.
    return MethodType(function, owner)


def _reduce_method(method: MethodType) -> tuple[Any, tuple[Any, object]]:
    return _bind_method, (method.__func__, method.__self__)


class _SnapshotPickler(pickle.Pickler):
    def __init__(self, stream: BinaryIO, root: object):
        super().__init__(stream, protocol=pickle.HIGHEST_PROTOCOL)
        self.root = root
        self.dispatch_table = copyreg.dispatch_table.copy()
        self.dispatch_table[MethodType] = _reduce_method

    def persistent_id(self, obj: object) -> str | None:
        return _ROOT_ID if obj is self.root else None


class _SnapshotUnpickler(pickle.Unpickler):
    def __init__(self, stream: BinaryIO, root: object):
        super().__init__(stream)
        self.root = root

    def persistent_load(self, pid: object) -> object:
        if isinstance(pid, str) and pid == _ROOT_ID:
            return self.root
        raise pickle.UnpicklingError(f"unknown persistent reference: {pid!r}")


class _InputPickler(_SnapshotPickler):
    def __init__(self, stream: BinaryIO, root: object, env: TaskLocalEnv):
        super().__init__(stream, root)
        self.references = InputEncoder(root, env)

    def persistent_id(self, obj: object) -> Any:
        if obj is vars(self.root):
            return "gv_attributes"
        if obj is self.root:
            return _ROOT_ID
        path = self.references.gv_path(obj)
        if path is None or any("attribute" not in step for step in path):
            # A container position describes the old GV, not the identity of
            # a Signal argument in the candidate. Keep that argument frozen.
            return None
        return (
            "gv_member",
            tuple(step["attribute"] for step in path),
            type(obj).__module__,
            type(obj).__qualname__,
        )


class _InputUnpickler(_SnapshotUnpickler):
    def __init__(self, stream: BinaryIO, root: object, candidate: dict[str, Any]):
        super().__init__(stream, root)
        self.candidate = candidate

    def persistent_load(self, pid: object) -> object:
        if pid == "gv_attributes":
            return self.candidate
        if isinstance(pid, tuple) and pid[0] == "gv_member":
            _, names, module, name = pid
            try:
                value = self.candidate[names[0]]
                for attribute in names[1:]:
                    value = vars(value)[attribute]
            except (KeyError, TypeError) as error:
                raise SnapshotCompatibilityError(
                    f"candidate lacks GV member {'.'.join(names)}"
                ) from error
            if (type(value).__module__, type(value).__qualname__) != (module, name):
                raise SnapshotCompatibilityError(
                    f"candidate has an incompatible type for GV member {'.'.join(names)}"
                )
            return value
        return super().persistent_load(pid)


def freeze_input(root: object, env: TaskLocalEnv, execution: object) -> bytes:
    """Copy an I point's execution state with references to GV attributes."""
    stream = BytesIO()
    try:
        _InputPickler(stream, root, env).dump(execution)
    except (pickle.PickleError, AttributeError, TypeError) as error:
        raise SnapshotError(f"cannot capture I point: {error}") from error
    return stream.getvalue()


def bind_input(root: object, candidate: dict[str, Any], frozen: bytes) -> object:
    """Restore a fresh execution graph bound to the candidate's GV members."""
    return _InputUnpickler(BytesIO(frozen), root, candidate).load()


def _encode_header(metadata: SnapshotMetadata) -> bytes:
    header = (
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if len(header) > _MAX_HEADER_BYTES:
        raise SnapshotFormatError("oversized snapshot header")
    return header


def _read_header(stream: BinaryIO) -> SnapshotMetadata:
    if stream.readline(len(MAGIC) + 1) != MAGIC:
        raise SnapshotFormatError("invalid snapshot magic line")
    header = stream.readline(_MAX_HEADER_BYTES + 1)
    if len(header) > _MAX_HEADER_BYTES or not header.endswith(b"\n"):
        raise SnapshotFormatError("missing or oversized snapshot header")
    try:
        metadata = json.loads(header.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise SnapshotFormatError(f"invalid UTF-8 JSON snapshot header: {exc}") from exc
    metadata = _validate_metadata(metadata)
    if stream.readline(2) != b"\n":
        raise SnapshotFormatError("missing blank line before snapshot payload")
    return metadata


def _validate_metadata(metadata: Any) -> SnapshotMetadata:
    if not isinstance(metadata, dict):
        raise SnapshotFormatError("snapshot header must be a JSON object")
    for name in ("format_version", "model_version"):
        if type(metadata.get(name)) is not int:
            raise SnapshotFormatError(f"snapshot header requires integer {name}")
    for name in ("checkpoint", "created_at", "payload_sha256"):
        if not isinstance(metadata.get(name), str) or not metadata[name]:
            raise SnapshotFormatError(f"snapshot header requires nonempty {name}")
    if "input_point" in metadata and (
        not isinstance(metadata["input_point"], str) or not metadata["input_point"]
    ):
        raise SnapshotFormatError("input_point must be a nonempty string")
    for name in ("notified", "waits_for"):
        if name in metadata and (
            not isinstance(metadata[name], list)
            or any(
                not isinstance(item, str)
                or not all(part.isidentifier() for part in item.split("."))
                for item in metadata[name]
            )
        ):
            raise SnapshotFormatError(f"invalid BusyWait instance names in {name}")
    try:
        created_at = datetime.fromisoformat(metadata["created_at"])
    except ValueError as exc:
        raise SnapshotFormatError("invalid snapshot creation time") from exc
    if created_at.utcoffset() != UTC.utcoffset(None):
        raise SnapshotFormatError("snapshot creation time must be in UTC")
    for name in ("payload_sha256", "gv_sha256"):
        if name in metadata:
            digest = metadata[name]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise SnapshotFormatError(f"invalid snapshot {name} checksum")
    return cast(SnapshotMetadata, metadata)


def _new_metadata(
    *,
    checkpoint: str,
    model_version: int,
    input_point: str | None = None,
    format_version: int,
    notified: frozenset[str],
    waits_for: frozenset[str],
) -> SnapshotMetadata:
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ValueError("checkpoint must be a nonempty string")
    if input_point is not None and (
        not isinstance(input_point, str) or not input_point
    ):
        raise ValueError("input_point must be a nonempty string")
    metadata: SnapshotMetadata = {
        "format_version": format_version,
        "model_version": model_version,
        "checkpoint": checkpoint,
        "created_at": datetime.now(UTC).isoformat(),
        "payload_sha256": "0" * 64,
    }
    if input_point is not None:
        metadata["input_point"] = input_point
    if notified:
        metadata["notified"] = sorted(notified)
    if waits_for:
        metadata["waits_for"] = sorted(waits_for)
    return metadata


def _gv_digest(root: object) -> str:
    stream = BytesIO()
    _SnapshotPickler(stream, root).dump(vars(root))
    return hashlib.sha256(stream.getbuffer()).hexdigest()


def save_output_snapshot(
    root: object,
    path: Path,
    *,
    model_version: int,
    input_point: str | None,
    continuation: object | None,
    notified: frozenset[str] = frozenset(),
    waits_for: frozenset[str] = frozenset(),
    capture_gv_digest: bool = False,
) -> SnapshotMetadata:
    """Save a FlowRun output without replacing an existing logical name."""
    if path.exists() or path.name in _index_entries(path.parent):
        raise SnapshotError(f"refusing to overwrite output snapshot {path}")
    batch = _OUTPUT_BATCH.get()
    if batch is None or (continuation is not None and not batch.pack_breaks):
        return save_snapshot(
            root,
            path,
            checkpoint=path.stem,
            model_version=model_version,
            input_point=input_point,
            continuation=continuation,
            notified=notified,
            waits_for=waits_for,
            capture_gv_digest=capture_gv_digest,
        )

    metadata = _new_metadata(
        checkpoint=path.stem,
        model_version=model_version,
        input_point=input_point,
        format_version=FORMAT_VERSION
        if continuation is None
        else CONTINUATION_FORMAT_VERSION,
        notified=notified,
        waits_for=waits_for,
    )
    try:
        stream = BytesIO()
        state = vars(root)
        payload = (
            state
            if continuation is None
            else {"gv": state, "continuation": continuation}
        )
        _SnapshotPickler(stream, root).dump(payload)
        body = stream.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        metadata["payload_sha256"] = digest
        index_path = path.parent.resolve() / _INDEX_NAME
        if continuation is not None:
            if capture_gv_digest:
                metadata["gv_sha256"] = _gv_digest(root)
            span = _append_break(batch, index_path.parent / _BREAK_PACK_NAME, body)
            batch.indexes[index_path][path.name] = {**metadata, "pack": span}
        else:
            body_path = _body_path(path.parent, digest)
            if body_path not in batch.checked_bodies:
                body_path.parent.mkdir(exist_ok=True)
                try:
                    previous = body_path.read_bytes()
                except FileNotFoundError:
                    _write_atomic(body_path, body)
                else:
                    if previous != body:
                        raise SnapshotFormatError(
                            f"snapshot body differs from its digest: {body_path}"
                        )
                batch.checked_bodies.add(body_path)
            batch.indexes[index_path][path.name] = metadata
        batch.dirty.add(index_path)
    except Exception as error:
        raise SnapshotError(f"cannot save snapshot {path}: {error}") from error
    return metadata


def save_snapshot(
    root: object,
    path: str | os.PathLike[str],
    *,
    checkpoint: str,
    model_version: int,
    input_point: str | None = None,
    continuation: object | None = None,
    notified: frozenset[str] = frozenset(),
    waits_for: frozenset[str] = frozenset(),
    capture_gv_digest: bool = False,
) -> SnapshotMetadata:
    """Save all instance attributes, committing only a fully written file.

    The parent directory must exist. Root references use a persistent Pickle
    ID, while the attribute dictionary itself remains part of the object graph.
    """
    path = Path(path)
    metadata = _new_metadata(
        checkpoint=checkpoint,
        model_version=model_version,
        input_point=input_point,
        format_version=FORMAT_VERSION
        if continuation is None
        else CONTINUATION_FORMAT_VERSION,
        notified=notified,
        waits_for=waits_for,
    )
    temporary_path: Path | None = None
    try:
        if continuation is not None and capture_gv_digest:
            metadata["gv_sha256"] = _gv_digest(root)
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            stream = cast(BufferedRandom, temporary.file)
            stream.write(MAGIC)
            stream.write(_encode_header(metadata))
            stream.write(b"\n")
            payload_offset = stream.tell()
            payload = (
                vars(root)
                if continuation is None
                else {"gv": vars(root), "continuation": continuation}
            )
            _SnapshotPickler(stream, root).dump(payload)
            stream.flush()
            stream.seek(payload_offset)
            metadata["payload_sha256"] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
            # The hexadecimal digest has a fixed width, so rewriting the header
            # cannot move the payload or invalidate any Pickle references.
            stream.seek(len(MAGIC))
            stream.write(_encode_header(metadata))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception as exc:
        raise SnapshotError(f"cannot save snapshot {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return metadata


def read_snapshot(
    root: object,
    path: str | os.PathLike[str],
    *,
    model_version: int,
    expected_checkpoint: str | None = None,
) -> tuple[SnapshotMetadata, dict[str, Any], object | None]:
    """Read a trusted snapshot without installing its state on root."""
    path = Path(path)
    try:
        with ExitStack() as files:
            try:
                stream = files.enter_context(path.open("rb"))
            except FileNotFoundError:
                if path.parent.name != "outputs":
                    raise
                entry = _index_entries(path.parent).get(path.name)
                if entry is None:
                    raise
                if not isinstance(entry, dict):
                    raise SnapshotFormatError("invalid indexed output metadata")
                header = dict(entry)
                span = header.pop("pack", None)
                metadata = _validate_metadata(header)
                if metadata["checkpoint"] != path.stem:
                    raise SnapshotFormatError("invalid indexed output checkpoint")
                if metadata["format_version"] == FORMAT_VERSION and span is None:
                    body = _body_path(path.parent, metadata["payload_sha256"])
                    stream = files.enter_context(body.open("rb"))
                elif metadata["format_version"] == CONTINUATION_FORMAT_VERSION:
                    pack_path = path.parent.resolve() / _BREAK_PACK_NAME
                    stream = files.enter_context(_read_packed_break(pack_path, span))
                else:
                    raise SnapshotFormatError("invalid indexed output format")
            else:
                metadata = _read_header(stream)
            if metadata["format_version"] not in (
                FORMAT_VERSION,
                CONTINUATION_FORMAT_VERSION,
            ):
                raise SnapshotCompatibilityError(
                    f"incompatible format_version: got {metadata['format_version']!r}"
                )
            if metadata["model_version"] != model_version:
                raise SnapshotCompatibilityError(
                    f"incompatible model_version: expected {model_version!r}, "
                    f"got {metadata['model_version']!r}"
                )
            if (
                expected_checkpoint is not None
                and metadata["checkpoint"] != expected_checkpoint
            ):
                raise SnapshotCompatibilityError(
                    f"checkpoint mismatch: expected {expected_checkpoint!r}, "
                    f"got {metadata['checkpoint']!r}"
                )
            payload_offset = stream.tell()
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != metadata["payload_sha256"]:
                raise SnapshotFormatError("snapshot payload checksum mismatch")
            stream.seek(payload_offset)
            try:
                payload = _SnapshotUnpickler(stream, root).load()
            except Exception as exc:
                raise SnapshotFormatError(
                    f"cannot deserialize snapshot: {exc}"
                ) from exc
            continuation = None
            state = payload
            if metadata["format_version"] == CONTINUATION_FORMAT_VERSION:
                if (
                    not isinstance(payload, dict)
                    or payload.keys() != {"gv", "continuation"}
                    or payload["continuation"] is None
                ):
                    raise SnapshotFormatError("invalid continuation payload")
                state, continuation = payload["gv"], payload["continuation"]
            if not isinstance(state, dict) or any(
                not isinstance(name, str) for name in state
            ):
                raise SnapshotFormatError(
                    "snapshot payload must be an instance dictionary"
                )
            if stream.read(1):
                raise SnapshotFormatError("unexpected data after snapshot payload")
    except SnapshotError as exc:
        raise type(exc)(f"cannot load snapshot {path}: {exc}") from exc
    except OSError as exc:
        raise SnapshotError(f"cannot load snapshot {path}: {exc}") from exc
    return metadata, state, continuation


def load_snapshot(
    root: object,
    path: str | os.PathLike[str],
    *,
    model_version: int,
    expected_checkpoint: str | None = None,
) -> SnapshotMetadata:
    """Validate a trusted file and replace root's state with its GV."""
    metadata, state, _ = read_snapshot(
        root, path, model_version=model_version, expected_checkpoint=expected_checkpoint
    )
    # Assign the deserialized dictionary itself to preserve aliases and cycles
    # involving root.__dict__. No live state is replaced until all checks pass.
    root.__dict__ = state
    return metadata

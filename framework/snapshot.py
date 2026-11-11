"""Versioned, atomic file snapshots of an instance's Pickle object graph."""

from __future__ import annotations

import copyreg
import hashlib
import json
import os
import pickle
import tempfile
from datetime import UTC, datetime
from io import BufferedRandom
from pathlib import Path
from types import MethodType
from typing import Any, BinaryIO, TypedDict, cast

MAGIC = b"M_XV6_SNAPSHOT\n"
FORMAT_VERSION = 1
_MAX_HEADER_BYTES = 64 * 1024
_ROOT_ID = "snapshot_root"


class SnapshotMetadata(TypedDict):
    format_version: int
    model_version: int
    checkpoint: str
    created_at: str
    payload_sha256: str


class SnapshotError(Exception):
    """A snapshot could not be saved or loaded."""


class SnapshotFormatError(SnapshotError):
    """A snapshot is malformed or its payload is damaged."""


class SnapshotCompatibilityError(SnapshotError):
    """A snapshot's version or checkpoint does not match the caller."""


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
    if not isinstance(metadata, dict):
        raise SnapshotFormatError("snapshot header must be a JSON object")
    for name in ("format_version", "model_version"):
        if type(metadata.get(name)) is not int:
            raise SnapshotFormatError(f"snapshot header requires integer {name}")
    for name in ("checkpoint", "created_at", "payload_sha256"):
        if not isinstance(metadata.get(name), str) or not metadata[name]:
            raise SnapshotFormatError(f"snapshot header requires nonempty {name}")
    try:
        created_at = datetime.fromisoformat(metadata["created_at"])
    except ValueError as exc:
        raise SnapshotFormatError("invalid snapshot creation time") from exc
    if created_at.utcoffset() != UTC.utcoffset(None):
        raise SnapshotFormatError("snapshot creation time must be in UTC")
    digest = metadata["payload_sha256"]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise SnapshotFormatError("invalid snapshot payload checksum")
    if stream.readline(2) != b"\n":
        raise SnapshotFormatError("missing blank line before snapshot payload")
    return cast(SnapshotMetadata, metadata)


def save_snapshot(
    root: object,
    path: str | os.PathLike[str],
    *,
    checkpoint: str,
    model_version: int,
) -> SnapshotMetadata:
    """Save all instance attributes, committing only a fully written file.

    The parent directory must exist. Root references use a persistent Pickle
    ID, while the attribute dictionary itself remains part of the object graph.
    """
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ValueError("checkpoint must be a nonempty string")
    path = Path(path)
    metadata: SnapshotMetadata = {
        "format_version": FORMAT_VERSION,
        "model_version": model_version,
        "checkpoint": checkpoint,
        "created_at": datetime.now(UTC).isoformat(),
        "payload_sha256": "0" * 64,
    }
    temporary_path: Path | None = None
    try:
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
            _SnapshotPickler(stream, root).dump(vars(root))
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


def load_snapshot(
    root: object,
    path: str | os.PathLike[str],
    *,
    model_version: int,
    expected_checkpoint: str | None = None,
) -> SnapshotMetadata:
    """Validate and deserialize a trusted file, then replace root's state.

    Pickle can execute code: only load files from trusted sources. Existing
    references to root stay valid; references to its old children stay stale.
    """
    path = Path(path)
    try:
        with path.open("rb") as stream:
            metadata = _read_header(stream)
            for name, expected in (
                ("format_version", FORMAT_VERSION),
                ("model_version", model_version),
            ):
                actual = metadata[name]
                if actual != expected:
                    raise SnapshotCompatibilityError(
                        f"incompatible {name}: expected {expected!r}, got {actual!r}"
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
                state = _SnapshotUnpickler(stream, root).load()
            except Exception as exc:
                raise SnapshotFormatError(
                    f"cannot deserialize snapshot: {exc}"
                ) from exc
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
    # Assign the deserialized dictionary itself to preserve aliases and cycles
    # involving root.__dict__. No live state is replaced until all checks pass.
    root.__dict__ = state
    return metadata

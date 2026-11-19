"""Reference-aware descriptions of operation inputs, without execution state."""

from __future__ import annotations

import base64
import math
from collections import deque
from enum import Enum
from types import MethodType
from typing import Any

from framework.engine import System, TaskLocalEnv


class InputEncodingError(ValueError):
    """An operation input cannot be represented in a point record."""


class InputEncoder:
    """GV references are paths; other objects have record-local graph IDs.

    Paths describe the input snapshot, not identities across different GV
    snapshots. Cross-snapshot rebinding is deliberately outside this codec.
    """

    def __init__(self, root: object, env: TaskLocalEnv):
        self.env = env
        self.objects: list[dict[str, Any]] = []
        self._local_ids: dict[int, int] = {}
        self._gv_paths: dict[int, list[dict[str, Any]]] = {}
        pending = deque([(root, [])])
        while pending:
            value, path = pending.popleft()
            if self._scalar(value) or isinstance(value, (Enum, type)):
                continue
            if value is env or value is env.cv or id(value) in self._gv_paths:
                continue
            self._gv_paths[id(value)] = path
            if isinstance(value, dict):
                for index, (key, item) in enumerate(value.items()):
                    pending.append((key, path + [{"dict_key": index}]))
                    pending.append((item, path + [{"dict_value": index}]))
            elif isinstance(value, (list, tuple)):
                pending.extend(
                    (item, path + [{"index": index}])
                    for index, item in enumerate(value)
                )
            elif isinstance(value, (set, frozenset)):
                # Sets themselves have paths, but their iteration order does
                # not identify members across snapshot deserialization.
                if any(
                    not self._scalar(item) and not isinstance(item, Enum)
                    for item in value
                ):
                    raise InputEncodingError(
                        "GV sets with object members have no stable member paths"
                    )
            elif isinstance(value, MethodType):
                pending.append((value.__self__, path + [{"method_owner": True}]))
            elif hasattr(value, "__dict__"):
                pending.extend(
                    (item, path + [{"attribute": name}])
                    for name, item in sorted(vars(value).items())
                )

    @staticmethod
    def _scalar(value: Any) -> bool:
        return value is None or type(value) in (bool, int, float, str, bytes)

    @staticmethod
    def _type_name(value: type) -> dict[str, str]:
        return {"module": value.__module__, "name": value.__qualname__}

    def fields(self, values: dict[str, Any]) -> dict[str, Any]:
        result = {name: self._encode(value) for name, value in values.items()}
        result["objects"] = self.objects
        return result

    def gv_path(self, value: object) -> list[dict[str, Any]] | None:
        return self._gv_paths.get(id(value))

    def _encode(self, value: Any) -> Any:
        if isinstance(value, Enum):
            return {"enum": self._type_name(type(value)), "member": value.name}
        if value is None or type(value) in (bool, int, str):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else {"float": str(value)}
        if isinstance(value, bytes):
            return {"bytes": base64.b64encode(value).decode("ascii")}
        if value is self.env:
            return {"environment": []}
        if value is self.env.cv:
            return {"environment": ["cv"]}
        if (
            isinstance(value, TaskLocalEnv)
            or value is self.env.signal_queues
            or isinstance(value, deque)
        ):
            raise InputEncodingError("Env and signal queues cannot be persisted")
        if id(value) in self._gv_paths:
            return {"gv": self._gv_paths[id(value)]}
        if isinstance(value, type) and issubclass(value, System):
            return {"type": self._type_name(value)}
        if id(value) in self._local_ids:
            return {"local": self._local_ids[id(value)]}
        number = len(self.objects)
        self._local_ids[id(value)] = number
        entry: dict[str, Any] = {}
        self.objects.append(entry)
        if isinstance(value, MethodType):
            entry.update(
                kind="method",
                function={
                    "module": value.__func__.__module__,
                    "name": value.__func__.__qualname__,
                },
                owner=self._encode(value.__self__),
            )
        elif isinstance(value, dict):
            entry.update(
                kind="dict",
                items=[
                    [self._encode(key), self._encode(item)]
                    for key, item in value.items()
                ],
            )
        elif isinstance(value, (list, tuple, set, frozenset)):
            entry.update(
                kind=type(value).__name__, items=[self._encode(item) for item in value]
            )
        elif isinstance(value, System):
            entry.update(
                kind="model",
                type=self._type_name(type(value)),
                attributes={
                    name: self._encode(item)
                    for name, item in sorted(vars(value).items())
                },
            )
        else:
            raise InputEncodingError(
                f"unsupported operation input type: {type(value).__module__}.{type(value).__qualname__}"
            )
        return {"local": number}

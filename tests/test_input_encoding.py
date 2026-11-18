"""Operation input descriptions preserve values, aliases and model references."""

import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest

from framework.engine import System, TaskLocalEnv
from framework.input_encoding import InputEncoder, InputEncodingError


class State(Enum):
    READY = "ready"


@dataclass
class Model(System):
    value: int = 1
    child: Any = None

    def callback(self):
        pass


@dataclass
class Root:
    model: Model = field(default_factory=Model)
    items: list[Any] = field(default_factory=list)
    callback: Any = None


def test_gv_references_use_paths_and_bound_methods_refer_to_their_owner():
    root, env = Root(), TaskLocalEnv(name="example")
    root.items.append(root.model)
    root.model.child = root
    data = InputEncoder(root, env).fields(
        {"model": root.model, "alias": root.items[0], "callback": root.model.callback}
    )
    assert data["model"] == data["alias"] == {"gv": [{"attribute": "model"}]}
    callback = data["objects"][data["callback"]["local"]]
    assert callback["kind"] == "method"
    assert callback["owner"] == data["model"]
    assert callback["function"]["name"] == "Model.callback"
    assert "0x" not in json.dumps(data)


def test_object_reachable_only_through_a_gv_callback_has_a_gv_path():
    root, env = Root(), TaskLocalEnv(name="example")
    owner = Model()
    root.callback = owner.callback
    data = InputEncoder(root, env).fields({"owner": owner})
    assert data["owner"] == {"gv": [{"attribute": "callback"}, {"method_owner": True}]}


def test_local_models_cycles_and_aliases_are_encoded_without_mutable_live_references():
    root, env = Root(), TaskLocalEnv(name="example")
    local = Model()
    local.child = local
    values: list[Any] = [local, local]
    values.append(values)
    data = InputEncoder(root, env).fields(
        {"values": values, "callback": local.callback}
    )
    sequence = data["objects"][data["values"]["local"]]
    first, second, cycle = sequence["items"]
    assert first == second
    assert cycle == data["values"]
    model = data["objects"][first["local"]]
    assert model["kind"] == "model"
    assert model["attributes"]["child"] == first
    callback = data["objects"][data["callback"]["local"]]
    assert callback["owner"] == first
    frozen = json.dumps(data, sort_keys=True)
    local.value = 99
    values.clear()
    assert json.dumps(data, sort_keys=True) == frozen


def test_scalar_container_enum_and_environment_descriptions_are_json_values():
    env = TaskLocalEnv(name="example")
    data = InputEncoder(Root(), env).fields(
        {
            "state": State.READY,
            "payload": b"hello",
            "env": env,
            "cv": env.cv,
            "mapping": {1: (False, None, "x")},
            "set": {1, 2},
            "infinity": float("inf"),
        }
    )
    assert data["state"]["member"] == "READY"
    assert data["payload"] == {"bytes": "aGVsbG8="}
    assert data["env"] == {"environment": []}
    assert data["cv"] == {"environment": ["cv"]}
    assert "stacks" not in json.dumps(data, allow_nan=False)


@pytest.mark.parametrize(
    "value", [object(), lambda: None, SimpleNamespace(value=1), deque()]
)
def test_unrepresentable_values_fail_explicitly(value):
    with pytest.raises(InputEncodingError):
        InputEncoder(Root(), TaskLocalEnv(name="example")).fields({"value": value})


def test_queues_cannot_be_encoded_through_their_container():
    env = TaskLocalEnv(name="example")
    with pytest.raises(InputEncodingError, match="queues"):
        InputEncoder(Root(), env).fields({"queue": env.signal_queues})

"""Iterative spec execution with explicit Signal frames and return destinations."""

from __future__ import annotations

import ast
import hashlib
import operator
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from framework.contracts import ContractViolation, Phase
from framework.engine import FlowPaused, Signal, TaskLocalEnv, _get_target_ref
from framework.flow_run import CaptureInput, FlowRun, Fork, RunPosition, require_run
from framework.snapshot import (
    SnapshotCompatibilityError,
    bind_input,
    freeze_input,
    read_snapshot,
)
from framework.spec import (
    Branch,
    Expression,
    Method,
    SpecError,
    Specification,
    SpecSystem,
)


@dataclass
class Frame:
    signal: Signal
    system: SpecSystem
    specification: Specification
    method: Method
    locals: dict[str, Any]
    branch: Branch | None = None
    branch_index: int = 0
    position: int = 0
    guards: list[Any] = field(default_factory=list)


@dataclass
class Drive:
    queue: deque[Signal]
    caller_point: str | None = None
    first: bool = True
    result: Any = None

    def accept(self, result: Any) -> None:
        if self.first:
            self.first = False
            self.result = result


@dataclass
class FrameState:
    signal: Signal
    system: SpecSystem
    locals: dict[str, Any]
    specification: str
    branch_index: int
    position: int
    guards: list[Any]


@dataclass
class ExecutionState:
    owner: SpecSystem | None
    env: TaskLocalEnv
    frames: list[FrameState]
    drives: list[Drive]
    specs: dict[Path, str]


@dataclass
class Continuation(ExecutionState):
    history: RunPosition


@dataclass
class InputState(ExecutionState):
    point: str
    pending: frozenset[str]
    signal: Signal | None


class Interpreter:
    """Model recursion lives in frames, rather than the Python execution stack."""

    def __init__(self):
        self.frames: list[Frame] = []
        self.drives: list[Drive] = []
        self.owner: SpecSystem | None = None
        self.can_pause = False

    def run(self, signal: Signal) -> Any:
        run = require_run(signal.env)
        self._check_root(run.root)
        system = self._target(signal)
        if isinstance(system, SpecSystem):
            cpu_flow = system.__spec_registry__.types.get("CpuFlow")
            if cpu_flow is not None and isinstance(system, cpu_flow):
                self.owner = system
        queues = signal.env.signal_queues
        self.can_pause = len(queues) == 1 and queues[0] is signal.queue
        signal.queue.appendleft(signal)
        self.drives.append(Drive(signal.queue))
        return self._execute(run)

    @staticmethod
    def _check_root(root: object) -> None:
        from global_vars import gv

        if root is not gv:
            raise RuntimeError("FlowRun root must be the current gv")

    @staticmethod
    def _target(signal: Signal):
        return (
            signal.source if signal.target == "self" else _get_target_ref(signal.target)
        )

    def _execute(self, run: FlowRun, replay: InputState | None = None) -> Any:
        try:
            if replay is not None:
                frame = self.frames[-1]
                assert frame.branch is not None
                self._action(
                    frame, frame.branch.instructions[frame.position - 1], replay.signal
                )
            result = None
            while self.drives:
                drive = self.drives[-1]
                if len(self.frames) < len(self.drives):
                    if drive.queue:
                        signal = drive.queue.popleft()
                        system = self._target(signal)
                        if hasattr(getattr(system, signal.action), "__spec_action__"):
                            assert isinstance(system, SpecSystem)
                            self._push(signal, system)
                        else:
                            drive.accept(signal.handle())
                    else:
                        self._finish_drive(run, drive)
                        result = drive.result
                    continue
                frame = self.frames[-1]
                assert frame.branch is not None
                if frame.position >= len(frame.branch.instructions):
                    drive.accept(self._finish(frame))
                else:
                    instruction = frame.branch.instructions[frame.position]
                    frame.position += 1
                    self._step(frame, instruction)
            return result
        except FlowPaused:
            raise
        except BaseException as error:
            run.failed = True
            frames, self.frames = self.frames, []
            drives, self.drives = self.drives, []
            with ExitStack() as cleanup:
                for index, drive in enumerate(drives):
                    if drive.caller_point is not None:
                        cleanup.callback(self._close_drive, run.env, drive)
                    if index < len(frames):
                        cleanup.callback(self._close, frames[index], error)
                raise

    def _globals(self, frame: Frame) -> dict[str, Any]:
        from global_vars import gv

        registry = frame.system.__spec_registry__
        return dict(registry.namespace, gv=gv)

    def _eval(self, frame: Frame, expression: ast.expr | str) -> Any:
        if isinstance(expression, str):
            expression = ast.parse(expression, mode="eval").body
        return eval(
            compile(ast.Expression(expression), str(frame.specification.path), "eval"),
            self._globals(frame),
            frame.locals,
        )

    def _conditions(self, frame: Frame, expressions: list[Expression]) -> bool:
        values = [self._eval(frame, expression.text) for expression in expressions]
        if any(type(value) is not bool for value in values):
            raise TypeError(
                f"{frame.specification.path}: conditions must evaluate to bool"
            )
        return any(values)

    def _violation(self, frame: Frame, phase: Phase, expressions: list[Expression]):
        raise ContractViolation(
            phase,
            str(frame.specification.path),
            expressions[0].line if expressions else frame.method.line,
            f"{frame.specification.name}.{frame.method.name}",
            branch_index=frame.branch_index if phase == "ensures" else None,
        )

    def _push(self, signal: Signal, system: SpecSystem):
        registry = system.__spec_registry__
        specification, method = registry.method(system, signal.action)
        system.acquire(signal.env, signal.action)
        frame = Frame(signal, system, specification, method, {})
        self.frames.append(frame)
        bound = method.signature.bind(**signal.args)
        bound.apply_defaults()
        frame.locals = dict(bound.arguments, self=system, env=signal.env)
        matches = [
            index
            for index, branch in enumerate(method.branches)
            if branch.requires is None or self._conditions(frame, branch.requires)
        ]
        if not matches:
            self._violation(
                frame,
                "requires",
                [
                    expression
                    for branch in method.branches
                    for expression in (branch.requires or [])
                ],
            )
        if len(matches) != 1:
            raise ValueError(
                f"{specification.path}:{method.line}: requires branches overlap: {matches}"
            )
        frame.branch_index = matches[0]
        frame.branch = method.branches[matches[0]]

    def _assign(self, frame: Frame, target: ast.AST, value: Any):
        match target:
            case ast.Name(id=name):
                frame.locals[name] = value
            case ast.Attribute(value=owner, attr=name):
                setattr(self._eval(frame, owner), name, value)
            case ast.Subscript(value=owner, slice=index):
                self._eval(frame, owner)[self._eval(frame, index)] = value
            case ast.Tuple(elts=items) | ast.List(elts=items):
                values = list(value)
                if len(items) != len(values):
                    raise ValueError("assignment has mismatched unpacking lengths")
                for item, item_value in zip(items, values):
                    self._assign(frame, item, item_value)
            case _:
                raise SpecError("unsupported assignment target")

    def _statement(self, frame: Frame, node: ast.stmt):
        match node:
            case ast.Pass():
                pass
            case ast.Expr(value=value):
                self._eval(frame, value)
            case ast.Assign(targets=targets, value=expression):
                value = self._eval(frame, expression)
                for target in targets:
                    self._assign(frame, target, value)
            case ast.AnnAssign(target=target, value=expression) if (
                expression is not None
            ):
                self._assign(frame, target, self._eval(frame, expression))
            case ast.Assert(test=condition, msg=message):
                if not self._eval(frame, condition):
                    detail = (
                        "assertion failed"
                        if message is None
                        else self._eval(frame, message)
                    )
                    raise AssertionError(
                        f"{frame.specification.path}:{node.lineno}: {detail}"
                    )
            case ast.AugAssign(target=target, op=operation, value=value):
                operations = {
                    ast.Add: operator.iadd,
                    ast.Sub: operator.isub,
                    ast.Mult: operator.imul,
                }
                function = operations.get(type(operation))
                if function is None:
                    raise SpecError("unsupported augmented assignment")
                if isinstance(target, ast.Name):
                    previous = frame.locals[target.id]
                elif isinstance(target, ast.Attribute):
                    previous = getattr(self._eval(frame, target.value), target.attr)
                elif isinstance(target, ast.Subscript):
                    previous = self._eval(frame, target.value)[
                        self._eval(frame, target.slice)
                    ]
                else:
                    raise SpecError("unsupported augmented assignment target")
                self._assign(
                    frame, target, function(previous, self._eval(frame, value))
                )
            case _:
                raise SpecError(f"unsupported statement {type(node).__name__}")

    def _step(self, frame: Frame, instruction):
        node = instruction.node
        match instruction.operation:
            case "enter":
                guard = self._eval(frame, node)
                guard.__enter__()
                frame.guards.append(guard)
            case "leave":
                frame.guards.pop().__exit__(None, None, None)
            case "statement" | "break":
                if instruction.site is None:
                    self._statement(frame, node)
                else:
                    self._action(frame, instruction, None)
            case "drive" | "emit":
                call = node.value
                assert isinstance(call, ast.Call)
                operation = instruction.operation
                args = [self._eval(frame, arg) for arg in call.args]
                if len(args) != 2:
                    raise SpecError(f"{operation} expects target and action")
                kwargs = {}
                for keyword in call.keywords:
                    value = self._eval(frame, keyword.value)
                    if keyword.arg is None:
                        kwargs.update(value)
                    else:
                        kwargs[keyword.arg] = value
                target, action = args
                if not isinstance(target, str) or not isinstance(action, str):
                    raise SpecError("Signal target and action must be strings")
                queue = frame.signal.queue if operation == "emit" else deque()
                signal = Signal(
                    frame.system, target, action, kwargs, frame.signal.env, queue
                )
                self._action(frame, instruction, signal)
            case _:
                raise SpecError(f"unknown instruction {instruction.operation}")

    def _action(self, frame: Frame, instruction, signal: Signal | None) -> None:
        assert instruction.site is not None
        operation = instruction.operation
        env = frame.signal.env
        run = require_run(env)
        values = (
            {
                "source": signal.source,
                "target": signal.target,
                "action": signal.action,
                "args": signal.args,
            }
            if signal is not None
            else {
                "source": frame.system,
                "bindings": {
                    name: value
                    for name, value in frame.locals.items()
                    if name not in {"self", "env"}
                },
            }
        )
        point = run.begin(
            frame.specification,
            frame.method.name,
            frame.branch_index,
            instruction.site,
            "bare" if operation == "statement" else operation,
            values,
        )
        if (capture := run.capture_input) is not None:
            if not self.can_pause:
                raise SpecError("capturing an I point requires a resumable spec stack")
            run.capture_input = None
            saved = InputState(
                self.owner,
                env,
                self._frame_states(),
                self.drives,
                self._spec_fingerprints(),
                point,
                run.pending_before(point),
                signal,
            )
            capture(point, freeze_input(run.root, env, saved))

        if signal is None:
            node = instruction.node
            if operation == "break":
                self._break(run, frame, point, node.value.args[0])
            self._statement(frame, node)
            run.complete(point)
        else:
            signal.queue.append(signal)
            if operation == "emit":
                run.complete(point)
            else:
                depth = env.depth
                print(
                    f"{'    ' * depth}{frame.system}:\n"
                    f"{'    ' * (depth + 1)}{signal.action} -> {signal.target}"
                )
                env.depth += 1
                env.signal_queues.append(signal.queue)
                self.drives.append(Drive(signal.queue, point))

    def _close(self, frame: Frame, error: BaseException | None = None):
        try:
            while frame.guards:
                frame.guards.pop().__exit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
        finally:
            frame.system.release(frame.signal.env, frame.signal.action)

    @staticmethod
    def _close_drive(env: TaskLocalEnv, drive: Drive) -> None:
        assert env.signal_queues[-1] is drive.queue
        env.signal_queues.pop()
        env.depth -= 1

    @staticmethod
    def _drive_receiver(parent: Frame) -> ast.AST | None:
        assert parent.branch is not None
        instruction = parent.branch.instructions[parent.position - 1]
        assert instruction.operation == "drive"
        return (
            instruction.node.targets[0]
            if isinstance(instruction.node, ast.Assign)
            else None
        )

    def _finish_drive(self, run: FlowRun, drive: Drive) -> None:
        assert not drive.first
        self.drives.pop()
        if drive.caller_point is not None:
            self._close_drive(run.env, drive)
            parent = self.frames[-1]
            receiver = self._drive_receiver(parent)
            if receiver is not None:
                self._assign(parent, receiver, drive.result)
            run.complete(drive.caller_point)

    def _check_ensures(self, frame: Frame) -> None:
        assert frame.branch is not None
        if frame.branch.ensures is not None and not self._conditions(
            frame, frame.branch.ensures
        ):
            self._violation(frame, "ensures", frame.branch.ensures)

    def _spec_fingerprints(self) -> dict[Path, str]:
        return {
            spec.path.resolve(): spec.fingerprint
            for frame in self.frames
            for spec in frame.system.__spec_registry__.specifications.values()
        }

    def _frame_states(self) -> list[FrameState]:
        return [
            FrameState(
                frame.signal,
                frame.system,
                frame.locals,
                frame.specification.name,
                frame.branch_index,
                frame.position,
                frame.guards,
            )
            for frame in self.frames
        ]

    def _break(
        self, run: FlowRun, frame: Frame, point: str, expression: ast.expr
    ) -> None:
        owner = self.owner
        if owner is None or not self.can_pause:
            raise SpecError(
                "break requires a CpuFlow derivation with a resumable spec stack"
            )
        contract = next(
            (active for active in reversed(self.frames) if active.system is owner), None
        )
        if contract is None:
            raise SpecError("break requires an active contract of its CpuFlow instance")
        state = self._eval(frame, expression)
        states = owner.__spec_registry__.namespace.get("CpuFlowState")
        if type(state) is not states:
            raise SpecError("break expects a CpuFlowState")
        owner.state = state  # pyright: ignore[reportAttributeAccessIssue]
        self._check_ensures(contract)
        continuation = Continuation(
            owner=owner,
            env=run.env,
            frames=self._frame_states(),
            drives=self.drives,
            specs=self._spec_fingerprints(),
            history=run.position_after_pause(point),
        )
        path = run.complete(point, continuation=continuation)
        raise FlowPaused(point, state, path)

    def _restore(self, saved: Continuation | InputState) -> None:
        env = saved.env
        if (
            not saved.frames
            or len(saved.frames) != len(saved.drives)
            or len(saved.drives) != len(env.signal_queues)
            or env.depth < len(saved.drives)
        ):
            raise SnapshotCompatibilityError("invalid continuation stack")
        pending = (
            saved.history.pending if isinstance(saved, Continuation) else saved.pending
        )
        if saved.drives[0].caller_point is not None or pending != {
            drive.caller_point for drive in saved.drives[1:]
        }:
            raise SnapshotCompatibilityError("invalid pending drives in continuation")
        self.owner = saved.owner
        self.drives = saved.drives
        self.can_pause = True
        for item, drive, env_queue in zip(
            saved.frames, saved.drives, env.signal_queues
        ):
            if (
                item.signal.env is not env
                or item.signal.queue is not drive.queue
                or drive.queue is not env_queue
                or item.locals.get("env") is not env
                or item.locals.get("self") is not item.system
                or any(
                    signal.env is not env or signal.queue is not drive.queue
                    for signal in drive.queue
                )
            ):
                raise SnapshotCompatibilityError(
                    "invalid Signal or Frame bindings in continuation"
                )
            registry = item.system.__spec_registry__
            try:
                spec, method = registry.method(item.system, item.signal.action)
            except (KeyError, SpecError) as error:
                raise SnapshotCompatibilityError(
                    "cannot resolve continuation method"
                ) from error
            if spec.name != item.specification or not 0 <= item.branch_index < len(
                method.branches
            ):
                raise SnapshotCompatibilityError("invalid continuation contract branch")
            branch = method.branches[item.branch_index]
            if not 1 <= item.position <= len(branch.instructions):
                raise SnapshotCompatibilityError(
                    "invalid continuation instruction position"
                )
            instruction = branch.instructions[item.position - 1]
            if len(self.frames) < len(saved.frames) - 1:
                valid = instruction.operation == "drive"
            elif isinstance(saved, Continuation):
                valid = instruction.operation == "break"
            else:
                valid = instruction.site is not None and (
                    (instruction.operation in {"drive", "emit"})
                    == (saved.signal is not None)
                )
            if not valid:
                raise SnapshotCompatibilityError(
                    "invalid continuation instruction position"
                )
            self.frames.append(
                Frame(
                    item.signal,
                    item.system,
                    spec,
                    method,
                    item.locals,
                    branch,
                    item.branch_index,
                    item.position,
                    item.guards,
                )
            )

        owner = self.owner
        if owner is None and isinstance(saved, Continuation):
            raise SnapshotCompatibilityError("continuation lacks its CpuFlow contract")
        if owner is not None:
            cpu_flow = owner.__spec_registry__.types.get("CpuFlow")
            if (
                cpu_flow is None
                or not isinstance(owner, cpu_flow)
                or not any(frame.system is owner for frame in self.frames)
            ):
                raise SnapshotCompatibilityError(
                    "continuation lacks its CpuFlow contract"
                )
        if self._spec_fingerprints() != saved.specs:
            raise SnapshotCompatibilityError(
                "continuation spec fingerprints do not match"
            )
        for path, fingerprint in saved.specs.items():
            try:
                current = hashlib.sha256(path.read_text().encode()).hexdigest()
            except (OSError, ValueError) as error:
                raise SnapshotCompatibilityError(
                    f"cannot validate continuation spec {path}: {error}"
                ) from error
            if current != fingerprint:
                raise SnapshotCompatibilityError(f"continuation spec changed: {path}")

    def _finish(self, frame: Frame) -> Any:
        assert frame.branch is not None
        # Protection scopes must be closed before checking postconditions.
        while frame.guards:
            frame.guards.pop().__exit__(None, None, None)
        self._check_ensures(frame)
        result = frame.locals[frame.method.result] if frame.method.result else None
        self.frames.pop()
        self._close(frame)
        return result


def _continue(
    run: FlowRun, state: dict[str, Any], saved: Continuation | InputState
) -> Any:
    interpreter = Interpreter()
    interpreter._restore(saved)
    env = saved.env
    root_depth = env.depth - len(env.signal_queues)
    with run:
        run.root.__dict__ = state
        try:
            return interpreter._execute(
                run, saved if isinstance(saved, InputState) else None
            )
        finally:
            env.signal_queues.clear()
            env.depth = root_depth


def resume_checkpoint(
    root: object,
    path: Path,
    model_version: int,
    *,
    capture_input: CaptureInput | None = None,
) -> Any:
    """Continue from the instruction after the latest saved break."""
    Interpreter._check_root(root)
    metadata, state, saved = read_snapshot(
        root, path, model_version=model_version, expected_checkpoint=path.stem
    )
    point = metadata.get("input_point")
    if not isinstance(saved, Continuation) or point is None:
        raise SnapshotCompatibilityError(f"{path} is not a break checkpoint")
    run = FlowRun(
        root,
        saved.env,
        model_version,
        history=(path, saved.history, point),
        capture_input=capture_input,
    )
    return _continue(run, state, saved)


def fork_input(
    root: object, base: str, frozen: bytes, candidate: Path, model_version: int
) -> Any:
    """Run a new branch from an I point using a candidate O point's GV."""
    Interpreter._check_root(root)
    _, state, _ = read_snapshot(
        root, candidate, model_version=model_version, expected_checkpoint=candidate.stem
    )
    saved = bind_input(root, state, frozen)
    if not isinstance(saved, InputState) or saved.point != base:
        raise SnapshotCompatibilityError("captured I point does not match its base")
    run = FlowRun(
        root, saved.env, model_version, fork=Fork(base, candidate, saved.pending)
    )
    return _continue(run, state, saved)

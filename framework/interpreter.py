"""Iterative spec execution with explicit Signal frames and return destinations."""

from __future__ import annotations

import ast
import hashlib
import operator
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import Enum
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
    Instruction,
    Method,
    SpecError,
    Specification,
    SpecSystem,
)

_CONTINUATION_LAYOUT = 1


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
    # Nested calls from private methods have no I point to complete.
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
class Restart:
    system: SpecSystem
    signal: Signal
    state: Enum


@dataclass
class ExecutionState:
    owner: SpecSystem | None
    env: TaskLocalEnv
    frames: list[FrameState]
    drives: list[Drive]
    specs: dict[Path, str]
    restart: Restart | None = field(default=None, kw_only=True)


@dataclass
class Continuation(ExecutionState):
    history: RunPosition
    # Guarded drives occupy one instruction, so older program counters cannot
    # be used to resume a checkpoint, even with unchanged spec text.
    layout: int = field(default_factory=lambda: _CONTINUATION_LAYOUT, kw_only=True)


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
                    if index > 0:
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

    def _step(self, frame: Frame, instruction: Instruction) -> None:
        match instruction.operation:
            case "statement" | "break":
                if instruction.site is None:
                    self._statement(frame, instruction.node)
                else:
                    self._action(frame, instruction, None)
            case "guarded_drive":
                self._action(frame, instruction, None)
            case "drive" | "emit":
                self._action(
                    frame, instruction, self._prepare_signal(frame, instruction)
                )
            case _:
                raise SpecError(f"unknown instruction {instruction.operation}")

    def _prepare_signal(self, frame: Frame, instruction: Instruction) -> Signal:
        call = instruction.node.value
        assert isinstance(call, ast.Call)
        operation = "emit" if instruction.operation == "emit" else "drive"
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
        return Signal(frame.system, target, action, kwargs, frame.signal.env, queue)

    def _record_input(
        self,
        run: FlowRun,
        frame: Frame,
        instruction: Instruction,
        signal: Signal | None,
    ) -> str:
        assert instruction.site is not None
        operation = instruction.operation
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
        return run.begin(
            frame.specification,
            frame.method.name,
            frame.branch_index,
            instruction.site,
            {"statement": "bare", "guarded_drive": "with"}.get(operation, operation),
            values,
        )

    def _capture_input(
        self,
        run: FlowRun,
        point: str,
        signal: Signal | None,
        *,
        restart: Restart | None = None,
    ) -> None:
        if (capture := run.capture_input) is not None:
            if not self.can_pause:
                raise SpecError("capturing an I point requires a resumable spec stack")
            if not run.capture_all_inputs:
                run.capture_input = None
            saved = InputState(
                self.owner,
                run.env,
                self._frame_states(),
                self.drives,
                self._spec_fingerprints(restart),
                point,
                run.pending_before(point),
                signal,
                restart=restart,
            )
            capture(point, freeze_input(run.root, run.env, saved))

    def _action(
        self, frame: Frame, instruction: Instruction, signal: Signal | None
    ) -> None:
        operation = instruction.operation
        env = frame.signal.env
        run = require_run(env)
        point = None
        if not (
            frame.method.name.startswith("_")
            and operation in {"statement", "drive", "guarded_drive"}
        ):
            point = self._record_input(run, frame, instruction, signal)
            if operation != "break" or len(instruction.node.value.args) == 1:
                self._capture_input(run, point, signal)

        if operation == "guarded_drive":
            for expression in instruction.guards:
                guard = self._eval(frame, expression)
                guard.__enter__()
                frame.guards.append(guard)
            signal = self._prepare_signal(frame, instruction)

        if signal is None:
            node = instruction.node
            if operation == "break":
                assert point is not None and instruction.site is not None
                self._break(run, frame, point, node.value.args, instruction.site.line)
            self._statement(frame, node)
            if point is not None:
                run.complete(point)
        else:
            signal.queue.append(signal)
            if operation == "emit":
                assert point is not None
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

    def _finish_drive(self, run: FlowRun, drive: Drive) -> None:
        assert not drive.first
        self.drives.pop()
        if self.drives:
            self._close_drive(run.env, drive)
            parent = self.frames[-1]
            assert parent.branch is not None
            instruction = parent.branch.instructions[parent.position - 1]
            assert instruction.operation in {"drive", "guarded_drive"}
            if isinstance(instruction.node, ast.Assign):
                self._assign(parent, instruction.node.targets[0], drive.result)
            for _ in instruction.guards:
                parent.guards.pop().__exit__(None, None, None)
            if drive.caller_point is not None:
                run.complete(drive.caller_point)

    def _check_ensures(self, frame: Frame) -> None:
        assert frame.branch is not None
        if frame.branch.ensures is not None and not self._conditions(
            frame, frame.branch.ensures
        ):
            self._violation(frame, "ensures", frame.branch.ensures)

    def _spec_fingerprints(self, restart: Restart | None = None) -> dict[Path, str]:
        fingerprints: dict[Path, str] = {}
        for frame in self.frames:
            fingerprints.update(frame.system.__spec_registry__.spec_fingerprints)
        if restart is not None:
            fingerprints.update(restart.system.__spec_registry__.spec_fingerprints)
        return fingerprints

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

    def _restart_index(
        self, frame: Frame, target_expr: ast.expr, action_expr: ast.expr
    ) -> int:
        target = self._eval(frame, target_expr)
        action = self._eval(frame, action_expr)
        if not isinstance(target, str) or not isinstance(action, str):
            raise SpecError("break restart target and action must be strings")
        try:
            system = frame.system if target == "self" else _get_target_ref(target)
        except AttributeError as error:
            raise SpecError(f"unknown break restart target {target!r}") from error
        for index in range(len(self.frames) - 1, -1, -1):
            active = self.frames[index]
            if active.system is system and active.signal.action == action:
                return index
        raise SpecError(f"break restart {target}.{action} must be an active call")

    def _unwind_to_restart(self, run: FlowRun, index: int, state: Enum) -> Restart:
        target = self.frames[index]
        # The target's original drive remains pending. Descendant calls were
        # abandoned; keep their I records but do not publish completion O points.
        while len(self.frames) > index:
            self._close(self.frames.pop())
            if len(self.drives) > index + 1:
                drive = self.drives.pop()
                self._close_drive(run.env, drive)
                if drive.caller_point is not None:
                    run.abandon(drive.caller_point)
        self.drives[-1].queue.appendleft(target.signal)
        return Restart(target.system, target.signal, state)

    def _break(
        self, run: FlowRun, frame: Frame, point: str, args: list[ast.expr], line: int
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
        state = self._eval(frame, args[0])
        states = owner.__spec_registry__.namespace.get("CpuFlowState")
        if type(state) is not states:
            raise SpecError("break expects a CpuFlowState")
        restart_index = (
            self._restart_index(frame, args[1], args[2]) if len(args) == 3 else None
        )
        owner.state = state  # pyright: ignore[reportAttributeAccessIssue]
        self._check_ensures(contract)
        restart = (
            self._unwind_to_restart(run, restart_index, state)
            if restart_index is not None
            else None
        )
        continuation = Continuation(
            owner=owner,
            env=run.env,
            frames=self._frame_states(),
            drives=self.drives,
            specs=self._spec_fingerprints(restart),
            history=run.position_after_pause(point),
            restart=restart,
        )
        path = run.complete(point, continuation=continuation)
        if restart is not None:
            self._capture_input(run, point, None, restart=restart)
        raise FlowPaused(point, state, path, (frame.specification.path, line))

    def _restore(self, saved: Continuation | InputState, state: dict[str, Any]) -> None:
        if (
            isinstance(saved, Continuation)
            and getattr(saved, "layout", None) != _CONTINUATION_LAYOUT
        ):
            raise SnapshotCompatibilityError(
                "incompatible continuation instruction layout; regenerate checkpoints"
            )
        env = saved.env
        restart = saved.restart
        if (
            not saved.drives
            or len(saved.frames) + (restart is not None) != len(saved.drives)
            or len(saved.drives) != len(env.signal_queues)
            or env.depth < len(saved.drives)
        ):
            raise SnapshotCompatibilityError("invalid continuation stack")
        for drive, env_queue in zip(saved.drives, env.signal_queues):
            if drive.queue is not env_queue or any(
                signal.env is not env or signal.queue is not drive.queue
                for signal in drive.queue
            ):
                raise SnapshotCompatibilityError(
                    "invalid Signal or Frame bindings in continuation"
                )
        if restart is not None:
            queue = saved.drives[-1].queue
            if (
                not queue
                or queue[0] is not restart.signal
                or (isinstance(saved, InputState) and saved.signal is not None)
            ):
                raise SnapshotCompatibilityError(
                    "invalid restart Signal in continuation"
                )
            signal = restart.signal
            target = signal.source
            if signal.target != "self":
                name, *members = signal.target.split(".")
                try:
                    target = state[name]
                    for member in members:
                        target = getattr(target, member)
                except (KeyError, AttributeError) as error:
                    raise SnapshotCompatibilityError(
                        "cannot resolve restart target"
                    ) from error
            if target is not restart.system or not hasattr(
                getattr(target, signal.action, None), "__spec_action__"
            ):
                raise SnapshotCompatibilityError(
                    "invalid restart target in continuation"
                )
        pending = (
            saved.history.pending if isinstance(saved, Continuation) else saved.pending
        )
        if saved.drives[0].caller_point is not None or pending != {
            point
            for drive in saved.drives[1:]
            if (point := drive.caller_point) is not None
        }:
            raise SnapshotCompatibilityError("invalid pending drives in continuation")
        self.owner = saved.owner
        self.drives = saved.drives
        self.can_pause = True
        for item, drive in zip(saved.frames, saved.drives):
            if (
                item.signal.env is not env
                or item.signal.queue is not drive.queue
                or item.locals.get("env") is not env
                or item.locals.get("self") is not item.system
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
            if len(self.frames) < len(saved.drives) - 1:
                valid = instruction.operation in {"drive", "guarded_drive"}
                expected_guards = len(instruction.guards)
            elif isinstance(saved, Continuation):
                valid = instruction.operation == "break"
                expected_guards = 0
            else:
                valid = instruction.site is not None and (
                    (instruction.operation in {"drive", "emit"})
                    == (saved.signal is not None)
                )
                expected_guards = 0
            if not valid or len(item.guards) != expected_guards:
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
        if owner is None and (isinstance(saved, Continuation) or restart is not None):
            raise SnapshotCompatibilityError("continuation lacks its CpuFlow contract")
        if owner is not None:
            cpu_flow = owner.__spec_registry__.types.get("CpuFlow")
            if (
                cpu_flow is None
                or not isinstance(owner, cpu_flow)
                or not (
                    any(frame.system is owner for frame in self.frames)
                    or (restart is not None and restart.system is owner)
                )
            ):
                raise SnapshotCompatibilityError(
                    "continuation lacks its CpuFlow contract"
                )
            if restart is not None and type(restart.state) is not (
                owner.__spec_registry__.namespace.get("CpuFlowState")
            ):
                raise SnapshotCompatibilityError("invalid CpuFlow state in restart")
        if self._spec_fingerprints(restart) != saved.specs:
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
    interpreter._restore(saved, state)
    env = saved.env
    root_depth = env.depth - len(env.signal_queues)
    with run:
        run.root.__dict__ = state
        try:
            if saved.restart is not None and isinstance(saved, InputState):
                assert saved.owner is not None
                saved.owner.state = saved.restart.state  # pyright: ignore[reportAttributeAccessIssue]
            return interpreter._execute(
                run,
                saved
                if isinstance(saved, InputState) and saved.restart is None
                else None,
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
    capture_all_inputs: bool = False,
) -> Any:
    """Continue from the latest saved break's instruction or restart entry."""
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
        capture_all_inputs=capture_all_inputs,
    )
    return _continue(run, state, saved)


def fork_input(
    root: object,
    base: str,
    frozen: bytes,
    candidate: Path,
    model_version: int,
    *,
    capture_input: CaptureInput | None = None,
    capture_all_inputs: bool = False,
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
        root,
        saved.env,
        model_version,
        fork=Fork(base, candidate, saved.pending),
        capture_input=capture_input,
        capture_all_inputs=capture_all_inputs,
    )
    return _continue(run, state, saved)

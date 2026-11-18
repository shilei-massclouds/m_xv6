"""Iterative spec execution with explicit Signal frames and return destinations."""

from __future__ import annotations

import ast
import operator
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

from framework.contracts import ContractViolation, Phase
from framework.engine import Signal, _get_target_ref
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
    receiver: ast.AST | None = None
    restore_depth: int | None = None


class Interpreter:
    """Model recursion lives in frames, rather than the Python execution stack."""

    def __init__(self):
        self.frames: list[Frame] = []

    def run(self, signal: Signal) -> Any:
        try:
            self._push(signal)
            result = None
            while self.frames:
                frame = self.frames[-1]
                assert frame.branch is not None
                if frame.position >= len(frame.branch.instructions):
                    result = self._finish(frame)
                else:
                    instruction = frame.branch.instructions[frame.position]
                    frame.position += 1
                    self._step(frame, instruction)
            return result
        except BaseException as error:
            frames, self.frames = self.frames, []
            with ExitStack() as cleanup:
                for frame in frames:
                    cleanup.callback(self._close, frame, error)
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

    def _push(
        self,
        signal: Signal,
        receiver: ast.AST | None = None,
        restore_depth: int | None = None,
    ):
        system = (
            signal.source if signal.target == "self" else _get_target_ref(signal.target)
        )
        assert isinstance(system, SpecSystem)
        registry = system.__spec_registry__
        specification, method = registry.method(system, signal.action)
        system.acquire(signal.env, signal.action)
        frame = Frame(
            signal,
            system,
            specification,
            method,
            {},
            receiver=receiver,
            restore_depth=restore_depth,
        )
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
            case "statement":
                self._statement(frame, node)
            case "drive":
                call = node.value
                assert isinstance(call, ast.Call)
                args = [self._eval(frame, arg) for arg in call.args]
                if len(args) != 2:
                    raise SpecError("drive expects target and action")
                kwargs = {}
                for keyword in call.keywords:
                    value = self._eval(frame, keyword.value)
                    if keyword.arg is None:
                        kwargs.update(value)
                    else:
                        kwargs[keyword.arg] = value
                target, action = args
                signal = Signal(
                    frame.system,
                    target,
                    action,
                    kwargs,
                    frame.signal.env,
                    frame.signal.queue,
                )
                receiver = node.targets[0] if isinstance(node, ast.Assign) else None
                system = signal.source if target == "self" else _get_target_ref(target)
                if not hasattr(getattr(system, action), "__spec_action__"):
                    value = signal.handle()
                    if receiver is not None:
                        self._assign(frame, receiver, value)
                else:
                    env = signal.env
                    depth = env.depth
                    print(
                        f"{'    ' * depth}{frame.system}:\n{'    ' * (depth + 1)}{action} -> {target}"
                    )
                    env.depth += 1
                    try:
                        self._push(signal, receiver, depth)
                    except BaseException:
                        if not self.frames or self.frames[-1] is frame:
                            env.depth = depth
                        raise
            case _:
                raise SpecError(f"unknown instruction {instruction.operation}")

    def _close(self, frame: Frame, error: BaseException | None = None):
        try:
            while frame.guards:
                frame.guards.pop().__exit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
        finally:
            try:
                frame.system.release(frame.signal.env, frame.signal.action)
                frame.signal._save_snapshot()
            finally:
                if frame.restore_depth is not None:
                    frame.signal.env.depth = frame.restore_depth

    def _finish(self, frame: Frame) -> Any:
        assert frame.branch is not None
        # Protection scopes must be closed before checking postconditions.
        while frame.guards:
            frame.guards.pop().__exit__(None, None, None)
        if frame.branch.ensures is not None and not self._conditions(
            frame, frame.branch.ensures
        ):
            self._violation(frame, "ensures", frame.branch.ensures)
        result = frame.locals[frame.method.result] if frame.method.result else None
        self.frames.pop()
        self._close(frame)
        if self.frames:
            parent = self.frames[-1]
            if frame.receiver is not None:
                self._assign(parent, frame.receiver, result)
        return result

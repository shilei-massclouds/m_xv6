"""Parser and model registry for the System-Signal specification language."""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import re
import sys
import textwrap
import tokenize
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from framework import contention, sync_primitives
from framework.engine import System, exit_from, protected_by, visibility


class SpecError(ValueError):
    """A specification contains invalid or unsupported syntax."""


@dataclass
class Expression:
    text: str
    line: int


@dataclass
class ActionSite:
    line: int
    text: str


@dataclass
class Instruction:
    operation: str
    node: Any = None
    site: ActionSite | None = None


@dataclass
class Branch:
    requires: list[Expression] | None = None
    instructions: list[Instruction] = field(default_factory=list)
    ensures: list[Expression] | None = None


@dataclass
class Method:
    name: str
    signature: inspect.Signature
    result: str | None
    line: int
    branches: list[Branch] = field(default_factory=list)
    visibility: str | None = None
    protection: str | None = None


@dataclass
class Property:
    name: str
    annotation: str
    default: str


@dataclass
class Specification:
    name: str
    path: Path
    base: str = "System"
    properties: list[Property] = field(default_factory=list)
    methods: dict[str, Method] = field(default_factory=dict)
    enums: dict[str, dict[str, Any]] = field(default_factory=dict)
    visibility: str | None = None
    protection: str | None = None
    fingerprint: str = ""


_BREAK_NAME = "__spec_break__"
_SIGNAL_OPERATIONS = {"drive", "emit", _BREAK_NAME}


def _action_call(node: ast.AST | None) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _SIGNAL_OPERATIONS
    ):
        return "break" if node.func.id == _BREAK_NAME else node.func.id
    return None


def _parse_actions(source: str, filename: str) -> list[ast.stmt]:
    # Python reserves "break". Rewrite its token only, keeping the spec's
    # physical line numbers and original source for I point records.
    tokens = (
        token._replace(string=_BREAK_NAME)
        if token.type == tokenize.NAME and token.string == "break"
        else token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
    )
    return ast.parse(tokenize.untokenize(tokens), filename=filename).body


def _check_control_syntax(node: ast.AST, filename: str) -> None:
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.IfExp)):
            raise SpecError(
                f"{filename}:{child.lineno}: if is unsupported; use Requires/Actions/Ensures branches"
            )
        if isinstance(child, ast.Return):
            raise SpecError(
                f"{filename}:{child.lineno}: return is unsupported; declare an output variable with ->"
            )


def _reject_nested_operation(node: ast.AST, filename: str) -> None:
    for child in ast.walk(node):
        operation = _action_call(child)
        if isinstance(child, ast.Name) and child.id == _BREAK_NAME:
            operation = "break"
        if operation is not None:
            form = " or assignment value" if operation == "drive" else ""
            raise SpecError(
                f"{filename}:{getattr(node, 'lineno', 0)}: {operation} must be a complete Actions statement{form}"
            )


def _compile_actions(
    nodes: list[ast.stmt], filename: str, source_lines: list[str]
) -> list[Instruction]:
    instructions: list[Instruction] = []
    statement_lines: set[int] = set()

    def compile_block(block: list[ast.stmt]):
        for node in block:
            _check_control_syntax(node, filename)
            if node.lineno in statement_lines:
                raise SpecError(
                    f"{filename}:{node.lineno}: only one operation per physical line is allowed"
                )
            statement_lines.add(node.lineno)
            site = ActionSite(node.lineno, source_lines[node.lineno - 1].strip())
            if not isinstance(node, ast.With) and node.end_lineno != node.lineno:
                raise SpecError(
                    f"{filename}:{node.lineno}: an operation must occupy a single physical line"
                )
            if isinstance(node, ast.With):
                if any(item.optional_vars is not None for item in node.items):
                    raise SpecError(f"{filename}:{node.lineno}: with/as is unsupported")
                for item in node.items:
                    if item.context_expr.end_lineno != node.lineno:
                        raise SpecError(
                            f"{filename}:{node.lineno}: a protection declaration must occupy a single physical line"
                        )
                    _reject_nested_operation(item.context_expr, filename)
                    instructions.append(Instruction("enter", item.context_expr))
                compile_block(node.body)
                for _ in node.items:
                    instructions.append(Instruction("leave"))
            elif isinstance(node, (ast.Expr, ast.Assign)):
                value = node.value
                operation = _action_call(value)
                if operation is not None:
                    assert isinstance(value, ast.Call)
                    if operation != "drive" and not isinstance(node, ast.Expr):
                        raise SpecError(
                            f"{filename}:{node.lineno}: {operation} must be a complete statement"
                        )
                    if operation == "break" and (
                        len(value.args) != 1
                        or isinstance(value.args[0], ast.Starred)
                        or value.keywords
                    ):
                        raise SpecError(
                            f"{filename}:{node.lineno}: break expects one state"
                        )
                    for argument in [
                        *value.args,
                        *[keyword.value for keyword in value.keywords],
                    ]:
                        _reject_nested_operation(argument, filename)
                    if isinstance(node, ast.Assign) and len(node.targets) != 1:
                        raise SpecError(
                            f"{filename}:{node.lineno}: chained drive assignment"
                        )
                    if isinstance(node, ast.Assign):
                        _reject_nested_operation(node.targets[0], filename)
                    instructions.append(Instruction(operation, node, site))
                else:
                    _reject_nested_operation(node, filename)
                    instructions.append(Instruction("statement", node, site))
            elif isinstance(node, (ast.Pass, ast.Assert, ast.AugAssign, ast.AnnAssign)):
                _reject_nested_operation(node, filename)
                instructions.append(
                    Instruction(
                        "statement",
                        node,
                        None if isinstance(node, (ast.Pass, ast.Assert)) else site,
                    )
                )
            else:
                raise SpecError(
                    f"{filename}:{node.lineno}: unsupported action {type(node).__name__}"
                )

    compile_block(nodes)
    return instructions


def parse_spec(path: str | Path) -> Specification:
    path = Path(path)
    source_text = path.read_text()
    lines = source_text.splitlines()
    spec = Specification(
        path.stem, path, fingerprint=hashlib.sha256(source_text.encode()).hexdigest()
    )
    method: Method | None = None
    branch: Branch | None = None
    index = 0
    try:
        while index < len(lines):
            raw = lines[index]
            text = raw.strip()
            line = index + 1
            index += 1
            if not text or text.startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            if "\t" in raw[:indent]:
                raise SpecError("tabs are not allowed for indentation")
            if indent == 0:
                method = None
                branch = None
                if text.startswith("Prop "):
                    declaration = ast.parse(text[5:]).body[0]
                    ast.increment_lineno(declaration, line - 1)
                    _check_control_syntax(declaration, str(path))
                    _reject_nested_operation(declaration, str(path))
                    if not isinstance(declaration, ast.AnnAssign) or not isinstance(
                        declaration.target, ast.Name
                    ):
                        raise SpecError("Prop requires name: type [= expression]")
                    annotation = ast.unparse(declaration.annotation)
                    if declaration.value is not None:
                        default = ast.unparse(declaration.value)
                    elif annotation.startswith("list["):
                        default = "[]"
                    elif "None" in annotation:
                        default = "None"
                    else:
                        default = f"{annotation}()"
                    if any(
                        prop.name == declaration.target.id for prop in spec.properties
                    ):
                        raise SpecError(f"duplicate property {declaration.target.id}")
                    spec.properties.append(
                        Property(declaration.target.id, annotation, default)
                    )
                elif text.startswith(("Parent ", "Extends ")):
                    spec.base = text.split(maxsplit=1)[1].strip()
                elif text.startswith("Visibility "):
                    spec.visibility = text[11:].strip()
                elif text.startswith("ProtectedBy "):
                    spec.protection = text[12:].strip()
                elif text.startswith("Enum ") and text.endswith(":"):
                    name = text[5:-1].strip()
                    members: dict[str, Any] = {}
                    while index < len(lines) and (
                        not lines[index].strip() or lines[index].startswith("    ")
                    ):
                        member = lines[index].strip()
                        index += 1
                        if member and not member.startswith("#"):
                            key, separator, value = member.partition("=")
                            key = key.strip()
                            if not key.isidentifier() or key in members:
                                raise SpecError(
                                    f"invalid or duplicate enum member {key!r}"
                                )
                            members[key] = (
                                ast.literal_eval(value.strip())
                                if separator
                                else key.lower()
                            )
                    if not name.isidentifier() or not members or name in spec.enums:
                        raise SpecError("invalid or duplicate Enum")
                    spec.enums[name] = members
                elif text.startswith("Def "):
                    match = re.fullmatch(r"Def (\w+)\((.*)\)(?:\s*->\s*(\w+))?:", text)
                    if match is None:
                        raise SpecError("expected Def name(parameters) [-> result]:")
                    name, parameters, result = match.groups()
                    if name in spec.methods:
                        raise SpecError(f"duplicate method {name}")
                    parsed = ast.parse(f"def method({parameters}):\n    pass").body[0]
                    assert isinstance(parsed, ast.FunctionDef)
                    if (
                        parsed.args.posonlyargs
                        or parsed.args.vararg
                        or parsed.args.kwonlyargs
                    ):
                        raise SpecError(
                            "only named parameters and **kwargs are supported"
                        )
                    params = []
                    defaults = [None] * (
                        len(parsed.args.args) - len(parsed.args.defaults)
                    ) + list(parsed.args.defaults)
                    for argument, default in zip(parsed.args.args, defaults):
                        value = (
                            inspect.Parameter.empty
                            if default is None
                            else ast.literal_eval(default)
                        )
                        params.append(
                            inspect.Parameter(
                                argument.arg,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                default=value,
                            )
                        )
                    params.append(
                        inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD)
                    )
                    method = Method(name, inspect.Signature(params), result, line)
                    spec.methods[name] = method
                else:
                    raise SpecError(f"unknown declaration {text!r}")
            elif indent == 4 and method is not None:
                if text.startswith("Visibility "):
                    method.visibility = text[11:].strip()
                    continue
                if text.startswith("ProtectedBy "):
                    method.protection = text[12:].strip()
                    continue
                if text not in {"Requires:", "Actions:", "Ensures:"}:
                    raise SpecError(f"unknown method section {text!r}")
                start = index
                while index < len(lines) and (
                    not lines[index].strip()
                    or len(lines[index]) - len(lines[index].lstrip()) >= 8
                ):
                    index += 1
                content = lines[start:index]
                if text == "Requires:" or branch is None:
                    branch = Branch()
                    method.branches.append(branch)
                if text == "Actions:":
                    if branch.instructions:
                        raise SpecError("duplicate Actions section")
                    source = "\n" * start + textwrap.dedent("\n".join(content))
                    branch.instructions = _compile_actions(
                        _parse_actions(source, str(path)), str(path), lines
                    )
                else:
                    expressions = [
                        Expression(value.strip(), start + offset + 1)
                        for offset, value in enumerate(content)
                        if value.strip() and not value.lstrip().startswith("#")
                    ]
                    for expression in expressions:
                        parsed_expression = ast.parse(expression.text, mode="eval")
                        ast.increment_lineno(parsed_expression, expression.line - 1)
                        _check_control_syntax(parsed_expression, str(path))
                        _reject_nested_operation(parsed_expression, str(path))
                    if text == "Requires:":
                        branch.requires = expressions
                    else:
                        if branch.ensures is not None:
                            raise SpecError("duplicate Ensures section")
                        branch.ensures = expressions
            else:
                raise SpecError("unexpected indentation")
        if any(not item.branches for item in spec.methods.values()):
            raise SpecError("a method needs at least one section")
    except (SyntaxError, ValueError, AssertionError, tokenize.TokenError) as error:
        raise SpecError(f"{path}:{index}: {error}") from error
    return spec


class SpecSystem(System):
    """A model instance whose properties and actions come entirely from specs."""

    __spec_registry__: SpecRegistry

    def __init__(self, **values):
        registry = self.__spec_registry__
        for prop in registry.properties(type(self).__name__):
            setattr(
                self,
                prop.name,
                values.pop(prop.name)
                if prop.name in values
                else eval(prop.default, registry.namespace),
            )
        if values:
            raise TypeError(
                f"unknown properties for {type(self).__name__}: {', '.join(values)}"
            )

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class SpecRegistry:
    def __init__(self, paths: list[Path], *, module: str = __name__):
        self.module = module
        specs = [parse_spec(path) for path in paths]
        self.specifications = {spec.name: spec for spec in specs}
        if len(self.specifications) != len(specs):
            raise SpecError("duplicate specification names")
        self.namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "exit_from": exit_from,
        }
        for source in (contention, sync_primitives):
            self.namespace.update(
                {
                    name: value
                    for name, value in vars(source).items()
                    if not name.startswith("_")
                }
            )
        self.types: dict[str, Any] = {}
        self._building: set[str] = set()
        for spec in specs:
            for name, members in spec.enums.items():
                if name in self.namespace:
                    raise SpecError(f"duplicate enum {name}")
                enum = Enum(name, members, module=module)
                self.namespace[name] = enum
                setattr(sys.modules[module], name, enum)
        for spec in specs:
            self.model(spec.name)

    def properties(self, name: str) -> list[Property]:
        spec = self.specifications[name]
        inherited = [] if spec.base == "System" else self.properties(spec.base)
        own = {prop.name for prop in spec.properties}
        return [prop for prop in inherited if prop.name not in own] + spec.properties

    def model(self, name: str) -> Any:
        if name in self.types:
            return self.types[name]
        if name not in self.specifications:
            raise SpecError(f"unknown base specification {name}")
        if name in self._building:
            raise SpecError(f"cyclic specification inheritance at {name}")
        spec = self.specifications[name]
        self._building.add(name)
        try:
            base = SpecSystem if spec.base == "System" else self.model(spec.base)
        finally:
            self._building.remove(name)
        model = type(
            name, (base,), {"__module__": self.module, "__spec_registry__": self}
        )
        self.types[name] = model
        self.namespace[name] = model
        setattr(sys.modules[self.module], name, model)
        if spec.visibility:
            visibility(self.namespace[spec.visibility])(model)
        if spec.protection:
            protected_by(spec.protection)(model)
        for method in spec.methods.values():
            self._method(model, spec, method)
        return model

    def _method(self, model: Any, spec: Specification, method: Method):
        action_name = method.name

        def invoke(system, env, **kwargs):
            return system.drive(env, "self", action_name, **kwargs)

        # Publish functions too, so callbacks retain normal Pickle support.
        invoke.__name__ = f"{spec.name}__{method.name}"
        invoke.__qualname__ = invoke.__name__
        invoke.__module__ = self.module
        invoke.__dict__["__spec_action__"] = (spec.name, method.name)
        if method.visibility:
            visibility(self.namespace[method.visibility])(invoke)
        if method.protection:
            protected_by(method.protection)(invoke)
        setattr(sys.modules[self.module], invoke.__name__, invoke)
        setattr(model, method.name, invoke)

    def method(self, system: System, name: str) -> tuple[Specification, Method]:
        for cls in type(system).__mro__:
            spec = self.specifications.get(cls.__name__)
            if spec is not None and name in spec.methods:
                return spec, spec.methods[name]
        raise AttributeError(f"{type(system).__name__}.{name}")

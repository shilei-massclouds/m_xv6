"""Explore spec flows by passing output snapshots between their I points."""

from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol

from framework import snapshot_paths
from framework.engine import FlowPaused, Handoff, TaskLocalEnv
from framework.flow_run import (
    CapturedInput,
    CaptureInput,
    CaptureOutput,
    FlowRun,
    buffer_input_records,
)
from framework.interpreter import fork_input, resume_checkpoint, start_handoff
from framework.snapshot import SnapshotMetadata, deduplicate_outputs


class FlowOrigin(Protocol):
    env: TaskLocalEnv

    def start(self) -> None: ...


@dataclass(frozen=True)
class HandoffOrigin:
    name: str


# Each entry is the I point where an already visited flow can continue. None
# means that output has no continuation for the flow (e.g. its final O point).
type Frontier = dict[str, str | None]


@dataclass
class _Line:
    flow: str
    order: int = 1
    frontier: Frontier = field(default_factory=dict)
    inputs: dict[str, CapturedInput] = field(default_factory=dict)
    outputs: dict[Path, SnapshotMetadata] = field(default_factory=dict)
    handoffs: dict[Path, Handoff] = field(default_factory=dict)
    seeded_by: str | None = None


@dataclass
class _Input:
    line: _Line
    point: str
    execution: CapturedInput


@dataclass
class _Output:
    line: _Line
    path: Path
    metadata: SnapshotMetadata
    frontier: Frontier
    notified: frozenset[str]
    waits_for: frozenset[str]

    @property
    def digest(self) -> str:
        return self.metadata.get("gv_sha256", self.metadata["payload_sha256"])


@dataclass(frozen=True)
class _Launch:
    candidate: _Output
    handoff: Handoff


def _advance(
    step: Callable[[], object],
    root: object,
    model_version: int,
    capture_input: CaptureInput | None,
    capture_output: CaptureOutput,
    handoffs: dict[Path, Handoff],
) -> str:
    resumed_sites: set[tuple[Path, int]] = set()
    while True:
        try:
            step()
        except FlowPaused as pause:
            if pause.handoff is not None:
                handoffs[pause.checkpoint] = pause.handoff
            print(f"{pause.point}: {pause.state.name}\nCheckpoint: {pause.checkpoint}")
            if pause.site in resumed_sites:
                return "paused"
            resumed_sites.add(pause.site)
            step = partial(
                resume_checkpoint,
                root,
                pause.checkpoint,
                model_version,
                capture_input=capture_input,
                capture_output=capture_output,
                capture_all_inputs=True,
            )
        else:
            return "completed"


def _start(
    origin: FlowOrigin,
    root: object,
    model_version: int,
    capture_input: CaptureInput | None,
    capture_output: CaptureOutput,
) -> None:
    with FlowRun(
        root,
        origin.env,
        model_version,
        capture_input=capture_input,
        capture_output=capture_output,
        capture_all_inputs=True,
    ):
        origin.start()


class _Search:
    def __init__(self, names: list[str], handoff_names: set[str]):
        self.names = names
        self.handoff_names = handoff_names
        self.inputs: dict[str, _Input] = {}
        self.inputs_by_flow: dict[str, list[_Input]] = {name: [] for name in names}
        self.new_to_flow: dict[str, list[_Output]] = {name: [] for name in names}
        self.gv_digests: dict[str, str] = {}
        self.seen: set[tuple[object, ...]] = set()
        self.seen_launches: set[tuple[object, ...]] = set()
        self.work: deque[tuple[_Input, _Output, Frontier] | _Launch] = deque()
        self.summaries: dict[tuple[int, str, str], Counter[str]] = {}

    def _schedule(self, target: _Input, candidate: _Output) -> None:
        flow = target.line.flow
        execution = target.execution
        # A direct return extends the recorded prefix. When starting a new
        # flow from independent histories, do not mix alternative positions
        # of a third flow or replace one that already ran there.
        if flow not in candidate.frontier:
            prior = target.line.frontier
            if flow in self.handoff_names:
                # Before the first action, another flow may advance from the
                # same handoff. Afterwards, use a return to this exact I point,
                # since an unanchored GV does not include the receiver's work.
                source = target.line.seeded_by
                if (
                    source is None
                    or target.point != next(iter(target.line.inputs))
                    or source not in candidate.frontier
                    or candidate.frontier[source] != prior[source]
                ):
                    return
            if candidate.line.flow in prior or any(
                prior[name] != candidate.frontier[name]
                for name in prior.keys() & candidate.frontier.keys()
            ):
                return
        # A wait can only be spliced into a history that has the notification.
        if not candidate.waits_for <= execution.notified:
            return
        # The already published notification is part of the execution prefix.
        # A standalone candidate from before it cannot continue that prefix.
        # Its actions can still run after the publication on a derived line
        # that starts with the publishing flow's output.
        if not execution.notified <= candidate.notified:
            return
        frontier = {**target.line.frontier, **candidate.frontier}
        frontier.pop(flow, None)
        key = (
            flow,
            execution.control,
            candidate.digest,
            candidate.notified,
            candidate.waits_for,
            self._frontier_key(frontier),
        )
        if key in self.seen:
            return
        self.seen.add(key)
        self.work.append((target, candidate, frontier))

    def _frontier_key(self, frontier: Frontier) -> tuple:
        return tuple(
            (
                name,
                None
                if point is None
                else (
                    self.inputs[point].execution.control,
                    self.gv_digests[self.inputs[point].execution.previous_output],
                ),
            )
            for name, point in sorted(frontier.items())
        )

    def _schedule_handoff(self, candidate: _Output, handoff: Handoff) -> None:
        if handoff.flow not in self.handoff_names:
            raise ValueError(f"no handoff origin registered for {handoff.flow!r}")
        key = (
            handoff,
            candidate.digest,
            candidate.notified,
            candidate.waits_for,
            self._frontier_key(candidate.frontier),
        )
        if key not in self.seen_launches:
            self.seen_launches.add(key)
            self.work.append(_Launch(candidate, handoff))

    def publish(self, line: _Line) -> None:
        successors: dict[str, str] = {}
        new_inputs: list[_Input] = []
        for point, execution in line.inputs.items():
            target = self.inputs[point] = _Input(line, point, execution)
            self.inputs_by_flow[line.flow].append(target)
            new_inputs.append(target)
            successors.setdefault(execution.previous_output, point)
        for path, metadata in line.outputs.items():
            name = path.relative_to(snapshot_paths.SNAPSHOT_DIR).as_posix()
            self.gv_digests[name] = metadata.get(
                "gv_sha256", metadata["payload_sha256"]
            )
        for target in new_inputs:
            for candidate in self.new_to_flow[line.flow]:
                self._schedule(target, candidate)
        for path, metadata in line.outputs.items():
            # The initial GV has no completed operation/I point.
            if "input_point" not in metadata:
                continue
            name = path.relative_to(snapshot_paths.SNAPSHOT_DIR).as_posix()
            frontier = {**line.frontier, line.flow: successors.get(name)}
            candidate = _Output(
                line,
                path,
                metadata,
                frontier,
                frozenset(metadata.get("notified", [])),
                frozenset(metadata.get("waits_for", [])),
            )
            if (handoff := line.handoffs.get(path)) is not None:
                self._schedule_handoff(candidate, handoff)
            for flow in self.names:
                if flow == line.flow:
                    continue
                if flow not in frontier:
                    self.new_to_flow[flow].append(candidate)
                    for target in self.inputs_by_flow[flow]:
                        self._schedule(target, candidate)
                elif (point := frontier[flow]) is not None:
                    self._schedule(self.inputs[point], candidate)

    def run(self, root: object, model_version: int) -> None:
        while self.work:
            job = self.work.popleft()
            if isinstance(job, _Launch):
                candidate = job.candidate
                order = candidate.line.order + 1
                frontier = {
                    name: point
                    for name, point in candidate.frontier.items()
                    if name != job.handoff.flow
                }
                line = _Line(
                    job.handoff.flow, order, frontier, seeded_by=candidate.line.flow
                )
                label = f"{line.flow}.{job.handoff.action}"
                step = partial(
                    start_handoff, root, candidate.path, model_version, job.handoff
                )
            else:
                target, candidate, frontier = job
                order = 1 + max(target.line.order, candidate.line.order)
                line = _Line(target.line.flow, order, frontier)
                label = target.point
                step = partial(
                    fork_input,
                    root,
                    target.point,
                    target.execution,
                    candidate.path,
                    model_version,
                    capture_all_inputs=True,
                )
            print(f"{label} <- {candidate.path.stem}")
            capture = line.inputs.__setitem__
            output = line.outputs.__setitem__
            try:
                outcome = _advance(
                    partial(step, capture_input=capture, capture_output=output),
                    root,
                    model_version,
                    capture,
                    output,
                    line.handoffs,
                )
            except Exception as error:
                error.add_note(f"Deriving {label} from candidate {candidate.path}")
                raise
            summary = (order, candidate.line.flow, line.flow)
            self.summaries.setdefault(summary, Counter())[outcome] += 1
            self.publish(line)
        for (order, source, target), outcomes in sorted(self.summaries.items()):
            counts = ", ".join(f"{key}={count}" for key, count in outcomes.items())
            print(
                f"Order {order}: {source} -> {target}: {outcomes.total()} branches ({counts})"
            )


@buffer_input_records()
@deduplicate_outputs(pack_breaks=True)
def derive_flows(
    origins: Sequence[FlowOrigin | HandoffOrigin],
    *,
    root: object,
    reset: Callable[[], None],
    model_version: int,
) -> None:
    """Explore joins until no new execution states can be derived."""
    names = [
        origin.name if isinstance(origin, HandoffOrigin) else origin.env.name
        for origin in origins
    ]
    if len(set(names)) != len(names):
        raise ValueError("flow origins must have distinct names")

    search = _Search(
        names, {origin.name for origin in origins if isinstance(origin, HandoffOrigin)}
    )
    for origin, name in zip(origins, names):
        if isinstance(origin, HandoffOrigin):
            continue
        reset()
        line = _Line(name)
        capture = line.inputs.__setitem__ if len(names) > 1 else None
        output = line.outputs.__setitem__
        outcome = _advance(
            partial(_start, origin, root, model_version, capture, output),
            root,
            model_version,
            capture,
            output,
            line.handoffs,
        )
        print(f"Order 1: {name}: {outcome}, {len(line.outputs)} O points")
        search.publish(line)
    search.run(root, model_version)

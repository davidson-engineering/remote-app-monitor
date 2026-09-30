"""The monitor: a registry of elements that sources update and outputs display."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import logging
import threading
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol, Self, TypeVar

from .elements import JSON, Element, IndicatorLamp, TextElement

logger = logging.getLogger(__name__)

Update = Mapping[str, Any]
"""A set of new values keyed by element id (``{"X.velocity": 12.5}``)."""

# Unknown ids are reported once each; stop remembering new ones past this many
# so a noisy link can't grow memory without bound.
_MAX_REPORTED_KEYS = 1000

T = TypeVar("T")


@dataclass
class Group:
    """Elements displayed together under a heading. Their ids share the prefix
    ``"<name>."`` (e.g. ``"X.velocity"``)."""

    name: str
    elements: list[Element] = field(default_factory=list)
    border: bool = True


class Source(Protocol):
    """Something that produces batches of updates, e.g. one per chunk read."""

    def updates(self) -> AsyncIterator[list[Update]]: ...


class Output(Protocol):
    """Something that displays a monitor until cancelled.

    An output may also define ``async def start(monitor)`` for setup that can
    fail, like binding a port. The monitor awaits it before anything runs, so
    such errors are raised straight away (and from :meth:`Monitor.start`).
    ``async def flush(monitor)`` is awaited before :meth:`Monitor.stop` shuts
    down, to deliver the final values.
    """

    async def run(self, monitor: Monitor) -> None: ...


class Monitor:
    """Holds the elements, applies updates to them and tracks what changed.

    Updates are addressed by element id. An id the monitor hasn't seen creates
    a new element (a lamp for ``True``/``False``, text otherwise; dotted ids
    such as ``"job.rate"`` are grouped under ``"job"``). Declare elements
    yourself for bars, units and formats, and pass ``strict=True`` to reject
    unknown ids instead. For multi-part elements the id may be followed by a
    field (``"status.motor1.speed"`` updates field ``"motor1.speed"`` of
    element ``"status"``).

    ``update``, ``set``, ``add`` and ``add_group`` may be called from any
    thread. Every applied batch bumps :attr:`version`; outputs use
    :meth:`wait_for_change` and :meth:`changes_since` to redraw only when, and
    only what, something changed.
    """

    def __init__(self, *, strict: bool = False, max_elements: int = 500) -> None:
        self.strict = strict
        self.max_elements = max_elements
        self.layout: list[Element | Group] = []
        self.version = 0
        self.layout_version = 0
        self.rejected = 0
        self._elements: dict[str, Element] = {}
        self._versions: dict[str, int] = {}
        self._updated: dict[str, float] = {}  # id -> monotonic() of last update
        self._groups: dict[str, Group] = {}
        self._routes: dict[str, tuple[Element, str | None]] = {}
        self._reported: set[str] = set()
        self._changed = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._outputs: list[Output] = []

    # -- building -----------------------------------------------------------

    def add(self, *elements: Element) -> None:
        """Add elements, displayed in the order added."""
        self._on_loop(self._add, elements)

    def add_group(
        self, name: str, elements: Sequence[Element], *, border: bool = True
    ) -> Group:
        """Add copies of ``elements`` under ``name``.

        Element ``"velocity"`` becomes ``"<name>.velocity"``, so the same list can
        be reused for several groups (e.g. one per axis).
        """
        return self._on_loop(self._add_group, name, list(elements), border)

    def _add(self, elements: Sequence[Element]) -> None:
        self._check_new(elements)
        for element in elements:
            self._register(element)
            self.layout.append(element)
        self._layout_changed([element.id for element in elements])

    def _add_group(self, name: str, elements: list[Element], border: bool) -> Group:
        if name in self._groups:
            raise ValueError(f"duplicate group {name!r}")
        group = Group(name, [el.copy(f"{name}.{el.id}") for el in elements], border)
        self._check_new(group.elements)
        for element in group.elements:
            self._register(element)
        self._groups[name] = group
        self.layout.append(group)
        self._layout_changed([element.id for element in group.elements])
        return group

    def _check_new(self, elements: Sequence[Element]) -> None:
        ids = [element.id for element in elements]
        for id in ids:
            if id in self._elements or ids.count(id) > 1:
                raise ValueError(f"duplicate element id {id!r}")

    def _register(self, element: Element) -> None:
        self._elements[element.id] = element
        self._versions[element.id] = self.version
        self._routes.clear()

    def _create(self, key: str, value: Any) -> Element | None:
        """Create an element for an id seen for the first time."""
        if self.strict:
            self._reject(key, "no element with this id")
            return None
        if len(self._elements) >= self.max_elements:
            self._reject(
                key,
                f"no element with this id, and the monitor already has "
                f"{self.max_elements} elements (max_elements)",
            )
            return None
        if isinstance(value, bool):
            element: Element = IndicatorLamp(key, off_color="white")
        else:
            element = TextElement(key)
        self._register(element)
        prefix, dot, _ = key.rpartition(".")
        if dot:
            group = self._groups.get(prefix)
            if group is None:
                group = self._groups[prefix] = Group(prefix)
                self.layout.append(group)
            group.elements.append(element)
        else:
            self.layout.append(element)
        self.layout_version += 1
        return element

    # -- access -------------------------------------------------------------

    def __getitem__(self, id: str) -> Element:
        return self._elements[id]

    def __contains__(self, id: object) -> bool:
        return id in self._elements

    def __iter__(self) -> Iterator[Element]:
        return iter(list(self._elements.values()))

    def __len__(self) -> int:
        return len(self._elements)

    # -- updating -----------------------------------------------------------

    def update(self, *updates: Update) -> None:
        """Apply one or more ``{id: value}`` mappings, in order.

        Values an element rejects (and unknown ids, with ``strict=True``) are
        logged once per id and skipped; they never interrupt the rest of the
        batch. From another thread this returns immediately and the monitor's
        event loop applies the updates, in order, a moment later.
        """
        if loop := self._loop_elsewhere():
            batch = [dict(mapping) for mapping in updates]
            with contextlib.suppress(RuntimeError):  # loop closed meanwhile
                loop.call_soon_threadsafe(self._apply_all, batch)
                return
        self._apply_all(updates)

    def set(self, id: str, value: Any) -> None:
        """Update a single element (or field)."""
        self.update({id: value})

    def _apply_all(self, updates: Iterable[Update]) -> None:
        changed = []
        for mapping in updates:
            for key, value in mapping.items():
                if element := self._apply(key, value):
                    changed.append(element.id)
        if changed:
            now = monotonic()
            for id in changed:
                self._updated[id] = now
            self._mark_changed(changed)

    def _apply(self, key: str, value: Any) -> Element | None:
        """Update the element ``key`` addresses; return it, or None if rejected."""
        route = self._route(key)
        if route is None:
            if (element := self._create(key, value)) is None:
                return None
            route = (element, None)
        element, field = route
        try:
            if field is None:
                element.update(value)
            else:
                element.update_field(field, value)
        except KeyError as error:
            self._reject(key, str(error.args[0]) if error.args else "unknown field")
            return None
        except (TypeError, ValueError) as error:
            self._reject(key, f"bad value {value!r}: {error}")
            return None
        return element

    def _route(self, key: str) -> tuple[Element, str | None] | None:
        if route := self._routes.get(key):
            return route
        if key in self._elements:
            route = (self._elements[key], None)
        else:
            parts = key.split(".")
            for i in range(len(parts) - 1, 0, -1):
                if element := self._elements.get(".".join(parts[:i])):
                    route = (element, ".".join(parts[i:]))
                    break
            else:
                return None
        self._routes[key] = route
        return route

    def _reject(self, key: str, reason: str) -> None:
        self.rejected += 1
        if key in self._reported:
            logger.debug("Ignored update for %r: %s", key, reason)
        elif len(self._reported) < _MAX_REPORTED_KEYS:
            self._reported.add(key)
            logger.warning(
                "Ignored update for %r: %s (further problems with this id are "
                "logged at DEBUG)",
                key,
                reason,
            )

    def _layout_changed(self, ids: Iterable[str]) -> None:
        self.layout_version += 1
        self._mark_changed(ids)

    def _mark_changed(self, ids: Iterable[str]) -> None:
        self.version += 1
        for id in ids:
            self._versions[id] = self.version
        self._changed.set()
        self._changed = asyncio.Event()

    # -- observing ----------------------------------------------------------

    async def wait_for_change(self, since: int) -> int:
        """Wait until :attr:`version` differs from ``since``; return the new version."""
        self._bind()
        while self.version == since:
            await self._changed.wait()
        return self.version

    def changes_since(self, version: int) -> dict[str, JSON]:
        """``to_json()`` of every element updated after ``version``."""
        return {
            id: self._elements[id].to_json()
            for id, changed in self._versions.items()
            if changed > version
        }

    def snapshot(self) -> dict[str, JSON]:
        """``to_json()`` of every element."""
        return {id: element.to_json() for id, element in self._elements.items()}

    def ages(self) -> dict[str, float | None]:
        """Seconds since each element was last updated (None if never), for
        telling a value that is still arriving from one that stopped."""
        now = monotonic()
        return {
            id: None if (updated := self._updated.get(id)) is None else now - updated
            for id in self._elements
        }

    def describe(self) -> list[dict[str, JSON]]:
        """Layout and element metadata, for building a display."""
        items: list[dict[str, JSON]] = []
        for item in self.layout:
            if isinstance(item, Group):
                items.append(
                    {
                        "group": item.name,
                        "elements": [_describe(el) for el in item.elements],
                    }
                )
            else:
                items.append(_describe(item))
        return items

    # -- running ------------------------------------------------------------

    async def consume(self, source: Source) -> None:
        """Apply every batch ``source`` produces, until it ends or is cancelled."""
        self._bind()
        async for batch in source.updates():
            self._apply_all(batch)

    async def run(
        self, *, sources: Iterable[Source] = (), outputs: Iterable[Output] = ()
    ) -> None:
        """Feed all ``sources`` into the monitor and run all ``outputs``.

        Runs until cancelled. If a source or output fails, the others are
        cancelled and its error is raised.
        """
        sources, outputs = list(sources), list(outputs)
        await self._start_outputs(outputs)
        await self._run(sources, outputs)

    def serve(
        self, *, sources: Iterable[Source] = (), outputs: Iterable[Output] | None = None
    ) -> None:
        """Run in the foreground until Ctrl+C, then return quietly.

        ``outputs`` defaults to a :class:`WebDashboard`.
        """
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(self.run(sources=sources, outputs=_default_outputs(outputs)))

    def start(
        self,
        *,
        sources: Iterable[Source] = (),
        outputs: Iterable[Output] | None = None,
        timeout: float = 10,
    ) -> Self:
        """Run in a background thread, returning once the outputs are up.

        Your program then carries on and calls :meth:`set`/:meth:`update` from
        any thread. ``outputs`` defaults to a :class:`WebDashboard`. Setup
        errors (e.g. the port is in use) are raised here. It stops when your
        program exits, after delivering the final values, or earlier with
        :meth:`stop` or by using the monitor as a context manager.
        """
        if self._thread is not None:
            raise RuntimeError("this monitor is already running")
        sources, outputs = list(sources), _default_outputs(outputs)
        self._outputs = outputs
        ready = threading.Event()
        failure: list[BaseException] = []

        async def main() -> None:
            self._task = asyncio.current_task()
            await self._start_outputs(outputs)
            ready.set()
            await self._run(sources, outputs)

        def thread() -> None:
            try:
                asyncio.run(main())
            except asyncio.CancelledError:
                pass  # stopped
            except BaseException as error:
                if ready.is_set():
                    logger.error("Monitor stopped: %s", error, exc_info=error)
                failure.append(error)
            finally:
                self._task = None
                ready.set()

        self._thread = threading.Thread(target=thread, name="sightglass", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            raise TimeoutError(f"the monitor did not start within {timeout} s")
        if failure:
            self._thread = None
            raise failure[0]
        atexit.register(self.stop)
        return self

    def stop(self, timeout: float = 5) -> None:
        """Stop a monitor started with :meth:`start`: deliver the latest values
        to the outputs, shut them down and wait for the thread to finish."""
        thread, task, loop = self._thread, self._task, self._loop
        if thread is None:
            return
        if threading.get_ident() == self._loop_thread:
            raise RuntimeError("stop() can't be called from the monitor's own thread")
        atexit.unregister(self.stop)
        if task is not None and loop is not None and loop.is_running():
            with contextlib.suppress(Exception):  # stopping anyway
                asyncio.run_coroutine_threadsafe(self._flush_outputs(), loop).result(
                    timeout
                )
            with contextlib.suppress(RuntimeError):  # already finished
                loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout)
        self._thread = None

    async def _flush_outputs(self) -> None:
        for output in self._outputs:
            if (flush := getattr(output, "flush", None)) is not None:
                await flush(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    async def _start_outputs(self, outputs: list[Output]) -> None:
        self._bind()
        started: list[Output] = []
        try:
            for output in outputs:
                if (start := getattr(output, "start", None)) is not None:
                    await start(self)
                    started.append(output)
        except BaseException:
            for output in started:
                if (close := getattr(output, "close", None)) is not None:
                    await close()
            raise

    async def _run(self, sources: list[Source], outputs: list[Output]) -> None:
        try:
            async with asyncio.TaskGroup() as tasks:
                for source in sources:
                    tasks.create_task(self.consume(source))
                for output in outputs:
                    tasks.create_task(output.run(self))
        except ExceptionGroup as group:
            if len(group.exceptions) == 1:  # the usual case: show just that error
                raise group.exceptions[0] from None
            raise

    # -- threads ------------------------------------------------------------

    def _bind(self) -> None:
        """Remember the running event loop, so other threads can hand it work."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._loop_thread = threading.get_ident()
            self._changed = asyncio.Event()

    def _loop_elsewhere(self) -> asyncio.AbstractEventLoop | None:
        """The monitor's event loop, if it's running on a thread other than ours."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return None
        if threading.get_ident() == self._loop_thread:
            return None
        return loop

    def _on_loop(self, function: Callable[..., T], *args: Any) -> T:
        """Call ``function`` on the event loop's thread and wait for the result."""
        loop = self._loop_elsewhere()
        if loop is None:
            return function(*args)
        result: concurrent.futures.Future[T] = concurrent.futures.Future()

        def call() -> None:
            try:
                result.set_result(function(*args))
            except BaseException as error:
                result.set_exception(error)

        loop.call_soon_threadsafe(call)
        return result.result(timeout=10)  # never hang if the loop is shutting down


def start(
    *,
    sources: Iterable[Source] = (),
    outputs: Iterable[Output] | None = None,
    strict: bool = False,
) -> Monitor:
    """Create a :class:`Monitor` and :meth:`~Monitor.start` it in the background::

    monitor = start()                 # prints the dashboard's address
    monitor.set("progress", 0.5)      # from anywhere in your program
    """
    return Monitor(strict=strict).start(sources=sources, outputs=outputs)


def _default_outputs(outputs: Iterable[Output] | None) -> list[Output]:
    if outputs is not None:
        return list(outputs)
    from . import WebDashboard  # imported lazily: needs the [web] extra

    return [WebDashboard()]


def _describe(element: Element) -> dict[str, JSON]:
    return {"id": element.id, **element.describe()}

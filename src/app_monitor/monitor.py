"""The monitor: a registry of elements that sources update and outputs display."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .elements import JSON, Element

logger = logging.getLogger(__name__)

Update = Mapping[str, Any]
"""A set of new values keyed by element id (``{"X.velocity": 12.5}``)."""

# Unknown ids are reported once each; stop remembering new ones past this many
# so a noisy link can't grow memory without bound.
_MAX_REPORTED_KEYS = 1000


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
    """Something that displays a monitor until cancelled."""

    async def run(self, monitor: Monitor) -> None: ...


class Monitor:
    """Holds the elements, applies updates to them and tracks what changed.

    Updates are addressed by element id. For multi-part elements the id may be
    followed by a field (``"status.motor1.speed"`` updates field
    ``"motor1.speed"`` of element ``"status"``).

    Every applied batch bumps :attr:`version`; outputs use
    :meth:`wait_for_change` and :meth:`changes_since` to redraw only when, and
    only what, something changed. All methods must be called from the event
    loop's thread.
    """

    def __init__(self) -> None:
        self.layout: list[Element | Group] = []
        self.version = 0
        self._elements: dict[str, Element] = {}
        self._versions: dict[str, int] = {}
        self._routes: dict[str, tuple[Element, str | None]] = {}
        self._changed = asyncio.Event()
        self._reported: set[str] = set()
        self.rejected = 0

    # -- building -----------------------------------------------------------

    def add(self, *elements: Element) -> None:
        """Add elements, displayed in the order added."""
        for element in elements:
            self._register(element)
            self.layout.append(element)

    def add_group(
        self, name: str, elements: Sequence[Element], *, border: bool = True
    ) -> Group:
        """Add copies of ``elements`` under ``name``.

        Element ``"velocity"`` becomes ``"<name>.velocity"``, so the same list can
        be reused for several groups (e.g. one per axis).
        """
        group = Group(name, [el.copy(f"{name}.{el.id}") for el in elements], border)
        for element in group.elements:
            self._register(element)
        self.layout.append(group)
        return group

    def _register(self, element: Element) -> None:
        if element.id in self._elements:
            raise ValueError(f"duplicate element id {element.id!r}")
        self._elements[element.id] = element
        self._versions[element.id] = self.version
        self._routes.clear()

    # -- access -------------------------------------------------------------

    def __getitem__(self, id: str) -> Element:
        return self._elements[id]

    def __contains__(self, id: object) -> bool:
        return id in self._elements

    def __iter__(self) -> Iterator[Element]:
        return iter(self._elements.values())

    def __len__(self) -> int:
        return len(self._elements)

    # -- updating -----------------------------------------------------------

    def update(self, *updates: Update) -> None:
        """Apply one or more ``{id: value}`` mappings, in order.

        Unknown ids and values an element rejects are logged (once per id) and
        skipped; they never interrupt the rest of the batch.
        """
        changed = False
        version = self.version + 1
        for mapping in updates:
            for key, value in mapping.items():
                if element := self._apply(key, value):
                    self._versions[element.id] = version
                    changed = True
        if changed:
            self.version = version
            self._changed.set()
            self._changed = asyncio.Event()

    def set(self, id: str, value: Any) -> None:
        """Update a single element (or field)."""
        self.update({id: value})

    def _apply(self, key: str, value: Any) -> Element | None:
        """Update the element ``key`` addresses; return it, or None if rejected."""
        route = self._route(key)
        if route is None:
            self._reject(key, "no element with this id")
            return None
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

    # -- observing ----------------------------------------------------------

    async def wait_for_change(self, since: int) -> int:
        """Wait until :attr:`version` differs from ``since``; return the new version."""
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
        async for batch in source.updates():
            self.update(*batch)

    async def run(
        self, *, sources: Iterable[Source] = (), outputs: Iterable[Output] = ()
    ) -> None:
        """Feed all ``sources`` into the monitor and run all ``outputs``.

        Runs until cancelled. If any source or output fails, the others are
        cancelled and the error propagates.
        """
        async with asyncio.TaskGroup() as tasks:
            for source in sources:
                tasks.create_task(self.consume(source))
            for output in outputs:
                tasks.create_task(output.run(self))


def _describe(element: Element) -> dict[str, JSON]:
    return {"id": element.id, **element.describe()}

"""Web dashboard (requires ``aiohttp``).

The server pushes values to the browser over a WebSocket: a full snapshot on
connect (and again whenever elements are added), then only the elements that
changed, at most ``fps`` times a second. The bundled ``app_monitor.js`` writes
each value into every page element whose ``data-bind`` attribute names it, so
a dashboard is plain HTML::

    <span data-bind="position_x"></span>
    <div class="led" data-bind="machine_status.estop" data-mode="state"></div>

Without a page of your own, a generic page listing every element is served.

Any program can also push values with an HTTP POST to ``/update``, and read
the current values from ``/values``::

    curl -d '{"temperature": 21.5}' http://127.0.0.1:8080/update
    curl -d 'progress=5 status=running' http://127.0.0.1:8080/update
    curl http://127.0.0.1:8080/values
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hmac
import json
import logging
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from ..decoders import DecodeError, KeyValueDecoder, flatten
from ..monitor import Monitor

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "static"
MONITOR_KEY = web.AppKey("monitor", Monitor)


class WebDashboard:
    """Serves a dashboard page, streams the monitor's values to it, accepts
    updates posted to ``/update`` and returns the current values from
    ``/values`` (JSON).

    Args:
        page: your dashboard HTML file; a generic page is used if omitted.
        static_dir: served at ``/static/`` (your CSS, images, ...).
        host: interface to listen on. The default only accepts local
            connections; use ``"0.0.0.0"`` to allow other machines (who can
            then see the values, and post updates unless ``token`` is set).
        port: TCP port; 0 picks a free one (see :attr:`url` once started).
        title: heading of the generic page.
        token: if set, posting to ``/update`` requires the header
            ``Authorization: Bearer <token>``.
        stale_after: seconds without new data after which the page dims its
            values, for sources expected to update continuously.
        fps: maximum pushes per second to each browser.
        announce: print the address when the server starts.
        open_browser: open the page in a browser once the server is up (not
            on Linux without a display, e.g. over SSH).

    The page loads the client from ``/_app_monitor/app_monitor.js``; the LCD
    and LED styles and fonts are at ``/_app_monitor/panel.css``.
    """

    def __init__(
        self,
        page: str | Path | None = None,
        *,
        static_dir: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        title: str = "Monitor",
        token: str | None = None,
        stale_after: float | None = None,
        fps: float = 30,
        announce: bool = True,
        open_browser: bool = False,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.page = Path(page) if page else ASSETS / "auto.html"
        self.static_dir = Path(static_dir) if static_dir else None
        self.host = host
        self.port = port
        self.title = title
        self.token = token
        self.stale_after = stale_after
        self.fps = fps
        self.announce = announce
        self.open_browser = open_browser
        self._runner: web.AppRunner | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._sent: dict[int, int] = {}  # version each open page has been sent
        self._sent_changed = asyncio.Event()
        for path in (self.page, self.static_dir):
            if path and not path.exists():
                raise FileNotFoundError(path)

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    def app(self, monitor: Monitor) -> web.Application:
        app = web.Application()
        app[MONITOR_KEY] = monitor
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._websocket)
        app.router.add_post("/update", self._receive)
        app.router.add_get("/values", self._values)
        app.router.add_static("/_app_monitor/", ASSETS)
        if self.static_dir:
            app.router.add_static("/static/", self.static_dir)
        app.on_response_prepare.append(_revalidate)
        return app

    async def start(self, monitor: Monitor) -> None:
        """Start serving (called by the monitor before it runs anything)."""
        if self._runner is not None:
            return
        runner = web.AppRunner(self.app(monitor), access_log=None, shutdown_timeout=1)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except OSError as error:
            await runner.cleanup()
            if error.errno in (errno.EADDRINUSE, 10048) or "in use" in str(error):
                raise OSError(
                    error.errno,
                    f"port {self.port} is already in use (is another dashboard "
                    f"running?); choose a different port",
                ) from None
            raise
        self._runner = runner
        self.port = runner.addresses[0][1]
        if self.announce:
            everywhere = (
                " (on all network interfaces)" if self.host == "0.0.0.0" else ""
            )
            print(f"Dashboard: {self.url}{everywhere}", file=sys.stderr, flush=True)
        if self.open_browser and _has_display():
            # webbrowser.open can block briefly while it launches the browser.
            await asyncio.to_thread(webbrowser.open, self.url)

    async def run(self, monitor: Monitor) -> None:
        await self.start(monitor)
        try:
            await asyncio.Event().wait()  # serve until cancelled
        finally:
            await self.close()

    async def flush(self, monitor: Monitor, within: float = 1.0) -> None:
        """Wait (up to ``within`` seconds) until every open page has the latest
        values, so a program that is about to exit leaves its final state on
        screen."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while any(sent < monitor.version for sent in self._sent.values()):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            changed = self._sent_changed
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), remaining)

    def _record_sent(self, connection: int, version: int) -> None:
        self._sent[connection] = version
        self._sent_changed.set()
        self._sent_changed = asyncio.Event()

    async def close(self) -> None:
        if self._runner is not None:
            runner, self._runner = self._runner, None
            # Close pages' sockets first; the server would otherwise wait for
            # them, and they'd only notice when their connection timed out.
            await asyncio.gather(
                *(
                    ws.close(code=WSCloseCode.GOING_AWAY, message=b"server stopped")
                    for ws in list(self._sockets)
                ),
                return_exceptions=True,
            )
            await runner.cleanup()

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.page)

    async def _receive(self, request: web.Request) -> web.Response:
        if self.token is not None:
            given = request.headers.get("Authorization", "")
            if not hmac.compare_digest(given, f"Bearer {self.token}"):
                raise web.HTTPUnauthorized(text="missing or wrong token\n")
        try:
            updates = parse_posted_updates(await request.read())
        except DecodeError as error:
            raise web.HTTPBadRequest(text=f"{error}\n") from None
        request.app[MONITOR_KEY].update(*updates)
        return web.Response(status=204)

    async def _values(self, request: web.Request) -> web.Response:
        return web.json_response(request.app[MONITOR_KEY].snapshot())

    def _snapshot(self, monitor: Monitor) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "title": self.title,
            "staleAfter": self.stale_after,
            "layout": monitor.describe(),
            "values": monitor.snapshot(),
            "ages": monitor.ages(),
        }

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        monitor = request.app[MONITOR_KEY]
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        version, layout = monitor.version, monitor.layout_version
        self._sockets.add(ws)
        try:
            await ws.send_json(self._snapshot(monitor))
            self._record_sent(id(ws), version)
            pusher = asyncio.create_task(
                self._push_changes(ws, monitor, version, layout)
            )
            try:
                async for message in ws:  # the page sends nothing; wait for close
                    if message.type == WSMsgType.ERROR:
                        break
            finally:
                pusher.cancel()
        finally:
            self._sockets.discard(ws)
            self._sent.pop(id(ws), None)
        return ws

    async def _push_changes(
        self, ws: web.WebSocketResponse, monitor: Monitor, version: int, layout: int
    ) -> None:
        # Each browser gets its own loop, so a slow one just receives fewer,
        # larger updates instead of holding up the others.
        try:
            while not ws.closed:
                latest = await monitor.wait_for_change(version)
                if monitor.layout_version != layout:  # elements added: redraw
                    layout = monitor.layout_version
                    await ws.send_json(self._snapshot(monitor))
                else:
                    await ws.send_json(
                        {"type": "update", "values": monitor.changes_since(version)}
                    )
                version = latest
                self._record_sent(id(ws), version)
                await asyncio.sleep(1 / self.fps)
        except ConnectionError:
            pass


def _has_display() -> bool:
    """Whether opening a browser makes sense. On Linux without a graphical
    session, webbrowser would fall back to a text browser in the terminal."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def parse_posted_updates(body: bytes) -> list[dict[str, Any]]:
    """The updates in a POST body: a JSON object or list of objects, or lines of
    ``id value`` / ``key=value`` pairs."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise DecodeError("no updates in the request body")
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise DecodeError(f"invalid JSON: {error}") from None
        items = data if isinstance(data, list) else [data]
        if not all(isinstance(item, dict) for item in items):
            raise DecodeError("expected a JSON object or a list of objects")
        return [flatten(item) for item in items]
    decoder = KeyValueDecoder()
    return [
        decoder.decode_line(line.strip()) for line in text.splitlines() if line.strip()
    ]


async def _revalidate(request: web.Request, response: web.StreamResponse) -> None:
    # Without this, browsers cache pages and assets heuristically and keep
    # showing old CSS/JS after you edit your dashboard or upgrade the package.
    # Revalidating is cheap: unchanged files come back as 304 Not Modified.
    response.headers.setdefault("Cache-Control", "no-cache")

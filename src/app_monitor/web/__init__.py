"""Web dashboard (requires ``aiohttp``).

The server pushes values to the browser over a WebSocket: a full snapshot on
connect, then only the elements that changed, at most ``fps`` times a second.
The bundled ``app_monitor.js`` writes each value into every page element whose
``data-bind`` attribute names it, so a dashboard is plain HTML::

    <span data-bind="position_x"></span>
    <div class="led" data-bind="machine_status.estop" data-mode="state"></div>

Without a page of your own, a generic page listing every element is served.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from ..monitor import Monitor

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "static"
MONITOR_KEY = web.AppKey("monitor", Monitor)


class WebDashboard:
    """Serves a dashboard page and streams the monitor's values to it.

    Args:
        page: your dashboard HTML file; a generic page is used if omitted.
        static_dir: served at ``/static/`` (your CSS, images, ...).
        host: interface to listen on. The default only accepts local
            connections; use ``"0.0.0.0"`` to allow other machines.
        port: TCP port.
        fps: maximum pushes per second to each browser.

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
        fps: float = 30,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.page = Path(page) if page else ASSETS / "auto.html"
        self.static_dir = Path(static_dir) if static_dir else None
        self.host = host
        self.port = port
        self.fps = fps
        for path in (self.page, self.static_dir):
            if path and not path.exists():
                raise FileNotFoundError(path)

    def app(self, monitor: Monitor) -> web.Application:
        app = web.Application()
        app[MONITOR_KEY] = monitor
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._websocket)
        app.router.add_static("/_app_monitor/", ASSETS)
        if self.static_dir:
            app.router.add_static("/static/", self.static_dir)
        app.on_response_prepare.append(_revalidate)
        return app

    async def run(self, monitor: Monitor) -> None:
        runner = web.AppRunner(self.app(monitor), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
            logger.info("Dashboard running at http://%s:%d/", self.host, self.port)
            await asyncio.Event().wait()  # serve until cancelled
        finally:
            await runner.cleanup()

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.page)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        monitor = request.app[MONITOR_KEY]
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        version = monitor.version
        await ws.send_json(
            {
                "type": "snapshot",
                "layout": monitor.describe(),
                "values": monitor.snapshot(),
            }
        )
        pusher = asyncio.create_task(self._push_changes(ws, monitor, version))
        try:
            async for message in ws:  # the page sends nothing; this waits for close
                if message.type == WSMsgType.ERROR:
                    break
        finally:
            pusher.cancel()
        return ws

    async def _push_changes(
        self, ws: web.WebSocketResponse, monitor: Monitor, version: int
    ) -> None:
        # Each browser gets its own loop, so a slow one just receives fewer,
        # larger updates instead of holding up the others.
        try:
            while not ws.closed:
                latest = await monitor.wait_for_change(version)
                await ws.send_json(
                    {"type": "update", "values": monitor.changes_since(version)}
                )
                version = latest
                await asyncio.sleep(1 / self.fps)
        except ConnectionError:
            pass


async def _revalidate(request: web.Request, response: web.StreamResponse) -> None:
    # Without this, browsers cache pages and assets heuristically and keep
    # showing old CSS/JS after you edit your dashboard or upgrade the package.
    # Revalidating is cheap: unchanged files come back as 304 Not Modified.
    response.headers.setdefault("Cache-Control", "no-cache")

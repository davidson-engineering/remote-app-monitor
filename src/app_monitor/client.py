"""Send values to a running dashboard from another program.

Uses only the standard library, so it works in any Python program without
installing the dashboard's extras::

    from app_monitor.client import Client

    dashboard = Client()                    # http://127.0.0.1:8080
    dashboard.set("progress", 0.5)          # returns immediately
    dashboard.update({"rate": 18.6, "status": "running"})

Updates are sent in the background, in order, a few times a second. If the
dashboard isn't reachable yet they are kept and retried, and anything still
pending is sent when the program exits (waiting up to ``exit_timeout``).
For a single message, :func:`send` posts it straight away.

Any other language can do the same with an HTTP POST, e.g.
``curl -d 'progress=5' http://127.0.0.1:8080/update``.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Self

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8080"


class Client:
    """Sends updates to a :class:`~app_monitor.WebDashboard` over HTTP.

    Args:
        url: the dashboard's address (as printed when it starts).
        token: the dashboard's ``token``, if it has one.
        interval: seconds to gather updates before each send.
        max_pending: updates kept while the dashboard is unreachable; the
            oldest are dropped beyond this.
        exit_timeout: how long to keep trying to deliver pending updates when
            the program exits.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        token: str | None = None,
        interval: float = 0.05,
        max_pending: int = 10_000,
        exit_timeout: float = 2.0,
    ) -> None:
        self.url = _update_url(url)
        self.token = token
        self.interval = interval
        self.max_pending = max_pending
        self.exit_timeout = exit_timeout
        self._pending: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._flushing = False
        self._closing = False
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._send_forever, name="app_monitor client", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    def set(self, id: str, value: Any) -> None:
        """Queue a new value for one element."""
        self.update({id: value})

    def update(self, *updates: Mapping[str, Any]) -> None:
        """Queue ``{id: value}`` mappings; they are applied in order."""
        with self._condition:
            if self._closing:
                raise RuntimeError("this client is closed")
            self._pending.extend(dict(update) for update in updates)
            overflow = len(self._pending) - self.max_pending
            if overflow > 0:
                del self._pending[:overflow]
                if not self._dropped:
                    logger.warning(
                        "Dashboard at %s unreachable; dropping the oldest updates "
                        "beyond %d",
                        self.url,
                        self.max_pending,
                    )
                self._dropped += overflow
            self._condition.notify_all()

    def flush(self, timeout: float = 2.0) -> bool:
        """Send everything queued now. Returns False if it couldn't within
        ``timeout`` seconds (e.g. the dashboard isn't running)."""
        with self._condition:
            self._flushing = True
            self._condition.notify_all()
            try:
                return self._condition.wait_for(lambda: not self._pending, timeout)
            finally:
                self._flushing = False

    def close(self, timeout: float | None = None) -> None:
        """Deliver what's queued (waiting up to ``timeout``, default
        ``exit_timeout``) and stop the background thread."""
        if self._closing:
            return
        self.flush(self.exit_timeout if timeout is None else timeout)
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(1)
        atexit.unregister(self.close)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send_forever(self) -> None:
        failures = 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing)
                if self._closing:
                    return
                batch = list(self._pending)
            try:
                _post(self.url, batch, self.token, timeout=5)
            except (OSError, ValueError) as error:
                if _refused(error):
                    # The dashboard rejected these (bad token or data): retrying
                    # won't help, so report and drop them instead of blocking.
                    logger.error(
                        "Dashboard at %s rejected %d update(s): %s",
                        self.url,
                        len(batch),
                        _reason(error),
                    )
                else:
                    failures += 1
                    if failures == 1:
                        logger.warning(
                            "Can't reach the dashboard at %s (%s); will keep retrying",
                            self.url,
                            _reason(error),
                        )
                    with self._condition:  # back off, but wake up to close
                        self._condition.wait_for(
                            lambda: self._closing, min(0.1 * 2**failures, 2.0)
                        )
                    continue
            if failures:
                logger.info("Reached the dashboard at %s", self.url)
            failures = 0
            with self._condition:
                del self._pending[: len(batch)]
                self._condition.notify_all()
                self._condition.wait_for(
                    lambda: self._flushing or self._closing, self.interval
                )


def send(
    updates: Mapping[str, Any],
    url: str = DEFAULT_URL,
    *,
    token: str | None = None,
    timeout: float = 5.0,
) -> None:
    """Send one ``{id: value}`` mapping now, raising ConnectionError on failure."""
    target = _update_url(url)
    try:
        _post(target, [dict(updates)], token, timeout)
    except (OSError, ValueError) as error:
        raise ConnectionError(
            f"can't send to the dashboard at {target}: {_reason(error)}"
        ) from None


def _update_url(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/update") else f"{url}/update"


def _post(
    url: str, updates: list[dict[str, Any]], token: str | None, timeout: float
) -> None:
    body = json.dumps(updates, default=str).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as error:
        with error:  # it holds the open response; don't leak it
            detail = error.read().decode(errors="replace").strip()
        raise HTTPFailure(error.code, detail or str(error.reason)) from None


class HTTPFailure(OSError):
    """The dashboard answered with an HTTP error."""

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code


def _refused(error: BaseException) -> bool:
    return isinstance(error, HTTPFailure) and 400 <= error.code < 500


def _reason(error: BaseException) -> str:
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error)

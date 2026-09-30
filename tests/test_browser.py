"""The browser client (sightglass.js) in a real browser: pages stay bound to
live values exactly as documented.

Needs Chromium once: ``uv run playwright install chromium``. Uses Playwright's
async API, so these run alongside the other asyncio tests in one session.
"""

import asyncio
import os
import re
import subprocess
import sys

import pytest
from playwright.async_api import async_playwright, expect

from sightglass import (
    IndicatorLamp,
    Monitor,
    ProgressBar,
    RangeBar,
    Sparkline,
    TextElement,
    WebDashboard,
)

pytestmark = pytest.mark.browser


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.fixture
def serve(tmp_path):
    """``serve(body, monitor)``: start a dashboard whose page is ``body`` (the
    generic page for None) and return its address."""
    started = []

    def serve(body: str | None, monitor: Monitor, **options) -> str:
        page = None
        if body is not None:
            page = tmp_path / "page.html"
            page.write_text(
                f"<!doctype html><html><head><style>{options.pop('css', '')}"
                f"</style></head><body>{body}"
                '<script src="/_sightglass/sightglass.js"></script></body></html>'
            )
        web = WebDashboard(page, port=0, announce=False, **options)
        started.append(monitor.start(outputs=[web]))
        return web.url

    yield serve
    for monitor in started:
        monitor.stop()


async def test_text_width_and_state_bindings_follow_the_values(page, serve):
    monitor = Monitor()
    monitor.add(
        RangeBar("speed", max_value=200, units="km/h", precision=0),
        IndicatorLamp("pump"),
    )
    await page.goto(
        serve(
            '<span id="speed" data-bind="speed.text"></span>'
            '<div id="bar" data-bind="speed.ratio" data-mode="width"></div>'
            '<div id="pump" data-bind="pump" data-mode="state"></div>'
            '<span id="created" data-bind="job.status"></span>',
            monitor,
        )
    )
    await expect(page.locator("html")).to_have_attribute("data-connection", "open")
    await expect(page.locator("#pump")).to_have_attribute("data-state", "off")

    monitor.update({"speed": 50, "pump": True, "job.status": "running"})
    await expect(page.locator("#speed")).to_have_text("50 km/h")
    await expect(page.locator("#bar")).to_have_attribute("style", "width: 25%;")
    await expect(page.locator("#pump")).to_have_attribute("data-state", "on")
    await expect(page.locator("#created")).to_have_text("running")  # made on first use


async def test_state_binding_reads_text_from_devices(page, serve):
    monitor = Monitor()
    body = '<i id="pump" data-bind="pump" data-mode="state"></i>'
    await page.goto(serve(body, monitor))
    lamp = page.locator("#pump")
    for value, state in [("on", "on"), ("off", "off"), ("1", "on"), (" No ", "off")]:
        monitor.set("pump", value)
        await expect(lamp).to_have_attribute("data-state", state)


async def test_sparkline_binding_draws_the_history(page, serve):
    monitor = Monitor()
    monitor.add(Sparkline("rate", points=5))
    chart = '<div id="chart" data-bind="rate.values" data-mode="sparkline"></div>'
    await page.goto(serve(chart, monitor))
    monitor.update({"rate": 1}, {"rate": 3}, {"rate": 2})
    await expect(page.locator("#chart polyline")).to_have_attribute(
        "points", "0.00,19.00 50.00,1.00 100.00,10.00"
    )
    monitor.update(*[{"rate": 7}] * 5)  # steady: a level line, not one at zero
    await expect(page.locator("#chart polyline")).to_have_attribute(
        "points", "0.00,10.00 25.00,10.00 50.00,10.00 75.00,10.00 100.00,10.00"
    )


async def test_var_binding_hands_numbers_to_css(page, serve):
    monitor = Monitor()
    monitor.add(RangeBar("q", max_value=40), TextElement("pitch"), IndicatorLamp("lit"))
    await page.goto(
        serve(
            '<div id="q" data-bind="q.ratio" data-mode="var"></div>'
            '<div id="pitch" data-bind="pitch" data-mode="var"></div>'
            '<div id="lit" data-bind="lit" data-mode="var"></div>',
            monitor,
            css="#q { width: calc(var(--value) * 200px) }"
            "#pitch { rotate: calc(var(--value, 90) * 1deg) }",
        )
    )
    monitor.update({"q": 10, "pitch": "+45.5", "lit": True})
    await expect(page.locator("#q")).to_have_css("width", "50px")  # ratio 0.25
    await expect(page.locator("#pitch")).to_have_css("rotate", "45.5deg")  # device text
    await expect(page.locator("#lit")).to_have_css("--value", "1")

    monitor.set("pitch", "n/a")  # not a number: CSS falls back
    await expect(page.locator("#pitch")).to_have_css("rotate", "90deg")


async def test_stale_after_flags_a_value_that_stopped_arriving(page, serve):
    monitor = Monitor()
    monitor.add(TextElement("wind"), TextElement("tide"))
    url = serve(
        '<span id="wind" data-bind="wind" data-stale-after="1"></span>'
        '<span id="tide" data-bind="tide" data-stale-after="1"></span>'
        '<span id="plain" data-bind="wind"></span>',
        monitor,
    )
    await page.goto(url)
    wind, tide = page.locator("#wind"), page.locator("#tide")
    await expect(tide).to_have_attribute("data-stale", "true")  # nothing ever arrived
    await expect(wind).to_have_attribute("data-stale", "true")

    for _ in range(8):  # a steady value still counts as arriving
        monitor.set("wind", 12)
        await asyncio.sleep(0.25)
        await expect(wind).not_to_have_attribute("data-stale", "true")
    await expect(wind).to_have_attribute("data-stale", "true")  # stopped arriving
    await expect(page.locator("#plain")).not_to_have_attribute("data-stale", "true")

    await page.reload()  # a fresh page knows how old the values are
    await expect(wind).to_have_text("12")
    await expect(wind).to_have_attribute("data-stale", "true")
    monitor.set("wind", 14)
    await expect(wind).not_to_have_attribute("data-stale", "true")


async def test_stale_after_holds_while_the_page_is_disconnected(page, serve):
    """Cut off from the dashboard, the page can't tell whether values still
    arrive there, so it doesn't claim they stopped."""
    monitor = Monitor()
    body = '<span id="wind" data-bind="wind" data-stale-after="2"></span>'
    await page.goto(serve(body, monitor))
    wind = page.locator("#wind")
    monitor.set("wind", 12)
    await expect(wind).not_to_have_attribute("data-stale", "true")
    monitor.stop()
    await expect(page.locator("html")).to_have_attribute("data-connection", "closed")
    await asyncio.sleep(3)
    await expect(wind).not_to_have_attribute("data-stale", "true")


async def test_page_marks_quiet_sources_and_a_lost_connection(page, serve):
    monitor = Monitor()
    await page.goto(serve('<span data-bind="x"></span>', monitor, stale_after=1))
    html = page.locator("html")
    monitor.set("x", 1)
    await expect(html).to_have_attribute("data-stale", "true")
    monitor.set("x", 2)
    await expect(html).not_to_have_attribute("data-stale", "true")
    monitor.stop()
    await expect(html).to_have_attribute("data-connection", "closed")


async def test_generic_page_draws_declared_and_new_elements(page, serve):
    monitor = Monitor()
    monitor.add(ProgressBar("job", label="Job"))
    await page.goto(serve(None, monitor, title="Line 3"))
    await expect(page).to_have_title("Line 3")
    monitor.update({"job": 25, "status": "ok"})
    await expect(page.locator(".row", has_text="Job")).to_contain_text("25.0%")
    await expect(page.locator(".row", has_text="status")).to_contain_text("ok")
    await expect(page.locator("#connection")).to_have_text("Live")


async def test_launch_demo_page_comes_alive(page):
    """sightglass --demo launch: every feed arrives and the instruments move."""
    process = await asyncio.create_subprocess_exec(
        *[sys.executable, "-m", "sightglass", "--demo", "launch", "--port", "0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        url = (await process.stderr.readline()).decode().split()[1]
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else 0)
        await page.goto(url)
        clock = page.locator('.clock [data-bind="clock.time"]')
        await expect(clock).to_have_text(re.compile(r"^\d\d:\d\d:\d\d$"))
        await expect(page.locator(".feeds .led[data-bind]")).to_have_count(3)
        # The feeders are separate processes: allow them time to start.
        await expect(page.locator(".feeds [data-stale]")).to_have_count(
            0, timeout=15_000
        )
        gauge = page.locator('[data-bind="acceleration.ratio"]')
        await expect(gauge).to_have_css("--value", "0.2")  # 1 g, standing on the pad
        await expect(page.locator(".weather .chart polyline")).to_have_count(1)
        assert not errors
    finally:
        process.terminate()
        await asyncio.wait_for(process.communicate(), 10)

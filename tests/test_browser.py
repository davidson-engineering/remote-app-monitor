"""The browser client (app_monitor.js) in a real browser: pages stay bound to
live values exactly as documented.

Needs Chromium once: ``uv run playwright install chromium``. Uses Playwright's
async API, so these run alongside the other asyncio tests in one session.
"""

import pytest
from playwright.async_api import async_playwright, expect

from app_monitor import (
    IndicatorLamp,
    Monitor,
    ProgressBar,
    RangeBar,
    Sparkline,
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
                '<script src="/_app_monitor/app_monitor.js"></script></body></html>'
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

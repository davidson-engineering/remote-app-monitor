"""The weather mast at the pad: wind, gusts and temperature, once a second.

This program doesn't use sightglass at all: it posts plain text to the
dashboard's /update address, as any language or device could. Each post is
the same as

    curl -d 'weather.wind=12.4 weather.gust=17.9 weather.direction=240' http://127.0.0.1:8080/update

The demo starts it; to run it yourself:

    python -m sightglass.launch.weather http://127.0.0.1:8080
"""

import argparse
import multiprocessing
import random
import time
import urllib.request


def post(url: str, readings: dict[str, float]) -> None:
    body = " ".join(f"weather.{name}={value:.1f}" for name, value in readings.items())
    request = urllib.request.Request(f"{url.rstrip('/')}/update", data=body.encode())
    with urllib.request.urlopen(request, timeout=2):
        pass


def run(url: str) -> None:
    parent = multiprocessing.parent_process()  # None when run on its own
    wind, direction, temperature = 11.0, 240.0, 24.0
    reachable = True
    try:
        while parent is None or parent.is_alive():
            # Wander, but drift back to a calm day's 11 kt from the south-west.
            wind += 0.1 * (11 - wind) + random.gauss(0, 0.8)
            direction += 0.05 * (240 - direction) + random.gauss(0, 3)
            temperature += 0.05 * (24 - temperature) + random.gauss(0, 0.05)
            readings = {
                "wind": max(wind, 0),
                "gust": max(wind, 0) * random.uniform(1.2, 1.4),
                "direction": direction,
                "temperature": temperature,
            }
            try:
                post(url, readings)
                reachable = True
            except OSError as error:  # includes HTTP errors
                if reachable:  # say so once, then keep trying
                    print(f"weather: can't reach {url} ({error}); retrying")
                reachable = False
            time.sleep(1)
    except KeyboardInterrupt:  # Ctrl+C reaches every process
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="the dashboard's address")
    run(parser.parse_args().url)

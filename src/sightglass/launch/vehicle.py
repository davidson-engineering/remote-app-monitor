"""The rocket: telemetry as its flight computer would send it down.

A simple two-stage ascent is flown once, when this module is imported:
a vertical climb, a gravity turn, throttling back through maximum aerodynamic
pressure (max-Q) and to stay under 4.5 g, into a 200 km orbit. Vehicle(...)
then reads the flight at any moment of the launch cycle.

The demo's dashboard runs it as a SimulatedSource. A real vehicle's downlink would
arrive through SerialSource (a radio modem) or ZmqSource (a telemetry
decoder) with the same ids, and the page wouldn't change.
"""

import math
from typing import Any, NamedTuple

from .mission import (
    FAIRING,
    MECO,
    SECO,
    SEPARATION,
    SES,
    log_start,
    mission_time,
    stamp,
)

G0 = 9.80665  # m/s²
EARTH_RADIUS = 6_371_000.0  # m
STEP = 0.05  # s

IGNITION = -3.0  # engines light 3 s before liftoff, one every 0.1 s
LIMIT = 4.5  # g: the guidance throttles back to stay below this

# Thrust per unit of starting mass (m/s²), and the share of that mass burned
# per second at full throttle; fitted to reach orbit on the mission timeline.
THRUST_1, BURN_1 = 20.8, 0.00785
THRUST_2, BURN_2 = 29.4, 0.005
DRAG = 0.00003  # drag per unit mass per pascal of dynamic pressure

ENGINES = [*(f"e{n}" for n in range(1, 10)), "mvac"]  # 9 first-stage engines
EVENTS = ["liftoff", "maxq", "meco", "separation", "ses", "fairing", "seco"]


class State(NamedTuple):
    altitude: float  # m
    velocity: float  # m/s
    downrange: float  # m, along the ground
    acceleration: float  # g, as felt on board
    q: float  # dynamic pressure, Pa
    throttle: float  # 0 to 1
    pitch: float  # degrees above the horizon
    stage_1: float  # mass left, as a share of the stage's starting mass
    stage_2: float


def pitch_program(t: float) -> float:
    """Straight up, a gravity turn, then level by the end of the burn."""
    if t < 6:
        return 90.0
    if t < MECO:
        return 90 - 43.3 * ((t - 6) / (MECO - 6)) ** 0.91
    return 46.7 * max(0.0, 1 - (t - MECO) / (SECO - MECO)) ** 1.73


def fly() -> list[State]:
    """The ascent from liftoff to orbit, every STEP seconds."""
    flight = []
    t = velocity = altitude = downrange = 0.0
    mass_1 = mass_2 = 1.0
    while t <= SECO:
        pitch = pitch_program(t)
        climb = math.radians(pitch)
        q = 0.5 * 1.225 * math.exp(-altitude / 8500) * velocity**2
        throttle = thrust = drag = 0.0
        if t < MECO:
            throttle = min(
                1.0,
                max(0.7, 1 - (q - 26_000) / 10_000 * 0.3),  # through max-Q
                LIMIT * G0 * mass_1 / THRUST_1,
            )
            thrust = throttle * THRUST_1 / mass_1
            drag = q * DRAG / mass_1
            mass_1 -= BURN_1 * throttle * STEP
        elif t >= SES:
            throttle = min(1.0, LIMIT * G0 * mass_2 / THRUST_2)
            thrust = throttle * THRUST_2 / mass_2
            mass_2 -= BURN_2 * throttle * STEP
        flight.append(
            State(
                altitude,
                velocity,
                downrange,
                (thrust - drag) / G0,
                q,
                throttle,
                pitch,
                mass_1,
                mass_2,
            )
        )
        radius = EARTH_RADIUS + altitude
        across = velocity * math.cos(climb)
        # Gravity, less the lift from going round the Earth, which is all of
        # it at orbital speed.
        gravity = G0 * (EARTH_RADIUS / radius) ** 2 - across**2 / radius
        velocity += (thrust - drag - gravity * math.sin(climb)) * STEP
        altitude += velocity * math.sin(climb) * STEP
        downrange += across * STEP * EARTH_RADIUS / radius
        t += STEP
    return flight


FLIGHT = fly()
MAX_Q = max(range(len(FLIGHT)), key=lambda i: FLIGHT[i].q) * STEP
ON_PAD = State(0, 0, 0, 1, 0, 0, 90, 1, 1)
ORBIT = FLIGHT[-1]
EMPTY = (ORBIT.stage_1, ORBIT.stage_2)  # what's left when each burn ends


def state(t: float) -> State:
    """The vehicle ``t`` seconds after liftoff."""
    if t < 0:
        return ON_PAD._replace(throttle=1.0 if t >= IGNITION + 0.9 else 0.0)
    if t >= SECO:  # coasting in orbit
        coasted = (t - SECO) * ORBIT.velocity * EARTH_RADIUS
        return ORBIT._replace(
            downrange=ORBIT.downrange + coasted / (EARTH_RADIUS + ORBIT.altitude),
            acceleration=0.0,
            throttle=0.0,
        )
    i, part = divmod(t / STEP, 1)
    before, after = FLIGHT[int(i)], FLIGHT[min(int(i) + 1, len(FLIGHT) - 1)]
    return State(*(a + (b - a) * part for a, b in zip(before, after, strict=True)))


def tank(mass: float, empty: float, reserve: float) -> float:
    """Percent of a tank left: full at the start of its burn, ``reserve`` at the end."""
    return 100 * (reserve + (1 - reserve) * (mass - empty) / (1 - empty))


def bits(names: list[str], on: dict[str, bool]) -> int:
    return sum(1 << i for i, name in enumerate(names) if on[name])


class Vehicle:
    """Telemetry frames for the launch that started at ``started`` (a time.time())."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.frame = 0
        self.previous = log_start(started)

    def __call__(self, elapsed: float) -> dict[str, Any]:
        t = mission_time(self.started)
        if t < self.previous:  # the launch has started over
            self.previous = -math.inf
        now = state(t)
        self.frame += 1
        telemetry: dict[str, Any] = {
            "frame": self.frame,
            "altitude": now.altitude / 1000,
            "velocity": now.velocity,
            "downrange": now.downrange / 1000,
            "acceleration": now.acceleration,
            "q": now.q / 1000,
            "throttle": now.throttle * 100,
            "pitch": now.pitch,
            "stage": 1 if t < SEPARATION else 2,
            "engines": bits(ENGINES, self.engines(t)),
            "events": bits(
                EVENTS,
                {
                    "liftoff": t >= 0,
                    "maxq": t >= MAX_Q,
                    "meco": t >= MECO,
                    "separation": t >= SEPARATION,
                    "ses": t >= SES,
                    "fairing": t >= FAIRING,
                    "seco": t >= SECO,
                },
            ),
            "propellant.s1_lox": tank(now.stage_1, EMPTY[0], 0.02),
            "propellant.s1_fuel": tank(now.stage_1, EMPTY[0], 0.035),
            "propellant.s2_lox": tank(now.stage_2, EMPTY[1], 0.02),
            "propellant.s2_fuel": tank(now.stage_2, EMPTY[1], 0.03),
        }
        if event := self.event(self.previous, t, now):
            telemetry["log"] = f"{stamp(event[0])}  {event[1]}"
        self.previous = t
        return telemetry

    @staticmethod
    def engines(t: float) -> dict[str, bool]:
        lit = {e: IGNITION + 0.1 * n <= t < MECO for n, e in enumerate(ENGINES[:9])}
        return {**lit, "mvac": SES <= t < SECO}

    @staticmethod
    def event(before: float, t: float, now: State) -> tuple[float, str] | None:
        """The flight event between two frames, if there was one."""
        events = [
            (IGNITION, "Engine ignition"),
            (0.0, "Liftoff"),
            (MAX_Q, f"Max-Q, {now.q / 1000:.1f} kPa"),
            (MECO, f"Main engine cutoff at {now.velocity:,.0f} m/s"),
            (SEPARATION, "Stage separation"),
            (SES, "Second engine start"),
            (FAIRING, "Fairing separation"),
            (SECO, f"Orbit: {now.altitude / 1000:.0f} km at {now.velocity:,.0f} m/s"),
        ]
        for at, message in events:
            if before < at <= t:
                return at, message
        return None

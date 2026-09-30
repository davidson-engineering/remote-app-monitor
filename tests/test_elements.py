import pytest

from app_monitor import (
    Coordinate,
    IndicatorLamp,
    LogMonitor,
    MachineState,
    ProgressBar,
    RangeBar,
    Table,
    TextElement,
    TextFormat,
)
from app_monitor.formatting import visible_len


def test_label_defaults_to_last_id_segment():
    assert TextElement("X.velocity").label == "velocity"
    assert TextElement("speed", label="Speed").label == "Speed"


def test_empty_id_is_rejected():
    with pytest.raises(ValueError):
        TextElement("")


def test_text_element_formats_value_not_prefix():
    text = TextElement("x", prefix="X: ", units="mm", format=TextFormat(precision=2))
    text.update("1.5")
    assert text.render(40) == "X: 1.50 mm"
    assert text.to_json() == "1.50"


def test_text_element_scale():
    text = TextElement("x", scale=0.01, format=TextFormat(precision=1))
    text.update("250")
    assert text.to_json() == "2.5"
    with pytest.raises(ValueError):
        text.update("abc")


def test_progress_bar():
    bar = ProgressBar("p", total=10, label="Loading")
    bar.update(5)
    line = bar.render(40)
    assert line.startswith("Loading    [")
    assert "█" in line and "░" in line and line.endswith(" 50.0%")
    assert len(line) == 40
    assert bar.to_json() == {"text": "50.0%", "ratio": 0.5}
    bar.update(99)
    assert bar.ratio == 1.0


def test_range_bar_shows_actual_value_but_clamps_marker():
    bar = RangeBar("r", min_value=-10, max_value=10, units="mm", precision=1)
    assert bar.to_json() == {"text": "- mm", "ratio": None}  # no data yet
    assert "|" not in bar.render(50)
    bar.update(25)
    assert bar.to_json() == {"text": "25.0 mm", "ratio": 1.0}
    line = bar.render(50)
    assert line.index("|") == line.index("]") - 1  # marker pinned at the right end
    assert len(line) == 50


def test_range_bar_scale():
    bar = RangeBar("r", scale=60 / 6000)
    bar.update(3000)
    assert bar.value == 30


def test_table_cells_are_fields():
    table = Table("t", rows=["a", "b"], columns=["x", "y"], cell_width=5)
    table.update_field("a.x", 100)
    table.update({"b": {"y": 200}})
    assert table.to_json() == {"a": {"x": "100", "y": ""}, "b": {"x": "", "y": "200"}}
    lines = table.render(60).split("\n")
    assert lines[0] == "┌───┬───────┬───────┐"
    assert lines[3] == "│ a │  100  │       │"
    assert len({len(line) for line in lines}) == 1
    with pytest.raises(KeyError):
        table.update_field("c.x", 1)


def test_log_monitor_keeps_latest_lines():
    log = LogMonitor("log", lines=3)
    for message in ["one", "two", "three", "four"]:
        log.update(message)
    assert log.to_json() == "two\nthree\nfour"
    assert log.render(10).split("\n") == ["two       ", "three     ", "four      "]


def test_log_monitor_timestamp():
    log = LogMonitor("log", timestamp=True, timestamp_format="[%Y]")
    log.update("hi")
    assert log.to_json().endswith("] hi")


@pytest.mark.parametrize(
    ("value", "on"),
    [
        (True, True),
        (1, True),
        ("1", True),
        ("true", True),
        ("ON", True),
        (False, False),
        (0, False),
        ("0", False),
        ("false", False),
        ("off", False),
    ],
)
def test_indicator_lamp_parses_device_booleans(value, on):
    lamp = IndicatorLamp("lamp", label="Pump")
    lamp.update(value)
    assert lamp.to_json() is on
    color = "32" if on else "31"
    assert lamp.render(20) == f"Pump: \x1b[1;{color}m●\x1b[0m"


def test_indicator_lamp_rejects_garbage():
    with pytest.raises(ValueError):
        IndicatorLamp("lamp").update("maybe")


def test_machine_state_bits():
    state = MachineState("m", states=["a", "b", "c"])
    state.update(5)
    assert state.to_json() == {"a": True, "b": False, "c": True}
    state.update("0b010")
    assert state.to_json() == {"a": False, "b": True, "c": False}
    state.update_field("c", "1")
    assert state.bits == 0b110
    with pytest.raises(KeyError):
        state.update_field("d", 1)


def test_machine_state_render_wraps_to_width():
    state = MachineState("m", states=["deadman_switch", "motors_enabled", "estop"])
    lines = state.render(34).split("\n")
    assert len(lines) == 2
    assert all(visible_len(line) <= 34 for line in lines)


def test_coordinate():
    pos = Coordinate("pos", label="Position", units="mm")
    assert pos.to_json() == {"x": "-", "y": "-", "z": "-"}
    pos.update([1, -2, 3.5])
    pos.update_field("x", 0.25)
    assert pos.to_json() == {"x": "+0.2500", "y": "-2.0000", "z": "+3.5000"}
    assert pos.render(80) == "Position X: +0.2500 mm Y: -2.0000 mm Z: +3.5000 mm"
    with pytest.raises(ValueError):
        pos.update([1, 2])


def test_copy_is_independent():
    original = RangeBar("velocity")
    clone = original.copy("X.velocity")
    clone.update(5)
    assert (clone.id, clone.label, clone.value) == ("X.velocity", "velocity", 5)
    assert original.value is None


def test_range_bars_with_different_units_line_up():
    speed = RangeBar("v", units="mm/s")
    torque = RangeBar("t", units="Nm")
    for bar in (speed, torque):
        bar.update(50)
    assert speed.render(50).index("]") == torque.render(50).index("]")
    assert len(speed.render(50)) == len(torque.render(50)) == 50

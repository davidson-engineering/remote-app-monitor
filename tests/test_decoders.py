import struct

import pytest

from app_monitor import BinaryFrameDecoder, CsvDecoder, JsonDecoder, KeyValueDecoder


def test_csv_buffers_partial_lines():
    decoder = CsvDecoder(["a", "b"])
    assert decoder.feed(b"1,") == []
    assert decoder.feed(b"2\r\n3,4\n5") == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    assert decoder.feed(b",6\n") == [{"a": "5", "b": "6"}]


def test_csv_rejects_wrong_field_count_and_skips_empty_fields():
    decoder = CsvDecoder(["a", "b", "c"])
    assert decoder.feed(b"2,3\n1,,3\n\n") == [{"a": "1", "c": "3"}]
    assert decoder.rejected == 1


def test_line_decoder_drops_runaway_lines():
    decoder = CsvDecoder(["a"])
    decoder.feed(b"x" * 5000)
    assert decoder.rejected == 1
    assert decoder.feed(b"\n7\n") == [{"a": "7"}]


def test_line_decoder_decode_single_message():
    assert CsvDecoder(["a", "b"]).decode(b"1,2") == [{"a": "1", "b": "2"}]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"X.velocity 12.5", {"X.velocity": "12.5"}),
        (b"log Motor 2 stalled", {"log": "Motor 2 stalled"}),
        (b"logger var=12.5", {"logger": "var=12.5"}),
        (b"speed=3", {"speed": "3"}),
        (b"progress=5 rate=18.6", {"progress": "5", "rate": "18.6"}),
        (b'status="loading data" n=2', {"status": "loading data", "n": "2"}),
        (b"path=C:\\temp\\x", {"path": "C:\\temp\\x"}),  # backslashes kept
        (b"log it's done", {"log": "it's done"}),  # stray quote: plain value
    ],
)
def test_key_value(line, expected):
    assert KeyValueDecoder().decode(line) == [expected]


def test_key_value_rejects_bare_words():
    decoder = KeyValueDecoder()
    assert decoder.decode(b"hello") == []
    assert decoder.rejected == 1


def test_json_flattens_nested_objects():
    decoder = JsonDecoder()
    assert decoder.decode(b'{"X": {"velocity": 1.5}, "on": true, "t": {}}') == [
        {"X.velocity": 1.5, "on": True, "t": {}}
    ]
    assert decoder.decode(b"[1, 2]") == []
    assert decoder.decode(b"{nope") == []
    assert decoder.rejected == 2


def fixed_frame(element: int, value: int, params: bytes = b"") -> bytes:
    return (
        bytes([0xAA, element, 0x01])
        + struct.pack("<i", value)
        + bytes([len(params)])
        + params
        + b"\xff"
    )


def string_frame(element: int, text: str, params: bytes = b"") -> bytes:
    return (
        bytes([0xAA, element, 0x02])
        + text.encode()
        + b"\x00"
        + bytes([len(params)])
        + params
        + b"\xff"
    )


def test_binary_frames():
    decoder = BinaryFrameDecoder({1: "speed", 2: "status", 3: "motor1"})
    stream = (
        fixed_frame(1, -12345)
        + string_frame(2, "OK", params=b"\x03")
        + fixed_frame(9, 1)
    )
    assert decoder.feed(stream) == [
        {"speed": -12.345},
        {"status.motor1": "OK"},
        {"9": 0.001},
    ]


def test_binary_frames_split_at_every_byte():
    decoder = BinaryFrameDecoder({1: "speed"})
    frame = fixed_frame(1, 2500)
    updates = []
    for i in range(len(frame)):
        updates += decoder.feed(frame[i : i + 1])
    assert updates == [{"speed": 2.5}]


def test_binary_frames_payload_may_contain_start_and_end_bytes():
    value = struct.unpack("<i", bytes([0xAA, 0xFF, 0xAA, 0x00]))[0]
    assert BinaryFrameDecoder().feed(fixed_frame(1, value)) == [{"1": value / 1000}]


def test_binary_frames_resync_after_garbage_and_corruption():
    decoder = BinaryFrameDecoder({1: "a"})
    corrupt = fixed_frame(1, 1)[:-1] + b"\x00"  # wrong end byte
    unknown_type = bytes([0xAA, 1, 0x07])
    stream = b"\x01\x02" + corrupt + unknown_type + fixed_frame(1, 2000) + b"\x03"
    assert decoder.feed(stream) == [{"a": 2.0}]
    assert decoder.rejected >= 3


def test_binary_frames_reset_drops_partial_frame():
    decoder = BinaryFrameDecoder({1: "a"})
    decoder.feed(fixed_frame(1, 1)[:4])
    decoder.reset()
    assert decoder.feed(fixed_frame(1, 3000)) == [{"a": 3.0}]


def test_first_rejection_after_reset_is_expected(caplog):
    decoder = CsvDecoder(["a", "b"])
    decoder.reset()  # as on connecting: the stream is joined mid-line
    with caplog.at_level("WARNING", logger="app_monitor"):
        assert decoder.feed(b"2\n1,2\n") == [{"a": "1", "b": "2"}]
        assert decoder.rejected == 0
        assert caplog.records == []
        decoder.feed(b"bad\n")  # after that, problems are reported
    assert decoder.rejected == 1
    assert "expected 2 fields" in caplog.text


def test_misconfigured_decoder_still_warns_after_reset(caplog):
    decoder = CsvDecoder(["a", "b", "c"])
    decoder.reset()
    with caplog.at_level("WARNING", logger="app_monitor"):
        decoder.feed(b"1,2\n1,2\n")
    assert decoder.rejected == 1
    assert "expected 3 fields, got 2" in caplog.text

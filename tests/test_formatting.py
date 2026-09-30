import pytest

from app_monitor.formatting import Style, TextFormat, boxed, fit, visible_len


@pytest.mark.parametrize(
    ("value", "fmt", "expected"),
    [
        (123.456, TextFormat(width=8, precision=2), "  123.46"),
        (123.456, TextFormat(width=8, precision=2, force_sign=True), " +123.46"),
        (-123.456, TextFormat(width=8, precision=2, force_sign=True), " -123.46"),
        (
            123.456,
            TextFormat(width=8, precision=2, force_sign=True, padding="0"),
            "+0123.46",
        ),
        (-123.456, TextFormat(width=8, precision=2, padding="0"), "-0123.46"),
        (123.456, TextFormat(width=8, precision=2, padding="-"), "--123.46"),
        (
            "123.456",
            TextFormat(width=10, precision=3, force_sign=True, padding="0"),
            "+00123.456",
        ),
        (
            27.31,
            TextFormat(width=10, precision=3, force_sign=True, padding="0"),
            "+00027.310",
        ),
        (123.456, TextFormat(width=10, precision=0, padding="*"), "*******123"),
        (
            1234567890.123456789,
            TextFormat(width=20, precision=4, padding="0"),
            "000001234567890.1235",
        ),
        (123.456, TextFormat(), "123.456"),
        ("1500", TextFormat(), "1500"),  # numeric strings keep their int-ness
        ("invalid", TextFormat(width=8, precision=2), "invalid"),
        (True, TextFormat(precision=2), "True"),  # booleans aren't numbers
        (-0.0001, TextFormat(precision=3, force_sign=True), "+0.000"),  # no "-0.000"
        (12345.6, TextFormat(width=4, precision=1), "12345.6"),  # never truncated
    ],
)
def test_text_format(value, fmt, expected):
    assert fmt.format(value) == expected


def test_text_format_rejects_multi_character_padding():
    with pytest.raises(ValueError, match="one character"):
        TextFormat(padding="00")


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (Style(fg="cyan", bg="magenta", bold=True), "\x1b[1;36;45mX\x1b[0m"),
        (Style(fg="green", bold=True), "\x1b[1;32mX\x1b[0m"),
        (Style(bg="red", dim=True), "\x1b[2;41mX\x1b[0m"),
        (Style(fg="Yellow", bg="blue", bold=True, dim=True), "\x1b[1;2;33;44mX\x1b[0m"),
        (Style(), "X"),
    ],
)
def test_style(style, expected):
    assert style.apply("X") == expected


def test_style_rejects_unknown_colors():
    with pytest.raises(ValueError, match="unknown color 'teal'"):
        Style(fg="teal")


def test_visible_len_ignores_ansi_codes():
    assert visible_len(Style(fg="red", bold=True).apply("hello")) == 5


def test_fit_pads_and_truncates_by_visible_width():
    red = Style(fg="red").apply("ab")
    assert visible_len(fit(red, 5)) == 5
    assert fit("abcdef", 4) == "abc…"


def test_boxed():
    assert boxed("hi\nthere", 12, "X").split("\n") == [
        "┌─ X ──────┐",
        "│ hi       │",
        "│ there    │",
        "└──────────┘",
    ]

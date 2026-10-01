"""PLC types (Structured Text declarations, TwinCAT layouts, values) and
interface files. Standard library only: these run everywhere."""

import struct

import pytest

from sightglass.plc import (
    PlcTypeError,
    Types,
    UnknownType,
    Variable,
    canonical_type_name,
    matches,
    parse_variables,
    read_interface,
)

AXIS = """
// Axis status, as written in the PLC project
TYPE ST_Axis :
STRUCT
    fPosition : LREAL;          (* mm *)
    bEnabled  : BOOL := TRUE;
    nErrorId  : UDINT;
    sState    : STRING(20) := 'idle; really';
    eMode     : E_Mode;
    aTemps    : ARRAY [1..3] OF REAL;
END_STRUCT
END_TYPE

{attribute 'qualified_only'}
{attribute 'strict'}
TYPE E_Mode :
(
    eIdle := 0,
    eHoming,
    eRunning := 10
) UINT;
END_TYPE
"""


def layout(types: Types, name: str) -> list[tuple[str, int, str]]:
    return [
        (leaf.path, leaf.offset, leaf.type.name)
        for leaf in types.parse(name).leaves("v")
    ]


def test_members_are_aligned_to_their_size_by_default():
    types = Types(AXIS)
    assert layout(types, "ST_Axis") == [
        ("v.fPosition", 0, "LREAL"),
        ("v.bEnabled", 8, "BOOL"),
        ("v.nErrorId", 12, "UDINT"),
        ("v.sState", 16, "STRING(20)"),
        ("v.eMode", 38, "E_Mode"),
        ("v.aTemps[1]", 40, "REAL"),
        ("v.aTemps[2]", 44, "REAL"),
        ("v.aTemps[3]", 48, "REAL"),
    ]
    axis = types.parse("ST_Axis")
    assert (axis.size, axis.align) == (56, 8)  # padded to its largest member


@pytest.mark.parametrize(
    ("pack", "offsets", "size"),
    [("0", [0, 1], 9), ("1", [0, 1], 9), ("2", [0, 2], 10), ("4", [0, 4], 12)],
)
def test_pack_mode(pack, offsets, size):
    types = Types(
        f"{{attribute 'pack_mode' := '{pack}'}}\n"
        "TYPE ST_P : STRUCT a : BOOL; b : LREAL; END_STRUCT END_TYPE"
    )
    assert [offset for _, offset, _ in layout(types, "ST_P")] == offsets
    assert types.parse("ST_P").size == size


def test_nested_structs_arrays_and_extends():
    types = Types(
        """
        {attribute 'pack_mode' := '1'}
        TYPE ST_Packed : STRUCT a : BOOL; b : INT; END_STRUCT END_TYPE
        TYPE ST_Outer :
        STRUCT
            c : BOOL;
            inner : ST_Packed;        // packed: aligned to 1 byte
            grid : ARRAY [0..1, 0..1] OF SINT;
            d : DINT;
        END_STRUCT
        END_TYPE
        TYPE ST_More EXTENDS ST_Packed : STRUCT e : LREAL; END_STRUCT END_TYPE
        """
    )
    assert layout(types, "ST_Outer") == [
        ("v.c", 0, "BOOL"),
        ("v.inner.a", 1, "BOOL"),
        ("v.inner.b", 2, "INT"),
        ("v.grid[0,0]", 4, "SINT"),
        ("v.grid[0,1]", 5, "SINT"),
        ("v.grid[1,0]", 6, "SINT"),
        ("v.grid[1,1]", 7, "SINT"),
        ("v.d", 8, "DINT"),
    ]
    assert layout(types, "ST_More") == [
        ("v.a", 0, "BOOL"),
        ("v.b", 1, "INT"),
        ("v.e", 8, "LREAL"),
    ]
    assert [
        leaf.path for leaf in types.parse("ARRAY[2..3] OF ST_Packed").leaves("x")
    ] == [
        "x[2].a",
        "x[2].b",
        "x[3].a",
        "x[3].b",
    ]


def test_type_names_as_the_plc_reports_them():
    types = Types(AXIS + "TYPE T_Speed : LREAL; END_TYPE")
    for text, name, size in [
        ("LREAL", "LREAL", 8),
        ("string", "STRING(80)", 81),
        ("STRING[10]", "STRING(10)", 11),
        ("WSTRING(4)", "WSTRING(4)", 10),
        ("ARRAY [0..9] OF INT", "ARRAY [0..9] OF INT", 20),
        ("Tc2_Lib.ST_Axis", "ST_Axis", 56),
        ("T_Speed", "T_Speed", 8),
        ("TIME_OF_DAY", "TOD", 4),
    ]:
        parsed = types.parse(text)
        assert (parsed.name, parsed.size) == (name, size), text
    with pytest.raises(UnknownType, match="ST_Missing"):
        types.parse("ARRAY [0..1] OF ST_Missing")


def test_declarations_tolerate_what_twincat_writes():
    types = Types(
        """
        (* a block comment
           over lines *)
        TYPE ST_Io :
        STRUCT
            {attribute 'hide'}
            bIn AT %I* : BOOL;
            nA, nB : INT := 1;      /* two at once */
            aInit : ARRAY [0..1] OF INT := [1, (2)];
        END_STRUCT
        END_TYPE
        TYPE E_Plain : (A, B := 5, C) END_TYPE
        """
    )
    assert [path for path, _, _ in layout(types, "ST_Io")] == [
        "v.bIn",
        "v.nA",
        "v.nB",
        "v.aInit[0]",
        "v.aInit[1]",
    ]
    plain = types.parse("E_Plain")
    assert (plain.size, plain.values) == (2, (("A", 0), ("B", 5), ("C", 6)))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "TYPE ST_A :\nSTRUCT\n  x : ST_Nope;\nEND_STRUCT\nEND_TYPE",
            "types, line 3: ST_A.x has unknown type 'ST_Nope'",
        ),
        ("TYPE ST_A : STRUCT x : INT END_STRUCT END_TYPE", "expected ;"),
        ("(* never closed\nTYPE", "line 1: unterminated (*"),
        ("TYPE ST_A : STRUCT p : POINTER TO INT; END_STRUCT END_TYPE", "POINTER TO"),
        ("TYPE U_A : UNION a : INT; END_UNION END_TYPE", "UNION"),
        ("TYPE ST_A : STRUCT a : ARRAY [0..N] OF INT; END_STRUCT END_TYPE", "'N'"),
        ("TYPE ST_A : STRUCT a : ARRAY [5..1] OF INT; END_STRUCT END_TYPE", "reversed"),
        ("TYPE E_A : (X) REAL; END_TYPE", "needs an integer type"),
        ("TYPE E_A : (X := 300) USINT; END_TYPE", "out of range"),
        ("TYPE ST_A : STRUCT a : ST_A; END_STRUCT END_TYPE", "contains itself"),
        (
            "TYPE T_A : INT; END_TYPE\n\nTYPE T_A : INT; END_TYPE",
            "line 3: T_A is declared twice",
        ),
        ("TYPE ST_A : STRUCT END_STRUCT END_TYPE", "has no members"),
        (
            "{attribute 'pack_mode' := '3'}\n"
            "TYPE S : STRUCT a : INT; END_STRUCT END_TYPE",
            "pack_mode",
        ),
    ],
)
def test_declaration_mistakes_are_explained(source, message):
    with pytest.raises(
        PlcTypeError, match=message.replace("(", r"\(").replace("*", r"\*")
    ):
        Types(source)


def test_values_decode_and_encode():
    types = Types(AXIS)
    cases = [
        ("BOOL", b"\x01", True, ["on", "true", 1, True]),
        ("SINT", b"\xff", -1, [-1, "-1", -1.0]),
        ("UINT", b"\x34\x12", 0x1234, ["4660", 4660]),
        ("DINT", struct.pack("<i", -70000), -70000, ["-70000"]),
        ("LREAL", struct.pack("<d", 2.5), 2.5, ["2.5", 2.5]),
        ("STRING(5)", b"ab\0\0\0\0", "ab", ["ab"]),
        ("WSTRING(3)", "Ωx".encode("utf-16-le") + bytes(4), "Ωx", ["Ωx"]),
        (
            "E_Mode",
            struct.pack("<H", 10),
            "eRunning",
            ["eRunning", "E_Mode.eRunning", 10],
        ),
    ]
    for name, data, value, inputs in cases:
        kind = types.parse(name)
        assert kind.decode(data) == value, name
        for given in inputs:
            assert kind.encode(given) == data, (name, given)
    assert types.parse("E_Mode").decode(struct.pack("<H", 7)) == 7  # undeclared


def test_strings_are_windows_1252_or_utf8():
    kind = Types().parse("STRING(8)")
    assert kind.encode("21 °C") == "21 °C".encode("cp1252").ljust(9, b"\0")
    assert kind.decode("21 °C".encode("cp1252") + b"\0") == "21 °C"
    assert kind.decode("21 °C".encode() + b"\0") == "21 °C"
    assert kind.decode(kind.encode("日本")) == "日本"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("INT", 40000, r"out of range \(-32768 to 32767\)"),
        ("USINT", -1, "out of range"),
        ("INT", "1.5", "whole number"),
        ("INT", True, "whole number"),
        ("REAL", "fast", "expected a number"),
        ("REAL", 1e39, "too large"),
        ("BOOL", "maybe", "true/false"),
        ("STRING(3)", "four", "longer than 3 characters"),
        ("E_Mode", "eFlying", "expected one of eIdle, eHoming, eRunning"),
        ("E_Mode", 3, "expected one of"),
    ],
)
def test_values_that_dont_fit_are_refused(name, value, message):
    with pytest.raises(ValueError, match=message):
        Types(AXIS).parse(name).encode(value)


def test_canonical_type_names():
    assert canonical_type_name("string") == canonical_type_name("STRING(80)")
    assert canonical_type_name("STRING[20]") == "STRING(20)"
    assert canonical_type_name("TIME_OF_DAY") == "TOD"
    assert canonical_type_name("ARRAY [0..1] OF INT") == "ARRAY[0..1]OFINT"


def test_patterns():
    assert matches("MAIN.*", "main.nCount")
    assert matches("GVL.a?", "GVL.ab")
    assert matches("MAIN.arr[1]", "MAIN.arr[1]")  # brackets are indexes
    assert not matches("MAIN.arr[12]", "MAIN.arr1")
    assert Variable("MAIN.*").covers("MAIN.stAxis.fPos")
    assert Variable("MAIN.st").covers("main.ST.fPos")
    assert Variable("MAIN.a").covers("MAIN.a[3]")
    assert not Variable("MAIN.a").covers("MAIN.ab")


def test_variables_from_a_list_or_a_mapping():
    assert parse_variables(["MAIN.*", "GVL.x"]) == [
        Variable("MAIN.*"),
        Variable("GVL.x"),
    ]
    assert parse_variables(
        {"MAIN.st": "ST_Axis", "GVL.x": {"write": True}, "GVL.y": {}}
    ) == [
        Variable("MAIN.st", "ST_Axis"),
        Variable("GVL.x", write=True),
        Variable("GVL.y"),
    ]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("MAIN.*", "not text"),
        ([], "no variables"),
        ({"GVL.x": {"writable": True}}, "unknown option 'writable'"),
        ({"MAIN.*": {"write": True}}, "write = true needs an exact name"),
        ({"MAIN.*": "INT"}, "a type needs an exact name"),
        ({"GVL.x": {"write": "yes"}}, "write must be true or false"),
        (["GVL.x", "gvl.X"], "listed twice"),
        (["two words"], "not a PLC variable name"),
    ],
)
def test_variable_mistakes(spec, message):
    with pytest.raises((ValueError, TypeError), match=message):
        parse_variables(spec)


INTERFACE = '''
target = "5.12.34.56.1.1:851"
interval = 0.25

types = """
TYPE ST_Axis :
STRUCT
    fPosition : LREAL;
END_STRUCT
END_TYPE
"""

[variables]
"MAIN.stAxis1" = "ST_Axis"
"GVL.nSetpoint" = { write = true }
"MAIN.*" = {}
'''


def test_interface_file(tmp_path):
    path = tmp_path / "plc.toml"
    path.write_text(INTERFACE)
    settings = read_interface(path)
    assert settings["target"] == "5.12.34.56.1.1:851"
    assert settings["interval"] == 0.25
    assert "TYPE ST_Axis" in settings["types"]
    assert parse_variables(settings["variables"])[1] == Variable(
        "GVL.nSetpoint", write=True
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("interval = 0.25", "intervall = 0.25"), "unknown setting 'intervall'"),
        (('target = "5.12.34.56.1.1:851"', ""), "missing target"),
        (("interval = 0.25", "interval = 'fast'"), "interval has the wrong type"),
        (
            ("fPosition : LREAL;", "fPosition : LREEL;"),
            "plc.toml, line 8: ST_Axis.fPosition has unknown type 'LREEL'",
        ),
        (('"ST_Axis"', '"ST_Axes"'), "plc.toml: MAIN.stAxis1: unknown type 'ST_Axes'"),
        (("{ write = true }", "{ write = 1 }"), "write must be true or false"),
        (("[variables]", "variables"), "plc.toml: "),  # not TOML any more
    ],
)
def test_interface_file_mistakes_name_the_file(tmp_path, change, message):
    path = tmp_path / "plc.toml"
    path.write_text(INTERFACE.replace(*change))
    with pytest.raises(ValueError, match=message):
        read_interface(path)


def test_missing_interface_file(tmp_path):
    with pytest.raises(ValueError, match=r"can't read .*nope\.toml: No such file"):
        read_interface(tmp_path / "nope.toml")

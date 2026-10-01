"""PLC data types and interface files, for sources that read PLC variables.

Struct, enum and alias types are declared in IEC 61131-3 Structured Text, as
written in TwinCAT, so declarations can be pasted from the PLC project::

    {attribute 'pack_mode' := '1'}
    TYPE ST_Axis :
    STRUCT
        fPosition : LREAL;
        bEnabled  : BOOL;
    END_STRUCT
    END_TYPE

:class:`Types` parses them and lays them out in memory as TwinCAT 3 does
(each member aligned to its size, at most 8 bytes, unless a ``pack_mode``
attribute says otherwise), so a variable's bytes can be split into one value
per member (:meth:`PlcType.leaves`). :func:`read_interface` reads an
interface file: the PLC to connect to, the variables to show and the types
they use.
"""

from __future__ import annotations

import fnmatch
import itertools
import math
import re
import struct
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


class PlcTypeError(ValueError):
    """A type declaration or type name that can't be used."""


class UnknownType(PlcTypeError):
    """A type name that is neither elementary nor declared."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown type {name!r}")
        self.name = name


# -- types --------------------------------------------------------------------


@dataclass(frozen=True)
class PlcType:
    """A type's name (as the PLC reports it), size and alignment in bytes."""

    name: str
    size: int
    align: int

    def leaves(self, path: str, offset: int = 0) -> Iterator[Leaf]:
        """One :class:`Leaf` per single value in a variable of this type
        named ``path``: the variable itself, or each member and element."""
        yield Leaf(path, offset, self)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Leaf:
    """A single value inside a variable: its PLC name (``"MAIN.st.a[2]"``),
    offset from the start of the variable, and type."""

    path: str
    offset: int
    type: Value


class Value(PlcType):
    """A type holding one value, which it converts from and to bytes."""

    def decode(self, data: bytes, offset: int = 0) -> Any:
        raise NotImplementedError

    def encode(self, value: Any) -> bytes:
        """``value`` as exactly :attr:`size` bytes; ValueError if it doesn't fit."""
        raise NotImplementedError


@dataclass(frozen=True)
class Elementary(Value):
    format: str  # struct module format character

    def decode(self, data: bytes, offset: int = 0) -> Any:
        return struct.unpack_from("<" + self.format, data, offset)[0]

    def encode(self, value: Any) -> bytes:
        if self.format == "?":
            return struct.pack("<?", _to_bool(value))
        if self.format in "fd":
            number = _to_float(value)
            if self.format == "f" and math.isfinite(number) and abs(number) > _REAL_MAX:
                raise ValueError("too large")
            return struct.pack("<" + self.format, number)
        integer = _to_int(value)
        low, high = _INT_RANGES[self.format]
        if not low <= integer <= high:
            raise ValueError(f"out of range ({low} to {high})")
        return struct.pack("<" + self.format, integer)


@dataclass(frozen=True)
class String(Value):
    length: int  # characters, not counting the terminating NUL
    wide: bool

    def decode(self, data: bytes, offset: int = 0) -> str:
        raw = bytes(data[offset : offset + self.size])
        if self.wide:
            for i in range(0, len(raw) - 1, 2):
                if raw[i : i + 2] == b"\0\0":
                    raw = raw[:i]
                    break
            return raw.decode("utf-16-le", errors="replace")
        return _text(raw.split(b"\0", 1)[0])

    def encode(self, value: Any) -> bytes:
        text = value if isinstance(value, str) else str(value)
        if self.wide:
            data = text.encode("utf-16-le")
            characters = len(data) // 2
        else:
            # TwinCAT STRINGs are Windows-1252 unless declared UTF-8; decode()
            # tries UTF-8 first, so either round-trips.
            try:
                data = text.encode("cp1252")
            except UnicodeEncodeError:
                data = text.encode("utf-8")
            characters = len(data)
        if characters > self.length:
            raise ValueError(f"longer than {self.length} characters")
        return data.ljust(self.size, b"\0")


@dataclass(frozen=True)
class Enum(Value):
    base: Elementary
    values: tuple[tuple[str, int], ...]

    def decode(self, data: bytes, offset: int = 0) -> Any:
        number = self.base.decode(data, offset)
        for name, value in self.values:
            if value == number:
                return name
        return number

    def encode(self, value: Any) -> bytes:
        if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
            wanted = value.strip().rpartition(".")[2].upper()  # also E_Mode.eRun
            for name, number in self.values:
                if name.upper() == wanted:
                    return self.base.encode(number)
        else:
            number = _to_int(value)
            if any(number == declared for _, declared in self.values):
                return self.base.encode(number)
        names = ", ".join(name for name, _ in self.values)
        raise ValueError(f"expected one of {names}")


@dataclass(frozen=True)
class Array(PlcType):
    element: PlcType
    dims: tuple[tuple[int, int], ...]

    def leaves(self, path: str, offset: int = 0) -> Iterator[Leaf]:
        ranges = [range(low, high + 1) for low, high in self.dims]
        for i, index in enumerate(itertools.product(*ranges)):
            name = f"{path}[{','.join(map(str, index))}]"
            yield from self.element.leaves(name, offset + i * self.element.size)


@dataclass(frozen=True)
class Member:
    name: str
    type: PlcType
    offset: int


@dataclass(frozen=True)
class Struct(PlcType):
    members: tuple[Member, ...]

    def leaves(self, path: str, offset: int = 0) -> Iterator[Leaf]:
        for member in self.members:
            name = f"{path}.{member.name}"
            yield from member.type.leaves(name, offset + member.offset)


def _elementary(name: str, format: str) -> Elementary:
    size = struct.calcsize("<" + format)
    return Elementary(name, size, size, format)


ELEMENTARY: dict[str, Elementary] = {
    name: _elementary(name, format)
    for names, format in [
        ("BOOL", "?"),
        ("BYTE USINT", "B"),
        ("SINT", "b"),
        ("WORD UINT", "H"),
        ("INT", "h"),
        ("DWORD UDINT TIME TOD DATE DT", "I"),
        ("DINT", "i"),
        ("LWORD ULINT LTIME LTOD LDATE LDT", "Q"),
        ("LINT", "q"),
        ("REAL", "f"),
        ("LREAL", "d"),
    ]
    for name in names.split()
}
_SYNONYMS = {
    "TIME_OF_DAY": "TOD",
    "DATE_AND_TIME": "DT",
    "LTIME_OF_DAY": "LTOD",
    "LDATE_AND_TIME": "LDT",
}
ELEMENTARY.update({long: ELEMENTARY[short] for long, short in _SYNONYMS.items()})

_INT_RANGES = {
    code: (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    if code.islower()
    else (0, 2**bits - 1)
    for code, bits in zip("bBhHiIqQ", (8, 8, 16, 16, 32, 32, 64, 64), strict=True)
}
_REAL_MAX = struct.unpack("<f", b"\xff\xff\x7f\x7f")[0]
_DEFAULT_STRING = 80
_DEFAULT_PACK = 8  # TwinCAT 3


def canonical_type_name(name: str) -> str:
    """``name`` in one spelling, for comparing type names: ``"string"`` and
    ``"STRING(80)"``, or ``"TIME_OF_DAY"`` and ``"TOD"``, are the same."""
    text = re.sub(r"\s+", "", name).upper()
    text = re.sub(r"^(W?STRING)\[(\d+)\]$", r"\1(\2)", text)
    if text in ("STRING", "WSTRING"):
        text += f"({_DEFAULT_STRING})"
    return _SYNONYMS.get(text, text)


# -- declarations -------------------------------------------------------------


class Types:
    """Types declared in Structured Text (``TYPE ... END_TYPE`` blocks: structs,
    enums and aliases), plus the elementary types.

    Every declaration is checked when the object is created, so mistakes are
    reported straight away, with ``where`` and a line number (counted from
    ``first_line``, for declarations inside a bigger file).
    """

    def __init__(
        self, source: str = "", *, where: str = "types", first_line: int = 1
    ) -> None:
        self.where = where
        self._declared: dict[str, _Declaration] = {}
        for declaration in _Parser(source, where, first_line).declarations():
            if declaration.name.upper() in self._declared:
                raise PlcTypeError(
                    f"{where}, line {declaration.line}: {declaration.name} is "
                    "declared twice"
                )
            self._declared[declaration.name.upper()] = declaration
        self._resolved: dict[str, PlcType] = {}
        for key in self._declared:
            self._named(self._declared[key].name, ())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.upper() in self._declared

    def __len__(self) -> int:
        return len(self._declared)

    def parse(self, text: str) -> PlcType:
        """The type written as ``text``: a name (``"LREAL"``, ``"ST_Axis"``),
        ``"STRING(20)"`` or ``"ARRAY [1..4] OF ST_Axis"``."""
        parser = _Parser(text, f"type {text!r}")
        reference = parser.type_reference()
        parser.end()
        return self._resolve(reference, ())

    def _resolve(self, reference: _Reference, stack: tuple[str, ...]) -> PlcType:
        if isinstance(reference, _StringReference):
            unit = 2 if reference.wide else 1
            prefix = "WSTRING" if reference.wide else "STRING"
            return String(
                f"{prefix}({reference.length})",
                (reference.length + 1) * unit,
                unit,
                reference.length,
                reference.wide,
            )
        if isinstance(reference, _ArrayReference):
            element = self._resolve(reference.element, stack)
            count = math.prod(high - low + 1 for low, high in reference.dims)
            bounds = ",".join(f"{low}..{high}" for low, high in reference.dims)
            return Array(
                f"ARRAY [{bounds}] OF {element.name}",
                count * element.size,
                element.align,
                element,
                reference.dims,
            )
        return self._named(reference.name, stack)

    def _named(self, name: str, stack: tuple[str, ...]) -> PlcType:
        key = name.upper()
        if key in ELEMENTARY:
            return ELEMENTARY[key]
        if key in self._resolved:
            return self._resolved[key]
        declaration = self._declared.get(key)
        if declaration is None and "." in key:  # Library.ST_Type
            return self._named(name.rpartition(".")[2], stack)
        if declaration is None:
            raise UnknownType(name)
        if key in stack:
            raise PlcTypeError(f"{self.where}: {declaration.name} contains itself")
        try:
            resolved = self._build(declaration, (*stack, key))
        except UnknownType as error:
            raise PlcTypeError(
                f"{self.where}, line {declaration.line}: {declaration.name} uses "
                f"unknown type {error.name!r}"
            ) from None
        self._resolved[key] = resolved
        return resolved

    def _build(self, declaration: _Declaration, stack: tuple[str, ...]) -> PlcType:
        where = f"{self.where}, line {declaration.line}"
        if isinstance(declaration, _AliasDeclaration):
            target = self._resolve(declaration.type, stack)
            return replace(target, name=declaration.name)
        if isinstance(declaration, _EnumDeclaration):
            base = (
                ELEMENTARY["INT"]
                if declaration.base is None
                else self._resolve(declaration.base, stack)
            )
            if not isinstance(base, Elementary) or base.format in "?fd":
                raise PlcTypeError(f"{where}: {declaration.name} needs an integer type")
            for name, value in declaration.values:
                try:
                    base.encode(value)
                except ValueError as error:
                    raise PlcTypeError(f"{where}: {name}: {error}") from None
            values = declaration.values
            return Enum(declaration.name, base.size, base.align, base, values)
        assert isinstance(declaration, _StructDeclaration)
        pack = declaration.pack
        members: list[Member] = []
        offset, align = 0, 1
        if declaration.base is not None:
            base = self._named(declaration.base, stack)
            if not isinstance(base, Struct):
                raise PlcTypeError(f"{where}: {declaration.base} isn't a struct")
            members, offset, align = list(base.members), base.size, base.align
        for name, reference, line in declaration.members:
            try:
                member_type = self._resolve(reference, stack)
            except UnknownType as error:
                raise PlcTypeError(
                    f"{self.where}, line {line}: {declaration.name}.{name} has "
                    f"unknown type {error.name!r}"
                ) from None
            member_align = min(member_type.align, pack)
            offset = _round_up(offset, member_align)
            members.append(Member(name, member_type, offset))
            offset += member_type.size
            align = max(align, member_align)
        if not members:
            raise PlcTypeError(f"{where}: {declaration.name} has no members")
        return Struct(declaration.name, _round_up(offset, align), align, tuple(members))


def _round_up(offset: int, align: int) -> int:
    return -(-offset // align) * align


@dataclass(frozen=True)
class _NameReference:
    name: str


@dataclass(frozen=True)
class _StringReference:
    length: int
    wide: bool


@dataclass(frozen=True)
class _ArrayReference:
    dims: tuple[tuple[int, int], ...]
    element: _Reference


_Reference = _NameReference | _StringReference | _ArrayReference


@dataclass(frozen=True)
class _StructDeclaration:
    name: str
    line: int
    base: str | None
    members: tuple[tuple[str, _Reference, int], ...]  # name, type, line
    pack: int


@dataclass(frozen=True)
class _EnumDeclaration:
    name: str
    line: int
    values: tuple[tuple[str, int], ...]
    base: _Reference | None


@dataclass(frozen=True)
class _AliasDeclaration:
    name: str
    line: int
    type: _Reference


_Declaration = _StructDeclaration | _EnumDeclaration | _AliasDeclaration

_TOKENS = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<comment>//[^\n]*|\(\*.*?\*\)|/\*.*?\*/)
    | (?P<pragma>\{[^}]*\})
    | (?P<string>'(?:\$.|[^'$\n])*'|"(?:\$.|[^"$\n])*")
    | (?P<unterminated>\(\*|/\*|\{|'|")
    | (?P<number>\d[\d_]*(?:\#[0-9A-Fa-f_]+)?)
    | (?P<name>[A-Za-z_]\w*)
    | (?P<symbol>\.\.|:=|\S)
    """,
    re.VERBOSE | re.DOTALL,
)
_PACK_MODE = re.compile(r"attribute\s+'pack_mode'\s*:=\s*'(\d+)'", re.IGNORECASE)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    line: int


class _Parser:
    """Recursive descent over the declarations TwinCAT writes for DUTs."""

    def __init__(self, text: str, where: str, first_line: int = 1) -> None:
        self.where = where
        self.tokens: list[_Token] = []
        line = first_line
        for match in _TOKENS.finditer(text):
            kind, value = match.lastgroup or "", match.group()
            if kind == "unterminated":
                raise PlcTypeError(f"{where}, line {line}: unterminated {value}")
            if kind not in ("space", "comment"):
                self.tokens.append(_Token(kind, value, line))
            line += value.count("\n")
        self.position = 0

    # -- helpers --

    def peek(self) -> _Token | None:
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise self.error("unexpected end")
        self.position += 1
        return token

    def at(self, word: str) -> bool:
        token = self.peek()
        return token is not None and token.text.upper() == word

    def accept(self, word: str) -> bool:
        if self.at(word):
            self.position += 1
            return True
        return False

    def expect(self, word: str) -> None:
        if not self.accept(word):
            token = self.peek()
            found = f"{token.text!r}" if token else "the end"
            raise self.error(f"expected {word} but found {found}")

    def name(self) -> str:
        token = self.take()
        if token.kind != "name":
            raise self.error(f"expected a name but found {token.text!r}", token)
        return token.text

    def integer(self) -> int:
        negative = self.accept("-")
        token = self.take()
        if token.kind != "number":
            raise self.error(f"expected a number but found {token.text!r}", token)
        base, _, digits = token.text.replace("_", "").rpartition("#")
        value = int(digits, int(base) if base else 10)
        return -value if negative else value

    def pragmas(self) -> list[str]:
        found = []
        while (token := self.peek()) is not None and token.kind == "pragma":
            found.append(self.take().text)
        return found

    def end(self) -> None:
        if (token := self.peek()) is not None:
            raise self.error(f"unexpected {token.text!r}", token)

    def error(self, message: str, token: _Token | None = None) -> PlcTypeError:
        token = token or self.peek() or (self.tokens[-1] if self.tokens else None)
        line = f", line {token.line}" if token else ""
        return PlcTypeError(f"{self.where}{line}: {message}")

    def skip_initial_value(self) -> None:
        """Skip ``:= ...`` up to the ``;`` that ends it."""
        if not self.accept(":="):
            return
        depth = 0
        while (token := self.peek()) is not None:
            if token.text in "([":
                depth += 1
            elif token.text in ")]":
                depth -= 1
            elif depth == 0 and (token.text == ";" or token.text.upper() == "END_TYPE"):
                return
            self.position += 1

    # -- grammar --

    def declarations(self) -> list[_Declaration]:
        found: list[_Declaration] = []
        while self.peek() is not None:
            pragmas = self.pragmas()
            if self.peek() is None:
                break
            self.expect("TYPE")
            while not self.accept("END_TYPE"):
                pragmas += self.pragmas()
                found.append(self.declaration(pragmas))
                pragmas = []
        return found

    def declaration(self, pragmas: list[str]) -> _Declaration:
        line = self.peek().line if self.peek() else 0
        name = self.name()
        base = self.name() if self.accept("EXTENDS") else None
        if base is not None and self.at(","):
            raise self.error("a struct can extend only one struct")
        self.expect(":")
        pragmas += self.pragmas()
        if self.accept("STRUCT"):
            members = self.members()
            self.accept(";")
            pack = _DEFAULT_PACK
            for pragma in pragmas:
                if match := _PACK_MODE.search(pragma):
                    pack = max(int(match.group(1)), 1)
            if pack not in (1, 2, 4, 8):
                raise self.error(f"pack_mode must be 0, 1, 2, 4 or 8, not {pack}")
            return _StructDeclaration(name, line, base, members, pack)
        if base is not None:
            raise self.error("only structs can use EXTENDS")
        if self.at("UNION"):
            raise self.error("UNION types aren't supported")
        if self.accept("("):
            return self.enum(name, line)
        reference = self.type_reference()
        self.skip_initial_value()
        self.accept(";")
        return _AliasDeclaration(name, line, reference)

    def members(self) -> tuple[tuple[str, _Reference, int], ...]:
        members: list[tuple[str, _Reference, int]] = []
        while not self.accept("END_STRUCT"):
            self.pragmas()
            if self.accept("END_STRUCT"):
                break
            line = self.peek().line if self.peek() else 0
            names = [self.name()]
            while self.accept(","):
                names.append(self.name())
            if self.accept("AT"):  # AT %I*: the address doesn't change the layout
                while not self.at(":"):
                    self.take()
            self.expect(":")
            reference = self.type_reference()
            self.skip_initial_value()
            self.expect(";")
            members.extend((name, reference, line) for name in names)
        return tuple(members)

    def enum(self, name: str, line: int) -> _EnumDeclaration:
        values: list[tuple[str, int]] = []
        following = 0
        while True:
            self.pragmas()
            member = self.name()
            value = self.integer() if self.accept(":=") else following
            values.append((member, value))
            following = value + 1
            if not self.accept(","):
                break
        self.expect(")")
        base = None
        token = self.peek()
        if token is not None and token.kind == "name" and not self.at("END_TYPE"):
            base = self.type_reference()
        self.skip_initial_value()
        self.accept(";")
        return _EnumDeclaration(name, line, tuple(values), base)

    def type_reference(self) -> _Reference:
        token = self.take()
        if token.kind != "name":
            raise self.error(f"expected a type but found {token.text!r}", token)
        word = token.text.upper()
        if word in ("STRING", "WSTRING"):
            length = _DEFAULT_STRING
            for opening, closing in (("(", ")"), ("[", "]")):
                if self.accept(opening):
                    length = self.integer()
                    self.expect(closing)
                    break
            if length < 1:
                raise self.error(f"{word} needs at least one character", token)
            return _StringReference(length, word == "WSTRING")
        if word == "ARRAY":
            self.expect("[")
            dims = [self.bounds()]
            while self.accept(","):
                dims.append(self.bounds())
            self.expect("]")
            self.expect("OF")
            return _ArrayReference(tuple(dims), self.type_reference())
        if word in ("POINTER", "REFERENCE"):
            raise self.error(
                f"{word} TO isn't supported: its size depends on the PLC", token
            )
        if word in ("BIT", "__XWORD", "XWORD", "__UXINT", "__XINT", "PVOID"):
            raise self.error(f"{token.text} isn't supported", token)
        name = token.text
        while self.at(".") and self.position + 1 < len(self.tokens):
            self.take()
            name += "." + self.name()
        return _NameReference(name)

    def bounds(self) -> tuple[int, int]:
        token = self.peek()
        if token is not None and token.kind == "name":
            raise self.error(f"array bounds must be numbers, not {token.text!r}")
        low = self.integer()
        self.expect("..")
        high = self.integer()
        if high < low:
            raise self.error(f"array bounds {low}..{high} are reversed")
        return low, high


# -- values -------------------------------------------------------------------

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ValueError("expected true/false, on/off or 1/0")


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected a whole number")
    if isinstance(value, int):
        return value
    number = _to_float(value)
    if not number.is_integer():
        raise ValueError("expected a whole number")
    return int(number)


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("expected a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("expected a number") from None


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# -- interface files ----------------------------------------------------------


@dataclass(frozen=True)
class Variable:
    """A PLC variable (or ``*`` pattern) to read, with its type if given and
    whether values sent to it may be written to the PLC."""

    name: str
    type: str | None = None
    write: bool = False

    @property
    def is_pattern(self) -> bool:
        return "*" in self.name or "?" in self.name

    def covers(self, id: str) -> bool:
        """Whether ``id`` is this variable, one of its members or elements, or
        (for a pattern) matches it."""
        if self.is_pattern:
            return matches(self.name, id)
        name, key = self.name.upper(), id.upper()
        return key == name or key.startswith((name + ".", name + "["))


def matches(pattern: str, name: str) -> bool:
    """Whether a PLC name matches a pattern: ``*`` matches any characters and
    ``?`` one; ``[`` is literal (array indexes); case doesn't matter."""
    return fnmatch.fnmatchcase(name.upper(), pattern.upper().replace("[", "[[]"))


VariableSpec = Iterable[str] | Mapping[str, "str | Mapping[str, Any]"]


def parse_variables(spec: VariableSpec) -> list[Variable]:
    """The variables described by a list of names and patterns, or by a
    mapping of name to its type (``"ST_Axis"``) or options
    (``{"type": "ST_Axis", "write": True}``)."""
    if isinstance(spec, str):
        raise TypeError("variables must be a list of names or a mapping, not text")
    items: Iterable[tuple[str, Any]] = (
        spec.items() if isinstance(spec, Mapping) else ((name, {}) for name in spec)
    )
    variables: list[Variable] = []
    seen: set[str] = set()
    for name, given in items:
        if not isinstance(name, str) or not name.strip() or re.search(r"\s", name):
            raise ValueError(f"not a PLC variable name: {name!r}")
        options = {"type": given} if isinstance(given, str) else given
        if not isinstance(options, Mapping):
            raise ValueError(f"{name}: expected a type name or options, not {given!r}")
        if unknown := sorted(set(options) - {"type", "write"}):
            raise ValueError(
                f"{name}: unknown option {', '.join(map(repr, unknown))} "
                "(expected type, write)"
            )
        type_name, write = options.get("type"), options.get("write", False)
        if type_name is not None and not isinstance(type_name, str):
            raise ValueError(f'{name}: type must be text, like "ST_Axis"')
        if not isinstance(write, bool):
            raise ValueError(f"{name}: write must be true or false")
        variable = Variable(name, type_name, write)
        if variable.is_pattern and (write or type_name):
            option = "write = true" if write else "a type"
            raise ValueError(f"{name}: {option} needs an exact name, not a pattern")
        if name.upper() in seen:
            raise ValueError(f"{name} is listed twice")
        seen.add(name.upper())
        variables.append(variable)
    if not variables:
        raise ValueError("no variables to read")
    return variables


INTERFACE_SETTINGS = ("target", "ip", "interval", "types", "variables")


def declared_types(variables: Iterable[Variable], types: Types) -> dict[str, PlcType]:
    """The type of each variable that names one (keyed by upper-case name),
    raising PlcTypeError for a type that doesn't exist."""
    found = {}
    for variable in variables:
        if variable.type is not None:
            try:
                found[variable.name.upper()] = types.parse(variable.type)
            except PlcTypeError as error:
                raise PlcTypeError(f"{variable.name}: {error}") from None
    return found


def _line_of_types(text: str) -> int:
    """The line of the file where the ``types`` string starts, so mistakes in
    it are reported at the file's line numbers."""
    match = re.search(r'^[ \t]*types[ \t]*=[ \t]*("""|\'\'\'|"|\')(\r?\n)?', text, re.M)
    if match is None:
        return 1
    line = text.count("\n", 0, match.start()) + 1
    return line + 1 if match.group(2) else line  # TOML drops that first newline


def read_interface(path: str | Path) -> dict[str, Any]:
    """Read an interface file (TOML) into keyword arguments for a PLC source:
    ``target``, ``variables`` and, if given, ``types``, ``ip_address`` and
    ``interval``. Raises ValueError, naming the file, for anything wrong."""
    path = Path(path)
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except OSError as error:
        raise ValueError(f"can't read {path}: {error.strerror or error}") from None
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{path}: {error}") from None
    if unknown := sorted(set(data) - set(INTERFACE_SETTINGS)):
        raise ValueError(
            f"{path}: unknown setting {', '.join(map(repr, unknown))} "
            f"(expected {', '.join(INTERFACE_SETTINGS)})"
        )
    for key in ("target", "variables"):
        if key not in data:
            raise ValueError(f"{path}: missing {key}")
    settings: dict[str, Any] = {}
    for key, kind, argument in (
        ("target", str, "target"),
        ("ip", str, "ip_address"),
        ("types", str, "types"),
        ("interval", (int, float), "interval"),
    ):
        if key in data:
            if not isinstance(data[key], kind) or isinstance(data[key], bool):
                raise ValueError(f"{path}: {key} has the wrong type")
            settings[argument] = data[key]
    if not isinstance(data["variables"], Mapping):
        raise ValueError(f"{path}: variables must be a table ([variables])")
    try:
        types = Types(
            settings.get("types", ""),
            where=str(path),
            first_line=_line_of_types(path.read_text("utf-8")),
        )
        declared_types(parse_variables(data["variables"]), types)
    except (ValueError, TypeError) as error:
        message = str(error)
        if not message.startswith(str(path)):
            message = f"{path}: {message}"
        raise ValueError(message) from None
    settings["variables"] = data["variables"]
    return settings

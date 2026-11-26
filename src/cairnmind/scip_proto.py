"""Pure-Python SCIP (Sourcegraph Code Intelligence Protocol) Protobuf decoder & symbol parser.

Zero external dependencies: implements wire-level Protobuf v3 decoding for the official
``scip.proto`` schema (Index, Metadata, ToolInfo, Document, Occurrence, SingleLineRange,
MultiLineRange, SymbolInformation, Relationship) with strict fail-closed size and path bounds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import struct
from typing import Any

# Fail-closed bounds against malformed or adversarial index files.
MAX_SCIP_BYTES = 256 * 1024 * 1024  # 256 MiB
MAX_SCIP_DOCUMENTS = 100_000
MAX_SCIP_OCCURRENCES = 2_000_000
MAX_SCIP_SYMBOLS = 1_000_000

# SymbolRole bitmasks from scip.proto.
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE_ACCESS = 0x4
ROLE_READ_ACCESS = 0x8
ROLE_GENERATED = 0x10
ROLE_TEST = 0x20
ROLE_FORWARD_DEFINITION = 0x40

# SymbolInformation.Kind values from scip.proto mapped to Cairnmind's symbol kinds.
_SCIP_KIND_TO_CAIRNMIND: dict[int, str] = {
    7: "class",       # Class
    8: "constant",    # Constant
    9: "method",      # Constructor
    11: "enum",       # Enum
    12: "constant",   # EnumMember
    13: "field",      # Event
    15: "field",      # Field
    17: "function",   # Function
    18: "method",     # Getter
    21: "interface",  # Interface
    25: "function",   # Macro
    26: "method",     # Method
    29: "type",       # Module
    30: "type",       # Namespace
    33: "class",      # Object
    34: "method",     # Operator
    37: "parameter",  # Parameter
    41: "field",      # Property
    42: "interface",  # Protocol
    45: "method",     # Setter
    49: "class",      # Struct
    53: "interface",  # Trait
    54: "type",       # Type
    55: "type",       # TypeAlias
    56: "interface",  # TypeClass
    59: "type",       # Union
    60: "constant",   # Value
    61: "constant",   # Variable
    62: "interface",  # Contract
    66: "method",     # AbstractMethod
    67: "method",     # MethodSpecification
    68: "method",     # ProtocolMethod
    69: "method",     # PureVirtualMethod
    70: "method",     # TraitMethod
    71: "method",     # TypeClassMethod
    72: "method",     # Accessor
    73: "type",       # Delegate
    74: "method",     # MethodAlias
    75: "class",      # SingletonClass
    76: "method",     # SingletonMethod
    77: "field",      # StaticDataMember
    78: "field",      # StaticEvent
    79: "field",      # StaticField
    80: "method",     # StaticMethod
    81: "field",      # StaticProperty
    82: "constant",   # StaticVariable
    85: "interface",  # Mixin
    86: "interface",  # Concept
}

CALLABLE_KINDS = frozenset({"function", "method"})


class ScipFormatError(ValueError):
    """Raised when a binary SCIP file is truncated, malformed, or exceeds safety bounds."""


@dataclass(slots=True)
class ScipToolInfo:
    name: str = ""
    version: str = ""
    arguments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScipMetadata:
    version: int = 0
    tool_info: ScipToolInfo = field(default_factory=ScipToolInfo)
    project_root: str = ""
    text_document_encoding: int = 0


@dataclass(slots=True)
class ScipRelationship:
    symbol: str = ""
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False


@dataclass(slots=True)
class ScipSymbolInfo:
    symbol: str = ""
    documentation: list[str] = field(default_factory=list)
    relationships: list[ScipRelationship] = field(default_factory=list)
    kind: int = 0
    display_name: str = ""
    enclosing_symbol: str = ""


@dataclass(slots=True)
class ScipOccurrence:
    """One occurrence in a SCIP document (0-based lines and columns)."""
    start_line: int = 0
    start_col: int = 0
    end_line: int = 0
    end_col: int = 0
    symbol: str = ""
    symbol_roles: int = 0
    enclosing_start_line: int | None = None
    enclosing_end_line: int | None = None


@dataclass(slots=True)
class ScipDocument:
    relative_path: str = ""
    language: str = ""
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: list[ScipSymbolInfo] = field(default_factory=list)
    position_encoding: int = 0


@dataclass(slots=True)
class ScipIndex:
    metadata: ScipMetadata = field(default_factory=ScipMetadata)
    documents: list[ScipDocument] = field(default_factory=list)
    external_symbols: list[ScipSymbolInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Wire-level Protobuf v3 reader
# ---------------------------------------------------------------------------

def _read_varint(buf: memoryview, pos: int, end: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < end:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ScipFormatError("Protobuf varint exceeds 64 bits")
    raise ScipFormatError("Truncated protobuf varint")


def _to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def _skip_field(buf: memoryview, pos: int, end: int, wire_type: int) -> int:
    if wire_type == 0:
        _, pos = _read_varint(buf, pos, end)
        return pos
    if wire_type == 1:
        if pos + 8 > end:
            raise ScipFormatError("Truncated 64-bit protobuf field")
        return pos + 8
    if wire_type == 2:
        length, pos = _read_varint(buf, pos, end)
        if pos + length > end:
            raise ScipFormatError("Truncated length-delimited protobuf field")
        return pos + length
    if wire_type == 5:
        if pos + 4 > end:
            raise ScipFormatError("Truncated 32-bit protobuf field")
        return pos + 4
    raise ScipFormatError(f"Unsupported protobuf wire type {wire_type}")


def _read_bytes(buf: memoryview, pos: int, end: int) -> tuple[memoryview, int]:
    length, pos = _read_varint(buf, pos, end)
    if length < 0 or pos + length > end:
        raise ScipFormatError("Truncated length-delimited protobuf field")
    return buf[pos:pos + length], pos + length


def _read_str(buf: memoryview, pos: int, end: int) -> tuple[str, int]:
    raw, pos = _read_bytes(buf, pos, end)
    return bytes(raw).decode("utf-8", errors="replace"), pos


def _read_packed_int32(buf: memoryview, pos: int, end: int) -> tuple[list[int], int]:
    raw, pos = _read_bytes(buf, pos, end)
    items: list[int] = []
    sub_pos, sub_end = 0, len(raw)
    while sub_pos < sub_end:
        val, sub_pos = _read_varint(raw, sub_pos, sub_end)
        items.append(_to_int32(val))
    return items, pos


def _decode_single_line_range(buf: memoryview) -> tuple[int, int, int, int]:
    line = start_col = end_col = 0
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            line = _to_int32(val)
        elif field_num == 2 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            start_col = _to_int32(val)
        elif field_num == 3 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            end_col = _to_int32(val)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return line, start_col, line, end_col


def _decode_multi_line_range(buf: memoryview) -> tuple[int, int, int, int]:
    start_line = start_col = end_line = end_col = 0
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            start_line = _to_int32(val)
        elif field_num == 2 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            start_col = _to_int32(val)
        elif field_num == 3 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            end_line = _to_int32(val)
        elif field_num == 4 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            end_col = _to_int32(val)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return start_line, start_col, end_line, end_col


def _unpack_range_array(values: list[int]) -> tuple[int, int, int, int] | None:
    if len(values) == 3:
        return values[0], values[1], values[0], values[2]
    if len(values) == 4:
        return values[0], values[1], values[2], values[3]
    return None


def _decode_tool_info(buf: memoryview) -> ScipToolInfo:
    info = ScipToolInfo()
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 2:
            info.name, pos = _read_str(buf, pos, end)
        elif field_num == 2 and wire == 2:
            info.version, pos = _read_str(buf, pos, end)
        elif field_num == 3 and wire == 2:
            arg, pos = _read_str(buf, pos, end)
            info.arguments.append(arg)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return info


def _decode_metadata(buf: memoryview) -> ScipMetadata:
    meta = ScipMetadata()
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            meta.version = _to_int32(val)
        elif field_num == 2 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            meta.tool_info = _decode_tool_info(sub)
        elif field_num == 3 and wire == 2:
            meta.project_root, pos = _read_str(buf, pos, end)
        elif field_num == 4 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            meta.text_document_encoding = _to_int32(val)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return meta


def _decode_relationship(buf: memoryview) -> ScipRelationship:
    rel = ScipRelationship()
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 2:
            rel.symbol, pos = _read_str(buf, pos, end)
        elif field_num == 2 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            rel.is_reference = bool(val)
        elif field_num == 3 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            rel.is_implementation = bool(val)
        elif field_num == 4 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            rel.is_type_definition = bool(val)
        elif field_num == 5 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            rel.is_definition = bool(val)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return rel


def _decode_symbol_info(buf: memoryview) -> ScipSymbolInfo:
    info = ScipSymbolInfo()
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 2:
            info.symbol, pos = _read_str(buf, pos, end)
        elif field_num == 3 and wire == 2:
            doc, pos = _read_str(buf, pos, end)
            info.documentation.append(doc)
        elif field_num == 4 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            info.relationships.append(_decode_relationship(sub))
        elif field_num == 5 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            info.kind = _to_int32(val)
        elif field_num == 6 and wire == 2:
            info.display_name, pos = _read_str(buf, pos, end)
        elif field_num == 8 and wire == 2:
            info.enclosing_symbol, pos = _read_str(buf, pos, end)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return info


def _decode_occurrence(buf: memoryview) -> ScipOccurrence:
    occ = ScipOccurrence()
    raw_range: list[int] = []
    raw_enclosing: list[int] = []
    typed_range: tuple[int, int, int, int] | None = None
    typed_enclosing: tuple[int, int, int, int] | None = None
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 2:
            vals, pos = _read_packed_int32(buf, pos, end)
            raw_range.extend(vals)
        elif field_num == 1 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            raw_range.append(_to_int32(val))
        elif field_num == 2 and wire == 2:
            occ.symbol, pos = _read_str(buf, pos, end)
        elif field_num == 3 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            occ.symbol_roles = _to_int32(val)
        elif field_num == 7 and wire == 2:
            vals, pos = _read_packed_int32(buf, pos, end)
            raw_enclosing.extend(vals)
        elif field_num == 7 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            raw_enclosing.append(_to_int32(val))
        elif field_num == 8 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            typed_range = _decode_single_line_range(sub)
        elif field_num == 9 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            typed_range = _decode_multi_line_range(sub)
        elif field_num == 10 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            typed_enclosing = _decode_single_line_range(sub)
        elif field_num == 11 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            typed_enclosing = _decode_multi_line_range(sub)
        else:
            pos = _skip_field(buf, pos, end, wire)
    span = typed_range or _unpack_range_array(raw_range)
    if span is not None:
        occ.start_line, occ.start_col, occ.end_line, occ.end_col = span
    enc = typed_enclosing or _unpack_range_array(raw_enclosing)
    if enc is not None:
        occ.enclosing_start_line, occ.enclosing_end_line = enc[0], enc[2]
    return occ


def _validate_relative_path(raw: str) -> str:
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ScipFormatError(f"Invalid document path in SCIP index: {raw!r}")
    normalized = raw.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ScipFormatError(f"SCIP document path must be relative: {raw!r}")
    if ".." in posix.parts:
        raise ScipFormatError(f"SCIP document path escapes project root: {raw!r}")
    return posix.as_posix()


def _decode_document(buf: memoryview, counters: dict[str, int]) -> ScipDocument:
    doc = ScipDocument()
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 2:
            raw_path, pos = _read_str(buf, pos, end)
            doc.relative_path = _validate_relative_path(raw_path)
        elif field_num == 2 and wire == 2:
            counters["occurrences"] += 1
            if counters["occurrences"] > MAX_SCIP_OCCURRENCES:
                raise ScipFormatError(f"SCIP index exceeds {MAX_SCIP_OCCURRENCES} occurrences")
            sub, pos = _read_bytes(buf, pos, end)
            doc.occurrences.append(_decode_occurrence(sub))
        elif field_num == 3 and wire == 2:
            counters["symbols"] += 1
            if counters["symbols"] > MAX_SCIP_SYMBOLS:
                raise ScipFormatError(f"SCIP index exceeds {MAX_SCIP_SYMBOLS} symbols")
            sub, pos = _read_bytes(buf, pos, end)
            doc.symbols.append(_decode_symbol_info(sub))
        elif field_num == 4 and wire == 2:
            doc.language, pos = _read_str(buf, pos, end)
        elif field_num == 6 and wire == 0:
            val, pos = _read_varint(buf, pos, end)
            doc.position_encoding = _to_int32(val)
        else:
            pos = _skip_field(buf, pos, end, wire)
    return doc


def parse_scip_bytes(data: bytes | bytearray | memoryview, *, max_bytes: int = MAX_SCIP_BYTES) -> ScipIndex:
    """Decode a binary SCIP protobuf payload into a ScipIndex."""
    if len(data) > max_bytes:
        raise ScipFormatError(f"SCIP index exceeds {max_bytes} bytes")
    buf = memoryview(data)
    index = ScipIndex()
    counters = {"documents": 0, "occurrences": 0, "symbols": 0}
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num, wire = tag >> 3, tag & 0x7
        if field_num == 1 and wire == 2:
            sub, pos = _read_bytes(buf, pos, end)
            index.metadata = _decode_metadata(sub)
        elif field_num == 2 and wire == 2:
            counters["documents"] += 1
            if counters["documents"] > MAX_SCIP_DOCUMENTS:
                raise ScipFormatError(f"SCIP index exceeds {MAX_SCIP_DOCUMENTS} documents")
            sub, pos = _read_bytes(buf, pos, end)
            index.documents.append(_decode_document(sub, counters))
        elif field_num == 3 and wire == 2:
            counters["symbols"] += 1
            if counters["symbols"] > MAX_SCIP_SYMBOLS:
                raise ScipFormatError(f"SCIP index exceeds {MAX_SCIP_SYMBOLS} symbols")
            sub, pos = _read_bytes(buf, pos, end)
            index.external_symbols.append(_decode_symbol_info(sub))
        else:
            pos = _skip_field(buf, pos, end, wire)
    return index


def parse_scip_file(path: Path | str) -> ScipIndex:
    """Safely open (no symlink follow) and decode an ``index.scip`` file."""
    import os
    from .private_files import open_no_follow
    target = Path(path)
    fd = open_no_follow(target, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        if size > MAX_SCIP_BYTES:
            raise ScipFormatError(f"SCIP index exceeds {MAX_SCIP_BYTES} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SCIP_BYTES:
                raise ScipFormatError(f"SCIP index exceeds {MAX_SCIP_BYTES} bytes")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return parse_scip_bytes(b"".join(chunks))


# ---------------------------------------------------------------------------
# SCIP symbol grammar parser
# ---------------------------------------------------------------------------

def is_local_symbol(symbol: str) -> bool:
    return not symbol or symbol.startswith("local ")


def _split_space_escaped(text: str, max_parts: int) -> tuple[list[str], str]:
    """Split space-separated SCIP header fields where '  ' encodes a literal space."""
    parts: list[str] = []
    current: list[str] = []
    idx = 0
    length = len(text)
    while idx < length and len(parts) < max_parts:
        ch = text[idx]
        if ch == " ":
            if idx + 1 < length and text[idx + 1] == " ":
                current.append(" ")
                idx += 2
                continue
            parts.append("".join(current))
            current = []
            idx += 1
        else:
            current.append(ch)
            idx += 1
    if len(parts) < max_parts:
        parts.append("".join(current))
        return parts, ""
    return parts, text[idx:]


def _parse_descriptors(desc: str) -> list[tuple[str, str]]:
    """Parse SCIP descriptor chain into [(name, suffix_token), ...].

    Suffix tokens: '/' (namespace/package), '#' (type/class), '.' (term/field),
    '().' (method/function), '!' (macro), ':' (meta), '[]' (type param), '()' (param).
    """
    result: list[tuple[str, str]] = []
    idx = 0
    length = len(desc)
    while idx < length:
        if desc[idx] == "(":
            # Parameter descriptor: (name)
            end = desc.find(")", idx + 1)
            if end < 0:
                break
            result.append((desc[idx + 1:end].strip("`"), "()"))
            idx = end + 1
            continue
        if desc[idx] == "[":
            # Type parameter descriptor: [T]
            end = desc.find("]", idx + 1)
            if end < 0:
                break
            result.append((desc[idx + 1:end].strip("`"), "[]"))
            idx = end + 1
            continue
        # Identifier: either backtick-escaped or plain chars up to suffix delimiter.
        if desc[idx] == "`":
            idx += 1
            chars: list[str] = []
            while idx < length:
                if desc[idx] == "`":
                    if idx + 1 < length and desc[idx + 1] == "`":
                        chars.append("`")
                        idx += 2
                        continue
                    idx += 1
                    break
                chars.append(desc[idx])
                idx += 1
            name = "".join(chars)
        else:
            start = idx
            while idx < length and desc[idx] not in "/#.!:([":
                idx += 1
            name = desc[start:idx]
        if idx >= length:
            if name:
                result.append((name, "."))
            break
        ch = desc[idx]
        if ch == "(":
            # Method/function descriptor: name(disambiguator).
            close = desc.find(").", idx)
            if close >= 0:
                result.append((name, "()."))
                idx = close + 2
            else:
                close = desc.find(")", idx)
                result.append((name, "()."))
                idx = (close + 1) if close >= 0 else length
        elif ch in "/#.!:":
            result.append((name, ch))
            idx += 1
        else:
            idx += 1
    return result


def parse_scip_symbol(
    symbol: str,
    info: ScipSymbolInfo | None = None,
    *,
    kind: int = 0,
    display_name: str = "",
) -> dict[str, Any]:
    """Extract display name, owner, member, package, and Cairnmind kind from a SCIP symbol string."""
    if info is not None:
        if not kind:
            kind = info.kind
        if not display_name:
            display_name = info.display_name
    if is_local_symbol(symbol):
        return {"local": True, "name": display_name or symbol, "kind": "variable", "package": ""}
    header, desc_str = _split_space_escaped(symbol, 4)
    scheme = header[0] if len(header) > 0 else ""
    manager = header[1] if len(header) > 1 else ""
    package = header[2] if len(header) > 2 else ""
    descriptors = _parse_descriptors(desc_str)
    if not descriptors:
        name = display_name or package or symbol
        mapped_kind = _SCIP_KIND_TO_CAIRNMIND.get(kind, "type")
        return {"local": False, "scheme": scheme, "manager": manager, "package": package,
                "name": name, "kind": mapped_kind}
    last_name, last_suffix = descriptors[-1]
    mapped_kind = _SCIP_KIND_TO_CAIRNMIND.get(kind)
    if not mapped_kind:
        if last_suffix == "().":
            has_type_owner = any(suf == "#" for _, suf in descriptors[:-1])
            mapped_kind = "method" if has_type_owner else "function"
        elif last_suffix == "#":
            mapped_kind = "class"
        elif last_suffix == "!":
            mapped_kind = "function"
        elif last_suffix == "()":
            mapped_kind = "parameter"
        elif last_suffix == "[]":
            mapped_kind = "type"
        elif last_suffix == "/":
            mapped_kind = "type"
        else:
            has_type_owner = any(suf == "#" for _, suf in descriptors[:-1])
            mapped_kind = "field" if has_type_owner else "constant"
    # Build owner/member when nested inside a type (#) or callable (().)
    owner: str | None = None
    member: str | None = None
    leaf = display_name or last_name
    for idx in range(len(descriptors) - 2, -1, -1):
        prev_name, prev_suf = descriptors[idx]
        if prev_suf in {"#", "()."} and prev_name:
            if prev_name == "impl" and idx + 1 < len(descriptors) - 1 and descriptors[idx + 1][1] == "[]":
                owner = descriptors[idx + 1][0]
            else:
                owner = prev_name
            member = leaf
            break
    qualified = f"{owner}.{member}" if owner and member and mapped_kind in {"method", "field", "parameter"} else leaf
    external_pkg = ""
    if package and package != ".":
        external_pkg = f"{manager}:{package}" if manager and manager != "." else package
    elif descriptors and descriptors[0][1] == "/":
        external_pkg = descriptors[0][0]
    return {
        "local": False,
        "scheme": scheme,
        "manager": manager,
        "package": external_pkg,
        "name": qualified or last_name or symbol,
        "bare_name": leaf or last_name,
        "kind": mapped_kind,
        **({"owner": owner, "member": member} if owner and member else {}),
    }


# ---------------------------------------------------------------------------
# Minimal Protobuf v3 encoder (for deterministic binary SCIP test fixtures)
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_tag(field_num: int, wire_type: int) -> bytes:
    return _encode_varint((field_num << 3) | wire_type)


def _encode_bytes_field(field_num: int, payload: bytes) -> bytes:
    if not payload:
        return b""
    return _encode_tag(field_num, 2) + _encode_varint(len(payload)) + payload


def _encode_str_field(field_num: int, value: str) -> bytes:
    return _encode_bytes_field(field_num, value.encode("utf-8")) if value else b""


def _encode_varint_field(field_num: int, value: int) -> bytes:
    return (_encode_tag(field_num, 0) + _encode_varint(value)) if value else b""


def _encode_packed_int32_field(field_num: int, values: list[int]) -> bytes:
    if not values:
        return b""
    body = b"".join(_encode_varint(v) for v in values)
    return _encode_bytes_field(field_num, body)


def encode_scip_index(index: ScipIndex, *, typed_ranges: bool = False) -> bytes:
    """Encode a ScipIndex into a valid binary Protobuf v3 ``index.scip`` stream."""
    def enc_range_msg(sl: int, sc: int, el: int, ec: int, single_f: int, multi_f: int) -> bytes:
        if sl == el:
            body = (_encode_varint_field(1, sl) + _encode_varint_field(2, sc) + _encode_varint_field(3, ec))
            return _encode_bytes_field(single_f, body) if body else _encode_tag(single_f, 2) + b"\x00"
        body = (_encode_varint_field(1, sl) + _encode_varint_field(2, sc)
                + _encode_varint_field(3, el) + _encode_varint_field(4, ec))
        return _encode_bytes_field(multi_f, body)

    def enc_sym(sym: ScipSymbolInfo) -> bytes:
        parts = [_encode_str_field(1, sym.symbol)]
        for doc in sym.documentation:
            parts.append(_encode_str_field(3, doc))
        for rel in sym.relationships:
            rel_body = (_encode_str_field(1, rel.symbol)
                        + _encode_varint_field(2, int(rel.is_reference))
                        + _encode_varint_field(3, int(rel.is_implementation))
                        + _encode_varint_field(4, int(rel.is_type_definition))
                        + _encode_varint_field(5, int(rel.is_definition)))
            parts.append(_encode_bytes_field(4, rel_body))
        parts.append(_encode_varint_field(5, sym.kind))
        parts.append(_encode_str_field(6, sym.display_name))
        parts.append(_encode_str_field(8, sym.enclosing_symbol))
        return b"".join(parts)

    def enc_occ(occ: ScipOccurrence) -> bytes:
        parts: list[bytes] = []
        if typed_ranges:
            parts.append(enc_range_msg(occ.start_line, occ.start_col, occ.end_line, occ.end_col, 8, 9))
        else:
            r = ([occ.start_line, occ.start_col, occ.end_col] if occ.start_line == occ.end_line
                 else [occ.start_line, occ.start_col, occ.end_line, occ.end_col])
            parts.append(_encode_packed_int32_field(1, r))
        parts.append(_encode_str_field(2, occ.symbol))
        parts.append(_encode_varint_field(3, occ.symbol_roles))
        if occ.enclosing_start_line is not None and occ.enclosing_end_line is not None:
            if typed_ranges:
                parts.append(enc_range_msg(occ.enclosing_start_line, 0, occ.enclosing_end_line, 0, 10, 11))
            else:
                er = ([occ.enclosing_start_line, 0, 0] if occ.enclosing_start_line == occ.enclosing_end_line
                      else [occ.enclosing_start_line, 0, occ.enclosing_end_line, 0])
                parts.append(_encode_packed_int32_field(7, er))
        return b"".join(parts)

    tool = index.metadata.tool_info
    tool_bytes = (_encode_str_field(1, tool.name) + _encode_str_field(2, tool.version)
                  + b"".join(_encode_str_field(3, a) for a in tool.arguments))
    meta_bytes = (_encode_varint_field(1, index.metadata.version)
                  + _encode_bytes_field(2, tool_bytes)
                  + _encode_str_field(3, index.metadata.project_root)
                  + _encode_varint_field(4, index.metadata.text_document_encoding))
    out = [_encode_bytes_field(1, meta_bytes)]
    for doc in index.documents:
        doc_parts = [
            _encode_str_field(1, doc.relative_path),
            *(b for occ in doc.occurrences for b in (_encode_bytes_field(2, enc_occ(occ)),)),
            *(b for sym in doc.symbols for b in (_encode_bytes_field(3, enc_sym(sym)),)),
            _encode_str_field(4, doc.language),
            _encode_varint_field(6, doc.position_encoding),
        ]
        out.append(_encode_bytes_field(2, b"".join(doc_parts)))
    for ext in index.external_symbols:
        out.append(_encode_bytes_field(3, enc_sym(ext)))
    return b"".join(out)


# Keep struct imported for any fixed-width validation helpers if needed.
_ = struct

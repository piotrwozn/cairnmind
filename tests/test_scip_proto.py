"""Fast-tier unit tests for pure-Python SCIP protobuf decoding and symbol parsing."""
from __future__ import annotations

import pytest

from cairnmind.scip_proto import (
    MAX_SCIP_BYTES,
    ROLE_DEFINITION,
    ROLE_IMPORT,
    ROLE_READ_ACCESS,
    ScipDocument,
    ScipFormatError,
    ScipIndex,
    ScipMetadata,
    ScipOccurrence,
    ScipRelationship,
    ScipSymbolInfo,
    ScipToolInfo,
    _encode_bytes_field,
    _encode_varint,
    encode_scip_index,
    is_local_symbol,
    parse_scip_bytes,
    parse_scip_file,
    parse_scip_symbol,
)


def test_scip_round_trip_legacy_and_typed_ranges(tmp_path):
    index = ScipIndex(
        metadata=ScipMetadata(
            version=0,
            tool_info=ScipToolInfo(name="rust-analyzer", version="2026-11-23", arguments=["scip", "."]),
            project_root="file:///workspace",
            text_document_encoding=1,
        ),
        documents=[
            ScipDocument(
                relative_path="src/lib.rs",
                language="rust",
                occurrences=[
                    ScipOccurrence(
                        start_line=2,
                        start_col=7,
                        end_line=2,
                        end_col=14,
                        symbol="rust-analyzer cargo demo 0.1.0 compute().",
                        symbol_roles=ROLE_DEFINITION,
                        enclosing_start_line=2,
                        enclosing_end_line=6,
                    ),
                    ScipOccurrence(
                        start_line=10,
                        start_col=4,
                        end_line=12,
                        end_col=8,
                        symbol="rust-analyzer cargo demo 0.1.0 Engine#",
                        symbol_roles=ROLE_READ_ACCESS,
                    ),
                ],
                symbols=[
                    ScipSymbolInfo(
                        symbol="rust-analyzer cargo demo 0.1.0 compute().",
                        documentation=["Computes result"],
                        kind=17,  # Function
                        display_name="compute",
                        relationships=[
                            ScipRelationship(
                                symbol="rust-analyzer cargo demo 0.1.0 Trait#",
                                is_implementation=True,
                            )
                        ],
                    )
                ],
            )
        ],
        external_symbols=[
            ScipSymbolInfo(symbol="rust-analyzer cargo std 1.80.0 io/Read#", kind=21, display_name="Read")
        ],
    )

    for typed in (False, True):
        raw = encode_scip_index(index, typed_ranges=typed)
        decoded = parse_scip_bytes(raw)
        assert decoded.metadata.tool_info.name == "rust-analyzer"
        assert decoded.metadata.tool_info.version == "2026-11-23"
        assert decoded.metadata.tool_info.arguments == ["scip", "."]
        assert len(decoded.documents) == 1
        doc = decoded.documents[0]
        assert doc.relative_path == "src/lib.rs"
        assert doc.language == "rust"
        assert len(doc.occurrences) == 2
        occ0 = doc.occurrences[0]
        assert (occ0.start_line, occ0.start_col, occ0.end_line, occ0.end_col) == (2, 7, 2, 14)
        assert (occ0.enclosing_start_line, occ0.enclosing_end_line) == (2, 6)
        assert occ0.symbol_roles == ROLE_DEFINITION
        occ1 = doc.occurrences[1]
        assert (occ1.start_line, occ1.start_col, occ1.end_line, occ1.end_col) == (10, 4, 12, 8)
        assert len(doc.symbols) == 1
        assert doc.symbols[0].display_name == "compute"
        assert doc.symbols[0].relationships[0].is_implementation is True
        assert len(decoded.external_symbols) == 1

    # Also test parse_scip_file via open_no_follow
    scip_file = tmp_path / "index.scip"
    scip_file.write_bytes(encode_scip_index(index))
    from_disk = parse_scip_file(scip_file)
    assert from_disk.documents[0].relative_path == "src/lib.rs"


def test_scip_symbol_grammar_across_all_9_languages():
    cases = [
        # Go
        ("scip-go gomod github.com/acme/app v0.1.0 pkg/Server#Serve().", "Server.Serve", "method", "Server", "Serve"),
        # Rust (free function + inherent impl method)
        ("rust-analyzer cargo app 0.1.0 math/add().", "add", "function", None, None),
        ("rust-analyzer cargo app 0.1.0 math/impl#[Adder]add().", "Adder.add", "method", "Adder", "add"),
        # Java
        ("scip-java maven com.acme:app 1.0 com/acme/OrderService#create().", "OrderService.create", "method", "OrderService", "create"),
        # Kotlin
        ("scip-java maven com.acme:kt 1.0 com/acme/Repo#", "Repo", "class", None, None),
        # C/C++
        ("scip-clang . . . core/Parser#tokens.", "Parser.tokens", "field", "Parser", "tokens"),
        # C#
        ("scip-dotnet nuget App 1.0.0 App/BillingService#Charge().", "BillingService.Charge", "method", "BillingService", "Charge"),
        # PHP
        ("scip-php composer acme/shop 1.0.0 src/Cart#total().", "Cart.total", "method", "Cart", "total"),
        # Ruby
        ("scip-ruby bundler app 0.1.0 Billing/Invoice#render().", "Invoice.render", "method", "Invoice", "render"),
        # Constant / macro
        ("scip-clang . . . include/MAX_LIMIT!", "MAX_LIMIT", "function", None, None),
    ]
    for symbol, exp_name, exp_kind, exp_owner, exp_member in cases:
        parsed = parse_scip_symbol(symbol)
        assert parsed["name"] == exp_name, f"{symbol} -> {parsed}"
        assert parsed["kind"] == exp_kind, f"{symbol} -> {parsed}"
        assert parsed.get("owner") == exp_owner, f"{symbol} -> {parsed}"
        assert parsed.get("member") == exp_member, f"{symbol} -> {parsed}"

    assert is_local_symbol("local 0") is True
    assert is_local_symbol("local 42") is True
    assert is_local_symbol("scip-go gomod a b foo().") is False

    # SymbolInformation.kind overrides descriptor suffix heuristic
    info = ScipSymbolInfo(symbol="scip-java maven a:b 1.0 com/acme/Service#", kind=21, display_name="Service")
    assert parse_scip_symbol(info.symbol, info)["kind"] == "interface"


def test_scip_proto_rejects_malformed_and_path_traversal():
    # Path traversal in Document.relative_path
    evil = ScipIndex(documents=[ScipDocument(relative_path="../secret.go", language="go")])
    with pytest.raises(ScipFormatError, match="escapes project root"):
        parse_scip_bytes(encode_scip_index(evil))

    # Absolute path in Document.relative_path
    abs_doc = ScipIndex(documents=[ScipDocument(relative_path="/etc/passwd", language="go")])
    with pytest.raises(ScipFormatError, match="must be relative"):
        parse_scip_bytes(encode_scip_index(abs_doc))

    # Windows drive path in Document.relative_path
    win_doc = ScipIndex(documents=[ScipDocument(relative_path="C:/Windows/win.ini", language="go")])
    with pytest.raises(ScipFormatError, match="must be relative"):
        parse_scip_bytes(encode_scip_index(win_doc))

    # Truncated varint / length-delimited field
    with pytest.raises(ScipFormatError):
        parse_scip_bytes(b"\x12\x7f\x01\x02")

    # Size ceiling check
    with pytest.raises(ScipFormatError, match="exceeds"):
        parse_scip_bytes(b"\x00" * 16, max_bytes=8)

    # Unknown fields are safely skipped (forward compatibility)
    unknown_field = _encode_varint((99 << 3) | 2) + _encode_varint(3) + b"xyz"
    doc_payload = _encode_bytes_field(1, b"main.go") + unknown_field
    raw = _encode_bytes_field(2, doc_payload)
    decoded = parse_scip_bytes(raw)
    assert decoded.documents[0].relative_path == "main.go"
    assert MAX_SCIP_BYTES == 256 * 1024 * 1024
    assert ROLE_IMPORT == 0x2

"""Tests for the shared address/text primitives (rung.addresses)."""

from rung import addresses as addr


def test_clean_collapses_whitespace_and_strips_zero_width() -> None:
    assert addr.clean("  Foo   Bar \n Baz ") == "Foo Bar Baz"
    assert addr.clean("Zen​Leaf") == "ZenLeaf"          # zero-width space removed
    assert addr.clean("﻿Acme") == "Acme"                # BOM removed
    assert addr.clean(None) is None
    assert addr.clean("   ") is None                          # whitespace-only -> None
    assert addr.clean("") is None


def test_zip_re_matches_five_and_nine_digit() -> None:
    assert addr.ZIP_RE.search("Camp Hill PA 17011").group() == "17011"
    assert addr.ZIP_RE.search("foo 97031-2384 bar").group() == "97031-2384"
    assert addr.ZIP_RE.search("no zip here 123") is None


def test_phone_re_matches_common_formats() -> None:
    for raw in ("(412) 555-0100", "412-555-0100", "412.555.0100", "4125550100"):
        assert addr.PHONE_RE.search(raw) is not None


def test_block_address_re_captures_groups() -> None:
    m = addr.BLOCK_ADDRESS_RE.search("100 Liberty Ave, Pittsburgh, PA 15222")
    assert m is not None
    assert m.groups() == ("100 Liberty Ave", "Pittsburgh", "PA", "15222")


def test_extract_address_blocks_one_record_per_address() -> None:
    # Operators list several locations in one block; the name (heading) and the
    # addresses live in the SAME container — the extractor emits one record each.
    html = """
        <div>
          <h3>Acme Cannabis</h3>
          100 Liberty Ave, Pittsburgh, PA 15222
          200 Forbes St, Erie, PA 16501
        </div>
    """
    records = addr.extract_address_blocks(html)
    by_addr = {r.address: r for r in records}
    assert set(by_addr) == {"100 Liberty Ave", "200 Forbes St"}
    pgh = by_addr["100 Liberty Ave"]
    assert pgh.name == "Acme Cannabis" and pgh.city == "Pittsburgh"
    assert pgh.state == "PA" and pgh.zip_code == "15222" and pgh.source == "html"


def test_extract_address_blocks_keeps_tightest_container() -> None:
    # The outer <section> also "contains" the address via its child <div>; only the
    # tightest element holding both a name and the address emits, so we get one record.
    html = (
        "<section><h2>Region</h2>"
        "<div><h3>Acme</h3> 100 Liberty Ave, Pittsburgh, PA 15222</div></section>"
    )
    records = addr.extract_address_blocks(html)
    assert len(records) == 1
    assert records[0].address == "100 Liberty Ave" and records[0].name == "Acme"


def test_extract_address_blocks_dedupes_on_name_and_street() -> None:
    html = (
        "<ul>"
        "<li><a>Store</a> 1 Main St, Town, PA 11111</li>"
        "<li><a>Store</a> 1 Main St, Town, PA 11111</li>"
        "</ul>"
    )
    assert len(addr.extract_address_blocks(html)) == 1


def test_extract_address_blocks_name_from_text_before_address() -> None:
    # No heading/link → name is the text before the address.
    html = "<p>Greenleaf Dispensary 9 Oak Rd, Erie, PA 16501</p>"
    records = addr.extract_address_blocks(html)
    assert len(records) == 1
    assert records[0].name == "Greenleaf Dispensary"


def test_extract_address_blocks_skips_overlong_name() -> None:
    long_name = "X" * 90
    html = f"<p>{long_name} 9 Oak Rd, Erie, PA 16501</p>"
    assert addr.extract_address_blocks(html) == []

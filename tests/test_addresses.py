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


def test_zip_re_matches_canadian_postal_codes() -> None:
    # Both the spaced form and AGCO's unspaced roster form; lowercase prose doesn't match.
    assert addr.ZIP_RE.search("Sudbury ON P3E 4M8").group() == "P3E 4M8"
    assert addr.ZIP_RE.search("Sudbury ON P3E4M8").group() == "P3E4M8"
    assert addr.ZIP_RE.fullmatch("M5V 2T6") is not None
    assert addr.ZIP_RE.search("no postal here a1a 1a1") is None


def test_phone_re_matches_common_formats() -> None:
    for raw in ("(412) 555-0100", "412-555-0100", "412.555.0100", "4125550100"):
        assert addr.PHONE_RE.search(raw) is not None


def test_block_address_re_captures_groups() -> None:
    m = addr.BLOCK_ADDRESS_RE.search("100 Liberty Ave, Pittsburgh, PA 15222")
    assert m is not None
    assert m.groups() == ("100 Liberty Ave", "Pittsburgh", "PA", "15222")


def test_block_address_re_captures_canadian_address() -> None:
    m = addr.BLOCK_ADDRESS_RE.search("435 Yonge St, Toronto, ON M5B 1T3")
    assert m is not None
    assert m.groups() == ("435 Yonge St", "Toronto", "ON", "M5B 1T3")


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


# ── line blocks ──────────────────────────────────────────────────────────────
# Alabama's AMCC roster is a <p> of <br/>-separated lines. `BLOCK_ADDRESS_RE` needs a comma
# between street and city ("street, city, ST zip") and so returns nothing on it.

_AMCC = """
<div class="textwidget"><p><strong><span>OPENING JUNE 4, 2026</span><br/>
Callie's Apothecary</strong><br/>
<strong>5232 Atlanta Highway</strong><br/>
<strong>Montgomery, AL 36109</strong><br/>
<strong>Hours: Monday - Friday | 10 AM - 6 PM</strong><br/>
<strong>Website: <a href="https://shoppecalliesal.com/">https://shoppecalliesal.com/</a></strong></p></div>
"""


def test_line_blocks_reads_the_alabama_roster_that_block_addresses_cannot() -> None:
    assert addr.extract_address_blocks(_AMCC) == []      # the shape that motivated this
    records = addr.extract_line_blocks(_AMCC)
    assert len(records) == 1
    got = records[0]
    # The name is the line ABOVE the street — not the ALL-CAPS banner directly above it.
    assert got.name == "Callie's Apothecary"
    assert (got.address, got.city, got.state, got.zip_code) == (
        "5232 Atlanta Highway", "Montgomery", "AL", "36109")


def test_name_before_rejects_everything_that_is_not_a_name() -> None:
    lines = ["OPENING JUNE 4, 2026", "Callie's Apothecary", "5232 Atlanta Highway",
             "Montgomery, AL 36109"]
    assert addr.name_before(lines, 2) == "Callie's Apothecary"
    assert addr.name_before(lines, 1) is None            # ALL-CAPS banner
    assert addr.name_before(lines, 0) is None            # nothing above the first line
    for junk in ("Shop Now", "Directions", "info@x.com", "https://x.com", "(555) 123-4567",
                 "9 Oak Ave", "Erie, PA 16501"):
        assert addr.name_before([junk, "1 Main St"], 1) is None, junk


def test_line_blocks_handles_single_line_and_canadian_postal_codes() -> None:
    html = "<p>MariMart<br/>865 US-22, Blairsville PA 15717</p>"
    (got,) = addr.extract_line_blocks(html)
    assert (got.name, got.city, got.state, got.zip_code) == ("MariMart", "Blairsville", "PA", "15717")

    canada = "<p>Canna Cabana<br/>123 Queen St W<br/>Toronto, ON M5H 2M9</p>"
    (got,) = addr.extract_line_blocks(canada)
    assert (got.state, got.zip_code) == ("ON", "M5H 2M9")


def test_line_blocks_skips_an_address_with_no_name_and_dedupes_by_street() -> None:
    # A roster row we cannot attribute to an operator is useless for compare-stores.
    assert addr.extract_line_blocks("<p>1 Main St<br/>Erie, PA 16501</p>") == []
    dupe = "<p>Acme<br/>1 Main St<br/>Erie, PA 16501</p><p>Acme<br/>1 Main St<br/>Erie, PA 16501</p>"
    assert len(addr.extract_line_blocks(dupe)) == 1

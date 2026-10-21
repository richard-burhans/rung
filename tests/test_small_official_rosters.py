"""The four one-page official rosters that replaced Dutchie bootstraps (2026-10-19): West Virginia,
North Dakota, South Dakota and Hawaii. Snippets in each page's own markup."""

import asyncio
import json

import pytest

from rung.sources import extract


def _wv_page(status: str = "Operational with Product", trade_name: str = "Newleaf") -> str:
    popup = (f'<div class="views-field views-field-field-trade-name"><div class="field-content">{trade_name}</div></div>'
             '<p class="address"><span class="address-line1">355 WAYFARER DRIVE</span><br>'
             '<span class="locality">TRIADELPHIA</span>, <span class="administrative-area">WV</span> '
             '<span class="postal-code">26059</span></p><div class="views-field views-field-nothing">'
             '<span class="field-content">Dispensary Name: <a href="/node/512">New Leaf WV, LLC</a> <br />'
             'Phone Number: <a href="tel:304-578-5231">304-578-5231</a></span></div>'
             f'<div class="views-field views-field-views-conditional-field"><span class="field-content">{status}</span></div>')
    settings = {"leaflet": {"leaflet-map-view-x": {"features": [
        {"type": "point", "lat": "40.0602", "lon": "-80.5882", "popup": {"value": popup}}]}}}
    return ('<script type="application/json" data-drupal-selector="drupal-settings-json">'
            f'{json.dumps(settings)}</script>')


def test_west_virginia_reads_the_map_markers_and_keeps_operational_stores() -> None:
    [r] = extract._wv_omc_records(_wv_page())
    assert (r.name, r.address, r.city, r.zip_code, r.phone, r.latitude, r.longitude, r.source) == (
        "Newleaf", "355 WAYFARER DRIVE", "TRIADELPHIA", "26059", "304-578-5231", 40.0602, -80.5882, "wv_omc")
    assert extract._wv_omc_records(_wv_page("Not Yet Operational")) == []
    # A marker with no trade name is named by its licensee rather than dropped as nameless.
    assert [r.name for r in extract._wv_omc_records(_wv_page(trade_name=""))] == ["New Leaf WV, LLC"]


def test_north_dakota_reads_city_store_street_phone_and_site_in_order() -> None:
    page = ('<div class="field--name-field-wysiwyg-text"><h2>Fargo</h2><h3>The Botanist</h3><p>3438 Main Ave</p>'
            '<p>(701) 555-0100</p><p><a href="https://botanist.example/">botanist</a></p>'
            '<h3>Curaleaf</h3><p>1 Broadway</p><p>(701) 555-0199</p><h4>Store Hours</h4><p>Monday: 10-6</p></div>')
    rows = [(r.name, r.address, r.city, r.phone, r.website) for r in extract._nd_hhs_records(page)]
    assert rows == [("The Botanist", "3438 Main Ave", "Fargo", "(701) 555-0100", "https://botanist.example/"),
                    ("Curaleaf", "1 Broadway", "Fargo", "(701) 555-0199", None)]


def test_south_dakota_reads_only_the_dispensary_table() -> None:
    page = ('<h2>Dispensary Establishments</h2><table><tr><th>Legal Name</th><th>Doing Business As</th>'
            '<th>Location</th></tr><tr><td>Dakota Herb LLC</td><td>Dakota Herb</td><td>Aberdeen</td></tr></table>'
            '<h3>Cultivation Establishments</h3><table><tr><th>Legal Name</th><th>Doing Business As</th>'
            '<th>Location</th></tr><tr><td>Prairie Farms LLC</td><td>Prairie Farms</td><td>Arlington</td></tr></table>')
    assert [(r.name, r.city, r.address) for r in extract._sd_doh_records(page)] == [("Dakota Herb", "Aberdeen", None)]


def test_hawaii_lists_licensed_operators_with_their_sites() -> None:
    page = ('<table><tr><td><strong>Hawaii County</strong><p><a href="https://www.hawaiianethos.com/">Hawaiian Ethos'
            '</a></p></td><td><strong>Maui County</strong><p><a href="https://www.mauigrown.com/">Maui Grown '
            'Therapies</a></p></td></tr></table>')
    assert [(r.name, r.website, r.address) for r in extract._hi_doh_records(page)] == [
        ("Hawaiian Ethos", "https://www.hawaiianethos.com/", None),
        ("Maui Grown Therapies", "https://www.mauigrown.com/", None)]


def test_texas_reads_each_licensee_paragraph_and_unwraps_its_website() -> None:
    page = ('<p><strong>Current Active Licensed Dispensaries:</strong></p>'
            '<p><strong>Fluent </strong>(formerly Cansortium Texas)<br><strong>License #0004</strong><br>'
            '<a href="mailto:info@fluenttx.com"><strong>info@fluenttx.com</strong></a><br>'
            '<a href="https://urldefense.com/v3/__https:/getfluent.com/__;!!x$"><span><strong>https://getfluent.com/'
            '</strong></span></a></p><p>&nbsp;</p>'
            '<p><span><strong>Goodblend </strong>(Surterra Texas LLC)</span><br><span><strong>License #0006</strong>'
            '</span></p>')
    assert [(r.name, r.licence_number, r.website, r.address) for r in extract._tx_dps_records(page)] == [
        ("Fluent", "0004", "https://getfluent.com/", None), ("Goodblend", "0006", None, None)]


def test_a_page_that_no_longer_has_the_shape_is_a_failure_not_an_empty_roster() -> None:
    class _Resp:
        status_code, url, text = 200, "https://omc.wv.gov/dispensary", "<html>redesigned</html>"

    class _Session:
        async def get(self, *_a, **_k):
            return _Resp()
    with pytest.raises(extract.ExtractionFailed, match="no roster"):
        asyncio.run(extract._extract_wv_omc("https://omc.wv.gov/dispensary", _Session()))
    assert {"wv_omc", "nd_hhs", "sd_doh", "hi_doh", "tx_dps"} <= extract.HANDLED_LIST_TYPES

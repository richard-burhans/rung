"""Oklahoma's roster from OMMA Verify: the Salesforce guest call, the record mapping, the partition cap,
and the licensee's address-disclosure choice (2026-10-19)."""

import asyncio
import json

import pytest

from rung.sources import extract, salesforce_aura

_PAGE = ('<script>window.x={"fwuid":"FWUID123","app":"siteforce:communityApp","loaded":'
         '{"APPLICATION@markup://siteforce:communityApp":"1888_APPHASH"}}</script>')


def _row(**over):
    row = {"licenseType": "Dispensary", "licenseStatus": "Active", "licenseNumber": "DAAA-020P-A636",
           "legalEntityName": "Indica Cannabis Co.", "addressDisclosure": True,
           "physicalAddress": "12404 N MacArthur Blvd, Oklahoma City, Oklahoma, 73142",
           "telephoneNumber": "(361) 550-2764"}
    return row | over


def test_the_page_bootstrap_carries_the_guest_context() -> None:
    ctx = salesforce_aura.parse_context("https://medportal.omma.ok.gov/", "/s/verify-license-number", _PAGE)
    assert (ctx.base, ctx.fwuid, ctx.app_hash) == ("https://medportal.omma.ok.gov", "FWUID123", "1888_APPHASH")
    form = salesforce_aura.apex_form(ctx, "OMMA_VerifyLicenseNumberController", "getBusinessLicenses",
                                     {"searchString": "DAAA-0"})
    action = json.loads(form["message"])["actions"][0]["params"]
    assert (action["classname"], action["method"], action["params"]) == (
        "OMMA_VerifyLicenseNumberController", "getBusinessLicenses", {"searchString": "DAAA-0"})
    assert json.loads(form["aura.context"])["fwuid"] == "FWUID123" and form["aura.token"] == "null"
    with pytest.raises(salesforce_aura.AuraError, match="no Aura bootstrap"):
        salesforce_aura.parse_context("https://x.test", "/s/p", "<html>not a community</html>")


def test_an_action_that_did_not_succeed_is_an_error_not_an_empty_answer() -> None:
    assert salesforce_aura.action_value({"actions": [{"state": "SUCCESS", "returnValue": {"a": 1}}]}) == {"a": 1}
    with pytest.raises(salesforce_aura.AuraError, match="ERROR"):
        salesforce_aura.action_value({"actions": [{"state": "ERROR", "error": [{"message": "x"}]}]})
    with pytest.raises(salesforce_aura.AuraError, match="not an Aura"):
        salesforce_aura.action_value({"event": "x"})


def test_omma_addresses_and_records() -> None:
    assert extract.omma_address("140 S Midwest Blvd., Suite C, Midwest City, Oklahoma, 73110") == (
        "140 S Midwest Blvd., Suite C", "Midwest City", "73110")
    assert extract.omma_address("1616 S ELM DR, TERLTON, Pawnee, 74081-3309") == ("1616 S ELM DR", "TERLTON", "74081")
    record = extract.omma_record(_row(tradeName="Indica"))
    assert record is not None
    assert (record.name, record.address, record.city, record.zip_code, record.licence_number, record.source) == (
        "Indica", "12404 N MacArthur Blvd", "Oklahoma City", "73142", "DAAA-020P-A636", "ok_omma")
    assert record.address_withheld is False
    assert extract.omma_record(_row(addressDisclosure=False)).address_withheld is True   # type: ignore[union-attr]
    assert extract.omma_record(_row(licenseStatus="Active - Renewal Pending")) is not None
    assert extract.omma_record(_row(licenseStatus="Expired")) is None
    assert extract.omma_record(_row(licenseType="Processor")) is None


def test_a_partition_at_the_cap_is_a_partial_roster(monkeypatch) -> None:
    """A search returns at most 2,000 rows (the next offset answers empty): a full page may be cut short."""
    async def context(_session, base, path):
        return salesforce_aura.AuraContext(base, path, "F", "A")

    async def apex(_session, _ctx, _cls, _method, params):
        n = extract._OMMA_PAGE if params["searchString"] == "DAAA-7" else 1
        return {"returnValue": {"records": [_row(licenseNumber=f"{params['searchString']}-{i}") for i in range(n)]}}

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(salesforce_aura, "page_context", context)
    monkeypatch.setattr(salesforce_aura, "call_apex", apex)
    monkeypatch.setattr(extract.asyncio, "sleep", no_sleep)
    with pytest.raises(extract.PartialRoster, match="DAAA-7"):
        asyncio.run(extract._extract_ok_omma("https://medportal.omma.ok.gov/s/verify-license-number", None))


def test_ok_omma_is_a_handled_list_type() -> None:
    assert "ok_omma" in extract.HANDLED_LIST_TYPES and extract._ASYNC_HANDLERS["ok_omma"] is extract._extract_ok_omma

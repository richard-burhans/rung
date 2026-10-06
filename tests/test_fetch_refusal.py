"""The one shared refusal helper: a failed request is never read as "no data here".

Nine fetchers each hand-wrote `if response.status_code != 200: return None`, so a 403, a 404, a 5xx,
a timeout and a Cloudflare page answering 200 all reached the rung as the same nothing and the
registry recorded `no_plausible_records` — 6,883 of 6,883 failing menu targets on 2026-09-15.
`rung.http` now classifies once, at tier 0, and `rung.access` translates the kind. These pin the
classification table; `test_access_outcomes.py` pins the translation.
"""

import json

import pytest

from rung import http


class _Resp:
    def __init__(self, status_code: int, text: str = "{}") -> None:
        self.status_code = status_code
        self.text = text


URL = "https://api.example.test/menu/1"


@pytest.mark.parametrize("status", sorted(http.REFUSED_STATUSES))
def test_a_refusal_status_is_blocked(status: int) -> None:
    refused = http.refusal(_Resp(status), URL)
    assert refused is not None and refused.kind == "blocked"
    assert refused.status == status and refused.url == URL
    assert str(status) in str(refused) and URL in str(refused)


@pytest.mark.parametrize("status", sorted(http.GONE_STATUSES))
def test_a_gone_status_is_unavailable_unless_missing_is_an_ordinary_answer(status: int) -> None:
    refused = http.refusal(_Resp(status), URL)
    assert refused is not None and refused.kind == "unavailable"
    assert http.refusal(_Resp(status), URL, missing_ok=True) is None


@pytest.mark.parametrize("status", [400, 500, 502, 503, 504])
def test_any_other_failure_status_is_broken(status: int) -> None:
    refused = http.refusal(_Resp(status), URL)
    assert refused is not None and refused.kind == "broken"


class _WafResp(_Resp):
    def __init__(self, status_code: int, headers: dict[str, str], text: str = "<html>challenge</html>") -> None:
        super().__init__(status_code, text)
        self.headers = headers


@pytest.mark.parametrize("action", ["challenge", "captcha"])
def test_an_aws_waf_challenge_is_blocked_whatever_status_carries_it(action: str) -> None:
    """AWS WAF answers a challenge with HTTP **202** and names it in a header. By status alone that
    was "any other non-200", i.e. `broken` — "fix the rung" — and 33 Hifyre menus sat under that
    verdict from the day they were discovered. Measured 2026-10-04 on five CloudFront-fronted
    chains: `x-amzn-waf-action: challenge`, 202, a 2 KB page that loads `challenge.js`."""
    for expect in ("html", "json"):
        refused = http.refusal(_WafResp(202, {"x-amzn-waf-action": action}), URL, expect=expect)
        assert refused is not None and refused.kind == "blocked" and refused.status == 202
        assert action in str(refused) and URL in str(refused)
    # Header names are case-insensitive, and a real client may hand them back in any case.
    assert http.refusal(_WafResp(202, {"X-Amzn-Waf-Action": action}), URL).kind == "blocked"
    # …and the header outranks a status that would otherwise read as something else.
    assert http.refusal(_WafResp(200, {"x-amzn-waf-action": action}, "{}"), URL).kind == "blocked"


def test_a_cloudflare_challenge_is_blocked_on_a_503_as_on_a_403() -> None:
    """P-33's open question — is a NON-200 interstitial a wall? — ruled 2026-10-06: a challenge the
    edge NAMES in a header is a wall on any status. Cloudflare's managed challenge rides a 403
    (measured on Leafly, 2026-10-05); its older "Under Attack" JavaScript challenge rode a 503, which
    by status alone read `broken` — "fix the rung" — for a wall. The header is the measured signal;
    the status is not."""
    for status in (403, 503, 429, 200):
        refused = http.refusal(_WafResp(status, {"cf-mitigated": "challenge"}, "<html>Just a moment"), URL)
        assert refused is not None and refused.kind == "blocked" and refused.status == status
        assert "cf-mitigated: challenge" in str(refused)


def test_an_unnamed_non_200_is_still_broken_and_now_carries_its_body_head() -> None:
    """ANTI-VACUITY for the ruling above: a 503 WITHOUT a challenge header is an origin failing, not a
    wall, and is not widened into one by text-matching — but the head of an HTML body now travels
    in the message, so a 503 challenge page that names no header shows up in `access_methods.error`
    instead of being indistinguishable from an outage."""
    refused = http.refusal(_Resp(503, "<!doctype html><html><title>Just a moment...</title>"), URL)
    assert refused is not None and refused.kind == "broken" and refused.status == 503
    assert "Just a moment" in str(refused)
    # A non-HTML body (a JSON error, an empty body) adds nothing to the message.
    assert str(http.refusal(_Resp(500, '{"error": "boom"}'), URL)).endswith(URL)
    assert str(http.refusal(_Resp(502, ""), URL)).endswith(URL)


def test_a_202_that_does_not_name_a_waf_is_still_broken() -> None:
    """ANTI-VACUITY: it is the HEADER that makes it a wall. A bare 202 is an answer we do not
    understand, and one with unrelated headers (or none at all) stays `broken`."""
    assert http.refusal(_Resp(202), URL).kind == "broken"
    assert http.refusal(_WafResp(202, {"server": "CloudFront"}), URL).kind == "broken"
    assert http.refusal(_WafResp(202, {"x-amzn-waf-action": ""}), URL).kind == "broken"


def test_an_edge_names_its_own_challenge_and_a_hosts_answer_names_none() -> None:
    """Cloudflare's managed challenge rides a 403 with `cf-mitigated: challenge` (measured on Leafly
    through residential exits, 2026-10-05); AWS WAF's rides a 202 with its own header. Either says
    the edge judged the ADDRESS — which a caller holding other addresses can act on."""
    assert http.edge_challenge(_WafResp(403, {"cf-mitigated": "challenge"})) == "cf-mitigated: challenge"
    assert http.edge_challenge(_WafResp(403, {"CF-Mitigated": "challenge"})) == "cf-mitigated: challenge"
    assert http.edge_challenge(_WafResp(202, {"x-amzn-waf-action": "captcha"})) == "x-amzn-waf-action: captcha"
    # ANTI-VACUITY: a 403 the host itself wrote, an edge header with no value, no headers at all.
    assert http.edge_challenge(_WafResp(403, {"server": "cloudflare"})) is None
    assert http.edge_challenge(_WafResp(403, {"cf-mitigated": ""})) is None
    assert http.edge_challenge(_Resp(403)) is None


def test_a_json_200_is_the_payload() -> None:
    assert http.refusal(_Resp(200, json.dumps({"data": []})), URL) is None
    assert http.refusal(_Resp(200, "  [1, 2]"), URL) is None


def test_html_answering_200_where_json_was_expected_is_a_refusal_not_a_shape_quirk() -> None:
    """The reCAPTCHA / Incapsula / Cloudflare shape this project has met three times."""
    refused = http.refusal(_Resp(200, "<!DOCTYPE html><html><body>Checking your browser"), URL)
    assert refused is not None and refused.kind == "blocked" and refused.status == 200
    assert "interstitial" in str(refused)


def test_a_non_json_non_html_200_is_broken() -> None:
    refused = http.refusal(_Resp(200, "OK"), URL)
    assert refused is not None and refused.kind == "broken"


def test_an_html_fetcher_judges_the_status_only() -> None:
    assert http.refusal(_Resp(200, "<html>a real server-rendered menu</html>"), URL, expect="html") is None
    refused = http.refusal(_Resp(403, "<html>"), URL, expect="html")
    assert refused is not None and refused.kind == "blocked"


def test_a_response_with_no_status_is_broken() -> None:
    refused = http.refusal(object(), URL)
    assert refused is not None and refused.kind == "broken"


def test_raise_for_refusal_raises_exactly_what_refusal_returns() -> None:
    http.raise_for_refusal(_Resp(200), URL)  # no raise
    with pytest.raises(http.FetchRefused) as info:
        http.raise_for_refusal(_Resp(429), URL)
    assert info.value.kind == "blocked"


def test_a_transport_error_wraps_as_broken_and_names_the_exception() -> None:
    refused = http.refused_by(TimeoutError("read timed out"), URL)
    assert refused.kind == "broken" and refused.status is None
    assert "TimeoutError" in str(refused) and URL in str(refused)


def test_every_kind_the_helper_can_produce_is_a_kind_the_engine_translates() -> None:
    """The two modules spell the kinds independently; this is the seam."""
    from rung import access

    produced = {
        http.refusal(_Resp(403), URL).kind,
        http.refusal(_Resp(404), URL).kind,
        http.refusal(_Resp(500), URL).kind,
        http.refusal(_Resp(200, "<html>"), URL).kind,
        http.refusal(_Resp(200, "nope"), URL).kind,
        http.refused_by(OSError("x"), URL).kind,
    }
    assert produced == set(access._OUTCOME_FOR_KIND)

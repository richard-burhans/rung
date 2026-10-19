"""Call a guest Apex method on a Salesforce Experience Cloud (community) site, over plain HTTP.

Regulators increasingly run licensing on Salesforce communities — Oklahoma's OMMA MedPortal
(`medportal.omma.ok.gov`) since 2026 — and the public "verify a license" page of such a site is a
JavaScript app whose data comes from one endpoint, ``/s/sfsites/aura``. That is why a roster behind one
looks "JS-gated": no list is in the HTML, and `find-lists` sees an empty shell. The data is not gated at
all. The page's own bootstrap carries the two values a guest call needs — the framework id (``fwuid``)
and the loaded-app hash — and an ``aura://ApexActionController/ACTION$execute`` call with them returns
exactly what the page would render. No browser, no login.

    ctx = await page_context(session, "https://medportal.omma.ok.gov", "/s/verify-license-number")
    value = await call_apex(session, ctx, "OMMA_VerifyLicenseNumberController", "getBusinessLicenses",
                            {"searchString": "DAAA-0", "offsetSize": 0, "pageSize": 2000})

The framework id rotates when Salesforce releases; a call made with a stale one answers
``clientOutOfSync``. `call_apex` re-reads the page and retries once before raising.
"""

import json
import re
from dataclasses import dataclass

from rung.http import TRANSPORT_ERRORS

_FWUID_RE = re.compile(r'"fwuid":"([^"]+)"')
_APP_RE = re.compile(r'APPLICATION@markup://siteforce:communityApp\\?"\s*:\s*\\?"([^"\\]+)')
_APP = "siteforce:communityApp"


class AuraError(Exception):
    """The site answered, but not with the action's value — a missing bootstrap, a non-JSON body, or an
    action whose ``state`` is not ``SUCCESS``. Carries what the site said."""


@dataclass(frozen=True)
class AuraContext:
    """What a guest Aura call needs from the page that would have made it."""

    base: str          # https://medportal.omma.ok.gov
    page_path: str     # /s/verify-license-number
    fwuid: str
    app_hash: str


def parse_context(base: str, page_path: str, html: str) -> AuraContext:
    """The Aura context in a community page's bootstrap. Pure. Raises `AuraError` when it is absent —
    the page is not a Salesforce community page, or its markup has changed."""
    fwuid, app = _FWUID_RE.search(html), _APP_RE.search(html)
    if fwuid is None or app is None:
        raise AuraError(f"{base}{page_path}: no Aura bootstrap (fwuid {'found' if fwuid else 'missing'}, "
                        f"app hash {'found' if app else 'missing'})")
    return AuraContext(base.rstrip("/"), page_path, fwuid.group(1), app.group(1))


async def page_context(session, base: str, page_path: str) -> AuraContext:
    """Fetch a community page and read its Aura context."""
    response = await session.get(base.rstrip("/") + page_path, timeout=60)
    if response.status_code >= 400:
        raise AuraError(f"{base}{page_path}: HTTP {response.status_code}")
    return parse_context(base, page_path, response.text)


def apex_form(ctx: AuraContext, classname: str, method: str, params: dict) -> dict[str, str]:
    """The form body of one guest ``ApexActionController`` call. Pure."""
    message = {"actions": [{
        "id": "1;a", "descriptor": "aura://ApexActionController/ACTION$execute", "callingDescriptor": "UNKNOWN",
        "params": {"namespace": "", "classname": classname, "method": method, "params": params,
                   "cacheable": False, "isContinuation": False},
    }]}
    context = {"mode": "PROD", "fwuid": ctx.fwuid, "app": _APP,
               "loaded": {f"APPLICATION@markup://{_APP}": ctx.app_hash}, "dn": [], "globals": {}, "uad": False}
    return {"message": json.dumps(message), "aura.context": json.dumps(context),
            "aura.pageURI": ctx.page_path, "aura.token": "null"}


def action_value(body: object) -> object:
    """The first action's return value from an Aura response body. Pure. An Apex method's own value
    sits one level further down, under ``returnValue`` again — that is the caller's to unwrap, because
    not every action wraps it."""
    if not isinstance(body, dict) or not isinstance(body.get("actions"), list) or not body["actions"]:
        raise AuraError(f"not an Aura action response: {str(body)[:200]}")
    action = body["actions"][0]
    if action.get("state") != "SUCCESS":
        raise AuraError(f"action {action.get('state')}: {json.dumps(action.get('error'))[:300]}")
    return action.get("returnValue")


def _out_of_sync(body: object) -> bool:
    return isinstance(body, dict) and "clientOutOfSync" in json.dumps(body.get("exceptionEvent") or body.get("event") or "")


async def call_apex(session, ctx: AuraContext, classname: str, method: str, params: dict) -> object:
    """Run one guest Apex method and return the first action's value. Re-reads the page once if the
    site says the framework id is stale (``clientOutOfSync``)."""
    for attempt in range(2):
        try:
            response = await session.post(f"{ctx.base}/s/sfsites/aura?r=1&aura.ApexAction.execute=1",
                                          data=apex_form(ctx, classname, method, params), timeout=90)
        except TRANSPORT_ERRORS as exc:
            raise AuraError(f"{ctx.base}: {type(exc).__name__}: {exc}") from exc
        text = response.text
        body = json.loads(text) if text.lstrip().startswith("{") else None
        if body is None:
            # An out-of-sync answer is a `*/{...}/*`-wrapped event, not plain JSON.
            if attempt == 0 and "clientOutOfSync" in text:
                ctx = await page_context(session, ctx.base, ctx.page_path)
                continue
            raise AuraError(f"{ctx.base}: HTTP {response.status_code}, not JSON: {text[:200]}")
        if attempt == 0 and _out_of_sync(body):
            ctx = await page_context(session, ctx.base, ctx.page_path)
            continue
        return action_value(body)
    raise AuraError(f"{ctx.base}: still out of sync after re-reading the page")

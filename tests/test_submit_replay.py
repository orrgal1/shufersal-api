"""Same-key /checkout/submit requests never re-run the checkout: a failed
attempt replays its stored error (code, message, details); a request that
arrives while the first is still running gets 202 in_progress (poll with the
same key), never "used, use a new key"."""
import asyncio

import httpx
import pytest

import main
from tests.test_live_driver import AUTH, _quote, client, live, site  # noqa: F401
from tests.test_unattended import _submit, ua  # noqa: F401


def test_failed_attempt_replays_its_error_and_popup_text_without_a_rerun(ua, client, site):
    site.checkout_popups = ["phoneCheckModal"]
    q = _quote(client)
    first = _submit(client, q, "ua-replay")
    assert first.status_code == 409, first.text
    err = first.json()["error"]
    assert err["code"] == "checkout_blocked"
    # The popup's own text is in the message and the details.
    assert "text of phoneCheckModal" in err["message"]
    assert err["details"]["popups"] == [{"popup": "phoneCheckModal", "text": "text of phoneCheckModal"}]
    rec = main.checkout_ledger.get("ua-replay")
    assert rec["state"] == "failed:checkout_blocked" and rec["result"] == first.json()
    events = len(site.events)
    sms = site.ui["sms_sent"]
    again = _submit(client, q, "ua-replay")
    assert again.status_code == 409 and again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(site.events) == events and site.ui["sms_sent"] == sms and site.place_clicks == 0


def test_legacy_failed_record_without_a_body_replays_its_code(ua, client):
    q = _quote(client)
    ledger = main.checkout_ledger
    assert ledger.claim("old", q["quote_id"]) is None
    ledger.finish("old", "failed:checkout_blocked", 409, {})
    res = _submit(client, q, "old")
    assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked", res.text
    assert "new key" not in res.text.lower()


def test_stale_records_after_a_restart(ua, client):
    ledger = main.checkout_ledger
    q1, q2 = _quote(client), _quote(client)
    ledger.claim("stale-claim", q1["quote_id"])  # in_progress, no process running it
    res = _submit(client, q1, "stale-claim")
    assert res.status_code == 409 and res.json()["error"]["code"] == "attempt_interrupted", res.text
    ledger._db().execute("INSERT INTO submissions (idempotency_key, quote_id, state, created_at, updated_at)"
                         " VALUES ('stale-placing', ?, 'placing', 'x', 'x')", (q2["quote_id"],))
    res = _submit(client, q2, "stale-placing")
    assert res.status_code == 409 and res.json()["error"]["code"] == "order_outcome_unresolved", res.text


def test_duplicate_while_running_gets_202_in_progress_then_the_result(monkeypatch, ua, client, site):
    q = _quote(client)
    release = None
    calls = []

    async def slow_checkout(**kw):
        calls.append(kw)
        await release.wait()
        from security import APIException
        raise APIException(status_code=409, code="checkout_blocked", message="A popup opened: X.")

    monkeypatch.setattr(main.shufersal_driver, "checkout", slow_checkout)
    body = {"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"], "expected_cart_hash": q["cart_hash"],
            "dry_run": False, "confirm": True}
    headers = {**AUTH, "Idempotency-Key": "dup"}

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            first = asyncio.create_task(c.post("/checkout/submit", json=body, headers=headers))
            while not calls:
                await asyncio.sleep(0)
            dup = await c.post("/checkout/submit", json=body, headers=headers)
            release.set()
            first = await first
            after = await c.post("/checkout/submit", json=body, headers=headers)
            return first, dup, after

    first, dup, after = asyncio.run(scenario())
    assert dup.status_code == 202, dup.text
    d = dup.json()
    assert d["status"] == "in_progress" and d["retry_after_seconds"] > 0
    assert "same key" in d["message"] and "Do NOT retry with a new key" in d["message"]
    assert dup.headers["Retry-After"] == str(d["retry_after_seconds"])
    assert "idempotency_key_used" not in dup.text
    assert first.status_code == 409 and first.json()["error"]["code"] == "checkout_blocked"
    assert after.status_code == 409 and after.json() == first.json()
    assert len(calls) == 1  # the checkout ran once
    assert "dup" not in main.ACTIVE_SUBMIT_KEYS

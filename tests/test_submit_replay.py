"""Same-key /checkout/submit requests never re-run the checkout: a failed
attempt replays its stored error (code, message, details); a request that
arrives while the first is still running gets 202 in_progress (poll with the
same key), never "used, use a new key". A real order's first answer is 202
in_progress, sent before the checkout finishes; GET /checkout/submit/{key}
polls for the stored result."""
import asyncio
import json

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


def _body(q):
    return {"quote_id": q["quote_id"], "confirm_total_ils": q["total_ils"], "expected_cart_hash": q["cart_hash"],
            "dry_run": False, "confirm": True}


def test_first_response_is_202_before_the_checkout_finishes_then_polls_get_the_result(monkeypatch, ua, client):
    """The first answer for a fresh key is sent while the checkout is still running;
    same-key POSTs and GET /checkout/submit/{key} answer 202 until the stored result."""
    q = _quote(client)
    release, calls = None, []

    async def slow_checkout(**kw):
        calls.append(kw)
        await release.wait()
        from security import APIException
        raise APIException(status_code=409, code="checkout_blocked", message="A popup opened: X.")

    monkeypatch.setattr(main.shufersal_driver, "checkout", slow_checkout)
    body = json.dumps(_body(q)).encode()
    auth = [(b"authorization", AUTH["Authorization"].encode())]

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        sent, sent_body = [], asyncio.Event()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/checkout/submit", "raw_path": b"/checkout/submit",
                 "query_string": b"", "root_path": "", "server": ("t", 80), "client": ("c", 1),
                 "headers": auth + [(b"content-type", b"application/json"), (b"idempotency-key", b"bg"),
                                    (b"content-length", str(len(body)).encode())]}
        chunks = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if chunks:
                return chunks.pop()
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                sent_body.set()

        cycle = asyncio.create_task(main.app(scope, receive, send))
        await asyncio.wait_for(sent_body.wait(), 5)
        assert not release.is_set() and len(calls) == 1 and not cycle.done()  # still running
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            dup = await c.post("/checkout/submit", json=_body(q), headers={**AUTH, "Idempotency-Key": "bg"})
            polled = await c.get("/checkout/submit/bg", headers=AUTH)
            release.set()
            await asyncio.wait_for(cycle, 5)
            done_get = await c.get("/checkout/submit/bg", headers=AUTH)
            done_post = await c.post("/checkout/submit", json=_body(q), headers={**AUTH, "Idempotency-Key": "bg"})
        return sent, dup, polled, done_get, done_post

    sent, dup, polled, done_get, done_post = asyncio.run(scenario())
    start = sent[0]
    assert start["status"] == 202
    assert (b"retry-after", b"30") in start["headers"]
    first = json.loads(sent[1]["body"])
    assert first["status"] == "in_progress" and first["poll"] == "same request, same Idempotency-Key"
    assert first["retry_after"] == 30 and first["poll_url"] == "/checkout/submit/bg"
    for res in (dup, polled):
        assert res.status_code == 202 and res.headers["Retry-After"] == "30", res.text
        d = res.json()
        assert d["status"] == "in_progress" and d["retry_after"] == d["retry_after_seconds"] == 30
        assert "Do NOT retry with a new key" in d["message"] and "idempotency_key_used" not in res.text
    for res in (done_get, done_post):
        assert res.status_code == 409 and res.json()["error"]["code"] == "checkout_blocked", res.text
        assert res.headers["Idempotent-Replayed"] == "true"
    assert done_get.json() == done_post.json()
    assert len(calls) == 1  # the checkout ran once
    assert "bg" not in main.ACTIVE_SUBMIT_KEYS and not main.SUBMIT_TASKS


def test_get_result_needs_auth_and_a_known_key(ua, client):
    assert client.get("/checkout/submit/nope").status_code in (401, 403)
    res = client.get("/checkout/submit/nope", headers=AUTH)
    assert res.status_code == 404 and res.json()["error"]["code"] == "idempotency_key_unknown", res.text


def test_get_result_reports_restart_states(ua, client):
    ledger = main.checkout_ledger
    q1, q2 = _quote(client), _quote(client)
    ledger.claim("gone-claim", q1["quote_id"])  # in_progress, no process running it
    res = client.get("/checkout/submit/gone-claim", headers=AUTH)
    assert res.status_code == 409 and res.json()["error"]["code"] == "attempt_interrupted", res.text
    ledger._db().execute("INSERT INTO submissions (idempotency_key, quote_id, state, created_at, updated_at)"
                         " VALUES ('gone-placing', ?, 'placing', 'x', 'x')", (q2["quote_id"],))
    res = client.get("/checkout/submit/gone-placing", headers=AUTH)
    assert res.status_code == 409 and res.json()["error"]["code"] == "order_outcome_unresolved", res.text


def test_unexpected_error_in_the_background_is_stored_as_outcome_unknown(monkeypatch, ua, client):
    q = _quote(client)

    async def boom(**kw):
        raise RuntimeError("secret-ish detail 4580")

    monkeypatch.setattr(main.shufersal_driver, "checkout", boom)
    first = client.post("/checkout/submit", json=_body(q), headers={**AUTH, "Idempotency-Key": "boom"})
    assert first.status_code == 202 and first.json()["status"] == "in_progress"
    res = client.get("/checkout/submit/boom", headers=AUTH)
    assert res.status_code == 502 and res.json()["error"]["code"] == "upstream_error", res.text
    assert "4580" not in res.text
    assert main.checkout_ledger.get("boom")["state"] == "outcome_unknown"
    assert "boom" not in main.ACTIVE_SUBMIT_KEYS


def test_dry_run_stays_synchronous(ua, client):
    q = _quote(client)
    res = client.post("/checkout/submit", json={**_body(q), "dry_run": True, "confirm": False}, headers=AUTH)
    assert res.status_code == 200 and res.json()["dry_run"] is True and "poll" not in res.json(), res.text


def test_shutdown_waits_for_a_running_attempt(monkeypatch):
    order = []

    async def start():
        order.append("start")

    async def stop():
        order.append("stop")

    monkeypatch.setattr(main.shufersal_driver, "start", start)
    monkeypatch.setattr(main.shufersal_driver, "stop", stop)

    async def scenario():
        async def attempt():
            await asyncio.sleep(0.05)
            order.append("attempt_done")

        async with main.lifespan(main.app):
            task = asyncio.create_task(attempt())
            main.SUBMIT_TASKS.add(task)
            task.add_done_callback(main.SUBMIT_TASKS.discard)
        return order

    assert asyncio.run(scenario()) == ["start", "attempt_done", "stop"]

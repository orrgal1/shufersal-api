"""Posts /checkout/submit the way a client does under the asynchronous contract.

A real order (a new Idempotency-Key) is answered at once with 202 in_progress
and runs in the background; under TestClient the request returns only after
the background attempt finished, so one poll yields the stored final result.
Dry runs, validation errors and replays are answered directly and returned as is.
"""
import main


def submit(client, url="/checkout/submit", **kwargs):
    first = client.post(url, **kwargs)
    key = (kwargs.get("headers") or {}).get("Idempotency-Key")
    if first.status_code != 202 or "poll" not in first.json():
        return first
    body = first.json()
    assert body["status"] == "in_progress" and body["poll"] == "same request, same Idempotency-Key"
    assert body["retry_after"] == main.IN_PROGRESS_POLL_SECONDS == int(first.headers["Retry-After"])
    assert body["poll_url"] == f"/checkout/submit/{key}"
    assert key not in main.ACTIVE_SUBMIT_KEYS  # the background attempt has finished
    auth = {k: v for k, v in kwargs["headers"].items() if k.lower() == "authorization"}
    return client.get(body["poll_url"], headers=auth)

"""Post-click diagnostics for the unattended checkout.

After the place-order click the driver only reads the page; it never clicks
place-order again. This module records what the page does after that click
(URL changes, new frames and windows, the site's placeOrder XHR and its
answer) and, when the outcome is not a read confirmation, saves a masked
screenshot and a sanitized DOM snapshot under config.DIAG_DIR.

Never recorded: input values (CVV, OTP), query strings (the placeOrder URL
carries jwtPayme, the bank frame URL carries session tokens), script bodies.
"""
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import config

logger = logging.getLogger("shufersal_driver")

PLACE_ORDER_PATH = "/checkout/placeOrder"


def safe_url(url: Any) -> str:
    """scheme://host/path only: query strings and fragments can carry tokens."""
    try:
        u = urlparse(str(url or ""))
    except ValueError:
        return "?"
    if not u.scheme:
        return (u.path or "")[:200]
    return f"{u.scheme}://{u.netloc}{u.path}"[:200]


def is_place_order(url: Any) -> bool:
    return PLACE_ORDER_PATH in urlparse(str(url or "")).path


def summarize_place_order(status: Optional[int], text: Optional[str]) -> Dict[str, Any]:
    """The site's placeOrder answer, reduced to non-secret facts.

    Shapes (checkout bundle, store action placeOrder): {success, redirect} ->
    the page navigates to the confirmation; {allocationTimeout} -> a
    .generalErrorChat popup; {directCreditCard3DSLink | otherCreditCard3DSLink}
    -> the .creditCardOtpPopup with the bank's iframe; an array of
    {type, code | errorCode, ...} -> the site's inline / toast error handlers;
    anything else -> handle("serverPaymentError")."""
    out: Dict[str, Any] = {"http": status, "kind": "unparsed"}
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return out
    if isinstance(data, list):
        errors = []
        for item in data[:5]:
            if isinstance(item, dict):
                errors.append(str(item.get("errorCode") or item.get("code") or item.get("type") or "?")[:60])
        out.update(kind="errors", errors=errors)
        return out
    if not isinstance(data, dict):
        return out
    if data.get("success"):
        out.update(kind="success", redirect=safe_url(data.get("redirect")))
    elif data.get("allocationTimeout"):
        out["kind"] = "allocation_timeout"
    elif data.get("directCreditCard3DSLink") or data.get("otherCreditCard3DSLink"):
        out["kind"] = "three_ds"
    else:
        out.update(kind="server_payment_error", keys=sorted(str(k)[:40] for k in data.keys())[:12])
    return out


class PostClickMonitor:
    """Listens to the page and the browser context from just before the
    place-order click until the outcome is known. Every event is logged once
    (via `log`) and kept in `events`. Listeners are removed by detach()."""

    def __init__(self, page, context, log: Callable[[str], None]):
        self.page, self.context, self.log = page, context, log
        self.events: List[Dict[str, Any]] = []
        self.popups: List[Any] = []
        self.place_order_requests = 0
        self.place_order_after3ds: List[bool] = []
        self.place_order_results: List[Dict[str, Any]] = []
        self._pending = set()
        self._last_url = safe_url(getattr(page, "url", ""))
        self._handlers: List[tuple] = []
        self.started = time.monotonic()

    # ---- wiring ----
    def attach(self):
        for target, event, fn in ((self.page, "framenavigated", self._on_navigated),
                                  (self.page, "frameattached", self._on_frame_attached),
                                  (self.page, "popup", self._on_popup),
                                  (self.page, "request", self._on_request),
                                  (self.page, "response", self._on_response),
                                  (self.page, "requestfailed", self._on_request_failed),
                                  (self.context, "page", self._on_context_page)):
            on = getattr(target, "on", None)
            if on is None:
                continue
            try:
                on(event, fn)
                self._handlers.append((target, event, fn))
            except Exception:
                continue

    def detach(self):
        for target, event, fn in self._handlers:
            remove = getattr(target, "remove_listener", None)
            try:
                if remove:
                    remove(event, fn)
            except Exception:
                pass
        self._handlers = []
        for task in list(self._pending):
            task.cancel()

    def _note(self, kind: str, detail: str):
        t = round(time.monotonic() - self.started, 1)
        self.events.append({"t": t, "kind": kind, "detail": detail})
        self.log(f"after the click (+{t}s): {kind}: {detail}")

    # ---- polled from the outcome loop (works where events are not delivered) ----
    def check_url(self, url: str):
        u = safe_url(url)
        if u and u != self._last_url:
            self._last_url = u
            self._note("page URL", u)

    # ---- event handlers (sync; never raise) ----
    def _on_navigated(self, frame):
        try:
            if frame == getattr(self.page, "main_frame", None):
                self.check_url(frame.url)
            else:
                self._note("frame navigated", safe_url(frame.url))
        except Exception:
            pass

    def _on_frame_attached(self, frame):
        try:
            self._note("frame attached", safe_url(frame.url) or "(blank)")
        except Exception:
            pass

    def _on_popup(self, popup):
        try:
            if popup not in self.popups:
                self.popups.append(popup)
                self._note("popup window opened", safe_url(popup.url) or "(blank)")
        except Exception:
            pass

    def _on_context_page(self, page):
        if page is self.page:
            return
        self._on_popup(page)

    def _on_request(self, request):
        try:
            if not is_place_order(request.url):
                return
            self.place_order_requests += 1
            after3ds = parse_qs(urlparse(request.url).query).get("after3ds", [""])[0] == "true"
            self.place_order_after3ds.append(after3ds)
            self._note("placeOrder request sent", f"after3ds={'true' if after3ds else 'false'}")
        except Exception:
            pass

    def _on_request_failed(self, request):
        try:
            if is_place_order(request.url):
                failure = getattr(request, "failure", None)
                self._note("placeOrder request failed", str(failure or "")[:120])
                self.place_order_results.append({"http": None, "kind": "request_failed"})
        except Exception:
            pass

    def _on_response(self, response):
        try:
            if not is_place_order(response.url):
                return
            import asyncio
            task = asyncio.ensure_future(self._read_response(response))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        except Exception:
            pass

    async def _read_response(self, response):
        status = getattr(response, "status", None)
        try:
            text = await response.text()
        except Exception:
            text = None
        summary = summarize_place_order(status, text)
        self.place_order_results.append(summary)
        self._note("placeOrder answered", json.dumps(summary, ensure_ascii=False))

    # ---- derived facts ----
    def in_flight(self) -> bool:
        return self.place_order_requests > len(self.place_order_results)

    def last_result(self) -> Optional[Dict[str, Any]]:
        return self.place_order_results[-1] if self.place_order_results else None


# The page's DOM with every input value, script body and URL query removed.
DOM_SNAPSHOT_JS = """() => { /*ua-dom-snapshot*/
    const root = document.documentElement.cloneNode(true);
    root.querySelectorAll('script, noscript, template').forEach(e => e.remove());
    root.querySelectorAll('input, textarea, select, option').forEach(e => {
        e.removeAttribute('value'); e.removeAttribute('selected'); e.removeAttribute('checked');
        if (e.tagName === 'TEXTAREA') e.textContent = '';
    });
    const strip = (v) => { try { const u = new URL(v, location.href); return u.origin + u.pathname; } catch (x) { return ''; } };
    root.querySelectorAll('[src], [href], [action], [data-src]').forEach(e => {
        for (const a of ['src', 'href', 'action', 'data-src']) {
            const v = e.getAttribute(a);
            if (v && !v.startsWith('#') && !v.startsWith('javascript:')) e.setAttribute(a, strip(v));
        }
    });
    root.querySelectorAll('meta[name*="csrf" i], meta[name*="token" i]').forEach(e => e.remove());
    return '<!-- ' + strip(location.href) + ' -->\\n' + root.outerHTML;
}"""


def _diag_dir() -> str:
    path = config.DIAG_DIR
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _write_private(path: str, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


async def _frame_html(frame) -> Optional[str]:
    try:
        html = await frame.evaluate(DOM_SNAPSHOT_JS)
        return html if isinstance(html, str) else f"<!-- unexpected snapshot type {type(html).__name__} -->"
    except Exception as e:
        return f"<!-- unreadable: {type(e).__name__} -->"


async def save_diagnostics(page, tag: str, monitor: Optional[PostClickMonitor] = None,
                           extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Saves <stamp>-<tag>.png (inputs masked), .html (sanitized DOM of the
    page, its frames and any popup windows) and .json (post-click events).
    Returns the common path prefix, or None if nothing could be saved."""
    try:
        directory = _diag_dir()
    except OSError as e:
        logger.warning(f"unattended checkout: diagnostics dir unavailable ({type(e).__name__})")
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in tag)[:40]
    prefix = os.path.join(directory, f"{stamp}-{safe_tag}")
    saved = []
    pages = [("page", page)] + [(f"popup{i + 1}", p) for i, p in enumerate(monitor.popups if monitor else [])]

    for name, p in pages:
        try:
            if p is None or (hasattr(p, "is_closed") and p.is_closed()):
                continue
        except Exception:
            continue
        shot = f"{prefix}-{name}.png" if name != "page" else f"{prefix}.png"
        try:
            masks = []
            locator = getattr(p, "locator", None)
            if locator:
                masks.append(p.locator("input, textarea"))
            data = await p.screenshot(full_page=True, mask=masks) if masks else await p.screenshot(full_page=True)
            _write_private(shot, data)
            saved.append(shot)
        except Exception as e:
            logger.info(f"unattended checkout: screenshot of {name} failed ({type(e).__name__})")

    parts = []
    for name, p in pages:
        frames = list(getattr(p, "frames", None) or [])
        main = getattr(p, "main_frame", None)
        if not frames:
            parts.append(f"<!-- ===== {name} ===== -->\n" + (await _frame_html(p) or ""))
            continue
        for i, frame in enumerate(frames[:12]):
            label = f"{name} main frame" if frame == main else f"{name} frame {i}: {safe_url(getattr(frame, 'url', ''))}"
            parts.append(f"<!-- ===== {label} ===== -->\n" + (await _frame_html(frame) or ""))
    try:
        _write_private(prefix + ".html", "\n\n".join(parts).encode("utf-8"))
        saved.append(prefix + ".html")
    except OSError as e:
        logger.info(f"unattended checkout: DOM snapshot not written ({type(e).__name__})")

    record = {"tag": tag, "url": safe_url(getattr(page, "url", "")),
              "events": monitor.events if monitor else [],
              "place_order_results": monitor.place_order_results if monitor else [],
              **(extra or {})}
    try:
        _write_private(prefix + ".json", json.dumps(record, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
        saved.append(prefix + ".json")
    except OSError:
        pass
    if not saved:
        return None
    logger.info(f"unattended checkout: diagnostics saved: {prefix}.* ({len(saved)} files)")
    return prefix

"""One-time codes from Or's local sms-api (read-only macOS Messages).

A code is accepted only from a message that
  - arrived after the step was triggered (rowid above the baseline taken before
    the trigger AND received_at no earlier than the trigger time),
  - is at most SMS_CODE_MAX_AGE_SECONDS old,
  - comes from the expected sender,
  - for 3-D Secure: names the expected card, the expected amount and a
    Shufersal merchant when the text includes them.
Codes are never logged or returned anywhere except to the caller that types them.
"""
import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import config

logger = logging.getLogger("shufersal_driver")

# Clock skew allowed between the phone's timestamp and this Mac's clock.
SKEW = timedelta(seconds=2)

# Isracard 3-D Secure text (seen in Or's Messages, 2026-10-14):
#   "זיהינו כעת עסקה בסכום 1234.00 ILS ב <merchant> בכרטיסך 1234, ...
#    הקוד שלך להשלמת העסקה הינו 123456"  (plus a Russian copy).
ISSUER_CODE_RES = [re.compile(r"הקוד שלך להשלמת העסקה הינו\s*(\d{4,8})"),
                   re.compile(r"Код для завершения оплаты\s*(\d{4,8})")]
ISSUER_AMOUNT_RES = [re.compile(r"בסכום\s*([\d,]+(?:\.\d+)?)\s*(?:ILS|₪|ש\"ח)"),
                     re.compile(r"на сумму\s*([\d,]+(?:\.\d+)?)\s*ILS")]
ISSUER_LAST4_RES = [re.compile(r"בכרטיסך\s*(\d{4})"), re.compile(r"оканчивается на\s*(\d{4})")]
ISSUER_MERCHANT_RES = [re.compile(r"ILS\s+ב\s*(.+?)\s+בכרטיסך"), re.compile(r"на сайте\s+(.+?)\s+на сумму")]


class SmsCodeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _http_messages(limit: int = 30) -> List[Dict[str, Any]]:
    url = config.SMS_API_URL.rstrip("/") + "/messages?" + urllib.parse.urlencode({"limit": limit})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.sms_api_token()}"})
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode("utf-8")).get("messages") or []


def _first(res_list, text) -> Optional[str]:
    for r in res_list:
        m = r.search(text)
        if m:
            return m.group(1)
    return None


def _num(s: Optional[str]) -> Optional[float]:
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def check_login_code(text: str) -> Dict[str, Any]:
    """Shufersal's re-auth SMS: exactly one standalone 6-digit number (the site's
    input takes 6 digits)."""
    codes = set(re.findall(r"(?<![\d.,])(\d{6})(?![\d.,])", text or ""))
    if len(codes) != 1:
        return {"ok": False, "reason": "no single 6-digit code in the text"}
    return {"ok": True, "code": codes.pop()}


def check_issuer_code(text: str, amount: Optional[float], last4: Optional[str]) -> Dict[str, Any]:
    code = _first(ISSUER_CODE_RES, text or "")
    if not code:
        return {"ok": False, "reason": "no issuer code phrase in the text"}
    got_amount = _num(_first(ISSUER_AMOUNT_RES, text))
    if got_amount is not None and (amount is None or abs(got_amount - amount) > 0.01):
        return {"ok": False, "reason": f"amount {got_amount} does not match the order total"}
    got_last4 = _first(ISSUER_LAST4_RES, text)
    if got_last4 is not None and last4 and got_last4 != last4:
        return {"ok": False, "reason": "card digits do not match the saved card"}
    merchant = _first(ISSUER_MERCHANT_RES, text)
    if merchant is not None and not any(h in merchant.lower() for h in config.THREE_DS_MERCHANT_HINTS):
        return {"ok": False, "reason": f"merchant '{merchant[:40]}' is not Shufersal"}
    return {"ok": True, "code": code}


class SmsCodeReader:
    def __init__(self, fetch: Optional[Callable[[], List[Dict[str, Any]]]] = None):
        self._fetch = fetch or _http_messages

    async def _messages(self) -> List[Dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._fetch)
        except Exception as e:
            raise SmsCodeError("sms_unavailable", f"The sms-api could not be read ({type(e).__name__}).")

    async def baseline(self) -> Dict[str, Any]:
        """Call BEFORE triggering the step that sends the code."""
        msgs = await self._messages()
        top = max((int(m.get("rowid") or 0) for m in msgs), default=0)
        return {"rowid": top, "at": datetime.now(timezone.utc)}

    async def wait_for_code(self, kind: str, base: Dict[str, Any], *, amount: Optional[float] = None,
                            last4: Optional[str] = None, timeout: Optional[float] = None) -> str:
        """kind: "login" (Shufersal re-auth) or "3ds" (card issuer)."""
        sender = (config.LOGIN_SMS_SENDER if kind == "login" else config.CARD_SMS_SENDER).casefold()
        deadline = time.monotonic() + (config.SMS_WAIT_SECONDS if timeout is None else timeout)
        rejected = set()
        while True:
            now = datetime.now(timezone.utc)
            accepted = {}
            for m in await self._messages():
                rowid = int(m.get("rowid") or 0)
                if rowid <= base["rowid"] or str(m.get("sender") or "").casefold() != sender:
                    continue
                try:
                    received = datetime.fromisoformat(str(m.get("received_at")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if received < base["at"] - SKEW or now - received > timedelta(seconds=config.SMS_CODE_MAX_AGE_SECONDS):
                    reason = "older than the trigger or the 5-minute window"
                    verdict = {"ok": False, "reason": reason}
                elif kind == "login":
                    verdict = check_login_code(m.get("text") or "")
                else:
                    verdict = check_issuer_code(m.get("text") or "", amount, last4)
                if verdict["ok"]:
                    accepted[rowid] = verdict["code"]
                elif rowid not in rejected:
                    rejected.add(rowid)
                    logger.warning(f"unattended: ignored a {kind} SMS (row {rowid}): {verdict['reason']}")
            if len(set(accepted.values())) > 1:
                raise SmsCodeError("sms_code_ambiguous", f"Several different fresh {kind} codes arrived.")
            if accepted:
                logger.info(f"unattended: fresh {kind} code received (row {max(accepted)})")
                return accepted[max(accepted)]
            if time.monotonic() >= deadline:
                raise SmsCodeError("sms_code_timeout", f"No valid {kind} code arrived by SMS in time.")
            await asyncio.sleep(config.SMS_POLL_SECONDS)

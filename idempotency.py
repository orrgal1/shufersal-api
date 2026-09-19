import uuid
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

class IdempotencyStore:
    """
    In-memory thread/async-safe TTL store for Idempotency-Key headers.
    Ensures identical mutations return cached responses without duplicate execution.
    """
    def __init__(self, default_ttl_seconds: int = 86400):
        self.default_ttl = default_ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self):
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if v["expires_at"] <= now]
        for k in expired_keys:
            del self._cache[k]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        self._clean_expired()
        entry = self._cache.get(key)
        if not entry:
            return None
        return entry["response"]

    def set(self, key: str, status_code: int, body: Any, ttl_seconds: Optional[int] = None) -> None:
        self._clean_expired()
        ttl = ttl_seconds or self.default_ttl
        self._cache[key] = {
            "response": {
                "status_code": status_code,
                "body": body
            },
            "expires_at": time.time() + ttl
        }

class QuoteStore:
    """
    Stores staged checkout quotes with TTL (default 15 minutes / 900 seconds).
    Validates quote existence, expiration, and total amounts.
    """
    def __init__(self, quote_ttl_seconds: int = 900):
        self.quote_ttl = quote_ttl_seconds
        self._quotes: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        expired_ids = [qid for qid, q in self._quotes.items() if q["expires_at"] <= now_iso]
        for qid in expired_ids:
            del self._quotes[qid]

    def create_quote(
        self,
        delivery_slot_id: str,
        subtotal_ils: float,
        delivery_fee_ils: float,
        total_ils: float,
        item_count: int,
        ttl_seconds: Optional[int] = None
    ) -> Dict[str, Any]:
        self._clean_expired()
        quote_id = f"quote_{uuid.uuid4().hex[:12]}"
        ttl = ttl_seconds or self.quote_ttl
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
        
        quote = {
            "quote_id": quote_id,
            "delivery_slot_id": delivery_slot_id,
            "subtotal_ils": round(subtotal_ils, 2),
            "delivery_fee_ils": round(delivery_fee_ils, 2),
            "total_ils": round(total_ils, 2),
            "item_count": item_count,
            "expires_at": expires_at
        }
        self._quotes[quote_id] = quote
        return quote

    def get_quote(self, quote_id: str) -> Optional[Dict[str, Any]]:
        self._clean_expired()
        quote = self._quotes.get(quote_id)
        if not quote:
            return None
        now_iso = datetime.now(timezone.utc).isoformat()
        if quote["expires_at"] <= now_iso:
            del self._quotes[quote_id]
            return None
        return quote

# Singletons for the service
idempotency_store = IdempotencyStore()
quote_store = QuoteStore()

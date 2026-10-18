"""Durable, single-use Idempotency-Keys for order submission.

A key is claimed before the order is attempted and is never released, so a
retry, a crash or a restart can never submit the same key twice. Failed
attempts keep their key burned; the caller must use a new key.

Each quote also allows only one real placement attempt: after any attempt
(including outcome_unknown or a pending 3-D Secure challenge) a new key on the
same quote is refused, so placing again needs a fresh quote and cart check.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class CheckoutLedger:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS submissions ("
                " idempotency_key TEXT PRIMARY KEY, quote_id TEXT NOT NULL, state TEXT NOT NULL,"
                " http_status INTEGER, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_quote ON submissions (quote_id)")
        return self._conn

    def claim(self, key: str, quote_id: str) -> Optional[Dict[str, Any]]:
        """Claims the key for this quote. Returns None when newly claimed, the
        existing record when the key was used before, or {"state": "quote_consumed"}
        when another key already attempted this quote."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            db = self._db()
            try:
                db.execute(
                    "INSERT INTO submissions (idempotency_key, quote_id, state, created_at, updated_at)"
                    " VALUES (?, ?, 'in_progress', ?, ?)", (key, quote_id, now, now))
                return None
            except sqlite3.IntegrityError:
                existing = self.get(key)
                return existing if existing else {"state": "quote_consumed", "quote_id": quote_id}

    def finish(self, key: str, state: str, http_status: int, result: Dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db().execute(
                "UPDATE submissions SET state=?, http_status=?, result=?, updated_at=? WHERE idempotency_key=?",
                (state, http_status, json.dumps(result, ensure_ascii=False), now, key))

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._db().execute(
            "SELECT idempotency_key, quote_id, state, http_status, result FROM submissions WHERE idempotency_key=?",
            (key,)).fetchone()
        if not row:
            return None
        return {"idempotency_key": row[0], "quote_id": row[1], "state": row[2], "http_status": row[3],
                "result": json.loads(row[4]) if row[4] else None}

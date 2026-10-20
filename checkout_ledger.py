"""Durable, single-use Idempotency-Keys for order submission.

A key is claimed before the order is attempted and is never released, so a
retry, a crash or a restart can never submit the same key twice. Failed
attempts keep their key burned; the caller must use a new key.

Each quote also allows only one real placement attempt: after any attempt
(including outcome_unknown or a pending 3-D Secure challenge) a new key on the
same quote is refused, so placing again needs a fresh quote and cart check.

An attempt whose outcome is ambiguous (the place-order click happened but no
confirmation page was read, or the process died mid-placement) blocks every
further order until Or resolves it:

    venv/bin/python checkout_ledger.py list
    venv/bin/python checkout_ledger.py resolve <idempotency_key> placed|not_placed
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# States whose real-world outcome is not known: an order may or may not exist.
# "placing" is written just before the place-order click, so a crash after the
# click still blocks.
UNRESOLVED_STATES = ("placing", "outcome_unknown", "placed_unconfirmed", "three_ds_required")


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
        existing record when the key was used before, {"state": "quote_consumed"}
        when another key already attempted this quote, or
        {"state": "blocked_unresolved", "unresolved": [...]} while an earlier
        attempt's outcome is unresolved."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            db = self._db()
            existing = self.get(key)
            if existing:
                return existing
            if db.execute("SELECT 1 FROM submissions WHERE quote_id=?", (quote_id,)).fetchone():
                return {"state": "quote_consumed", "quote_id": quote_id}
            unresolved = self.unresolved()
            if unresolved:
                return {"state": "blocked_unresolved", "quote_id": quote_id,
                        "unresolved": [u["idempotency_key"] for u in unresolved]}
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

    def mark(self, key: str, state: str):
        """Records an intermediate state (e.g. "placing" right before the click)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db().execute("UPDATE submissions SET state=?, updated_at=? WHERE idempotency_key=?",
                               (state, now, key))

    def unresolved(self) -> List[Dict[str, Any]]:
        marks = ",".join("?" for _ in UNRESOLVED_STATES)
        rows = self._db().execute(
            f"SELECT idempotency_key, quote_id, state, updated_at FROM submissions WHERE state IN ({marks})"
            " ORDER BY updated_at", UNRESOLVED_STATES).fetchall()
        return [{"idempotency_key": r[0], "quote_id": r[1], "state": r[2], "updated_at": r[3]} for r in rows]

    def resolve(self, key: str, resolution: str) -> bool:
        """Or's manual resolution of an ambiguous attempt after checking the site."""
        if resolution not in ("placed", "not_placed"):
            raise ValueError("resolution must be placed or not_placed")
        rec = self.get(key)
        if not rec or rec["state"] not in UNRESOLVED_STATES:
            return False
        self.mark(key, f"resolved_{resolution}")
        return True

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._db().execute(
            "SELECT idempotency_key, quote_id, state, http_status, result FROM submissions WHERE idempotency_key=?",
            (key,)).fetchone()
        if not row:
            return None
        return {"idempotency_key": row[0], "quote_id": row[1], "state": row[2], "http_status": row[3],
                "result": json.loads(row[4]) if row[4] else None}


if __name__ == "__main__":
    import sys
    import config
    ledger = CheckoutLedger(config.CHECKOUT_LEDGER)
    args = sys.argv[1:]
    if args[:1] == ["list"]:
        for u in ledger.unresolved():
            print(f"{u['idempotency_key']}\t{u['state']}\t{u['updated_at']}")
    elif args[:1] == ["resolve"] and len(args) == 3:
        print("resolved" if ledger.resolve(args[1], args[2]) else "no unresolved attempt with that key")
    else:
        print("usage: checkout_ledger.py list | resolve <idempotency_key> placed|not_placed")
        sys.exit(2)

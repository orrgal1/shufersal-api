import os

# Keep the durable checkout ledger out of the working tree during tests.
os.environ.setdefault("SHUFERSAL_CHECKOUT_LEDGER", ":memory:")


import pytest


@pytest.fixture(autouse=True)
def fresh_checkout_ledger(monkeypatch):
    """Each test gets its own ledger, so an unresolved attempt in one test
    cannot block orders in the next."""
    import main
    from checkout_ledger import CheckoutLedger
    ledger = CheckoutLedger(":memory:")
    monkeypatch.setattr(main, "checkout_ledger", ledger)
    return ledger


@pytest.fixture(autouse=True)
def diag_dir(monkeypatch, tmp_path):
    """Post-click diagnostics go to a per-test directory, never the working tree."""
    import config
    path = tmp_path / "diag"
    monkeypatch.setattr(config, "DIAG_DIR", str(path))
    return path

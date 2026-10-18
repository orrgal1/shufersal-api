import os

# Keep the durable checkout ledger out of the working tree during tests.
os.environ.setdefault("SHUFERSAL_CHECKOUT_LEDGER", ":memory:")

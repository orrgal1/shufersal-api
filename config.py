import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Authentication token (static fallback)
SHUFERSAL_API_TOKEN = os.getenv("SHUFERSAL_API_TOKEN", "") or (
    Path(".api_token").read_text(encoding="utf-8").strip()
    if Path(".api_token").exists() else ""
)

# JWT Secret & Algorithm
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    secret_file = Path(".jwt_secret")
    if secret_file.exists():
        JWT_SECRET = secret_file.read_text().strip()
    else:
        JWT_SECRET = secrets.token_hex(32)
        try:
            secret_file.write_text(JWT_SECRET)
        except Exception:
            pass

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Directory where Playwright keeps persistent browser state
SHUFERSAL_PROFILE_DIR = os.getenv("SHUFERSAL_PROFILE_DIR", "./user_data")

# HTTP Server bind configuration
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# Headless mode for Playwright browser (must be headless)
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("true", "1", "yes")

# Mock mode for testing/demo without active Playwright session
MOCK_MODE = os.getenv("MOCK_MODE", "false").strip().lower() in ("true", "1", "yes")

# Base URL for Shufersal
SHUFERSAL_BASE_URL = os.getenv("SHUFERSAL_BASE_URL", "https://www.shufersal.co.il")

# Loaded from the local .env at service startup. Never accept it from API callers.
SHUFERSAL_CVV = os.getenv("SHUFERSAL_CVV", "")

# Real order placement stays off until Or has watched a supervised dry run.
# Dry runs work regardless; placing an order also needs confirm=true and a
# fresh Idempotency-Key per order.
LIVE_CHECKOUT_ENABLED = os.getenv("SHUFERSAL_LIVE_CHECKOUT_ENABLED", "false").strip().lower() in ("true", "1", "yes")

# Durable record of every order-submission Idempotency-Key (survives restarts).
CHECKOUT_LEDGER = os.getenv("SHUFERSAL_CHECKOUT_LEDGER", "./.runtime/checkout_ledger.sqlite3")

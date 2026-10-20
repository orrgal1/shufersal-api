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

# Watched checkout: the browser is relaunched visible for the checkout step and
# the service waits this long for Or to finish payment and place the order.
WATCH_TIMEOUT_SECONDS = int(os.getenv("SHUFERSAL_WATCH_TIMEOUT_SECONDS", "900"))
WATCH_POLL_SECONDS = float(os.getenv("SHUFERSAL_WATCH_POLL_SECONDS", "3"))

# Unattended checkout: with watched=false and BOTH this flag and
# SHUFERSAL_LIVE_CHECKOUT_ENABLED on, the service itself clicks "מעבר לקופה",
# passes the SMS re-auth, pays with the saved card and handles 3-D Secure, reading
# the one-time codes from the local sms-api. Off by default.
UNATTENDED_CHECKOUT = os.getenv("SHUFERSAL_UNATTENDED_CHECKOUT", "false").strip().lower() in ("true", "1", "yes")
# Hard cap on the confirmed total of an unattended order (ILS).
MAX_ORDER_ILS = float(os.getenv("SHUFERSAL_MAX_ORDER_ILS", "600"))

# Local read-only sms-api (macOS Messages). It accepts the gateway's shared
# bearer token, which the gateway launcher passes to this service as
# SHUFERSAL_API_TOKEN.
SMS_API_URL = os.getenv("SHUFERSAL_SMS_API_URL", "http://127.0.0.1:8004")


def sms_api_token() -> str:
    for name in ("SMS_API_TOKEN", "SHARED_BEARER_TOKEN"):
        if os.getenv(name, "").strip():
            return os.getenv(name).strip()
    token_file = Path(os.getenv("SHUFERSAL_SMS_API_TOKEN_FILE", "../sms-api/.api_token"))
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return os.getenv("SHUFERSAL_API_TOKEN", "").strip()


# Expected SMS senders: Shufersal's re-auth code and the card issuer's 3-D Secure code.
LOGIN_SMS_SENDER = os.getenv("SHUFERSAL_LOGIN_SMS_SENDER", "SHUFERSAL")
CARD_SMS_SENDER = os.getenv("SHUFERSAL_CARD_SMS_SENDER", "Isracard")
# The merchant name in the issuer's 3-D Secure text must contain one of these (case-insensitive).
THREE_DS_MERCHANT_HINTS = [h.strip().lower() for h in
                           os.getenv("SHUFERSAL_3DS_MERCHANT_HINTS", "shufersal,שופרסל").split(",") if h.strip()]
SMS_CODE_MAX_AGE_SECONDS = 300
SMS_WAIT_SECONDS = float(os.getenv("SHUFERSAL_SMS_WAIT_SECONDS", "180"))
SMS_POLL_SECONDS = float(os.getenv("SHUFERSAL_SMS_POLL_SECONDS", "3"))
# How long to wait after the place-order click for the confirmation page / 3DS.
UNATTENDED_RESULT_SECONDS = float(os.getenv("SHUFERSAL_UNATTENDED_RESULT_SECONDS", "300"))
UNATTENDED_STEP_SECONDS = float(os.getenv("SHUFERSAL_UNATTENDED_STEP_SECONDS", "60"))
UNATTENDED_POLL_SECONDS = float(os.getenv("SHUFERSAL_UNATTENDED_POLL_SECONDS", "1"))

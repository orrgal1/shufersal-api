import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Authentication token (static fallback)
SHUFERSAL_API_TOKEN = os.getenv("SHUFERSAL_API_TOKEN", "")

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

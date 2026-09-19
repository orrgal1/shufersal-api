import re
import sys
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from fastapi import Request, status
from fastapi.security.utils import get_authorization_scheme_param
import jwt

import config

CARD_REGEX = re.compile(r'\b(?:\d[ -]*?){13,19}\b')
CVV_REGEX = re.compile(r'(?i)(cvv|cvc|cvn|security_code)[\s:=]+["\']?(\d{3,4})["\']?')
JWT_REGEX = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')

def generate_jwt_token(
    subject: str = "remote-agent",
    expires_days: int = 365,
    extra_claims: Optional[Dict[str, Any]] = None
) -> str:
    """Generates a signed JWT Bearer token for remote agents."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iss": "shufersal-local-api",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=expires_days)).timestamp())
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)

def sanitize_log_message(msg: str) -> str:
    """Sanitizes log messages by masking potential card numbers, CVVs, and JWT signatures."""
    cleaned = CARD_REGEX.sub("[REDACTED_CARD]", msg)
    cleaned = CVV_REGEX.sub(r'\1="[REDACTED_CVV]"', cleaned)
    cleaned = JWT_REGEX.sub("[REDACTED_JWT]", cleaned)
    return cleaned

def log_mutation(
    method: str,
    path: str,
    status_code: int,
    idempotency_key: Optional[str] = None,
    details: Optional[dict] = None
) -> None:
    """
    Logs every mutation to stdout with timestamp.
    Guarantees CVV or full card details are never logged.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    safe_details = {}
    if details:
        for k, v in details.items():
            if k.lower() in ("cvv", "cvc", "card_number", "pan", "full_card"):
                continue
            safe_details[k] = v

    log_entry = {
        "timestamp": now_iso,
        "type": "mutation",
        "method": method,
        "path": path,
        "status_code": status_code,
        "idempotency_key": idempotency_key,
        "details": safe_details
    }
    raw_str = json.dumps(log_entry, ensure_ascii=False)
    sys.stdout.write(sanitize_log_message(raw_str) + "\n")
    sys.stdout.flush()

class APIException(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)

# Endpoints exempt from Bearer authentication for public inspection
PUBLIC_PATHS = {"/openapi.json", "/docs", "/redoc", "/favicon.ico"}

async def verify_bearer_token(request: Request) -> Dict[str, Any]:
    """
    Validates Authorization: Bearer <JWT_TOKEN>.
    Exempts /openapi.json and /docs so external agents can inspect the API schemas.
    Raises APIException(401) if invalid, expired, or missing.
    """
    if request.url.path in PUBLIC_PATHS:
        return {"sub": "public"}

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="unauthorized",
            message="Missing Authorization header"
        )
    
    scheme, token = get_authorization_scheme_param(auth_header)
    if scheme.lower() != "bearer" or not token:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="unauthorized",
            message="Invalid authorization scheme; Bearer token required"
        )
    
    # 1. Attempt JWT decoding
    try:
        claims = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]}
        )
        return claims
    except jwt.ExpiredSignatureError:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="token_expired",
            message="JWT token has expired"
        )
    except jwt.InvalidTokenError:
        pass

    # 2. Fallback to static token if configured
    if config.SHUFERSAL_API_TOKEN and token == config.SHUFERSAL_API_TOKEN:
        return {"sub": "static-token"}

    raise APIException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code="unauthorized",
        message="Invalid JWT signature or unauthorized token"
    )

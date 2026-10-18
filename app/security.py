"""
Security helpers — password hashing and session tokens.

- Passwords are stored only as bcrypt hashes. The raw password is never saved and can't
  be recovered — only reset. (Admin can reset, never read.)
- Sessions use signed JWT tokens. A token proves "this request is from user X" without
  the server storing session state. Tokens expire, so a stolen/old one stops working.

Uses the `bcrypt` library directly (not via passlib) to avoid a known passlib/bcrypt
version-detection bug. bcrypt caps input at 72 bytes, so we hard-truncate first.
"""
import os
import datetime as dt
import bcrypt
from jose import jwt, JWTError

# Secret used to sign tokens. MUST be set in production (a long random string).
# Falls back to a dev-only default for local testing so the app runs out of the box.
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-change-me-in-production")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "720"))  # 30 days default


def _to_bytes(raw: str) -> bytes:
    # bcrypt hard limit is 72 bytes; truncate to stay safely within it.
    return raw.encode("utf-8")[:72]


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(_to_bytes(raw), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bytes(raw), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: str) -> str:
    now = dt.datetime.utcnow()
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + dt.timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str):
    """Returns the user_id if the token is valid and unexpired, else None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

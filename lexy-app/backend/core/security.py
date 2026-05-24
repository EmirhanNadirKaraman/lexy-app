import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

SECRET_KEY: str = os.getenv("SECRET_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set in .env")

# bcrypt silently ignores any bytes past the 72nd (S10). We cap new passwords at
# this length at the validation boundary (see models.schemas.RegisterRequest) so
# a user's password is never silently truncated — rather than pre-hashing, which
# would be a broader password-hashing migration.
BCRYPT_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> str:
    """Decode a JWT and return the user_id (sub claim). Raises jwt.InvalidTokenError on failure."""
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return payload["sub"]

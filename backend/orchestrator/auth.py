import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, Response
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from database import get_db
from models import User, UserSession

load_dotenv()


# ---------- Settings ----------

SESSION_COOKIE_NAME = "session_id"

# How long a login lasts
SESSION_MINUTES = int(os.getenv("SESSION_MINUTES", "12"))  # Default is 12 minutes

# Local (http):                   COOKIE_SECURE=false  COOKIE_SAMESITE=lax
# Deployed, frontend and API on
# different sites (Vercel+Render): COOKIE_SECURE=true   COOKIE_SAMESITE=none
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").lower()

if COOKIE_SAMESITE not in ("lax", "strict", "none"):
    raise ValueError("COOKIE_SAMESITE must be lax, strict or none")

if COOKIE_SAMESITE == "none" and not COOKIE_SECURE:
    raise ValueError("COOKIE_SAMESITE=none requires COOKIE_SECURE=true")

# Frontend addresses allowed to call the API (used for CORS and Origin checks)
ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]


# ---------- Passwords ----------

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


# ---------- Sessions ----------

def _now() -> datetime:
    # Naive UTC, so it works the same in SQLite and Postgres
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user_id: str) -> str:
    """Create a new session and return the value to put in the cookie."""

    # Remove this user's old, expired sessions
    db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.expires_at < _now(),
    ).delete()

    token = secrets.token_urlsafe(32)

    db.add(UserSession(
        id=_hash_token(token),
        user_id=user_id,
        expires_at=_now() + timedelta(days=SESSION_DAYS),
    ))
    db.commit()

    return token


def get_session_user_id(db: Session, token: str | None) -> str | None:
    """Return the user id for a cookie value, or None if it is not valid."""

    if not token:
        return None

    session = db.query(UserSession).filter(
        UserSession.id == _hash_token(token)
    ).first()

    if not session:
        return None

    if session.expires_at < _now():
        db.delete(session)
        db.commit()
        return None

    return session.user_id


def delete_session(db: Session, token: str | None) -> None:
    if not token:
        return

    db.query(UserSession).filter(
        UserSession.id == _hash_token(token)
    ).delete()
    db.commit()


# ---------- Cookie ----------

def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,  # JavaScript cannot read it
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=COOKIE_SECURE,
        httponly=True,
        samesite=COOKIE_SAMESITE,
    )


# ---------- Current user (for REST routes) ----------

def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    user_id = get_session_user_id(
        db,
        request.cookies.get(SESSION_COOKIE_NAME),
    )

    user = (
        db.query(User).filter(User.id == user_id).first()
        if user_id
        else None
    )

    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")

    return user
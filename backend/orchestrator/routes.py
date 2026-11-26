import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from database import get_db
from models import User
from auth import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    create_session,
    delete_session,
    get_current_user,
    hash_password,
    set_session_cookie,
    verify_password,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class SignupRequest(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def user_info(user: User) -> dict:
    return {
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
    }


@router.get("/health")
def health_check():
    return {"status": "healthy"}


@router.post("/signup")
def signup(
    data: SignupRequest,
    response: Response,
    db: Session = Depends(get_db),
):

    email = str(data.email).lower()

    existing_user = db.query(User).filter(
        User.email == email
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Email is already registered."
        )

    user = User(
        id=str(uuid.uuid4()),
        username=data.username.strip(),
        email=email,
        password_hash=hash_password(data.password),
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    # Log the new user in: start a session and set the cookie
    set_session_cookie(response, create_session(db, user.id))

    return {
        **user_info(user),
        "message": "Account created successfully.",
    }


@router.post("/login")
def login(
    data: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
):

    email = str(data.email).lower()

    user = db.query(User).filter(
        User.email == email
    ).first()

    if not user or not verify_password(
        data.password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password."
        )

    # A new session on every login
    logger.info("Login: user=%s", user.id)  # [log]
    set_session_cookie(response, create_session(db, user.id))

    return {
        **user_info(user),
        "message": "Login successful.",
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    # Delete the session on the server, so the cookie stops working everywhere
    delete_session(db, request.cookies.get(SESSION_COOKIE_NAME))
    clear_session_cookie(response)

    return {"message": "Logged out."}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_info(user)
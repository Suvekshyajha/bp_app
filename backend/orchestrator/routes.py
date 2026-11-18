
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from database import get_db
from models import User
from auth import hash_password, verify_password, create_access_token

router = APIRouter()


class SignupRequest(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.get("/health")
def health_check():
    return {"status": "healthy"}


@router.post("/signup")
def signup(data: SignupRequest, db: Session = Depends(get_db)):

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

    token = create_access_token(user.id)

    return {
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "message": "Account created successfully.",
        "access_token": token,
        "token_type": "bearer",
    }


@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):

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

    token = create_access_token(user.id)

    return {
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "message": "Login successful.",
        "access_token": token,
        "token_type": "bearer",
    }

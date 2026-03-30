from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from services.db_service import get_engine


def get_user_by_id(user_id: int) -> Optional[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, username, full_name, role, student_code, email, phone, is_active
                FROM users
                WHERE id=:id
                """
            ),
            {"id": int(user_id)},
        ).fetchone()

    if not row or not bool(row[7]):
        return None

    return {
        "id": int(row[0]),
        "username": str(row[1]),
        "full_name": str(row[2]),
        "role": str(row[3]),
        "student_code": row[4],
        "email": row[5],
        "phone": row[6],
    }


def authenticate(username: str, password: str) -> Optional[int]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, password_hash, is_active
                FROM users
                WHERE username=:username
                """
            ),
            {"username": username.strip()},
        ).fetchone()

    if not row or not bool(row[2]):
        return None
    return int(row[0]) if check_password_hash(row[1], password) else None


def register_student(
    *,
    username: str,
    password: str,
    full_name: str,
    student_code: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> int:
    username = username.strip()
    full_name = full_name.strip()
    if not username or not full_name:
        raise ValueError("required")
    if len(password) < 6:
        raise ValueError("password_short")

    engine = get_engine()
    password_hash = generate_password_hash(password)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO users (username, password_hash, full_name, role, student_code, email, phone)
                    VALUES (:username, :password_hash, :full_name, 'student', :student_code, :email, :phone)
                    """
                ),
                {
                    "username": username,
                    "password_hash": password_hash,
                    "full_name": full_name,
                    "student_code": student_code,
                    "email": email,
                    "phone": phone,
                },
            )
            return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    except Exception as exc:
        raise ValueError("conflict") from exc


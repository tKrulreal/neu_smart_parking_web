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


def list_users(*, keyword: str = "", role: str = "") -> list[dict]:
    where = []
    params: dict[str, object] = {}
    if keyword:
        where.append("(username LIKE :kw OR full_name LIKE :kw OR student_code LIKE :kw OR email LIKE :kw)")
        params["kw"] = f"%{keyword.strip()}%"
    if role:
        where.append("role = :role")
        params["role"] = role.strip()

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id, username, full_name, role, student_code, email, phone, is_active, created_at
                FROM users
                {where_sql}
                ORDER BY id DESC
                """
            ),
            params,
        ).mappings().all()
    return [dict(row) for row in rows]


def get_user_detail(user_id: int) -> Optional[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, username, full_name, role, student_code, email, phone, is_active, created_at
                FROM users
                WHERE id=:id
                """
            ),
            {"id": int(user_id)},
        ).mappings().first()
    return dict(row) if row else None


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
    student_code = (student_code or "").strip() or None
    if not username or not full_name:
        raise ValueError("required")
    if not student_code:
        raise ValueError("student_code_required")
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


def create_user(
    *,
    username: str,
    password: str,
    full_name: str,
    role: str,
    student_code: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    is_active: bool = True,
) -> int:
    username = username.strip()
    full_name = full_name.strip()
    if not username or not full_name:
        raise ValueError("required")
    if role not in {"admin", "guard", "student"}:
        raise ValueError("invalid_role")
    if role == "student" and not (student_code or "").strip():
        raise ValueError("student_code_required")
    if len(password) < 6:
        raise ValueError("password_short")

    engine = get_engine()
    password_hash = generate_password_hash(password)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO users
                    (username, password_hash, full_name, role, student_code, email, phone, is_active)
                    VALUES
                    (:username, :password_hash, :full_name, :role, :student_code, :email, :phone, :is_active)
                    """
                ),
                {
                    "username": username,
                    "password_hash": password_hash,
                    "full_name": full_name,
                    "role": role,
                    "student_code": student_code or None,
                    "email": email or None,
                    "phone": phone or None,
                    "is_active": 1 if is_active else 0,
                },
            )
            return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    except Exception as exc:
        raise ValueError("conflict") from exc


def update_user(
    user_id: int,
    *,
    username: str,
    full_name: str,
    role: str,
    student_code: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    password: str | None = None,
    is_active: bool = True,
) -> None:
    username = username.strip()
    full_name = full_name.strip()
    if not username or not full_name:
        raise ValueError("required")
    if role not in {"admin", "guard", "student"}:
        raise ValueError("invalid_role")
    if role == "student" and not (student_code or "").strip():
        raise ValueError("student_code_required")
    if password is not None and password != "" and len(password) < 6:
        raise ValueError("password_short")

    params: dict[str, object] = {
        "id": int(user_id),
        "username": username,
        "full_name": full_name,
        "role": role,
        "student_code": student_code or None,
        "email": email or None,
        "phone": phone or None,
        "is_active": 1 if is_active else 0,
    }
    password_sql = ""
    if password:
        params["password_hash"] = generate_password_hash(password)
        password_sql = ", password_hash=:password_hash"

    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE users
                    SET username=:username,
                        full_name=:full_name,
                        role=:role,
                        student_code=:student_code,
                        email=:email,
                        phone=:phone,
                        is_active=:is_active
                        {password_sql}
                    WHERE id=:id
                    """
                ),
                params,
            )
    except Exception as exc:
        raise ValueError("conflict") from exc


def toggle_user_active(user_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE users
                SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
                WHERE id=:id
                """
            ),
            {"id": int(user_id)},
        )


def delete_user(user_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": int(user_id)})


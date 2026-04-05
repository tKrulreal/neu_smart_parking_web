from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from services.db_service import get_engine

ALLOWED_VEHICLE_TYPES = {"motorbike", "car", "electric"}


def normalize_plate(value: str | None) -> str:
    raw = (value or "").upper().strip()
    return raw.replace("-", "").replace(" ", "").replace(".", "")


def list_vehicles(*, plate: str = "", vehicle_type: str = "", student_code: str = "") -> list[dict]:
    where = []
    params: dict[str, object] = {}
    if plate:
        where.append("replace(replace(replace(upper(v.plate), '-', ''), ' ', ''), '.', '') LIKE :plate")
        params["plate"] = f"%{normalize_plate(plate)}%"
    if vehicle_type:
        where.append("v.vehicle_type = :vehicle_type")
        params["vehicle_type"] = vehicle_type.strip()
    if student_code:
        where.append("v.student_code = :student_code")
        params["student_code"] = student_code.strip()

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT
                    v.id,
                    v.plate,
                    v.student_code,
                    COALESCE(v.owner_name, u.full_name) AS owner_name,
                    v.vehicle_type,
                    v.brand,
                    v.color,
                    v.image_path,
                    v.is_active,
                    v.created_at
                FROM vehicles v
                LEFT JOIN users u ON u.student_code = v.student_code
                {where_sql}
                ORDER BY v.id DESC
                """
            ),
            params,
        ).mappings().all()
    return [dict(row) for row in rows]


def get_vehicle_by_id(vehicle_id: int) -> Optional[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    v.id,
                    v.plate,
                    v.student_code,
                    COALESCE(v.owner_name, u.full_name) AS owner_name,
                    v.vehicle_type,
                    v.brand,
                    v.color,
                    v.image_path,
                    v.is_active,
                    v.created_at
                FROM vehicles v
                LEFT JOIN users u ON u.student_code = v.student_code
                WHERE v.id=:id
                """
            ),
            {"id": int(vehicle_id)},
        ).mappings().first()
    return dict(row) if row else None


def get_vehicle_by_plate(plate: str, *, active_only: bool = False) -> Optional[dict]:
    where_active = "AND v.is_active = 1" if active_only else ""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"""
                SELECT
                    v.id,
                    v.plate,
                    v.student_code,
                    COALESCE(v.owner_name, u.full_name) AS owner_name,
                    v.vehicle_type,
                    v.brand,
                    v.color,
                    v.image_path,
                    v.is_active,
                    v.created_at
                FROM vehicles v
                LEFT JOIN users u ON u.student_code = v.student_code
                WHERE replace(replace(replace(upper(v.plate), '-', ''), ' ', ''), '.', '')=:plate
                {where_active}
                LIMIT 1
                """
            ),
            {"plate": normalize_plate(plate)},
        ).mappings().first()
    return dict(row) if row else None


def create_vehicle(
    *,
    plate: str,
    student_code: str,
    owner_name: str | None = None,
    vehicle_type: str = "motorbike",
    brand: str | None = None,
    color: str | None = None,
    image_path: str | None = None,
    is_active: bool = False,
) -> int:
    if not plate.strip() or not student_code.strip():
        raise ValueError("required")
    if vehicle_type not in ALLOWED_VEHICLE_TYPES:
        raise ValueError("invalid_vehicle_type")

    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO vehicles
                    (plate, student_code, owner_name, vehicle_type, brand, color, image_path, is_active)
                    VALUES
                    (:plate, :student_code, :owner_name, :vehicle_type, :brand, :color, :image_path, :is_active)
                    """
                ),
                {
                    "plate": plate.strip().upper(),
                    "student_code": student_code.strip(),
                    "owner_name": owner_name or None,
                    "vehicle_type": vehicle_type or "motorbike",
                    "brand": brand or None,
                    "color": color or None,
                    "image_path": image_path or None,
                    "is_active": 1 if is_active else 0,
                },
            )
            return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    except Exception as exc:
        raise ValueError("conflict") from exc


def update_vehicle(
    vehicle_id: int,
    *,
    plate: str,
    student_code: str,
    owner_name: str | None = None,
    vehicle_type: str = "motorbike",
    brand: str | None = None,
    color: str | None = None,
    image_path: str | None = None,
    is_active: bool = False,
) -> None:
    if not plate.strip() or not student_code.strip():
        raise ValueError("required")
    if vehicle_type not in ALLOWED_VEHICLE_TYPES:
        raise ValueError("invalid_vehicle_type")

    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE vehicles
                    SET plate=:plate,
                        student_code=:student_code,
                        owner_name=:owner_name,
                        vehicle_type=:vehicle_type,
                        brand=:brand,
                        color=:color,
                        image_path=COALESCE(:image_path, image_path),
                        is_active=:is_active
                    WHERE id=:id
                    """
                ),
                {
                    "id": int(vehicle_id),
                    "plate": plate.strip().upper(),
                    "student_code": student_code.strip(),
                    "owner_name": owner_name or None,
                    "vehicle_type": vehicle_type or "motorbike",
                    "brand": brand or None,
                    "color": color or None,
                    "image_path": image_path,
                    "is_active": 1 if is_active else 0,
                },
            )
    except Exception as exc:
        raise ValueError("conflict") from exc


def set_vehicle_active(vehicle_id: int, is_active: bool) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE vehicles SET is_active=:is_active WHERE id=:id"),
            {"id": int(vehicle_id), "is_active": 1 if is_active else 0},
        )


def toggle_vehicle_active(vehicle_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE vehicles
                SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
                WHERE id=:id
                """
            ),
            {"id": int(vehicle_id)},
        )


def delete_vehicle(vehicle_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM vehicles WHERE id=:id"), {"id": int(vehicle_id)})

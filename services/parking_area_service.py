from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import text

from services.db_service import get_engine


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _normalize_area_payload(area: dict | None) -> dict | None:
    if not area:
        return None
    payload = dict(area)
    payload["capacity"] = int(payload.get("capacity") or 0)
    payload["occupancy"] = int(payload.get("occupancy") or 0)
    payload["available_slots"] = max(payload["capacity"] - payload["occupancy"], 0)
    payload["is_active"] = bool(payload.get("is_active", 0))
    payload["is_full"] = payload["occupancy"] >= payload["capacity"]
    payload["occupancy_rate"] = round((payload["occupancy"] / payload["capacity"]) * 100, 2) if payload["capacity"] else 0.0
    return payload


def _resolve_chart_days(days: int | None) -> int:
    try:
        value = int(days or 7)
    except (TypeError, ValueError):
        return 7
    return value if value in {7, 14, 30} else 7


def _build_daily_chart(conn, area_id: int, days: int) -> dict:
    period_days = _resolve_chart_days(days)
    end_date = date.today()
    start_date = end_date - timedelta(days=period_days - 1)
    params = {
        "parking_area_id": int(area_id),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }

    checkin_rows = conn.execute(
        text(
            """
            SELECT date(time_in) AS day_key, COUNT(*) AS total
            FROM parking_log
            WHERE parking_area_id = :parking_area_id
              AND date(time_in) BETWEEN :start_date AND :end_date
            GROUP BY date(time_in)
            ORDER BY date(time_in)
            """
        ),
        params,
    ).mappings().all()
    checkout_rows = conn.execute(
        text(
            """
            SELECT date(time_out) AS day_key, COUNT(*) AS total
            FROM parking_log
            WHERE parking_area_id = :parking_area_id
              AND time_out IS NOT NULL
              AND date(time_out) BETWEEN :start_date AND :end_date
            GROUP BY date(time_out)
            ORDER BY date(time_out)
            """
        ),
        params,
    ).mappings().all()

    checkin_map = {str(row["day_key"]): int(row["total"] or 0) for row in checkin_rows}
    checkout_map = {str(row["day_key"]): int(row["total"] or 0) for row in checkout_rows}

    daily_points: list[dict] = []
    max_value = 0
    total_checkins = 0
    total_checkouts = 0
    weekday_labels = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]

    for offset in range(period_days):
        current_day = start_date + timedelta(days=offset)
        day_key = current_day.isoformat()
        checkins = int(checkin_map.get(day_key, 0))
        checkouts = int(checkout_map.get(day_key, 0))
        total_checkins += checkins
        total_checkouts += checkouts
        max_value = max(max_value, checkins, checkouts)
        daily_points.append(
            {
                "date": day_key,
                "label": current_day.strftime("%d/%m"),
                "weekday": weekday_labels[current_day.weekday()],
                "checkins": checkins,
                "checkouts": checkouts,
            }
        )

    scale_max = max(max_value, 1)
    for point in daily_points:
        point["checkin_height"] = 10 if point["checkins"] == 0 else max(int((point["checkins"] / scale_max) * 180), 16)
        point["checkout_height"] = 10 if point["checkouts"] == 0 else max(int((point["checkouts"] / scale_max) * 180), 16)

    return {
        "days": daily_points,
        "range_days": period_days,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "max_value": scale_max,
        "total_checkins": total_checkins,
        "total_checkouts": total_checkouts,
    }


def _fetch_area_with_stats(conn, area_id: int) -> dict | None:
    row = conn.execute(
        text(
            """
            SELECT
                pa.id,
                pa.code,
                pa.name,
                pa.capacity,
                pa.description,
                pa.is_active,
                pa.created_at,
                COALESCE((
                    SELECT COUNT(*)
                    FROM parking_log pl
                    WHERE pl.parking_area_id = pa.id
                      AND pl.time_out IS NULL
                ), 0) AS occupancy
            FROM parking_areas pa
            WHERE pa.id = :id
            LIMIT 1
            """
        ),
        {"id": int(area_id)},
    ).mappings().first()
    return _normalize_area_payload(_row_to_dict(row))


def list_parking_areas(*, include_inactive: bool = True) -> list[dict]:
    where_sql = "" if include_inactive else "WHERE pa.is_active = 1"
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT
                    pa.id,
                    pa.code,
                    pa.name,
                    pa.capacity,
                    pa.description,
                    pa.is_active,
                    pa.created_at,
                    COALESCE(active_sessions.occupancy, 0) AS occupancy
                FROM parking_areas pa
                LEFT JOIN (
                    SELECT parking_area_id, COUNT(*) AS occupancy
                    FROM parking_log
                    WHERE time_out IS NULL
                    GROUP BY parking_area_id
                ) active_sessions
                    ON active_sessions.parking_area_id = pa.id
                {where_sql}
                ORDER BY pa.id
                """
            )
        ).mappings().all()
    return [_normalize_area_payload(dict(row)) for row in rows]


def get_parking_area_by_id(area_id: int, *, connection=None) -> Optional[dict]:
    if not area_id:
        return None
    if connection is not None:
        return _fetch_area_with_stats(connection, int(area_id))

    engine = get_engine()
    with engine.connect() as conn:
        return _fetch_area_with_stats(conn, int(area_id))


def get_default_parking_area() -> Optional[dict]:
    active_areas = list_parking_areas(include_inactive=False)
    if active_areas:
        return active_areas[0]
    areas = list_parking_areas(include_inactive=True)
    return areas[0] if areas else None


def update_parking_area(
    area_id: int,
    *,
    name: str,
    capacity: int | str,
    description: str | None = None,
    is_active: bool = True,
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("required")

    try:
        capacity_value = int(capacity)
    except (TypeError, ValueError):
        raise ValueError("invalid_capacity") from None
    if capacity_value <= 0:
        raise ValueError("invalid_capacity")

    engine = get_engine()
    with engine.begin() as conn:
        area = _fetch_area_with_stats(conn, int(area_id))
        if not area:
            raise ValueError("not_found")
        if capacity_value < int(area["occupancy"]):
            raise ValueError("capacity_below_occupancy")
        if not is_active and int(area["occupancy"]) > 0:
            raise ValueError("occupied_area_cannot_disable")

        conn.execute(
            text(
                """
                UPDATE parking_areas
                SET name = :name,
                    capacity = :capacity,
                    description = :description,
                    is_active = :is_active
                WHERE id = :id
                """
            ),
            {
                "id": int(area_id),
                "name": clean_name,
                "capacity": capacity_value,
                "description": (description or "").strip() or None,
                "is_active": 1 if is_active else 0,
            },
        )
        updated_area = _fetch_area_with_stats(conn, int(area_id))
    return updated_area or area


def get_parking_area_stats(area_id: int, *, days: int = 7) -> dict | None:
    engine = get_engine()
    with engine.connect() as conn:
        area = _fetch_area_with_stats(conn, int(area_id))
        if not area:
            return None

        params = {"parking_area_id": int(area_id)}
        daily_chart = _build_daily_chart(conn, int(area_id), days)
        stats = {
            "total_sessions": int(
                conn.execute(
                    text("SELECT COUNT(*) FROM parking_log WHERE parking_area_id = :parking_area_id"),
                    params,
                ).scalar()
                or 0
            ),
            "today_checkins": int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM parking_log
                        WHERE parking_area_id = :parking_area_id
                          AND date(time_in) = date('now', 'localtime')
                        """
                    ),
                    params,
                ).scalar()
                or 0
            ),
            "today_checkouts": int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM parking_log
                        WHERE parking_area_id = :parking_area_id
                          AND time_out IS NOT NULL
                          AND date(time_out) = date('now', 'localtime')
                        """
                    ),
                    params,
                ).scalar()
                or 0
            ),
            "completed_sessions": int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM parking_log
                        WHERE parking_area_id = :parking_area_id
                          AND time_out IS NOT NULL
                        """
                    ),
                    params,
                ).scalar()
                or 0
            ),
            "total_revenue": int(
                conn.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(fee), 0)
                        FROM parking_log
                        WHERE parking_area_id = :parking_area_id
                          AND time_out IS NOT NULL
                        """
                    ),
                    params,
                ).scalar()
                or 0
            ),
        }
        recent_logs = conn.execute(
            text(
                """
                SELECT
                    id,
                    plate,
                    student_code,
                    time_in,
                    time_out,
                    gate_in,
                    gate_out,
                    fee,
                    status,
                    note
                FROM parking_log
                WHERE parking_area_id = :parking_area_id
                ORDER BY id DESC
                LIMIT 12
                """
            ),
            params,
        ).mappings().all()

    return {
        "area": area,
        "stats": stats,
        "daily_chart": daily_chart,
        "recent_logs": [dict(row) for row in recent_logs],
    }

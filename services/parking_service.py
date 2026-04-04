from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from services.db_service import get_engine
from services.vehicle_service import get_vehicle_by_plate, normalize_plate


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _log_plate_scan(
    *,
    image_path: str,
    raw_text: str | None,
    normalized: str | None,
    score: float | None,
    source: str | None,
    direction: str,
    gate: str,
    status: str,
) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO plate_scan_log
                (image_path, raw_text, normalized_plate, score, source, direction, gate, status, created_at)
                VALUES
                (:image_path, :raw_text, :normalized_plate, :score, :source, :direction, :gate, :status, :created_at)
                """
            ),
            {
                "image_path": image_path,
                "raw_text": raw_text,
                "normalized_plate": normalized,
                "score": score,
                "source": source,
                "direction": direction,
                "gate": gate,
                "status": status,
                "created_at": _now_iso(),
            },
        )


def _get_qr_log(student_code: str, qr_payload: str | None) -> dict | None:
    if not student_code or not qr_payload:
        return None
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, student_code, qr_payload, qr_image_path, plate, parking_log_id, is_valid, used_for_exit, created_at
                FROM qr_logs
                WHERE student_code=:student_code AND qr_payload=:qr_payload
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"student_code": student_code, "qr_payload": qr_payload},
        ).mappings().first()
    return _row_to_dict(row)


def get_active_session_by_plate(plate: str) -> Optional[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, plate, student_code, time_in, time_out, gate_in, gate_out, fee, status, note
                FROM parking_log
                WHERE replace(replace(replace(upper(plate), '-', ''), ' ', ''), '.', '')=:plate
                  AND time_out IS NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"plate": normalize_plate(plate)},
        ).mappings().first()
    return _row_to_dict(row)


def calculate_fee(time_in: str, time_out: str | None = None) -> int:
    start = datetime.fromisoformat(time_in)
    end = datetime.fromisoformat(time_out) if time_out else datetime.now()
    duration_minutes = max(int((end - start).total_seconds() // 60), 1)
    hours = duration_minutes // 60 + (1 if duration_minutes % 60 else 0)
    return max(3000, hours * 3000)


def get_active_qr_for_session(parking_log_id: int) -> dict | None:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, student_code, qr_payload, qr_image_path, plate, parking_log_id, is_valid, used_for_exit, created_at
                FROM qr_logs
                WHERE parking_log_id=:parking_log_id AND is_valid = 1 AND used_for_exit = 0
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"parking_log_id": int(parking_log_id)},
        ).mappings().first()
    return _row_to_dict(row)


def create_qr_log(
    student_code: str,
    qr_payload: str,
    qr_image_path: str,
    *,
    plate: str | None = None,
    parking_log_id: int | None = None,
) -> dict:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO qr_logs (student_code, qr_payload, qr_image_path, plate, parking_log_id, is_valid, used_for_exit, created_at)
                VALUES (:student_code, :qr_payload, :qr_image_path, :plate, :parking_log_id, 1, 0, :created_at)
                """
            ),
            {
                "student_code": student_code,
                "qr_payload": qr_payload,
                "qr_image_path": qr_image_path,
                "plate": plate,
                "parking_log_id": parking_log_id,
                "created_at": _now_iso(),
            },
        )
        qr_log_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    return {
        "id": qr_log_id,
        "student_code": student_code,
        "qr_payload": qr_payload,
        "qr_image_path": qr_image_path,
        "plate": plate,
        "parking_log_id": parking_log_id,
        "is_valid": 1,
        "used_for_exit": 0,
    }


def analyze_gate_in_scan(
    *,
    image_path: str,
    detected_plate: str | None,
    raw_text: str | None,
    confidence: float | None,
    source: str | None,
    gate_name: str,
) -> dict:
    if not detected_plate:
        _log_plate_scan(
            image_path=image_path,
            raw_text=raw_text,
            normalized=None,
            score=confidence,
            source=source,
            direction="IN",
            gate=gate_name,
            status="UNREADABLE",
        )
        return {"status": "UNREADABLE", "vehicle": None, "active_session": None}

    vehicle = get_vehicle_by_plate(detected_plate, active_only=False)
    active_session = get_active_session_by_plate(detected_plate)

    if not vehicle:
        status = "UNKNOWN_VEHICLE"
    elif not vehicle["is_active"]:
        status = "PENDING_APPROVAL"
    elif active_session:
        status = "ALREADY_IN_PARKING"
    else:
        status = "READY_TO_ENTER"

    _log_plate_scan(
        image_path=image_path,
        raw_text=raw_text,
        normalized=normalize_plate(detected_plate),
        score=confidence,
        source=source,
        direction="IN",
        gate=gate_name,
        status=status,
    )
    return {"status": status, "vehicle": vehicle, "active_session": active_session}


def confirm_gate_in(*, plate: str, gate_name: str, note: str | None = None) -> dict:
    vehicle = get_vehicle_by_plate(plate, active_only=True)
    if not vehicle:
        raise ValueError("vehicle_not_found")
    if get_active_session_by_plate(plate):
        raise ValueError("already_in_parking")

    time_in = _now_iso()
    created_at = _now_iso()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO parking_log
                (plate, student_code, time_in, gate_in, fee, status, note, created_at)
                VALUES
                (:plate, :student_code, :time_in, :gate_in, 0, 'IN_PARKING', :note, :created_at)
                """
            ),
            {
                "plate": vehicle["plate"],
                "student_code": vehicle["student_code"],
                "time_in": time_in,
                "gate_in": gate_name,
                "note": note or None,
                "created_at": created_at,
            },
        )
        parking_log_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())

    return {
        "status": "ENTRY_CONFIRMED",
        "vehicle": vehicle,
        "session": {
            "id": parking_log_id,
            "plate": vehicle["plate"],
            "student_code": vehicle["student_code"],
            "time_in": time_in,
            "gate_in": gate_name,
            "status": "IN_PARKING",
        },
    }


def analyze_gate_out_scan(
    *,
    image_path: str,
    detected_plate: str | None,
    raw_text: str | None,
    confidence: float | None,
    source: str | None,
    gate_name: str,
    qr_student_code: str | None,
    qr_payload: str | None,
    qr_valid: bool,
) -> dict:
    if not detected_plate:
        _log_plate_scan(
            image_path=image_path,
            raw_text=raw_text,
            normalized=None,
            score=confidence,
            source=source,
            direction="OUT",
            gate=gate_name,
            status="UNREADABLE",
        )
        return {"status": "UNREADABLE"}

    vehicle = get_vehicle_by_plate(detected_plate, active_only=False)
    active_session = get_active_session_by_plate(detected_plate)
    qr_log = _get_qr_log(qr_student_code or "", qr_payload)

    if not vehicle:
        status = "UNKNOWN_VEHICLE"
    elif not active_session:
        status = "NO_ACTIVE_SESSION"
    elif not qr_student_code:
        status = "QR_REQUIRED"
    elif not qr_valid:
        status = "INVALID_QR"
    elif str(qr_student_code) != str(vehicle["student_code"]):
        status = "QR_MISMATCH"
    elif not qr_log:
        status = "INVALID_QR"
    elif qr_log.get("used_for_exit"):
        status = "QR_USED"
    elif not qr_log.get("is_valid"):
        status = "INVALID_QR"
    elif normalize_plate(qr_log.get("plate")) != normalize_plate(vehicle["plate"]):
        status = "QR_PLATE_MISMATCH"
    elif int(qr_log.get("parking_log_id") or 0) != int(active_session["id"]):
        status = "QR_SESSION_MISMATCH"
    else:
        status = "READY_TO_EXIT"

    _log_plate_scan(
        image_path=image_path,
        raw_text=raw_text or qr_payload,
        normalized=normalize_plate(detected_plate),
        score=confidence,
        source=source,
        direction="OUT",
        gate=gate_name,
        status=status,
    )

    fee = calculate_fee(active_session["time_in"]) if active_session else 0
    return {
        "status": status,
        "vehicle": vehicle,
        "active_session": active_session,
        "fee": fee,
        "qr_log": qr_log,
    }


def confirm_gate_out(*, plate: str, gate_name: str, qr_payload: str | None = None, note: str | None = None) -> dict:
    vehicle = get_vehicle_by_plate(plate, active_only=False)
    if not vehicle:
        raise ValueError("vehicle_not_found")

    active_session = get_active_session_by_plate(plate)
    if not active_session:
        raise ValueError("no_active_session")
    qr_log = _get_qr_log(vehicle["student_code"], qr_payload)
    if not qr_log:
        raise ValueError("invalid_qr")
    if qr_log.get("used_for_exit"):
        raise ValueError("qr_used")
    if not qr_log.get("is_valid"):
        raise ValueError("invalid_qr")
    if normalize_plate(qr_log.get("plate")) != normalize_plate(vehicle["plate"]):
        raise ValueError("qr_plate_mismatch")
    if int(qr_log.get("parking_log_id") or 0) != int(active_session["id"]):
        raise ValueError("qr_session_mismatch")

    time_out = _now_iso()
    fee = calculate_fee(active_session["time_in"], time_out)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE parking_log
                SET time_out=:time_out,
                    gate_out=:gate_out,
                    fee=:fee,
                    status='COMPLETED',
                    note=COALESCE(:note, note)
                WHERE id=:id
                """
            ),
            {
                "id": active_session["id"],
                "time_out": time_out,
                "gate_out": gate_name,
                "fee": fee,
                "note": note or None,
            },
        )
        conn.execute(
            text(
                """
                UPDATE qr_logs
                SET used_for_exit = 1,
                    is_valid = 0
                WHERE id = :id
                """
            ),
            {"id": qr_log["id"]},
        )

    return {
        "status": "EXIT_CONFIRMED",
        "vehicle": vehicle,
        "session": {**active_session, "time_out": time_out, "fee": fee, "gate_out": gate_name},
    }


def list_recent_entries(limit: int = 8) -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT plate, student_code, time_in, status
                FROM parking_log
                ORDER BY id DESC
                LIMIT :limit
                """
            ),
            {"limit": int(limit)},
        ).mappings().all()
    return [dict(row) for row in rows]


def list_history(*, plate: str = "", status: str = "", date: str = "") -> list[dict]:
    where = []
    params: dict[str, object] = {}
    if plate:
        where.append("replace(replace(replace(upper(plate), '-', ''), ' ', ''), '.', '') LIKE :plate")
        params["plate"] = f"%{normalize_plate(plate)}%"
    if status:
        where.append("status = :status")
        params["status"] = status
    if date:
        where.append("date(time_in) = :date")
        params["date"] = date
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id, plate, student_code, time_in, time_out, gate_in, gate_out, fee, status, note
                FROM parking_log
                {where_sql}
                ORDER BY id DESC
                """
            ),
            params,
        ).mappings().all()
    return [dict(row) for row in rows]


def build_csv_export(rows: list[dict]) -> str:
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["ID", "Plate", "Student Code", "Time In", "Time Out", "Gate In", "Gate Out", "Fee", "Status", "Note"])
    for row in rows:
        writer.writerow(
            [
                row.get("id"),
                row.get("plate"),
                row.get("student_code"),
                row.get("time_in"),
                row.get("time_out"),
                row.get("gate_in"),
                row.get("gate_out"),
                row.get("fee"),
                row.get("status"),
                row.get("note"),
            ]
        )
    return stream.getvalue()


def build_excel_export(rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Parking History"
    ws.append(["ID", "Plate", "Student Code", "Time In", "Time Out", "Gate In", "Gate Out", "Fee", "Status", "Note"])
    for row in rows:
        ws.append(
            [
                row.get("id"),
                row.get("plate"),
                row.get("student_code"),
                row.get("time_in"),
                row.get("time_out"),
                row.get("gate_in"),
                row.get("gate_out"),
                row.get("fee"),
                row.get("status"),
                row.get("note"),
            ]
        )
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()

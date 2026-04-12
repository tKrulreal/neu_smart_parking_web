from __future__ import annotations

import base64
import mimetypes
import os
import unicodedata
from datetime import datetime, timedelta
from functools import wraps
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import text
from werkzeug.utils import secure_filename

from config import Config
from services.db_service import get_engine, init_db
from services.parking_area_service import get_parking_area_stats, list_parking_areas, update_parking_area
from services.parking_service import (
    analyze_gate_in_scan,
    analyze_gate_out_scan,
    build_csv_export,
    build_excel_export,
    confirm_gate_in,
    confirm_gate_out,
    create_qr_log,
    get_active_qr_for_session,
    get_active_session_by_plate,
    list_history as list_history_records,
    list_recent_entries,
)
from services.user_service import (
    authenticate,
    create_user,
    delete_user,
    get_user_by_id,
    get_user_detail,
    list_users,
    register_student,
    toggle_user_active,
    update_user,
)
from services.vehicle_service import (
    create_vehicle,
    delete_vehicle,
    get_vehicle_by_id,
    get_vehicle_by_plate,
    list_vehicles,
    normalize_plate,
    set_vehicle_active,
    toggle_vehicle_active,
    update_vehicle,
)

app = Flask(__name__)
app.config.from_object(Config)
app.permanent_session_lifetime = timedelta(days=7)

init_db()


def _engine():
    return get_engine()


def _static_root() -> str:
    return os.path.abspath(app.static_folder or os.path.join(os.getcwd(), "static"))


def _static_target_dir(configured_dir: str, fallback_subdir: str) -> str:
    target = os.path.abspath(configured_dir)
    static_root = _static_root()
    try:
        if os.path.commonpath([target, static_root]) == static_root:
            return target
    except ValueError:
        pass
    return os.path.join(static_root, fallback_subdir)


def _fetch_one(sql: str, params: dict | None = None):
    with _engine().connect() as conn:
        return conn.execute(text(sql), params or {}).fetchone()


def _fetch_all(sql: str, params: dict | None = None):
    with _engine().connect() as conn:
        return conn.execute(text(sql), params or {}).fetchall()


def _fetch_scalar(sql: str, params: dict | None = None, default=0):
    with _engine().connect() as conn:
        value = conn.execute(text(sql), params or {}).scalar()
        return default if value is None else value


def _active_parking_map() -> dict[str, bool]:
    rows = _fetch_all("SELECT plate FROM parking_log WHERE time_out IS NULL")
    return {normalize_plate(row[0]): True for row in rows}


def _save_upload(file_storage, *, prefix: str) -> tuple[str | None, str | None]:
    if not file_storage or not file_storage.filename:
        return None, None
    upload_dir = _static_target_dir(Config.UPLOAD_FOLDER, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"{prefix}_{int(datetime.now().timestamp())}_{uuid4().hex[:8]}_{secure_filename(file_storage.filename)}"
    absolute_path = os.path.join(upload_dir, filename)
    file_storage.save(absolute_path)
    return f"uploads/{filename}", absolute_path


def _prepare_temporary_upload(file_storage, *, prefix: str) -> tuple[str | None, str | None, str | None]:
    if not file_storage or not file_storage.filename:
        return None, None, None

    payload = file_storage.read()
    if not payload:
        return None, None, None

    safe_name = secure_filename(file_storage.filename) or f"{prefix}.bin"
    suffix = os.path.splitext(safe_name)[1] or ".bin"
    mimetype = file_storage.mimetype or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

    with NamedTemporaryFile(delete=False, suffix=suffix, prefix=f"{prefix}_") as temp_file:
        temp_file.write(payload)
        temp_path = temp_file.name

    preview_src = None
    if mimetype.startswith("image/"):
        encoded = base64.b64encode(payload).decode("ascii")
        preview_src = f"data:{mimetype};base64,{encoded}"

    return preview_src, temp_path, f"upload://{prefix}/{safe_name}"


def _cleanup_temporary_upload(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _delete_static_asset(relative_path: str | None) -> None:
    if not relative_path:
        return
    static_root = _static_root()
    absolute_path = os.path.abspath(os.path.join(static_root, relative_path.replace("/", os.sep).lstrip("\\/")))
    try:
        if os.path.commonpath([absolute_path, static_root]) != static_root:
            return
    except ValueError:
        return
    try:
        os.remove(absolute_path)
    except OSError:
        pass


def _safe_next_url(target: str | None) -> str | None:
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    if not parts.path.startswith("/"):
        return None
    if target.startswith("//"):
        return None
    return target


def _duration_from_iso(time_in: str | None) -> str | None:
    if not time_in:
        return None
    started = datetime.fromisoformat(time_in)
    duration_minutes = max(int((datetime.now() - started).total_seconds() // 60), 1)
    return f"{duration_minutes // 60}h {duration_minutes % 60}m"


def _bool_form(field_name: str) -> bool:
    return (request.form.get(field_name) or "").lower() in {"1", "true", "on", "yes"}


def _normalize_gate_name(gate_name: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", gate_name or "")
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_name.upper().split())


def _user_form_defaults(data: dict | None = None) -> dict:
    payload = {
        "username": "",
        "full_name": "",
        "role": "student",
        "student_code": "",
        "email": "",
        "phone": "",
        "is_active": True,
    }
    if data:
        payload.update(
            {
                "username": data.get("username", ""),
                "full_name": data.get("full_name", ""),
                "role": data.get("role", "student"),
                "student_code": data.get("student_code") or "",
                "email": data.get("email") or "",
                "phone": data.get("phone") or "",
                "is_active": bool(data.get("is_active", 1)),
            }
        )
    return payload


def _vehicle_form_defaults(data: dict | None = None) -> dict:
    payload = {
        "plate": "",
        "student_code": "",
        "owner_name": "",
        "vehicle_type": "motorbike",
        "brand": "",
        "color": "",
        "is_active": False,
    }
    if data:
        payload.update(
            {
                "plate": data.get("plate", ""),
                "student_code": data.get("student_code", ""),
                "owner_name": data.get("owner_name") or "",
                "vehicle_type": data.get("vehicle_type", "motorbike"),
                "brand": data.get("brand") or "",
                "color": data.get("color") or "",
                "is_active": bool(data.get("is_active", 0)),
            }
        )
    return payload


def _parking_area_form_defaults(data: dict | None = None) -> dict:
    payload = {
        "name": "",
        "capacity": 50,
        "description": "",
        "is_active": True,
    }
    if data:
        payload.update(
            {
                "name": data.get("name", ""),
                "capacity": int(data.get("capacity") or 0),
                "description": data.get("description") or "",
                "is_active": bool(data.get("is_active", 1)),
            }
        )
    return payload


def _find_parking_area(raw_value: int | str | None, parking_areas: list[dict]) -> dict | None:
    if raw_value in (None, ""):
        return None
    try:
        area_id = int(raw_value)
    except (TypeError, ValueError):
        return None
    return next((item for item in parking_areas if int(item.get("id") or 0) == area_id), None)


def _resolve_selected_parking_area(raw_value: int | str | None, parking_areas: list[dict]) -> dict | None:
    selected_area = _find_parking_area(raw_value, parking_areas)
    if selected_area:
        return selected_area
    return next((item for item in parking_areas if item.get("is_active")), None) or (parking_areas[0] if parking_areas else None)


def _parse_chart_days(raw_value: int | str | None) -> int:
    try:
        days = int(raw_value or 7)
    except (TypeError, ValueError):
        return 7
    return days if days in {7, 14, 30} else 7


def _issue_session_qr(student_code: str, plate: str, parking_log_id: int) -> dict:
    from services.qr_service import create_qr_asset

    qr_output_dir = _static_target_dir(Config.QR_FOLDER, "qr_out")
    payload, absolute_path = create_qr_asset(student_code, output_dir=qr_output_dir)
    relative_path = os.path.relpath(absolute_path, _static_root()).replace("\\", "/")
    old_qr_paths = [
        row[0]
        for row in _fetch_all(
            """
            SELECT qr_image_path
            FROM qr_logs
            WHERE parking_log_id = :parking_log_id
              AND used_for_exit = 0
              AND qr_image_path IS NOT NULL
            """,
            {"parking_log_id": int(parking_log_id)},
        )
    ]
    with _engine().begin() as conn:
        conn.execute(
            text(
                """
                UPDATE qr_logs
                SET is_valid = 0
                WHERE parking_log_id = :parking_log_id
                  AND used_for_exit = 0
                """
            ),
            {"parking_log_id": int(parking_log_id)},
        )
    try:
        qr_log = create_qr_log(
            student_code,
            payload,
            relative_path,
            plate=plate,
            parking_log_id=int(parking_log_id),
        )
    except Exception:
        _delete_static_asset(relative_path)
        raise
    for old_qr_path in old_qr_paths:
        if old_qr_path != relative_path:
            _delete_static_asset(old_qr_path)
    return qr_log


def _expire_stale_qr_logs(student_code: str) -> None:
    if not student_code:
        return
    with _engine().begin() as conn:
        conn.execute(
            text(
                """
                UPDATE qr_logs
                SET is_valid = 0
                WHERE student_code = :student_code
                  AND (
                      used_for_exit = 1
                      OR parking_log_id IS NULL
                      OR parking_log_id NOT IN (
                          SELECT id
                          FROM parking_log
                          WHERE student_code = :student_code
                            AND time_out IS NULL
                      )
                  )
                """
            ),
            {"student_code": student_code},
        )


def _store_gate_candidate(key: str, plate: str, gate_name: str, **extra: object) -> None:
    session[key] = {
        "plate": normalize_plate(plate),
        "gate_name": _normalize_gate_name(gate_name),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }


def _consume_gate_candidate(
    key: str,
    plate: str,
    gate_name: str,
    max_age_minutes: int = 5,
    **expected: object,
) -> bool:
    candidate = session.get(key)
    session.pop(key, None)
    if not candidate:
        return False
    if candidate.get("plate") != normalize_plate(plate):
        return False
    if _normalize_gate_name(candidate.get("gate_name")) != _normalize_gate_name(gate_name):
        return False
    for field_name, expected_value in expected.items():
        if candidate.get(field_name) != expected_value:
            return False
    created_at = candidate.get("created_at")
    if not created_at:
        return False
    age = datetime.now() - datetime.fromisoformat(created_at)
    return age <= timedelta(minutes=max_age_minutes)


def roles_required(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if g.user.get("role") not in roles:
                flash("Bạn không có quyền truy cập trang này.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def _translate_role(role: str | None) -> str:
    return {
        "admin": "quản trị viên",
        "guard": "bảo vệ",
        "student": "sinh viên",
    }.get((role or "").lower(), role or "-")


def _translate_vehicle_type(vehicle_type: str | None) -> str:
    return {
        "motorbike": "Xe máy",
        "car": "Ô tô",
        "electric": "Xe điện",
    }.get((vehicle_type or "").lower(), vehicle_type or "-")


def _translate_status(status: str | None) -> str:
    return {
        "IN_PARKING": "Đang trong bãi",
        "COMPLETED": "Đã hoàn tất",
        "READY_TO_ENTER": "Sẵn sàng vào bãi",
        "ENTRY_CONFIRMED": "Đã xác nhận vào",
        "ALREADY_IN_PARKING": "Xe đang ở trong bãi",
        "PENDING_APPROVAL": "Xe chưa được phê duyệt",
        "UNKNOWN_VEHICLE": "Không tìm thấy xe",
        "UNREADABLE": "Không đọc được biển số",
        "READY_TO_EXIT": "Sẵn sàng cho ra",
        "EXIT_CONFIRMED": "Đã xác nhận ra",
        "QR_REQUIRED": "Thiếu QR hợp lệ",
        "INVALID_QR": "QR không hợp lệ",
        "QR_MISMATCH": "QR không khớp chủ xe",
        "QR_PLATE_MISMATCH": "QR không khớp biển số",
        "QR_SESSION_MISMATCH": "QR không khớp phiên gửi xe",
        "QR_USED": "QR đã được sử dụng",
        "PARKING_AREA_FULL": "Bãi xe đã đầy",
        "PARKING_AREA_INACTIVE": "Bãi xe tạm dừng hoạt động",
        "INVALID_PARKING_AREA": "Bãi xe không hợp lệ",
        "NO_ACTIVE_SESSION": "Không có phiên gửi xe",
        "SUCCESS": "Thành công",
        "OPEN": "Mở cổng",
        "UNKNOWN": "Không xác định",
        "ACTIVE": "Đang hoạt động",
    }.get((status or "").upper(), status or "-")


@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    if not user_id:
        g.user = None
        return
    user = get_user_by_id(int(user_id))
    if not user:
        session.clear()
        g.user = None
        return
    g.user = user


@app.context_processor
def inject_user():
    return {
        "current_user": getattr(g, "user", None),
        "current_year": datetime.now().year,
        "translate_role": _translate_role,
        "translate_vehicle_type": _translate_vehicle_type,
        "translate_status": _translate_status,
    }


@app.route("/")
def index():
    area_overrides = {
        1: {
            "image": "images/cong-tran-dai-nghia-4.jpg",
            "location": "Số 184 Trần Đại Nghĩa, Bạch Mai, Hà Nội",
        },
        2: {
            "image": "images/cong-pho-vong.jpg",
            "location": "Phố Vọng, Hai Bà Trưng, Hà Nội",
        },
        3: {
            "image": "images/neu-beautiful-top.jpg",
            "location": "Khu Ký túc xá NEU",
        },
        4: {
            "image": "images/cong-tran-dai-nghia-4.jpg",
            "location": "Số 184 Trần Đại Nghĩa, Bạch Mai, Hà Nội",
        },
    }
    parking_areas = []
    for area in list_parking_areas(include_inactive=True):
        payload = dict(area)
        override = area_overrides.get(int(payload["id"]), {})
        payload["hero_image"] = override.get("image", "images/neu-beautiful-top.jpg")
        payload["location"] = override.get("location") or payload.get("description") or "Khuôn viên Đại học Kinh tế Quốc dân"
        if not payload["is_active"]:
            payload["status_label"] = "Tạm dừng"
            payload["status_class"] = "is-muted"
        elif payload["is_full"]:
            payload["status_label"] = "Đã đầy"
            payload["status_class"] = "is-danger"
        else:
            payload["status_label"] = "Đang hoạt động"
            payload["status_class"] = "is-success"
        parking_areas.append(payload)
    return render_template("index.html", parking_areas=parking_areas)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user_id = authenticate(username, password)
        if not user_id:
            flash("Sai tài khoản hoặc mật khẩu.", "error")
            return render_template("login.html", username=username), 401

        session.clear()
        session.permanent = True
        session["user_id"] = int(user_id)
        next_url = _safe_next_url(request.args.get("next"))
        return redirect(next_url or url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        student_code = (request.form.get("student_code") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        form_data = {
            "username": username,
            "full_name": full_name,
            "student_code": student_code or "",
            "email": email or "",
            "phone": phone or "",
        }

        if password != password2:
            flash("Mật khẩu nhập lại không khớp.", "error")
            return render_template("register.html", **form_data), 400

        try:
            new_id = register_student(
                username=username,
                password=password,
                full_name=full_name,
                student_code=student_code,
                email=email,
                phone=phone,
            )
        except ValueError as exc:
            msg_map = {
                "required": "Vui lòng nhập tên đăng nhập và họ tên.",
                "student_code_required": "Mã sinh viên là bắt buộc với tài khoản sinh viên.",
                "password_short": "Mật khẩu tối thiểu 6 ký tự.",
            }
            flash(msg_map.get(str(exc), "Đăng ký thất bại. Vui lòng kiểm tra lại dữ liệu."), "error")
            return render_template("register.html", **form_data), 400

        session.clear()
        session.permanent = True
        session["user_id"] = int(new_id)
        flash("Đăng ký thành công.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Đã đăng xuất.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    if g.user.get("role") == "student":
        return redirect(url_for("self_dashboard"))

    parking_areas = list_parking_areas(include_inactive=True)
    total_users = int(_fetch_scalar("SELECT COUNT(*) FROM users"))
    total_vehicles = int(_fetch_scalar("SELECT COUNT(*) FROM vehicles"))
    vehicles_in_parking = int(_fetch_scalar("SELECT COUNT(*) FROM parking_log WHERE time_out IS NULL"))
    today_checkins = int(_fetch_scalar("SELECT COUNT(*) FROM parking_log WHERE date(time_in)=date('now','localtime')"))
    today_checkouts = int(
        _fetch_scalar(
            "SELECT COUNT(*) FROM parking_log WHERE time_out IS NOT NULL AND date(time_out)=date('now','localtime')"
        )
    )
    today_revenue = int(
        _fetch_scalar(
            "SELECT COALESCE(SUM(fee),0) FROM parking_log WHERE time_out IS NOT NULL AND date(time_out)=date('now','localtime')"
        )
    )
    recent_logs = _fetch_all(
        """
        SELECT
            pl.plate,
            pl.student_code,
            COALESCE(pa.name, 'Bãi xe mặc định') AS parking_area_name,
            pl.time_in,
            pl.time_out,
            pl.status
        FROM parking_log pl
        LEFT JOIN parking_areas pa ON pa.id = pl.parking_area_id
        ORDER BY pl.id DESC
        LIMIT 10
        """
    )
    recent_vehicle_scans = _fetch_all(
        """
        SELECT COALESCE(normalized_plate, raw_text, 'UNKNOWN') AS plate, direction, status, created_at
        FROM plate_scan_log
        ORDER BY id DESC
        LIMIT 8
        """
    )
    return render_template(
        "dashboard.html",
        total_users=total_users,
        total_vehicles=total_vehicles,
        vehicles_in_parking=vehicles_in_parking,
        today_checkins=today_checkins,
        today_checkouts=today_checkouts,
        today_revenue=today_revenue,
        current_date=datetime.now().strftime("%d/%m/%Y"),
        parking_areas=parking_areas,
        recent_logs=recent_logs,
        recent_vehicle_scans=recent_vehicle_scans,
    )


@app.route("/gate-in", methods=["GET", "POST"])
@roles_required("guard", "admin")
def gate_in():
    ocr = {"plate": None, "confidence": None}
    owner = None
    system_status = None
    error_message = None
    uploaded_preview = None
    issued_qr = None
    gate_name = (request.values.get("gate_name") or "Cổng 1").strip() or "Cổng 1"
    parking_areas = list_parking_areas(include_inactive=True)
    selected_area = _resolve_selected_parking_area(request.values.get("parking_area_id"), parking_areas)

    if request.method == "POST":
        action = request.form.get("action", "scan")
        gate_name = (request.form.get("gate_name") or gate_name).strip() or "Cổng 1"
        parking_areas = list_parking_areas(include_inactive=True)
        selected_area = _find_parking_area(request.form.get("parking_area_id"), parking_areas)

        if not selected_area:
            error_message = "Vui lòng chọn bãi xe hợp lệ."
        elif action == "confirm_entry":
            plate = (request.form.get("plate") or "").strip()
            ocr["plate"] = plate or None
            if plate:
                if not _consume_gate_candidate(
                    "gate_in_candidate",
                    plate,
                    gate_name,
                    parking_area_id=str(selected_area["id"]),
                ):
                    error_message = "Phiên quét vào cổng không hợp lệ hoặc đã hết hạn. Vui lòng quét lại."
                else:
                    try:
                        result = confirm_gate_in(
                            plate=plate,
                            gate_name=gate_name,
                            parking_area_id=int(selected_area["id"]),
                        )
                        owner = result["vehicle"]
                        entry_session = result["session"]
                        system_status = result["status"]
                        try:
                            issued_qr = _issue_session_qr(
                                owner["student_code"],
                                owner["plate"],
                                entry_session["id"],
                            )
                            flash(
                                f"Đã xác nhận xe {owner['plate']} vào {selected_area['name']} và tạo QR cho lượt gửi xe.",
                                "success",
                            )
                        except Exception:
                            flash(
                                f"Đã xác nhận xe {owner['plate']} vào bãi nhưng chưa tạo được QR. Vui lòng cấp lại QR từ trang sinh viên.",
                                "error",
                            )
                    except ValueError as exc:
                        message_map = {
                            "vehicle_not_found": "Không tìm thấy xe đã được phê duyệt.",
                            "already_in_parking": "Xe nay dang o trong bai.",
                            "invalid_parking_area": "Bãi xe không hợp lệ.",
                            "parking_area_inactive": "Bãi xe đang tạm dừng nhận xe.",
                            "parking_area_full": f"{selected_area['name']} đã đầy, không thể nhận thêm xe.",
                        }
                        error_message = message_map.get(str(exc), "Khong the xac nhan xe vao.")
            else:
                error_message = "Thiếu biển số để xác nhận xe vào."
        else:
            image = request.files.get("vehicle_image")
            uploaded_preview, absolute_path, trace_path = _prepare_temporary_upload(image, prefix="gate_in")
            if not absolute_path:
                error_message = "Vui lòng tải ảnh xe để nhận diện."
            else:
                try:
                    from services.plate_service import detect_plate_text

                    plate, raw_text, confidence, source, _ = detect_plate_text(absolute_path)
                    ocr["plate"] = plate or None
                    ocr["confidence"] = round((confidence or 0.0) * 100, 2) if plate else None
                    analysis = analyze_gate_in_scan(
                        image_path=trace_path or "upload://gate_in/unknown",
                        detected_plate=plate,
                        raw_text=raw_text,
                        confidence=confidence,
                        source=source,
                        gate_name=gate_name,
                        parking_area_id=int(selected_area["id"]),
                    )
                    owner = analysis["vehicle"]
                    system_status = analysis["status"]
                    if system_status == "READY_TO_ENTER" and plate:
                        _store_gate_candidate(
                            "gate_in_candidate",
                            plate,
                            gate_name,
                            parking_area_id=str(selected_area["id"]),
                        )
                    else:
                        session.pop("gate_in_candidate", None)
                except Exception:
                    error_message = "Không thể đọc biển số từ ảnh vừa tải lên."
                finally:
                    _cleanup_temporary_upload(absolute_path)

    parking_areas = list_parking_areas(include_inactive=True)
    selected_area = _resolve_selected_parking_area(selected_area["id"] if selected_area else None, parking_areas)
    logs = list_recent_entries(limit=8, parking_area_id=int(selected_area["id"])) if selected_area else []
    return render_template(
        "gate_in.html",
        gate_name=gate_name,
        status="Đang hoạt động",
        current_time=datetime.now().strftime("%H:%M:%S"),
        current_date=datetime.now().strftime("%d/%m/%Y"),
        parking_areas=parking_areas,
        selected_area=selected_area,
        ocr=ocr,
        owner=owner,
        system_status=system_status,
        error_message=error_message,
        uploaded_preview=uploaded_preview,
        issued_qr=issued_qr,
        logs=logs,
    )


@app.route("/gate-out", methods=["GET", "POST"])
@roles_required("guard", "admin")
def gate_out():
    context = {
        "current_time": datetime.now().strftime("%H:%M:%S"),
        "plate": None,
        "student_id": None,
        "entry_time": None,
        "parking_area_name": None,
        "student_name": None,
        "vehicle": None,
        "is_valid": False,
        "fee": 0,
        "duration": None,
        "rate": "Gói sinh viên theo ngày",
        "error_message": None,
        "plate_preview": None,
        "qr_preview": None,
        "qr_payload": None,
        "decision_status": None,
        "can_confirm_exit": False,
    }

    if request.method == "POST":
        action = request.form.get("action", "scan")
        gate_name = (request.form.get("gate_name") or "Cổng 1").strip()

        if action == "confirm_exit":
            plate = (request.form.get("plate") or "").strip()
            qr_payload = request.form.get("qr_payload") or None
            context["plate"] = plate or None
            context["qr_payload"] = qr_payload
            if not _consume_gate_candidate("gate_out_candidate", plate, gate_name, qr_payload=qr_payload or ""):
                context["error_message"] = "Phiên đối chiếu ra cổng không hợp lệ hoặc đã hết hạn. Vui lòng quét lại."
            else:
                try:
                    result = confirm_gate_out(plate=plate, gate_name=gate_name, qr_payload=qr_payload)
                    session_data = result["session"]
                    vehicle = result["vehicle"]
                    context.update(
                        {
                            "student_id": vehicle["student_code"],
                            "entry_time": session_data["time_in"],
                            "parking_area_name": session_data.get("parking_area_name"),
                            "student_name": vehicle["owner_name"],
                            "vehicle": vehicle,
                            "is_valid": False,
                            "fee": session_data["fee"],
                            "duration": _duration_from_iso(session_data["time_in"]),
                            "decision_status": result["status"],
                            "can_confirm_exit": False,
                        }
                    )
                    flash(f"Đã xác nhận xe {vehicle['plate']} ra cổng.", "success")
                except ValueError as exc:
                    message_map = {
                        "vehicle_not_found": "Không tìm thấy xe hợp lệ trong hệ thống.",
                        "no_active_session": "Không có phiên gửi xe đang hoạt động.",
                        "invalid_qr": "QR không hợp lệ hoặc đã hết hạn.",
                        "qr_plate_mismatch": "QR không thuộc về biển số đang được quét.",
                        "qr_session_mismatch": "QR không khớp với lượt gửi xe hiện tại.",
                        "qr_used": "QR này đã được sử dụng để ra cổng.",
                    }
                    context["error_message"] = message_map.get(str(exc), "Không thể xác nhận xe ra.")
        else:
            plate_file = request.files.get("plate_image")
            qr_file = request.files.get("qr_image")
            context["plate_preview"], plate_absolute, plate_trace = _prepare_temporary_upload(
                plate_file,
                prefix="gate_out_plate",
            )
            context["qr_preview"], qr_absolute, _ = _prepare_temporary_upload(
                qr_file,
                prefix="gate_out_qr",
            )

            if not plate_absolute:
                _cleanup_temporary_upload(qr_absolute)
                context["error_message"] = "Vui lòng tải ảnh biển số."
                return render_template("gate_out.html", **context)
            if not qr_absolute:
                _cleanup_temporary_upload(plate_absolute)
                context["error_message"] = "Vui lòng tải ảnh QR để đối chiếu chủ xe."
                return render_template("gate_out.html", **context)

            try:
                from services.plate_service import detect_plate_text
                from services.qr_service import read_qr_from_image

                plate_text, raw_text, confidence, source, _ = detect_plate_text(plate_absolute)
                qr_student_code, qr_payload, qr_valid = read_qr_from_image(qr_absolute)
                analysis = analyze_gate_out_scan(
                    image_path=plate_trace or "upload://gate_out/plate",
                    detected_plate=plate_text,
                    raw_text=raw_text,
                    confidence=confidence,
                    source=source,
                    gate_name=gate_name,
                    qr_student_code=qr_student_code,
                    qr_payload=qr_payload,
                    qr_valid=qr_valid,
                )
                context["plate"] = plate_text or None
                context["qr_payload"] = qr_payload
                context["decision_status"] = analysis["status"]
                context["vehicle"] = analysis.get("vehicle")
                active_session = analysis.get("active_session")
                if context["vehicle"]:
                    context["student_id"] = context["vehicle"]["student_code"]
                    context["student_name"] = context["vehicle"]["owner_name"]
                if active_session:
                    context["entry_time"] = active_session["time_in"]
                    context["parking_area_name"] = active_session.get("parking_area_name")
                    context["duration"] = _duration_from_iso(active_session["time_in"])
                context["fee"] = analysis.get("fee", 0)
                context["is_valid"] = analysis["status"] == "READY_TO_EXIT"
                context["can_confirm_exit"] = analysis["status"] == "READY_TO_EXIT"
                if context["is_valid"] and plate_text:
                    _store_gate_candidate("gate_out_candidate", plate_text, gate_name, qr_payload=qr_payload or "")
                else:
                    session.pop("gate_out_candidate", None)
                if analysis["status"] == "QR_REQUIRED":
                    context["error_message"] = "Không đọc được QR hợp lệ."
                elif analysis["status"] == "INVALID_QR":
                    context["error_message"] = "QR không hợp lệ hoặc đã hết hạn."
                elif analysis["status"] == "QR_MISMATCH":
                    context["error_message"] = "QR không khớp với chủ xe."
                elif analysis["status"] == "QR_PLATE_MISMATCH":
                    context["error_message"] = "QR không thuộc về biển số đang được quét."
                elif analysis["status"] == "QR_SESSION_MISMATCH":
                    context["error_message"] = "QR không khớp với lượt gửi xe hiện tại."
                elif analysis["status"] == "QR_USED":
                    context["error_message"] = "QR này đã được sử dụng, vui lòng tạo QR mới."
                elif analysis["status"] == "NO_ACTIVE_SESSION":
                    context["error_message"] = "Không tìm thấy phiên gửi xe đang hoạt động."
                elif analysis["status"] == "UNKNOWN_VEHICLE":
                    context["error_message"] = "Không tìm thấy phương tiện trong hệ thống."
                elif analysis["status"] == "UNREADABLE":
                    context["error_message"] = "Không thể nhận diện biển số."
            except Exception:
                context["error_message"] = "Không thể phân tích dữ liệu xe ra."
            finally:
                _cleanup_temporary_upload(plate_absolute)
                _cleanup_temporary_upload(qr_absolute)

    return render_template("gate_out.html", **context)


@app.route("/history")
@roles_required("guard", "admin")
def history():
    plate = (request.args.get("plate") or "").strip()
    status = (request.args.get("status") or "").strip()
    date = (request.args.get("date") or "").strip()
    parking_areas = list_parking_areas(include_inactive=True)
    selected_area = _find_parking_area(request.args.get("parking_area_id"), parking_areas)
    scoped_area_id = int(selected_area["id"]) if selected_area else None
    summary_sql = " AND parking_area_id = :parking_area_id" if scoped_area_id else ""
    summary_params = {"parking_area_id": scoped_area_id} if scoped_area_id else {}

    context = {
        "history": list_history_records(plate=plate, status=status, date=date, parking_area_id=scoped_area_id),
        "total_revenue": int(
            _fetch_scalar(
                f"SELECT COALESCE(SUM(fee),0) FROM parking_log WHERE time_out IS NOT NULL{summary_sql}",
                summary_params,
            )
        ),
        "today_count": int(
            _fetch_scalar(
                f"SELECT COUNT(*) FROM parking_log WHERE date(time_in)=date('now','localtime'){summary_sql}",
                summary_params,
            )
        ),
        "occupancy": int(
            _fetch_scalar(
                f"SELECT COUNT(*) FROM parking_log WHERE time_out IS NULL{summary_sql}",
                summary_params,
            )
        ),
        "parking_areas": parking_areas,
        "parking_area_filter": str(scoped_area_id) if scoped_area_id else "",
        "plate_filter": plate,
        "status_filter": status,
        "date_filter": date,
    }
    return render_template("history.html", **context)


@app.route("/export-csv")
@roles_required("guard", "admin")
def export_csv():
    parking_area = _find_parking_area(
        request.args.get("parking_area_id"),
        list_parking_areas(include_inactive=True),
    )
    rows = list_history_records(
        plate=(request.args.get("plate") or "").strip(),
        status=(request.args.get("status") or "").strip(),
        date=(request.args.get("date") or "").strip(),
        parking_area_id=int(parking_area["id"]) if parking_area else None,
    )
    csv_text = build_csv_export(rows)
    filename = f"parking_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/export-excel")
@roles_required("guard", "admin")
def export_excel():
    parking_area = _find_parking_area(
        request.args.get("parking_area_id"),
        list_parking_areas(include_inactive=True),
    )
    rows = list_history_records(
        plate=(request.args.get("plate") or "").strip(),
        status=(request.args.get("status") or "").strip(),
        date=(request.args.get("date") or "").strip(),
        parking_area_id=int(parking_area["id"]) if parking_area else None,
    )
    try:
        excel_bytes = build_excel_export(rows)
    except ModuleNotFoundError:
        flash("Chưa thể xuất Excel vì thiếu dependency openpyxl trong môi trường chạy.", "error")
        return redirect(url_for("history"))
    filename = f"parking_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        excel_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin")
@roles_required("admin")
def admin():
    return render_template("admin.html")


@app.route("/admin/parking-areas")
@roles_required("admin")
def parking_areas_admin():
    parking_areas = list_parking_areas(include_inactive=True)
    selected_area = _resolve_selected_parking_area(request.args.get("area_id"), parking_areas)
    chart_days = _parse_chart_days(request.args.get("days"))
    area_stats = get_parking_area_stats(int(selected_area["id"]), days=chart_days) if selected_area else None
    return render_template(
        "parking_areas.html",
        parking_areas=parking_areas,
        selected_area=selected_area,
        area_stats=area_stats,
        selected_days=chart_days,
        parking_area_form=_parking_area_form_defaults(selected_area),
    )


@app.route("/admin/parking-areas/<int:area_id>")
@roles_required("admin")
def parking_area_detail(area_id: int):
    parking_areas = list_parking_areas(include_inactive=True)
    selected_area = _find_parking_area(area_id, parking_areas)
    if not selected_area:
        flash("Bãi xe không tồn tại.", "error")
        return redirect(url_for("parking_areas_admin"))

    chart_days = _parse_chart_days(request.args.get("days"))
    area_stats = get_parking_area_stats(area_id, days=chart_days)
    return render_template(
        "parking_areas.html",
        parking_areas=parking_areas,
        selected_area=selected_area,
        area_stats=area_stats,
        selected_days=chart_days,
        parking_area_form=_parking_area_form_defaults(selected_area),
    )


@app.post("/admin/parking-areas/<int:area_id>/update")
@roles_required("admin")
def update_parking_area_route(area_id: int):
    chart_days = _parse_chart_days(request.form.get("days"))
    try:
        update_parking_area(
            area_id,
            name=(request.form.get("name") or "").strip(),
            capacity=(request.form.get("capacity") or "").strip(),
            description=(request.form.get("description") or "").strip() or None,
            is_active=_bool_form("is_active"),
        )
        flash("Đã cập nhật thông tin bãi xe.", "success")
    except ValueError as exc:
        message_map = {
            "required": "Vui lòng nhập tên bãi xe.",
            "invalid_capacity": "Sức chứa phải là số nguyên dương.",
            "not_found": "Bãi xe không tồn tại.",
            "capacity_below_occupancy": "Không thể giảm sức chứa nhỏ hơn số xe đang gửi.",
            "occupied_area_cannot_disable": "Không thể tạm dừng bãi xe khi vẫn còn xe đang gửi.",
        }
        flash(message_map.get(str(exc), "Không thể cập nhật bãi xe."), "error")
    return redirect(url_for("parking_area_detail", area_id=area_id, days=chart_days))


@app.route("/admin/users")
@roles_required("admin")
def user_management():
    keyword = (request.args.get("q") or "").strip()
    role = (request.args.get("role") or "").strip()
    edit_user_id = (request.args.get("edit") or "").strip()
    editing_user = get_user_detail(int(edit_user_id)) if edit_user_id.isdigit() else None
    users = list_users(keyword=keyword, role=role)
    context = {
        "users": users,
        "total_users": int(_fetch_scalar("SELECT COUNT(*) FROM users")),
        "active_users": int(_fetch_scalar("SELECT COUNT(*) FROM users WHERE is_active=1")),
        "locked_users": int(_fetch_scalar("SELECT COUNT(*) FROM users WHERE is_active=0")),
        "q": keyword,
        "role_filter": role,
        "editing_user": editing_user,
        "user_form": _user_form_defaults(editing_user),
    }
    return render_template("user_management.html", **context)


@app.post("/admin/users/create")
@roles_required("admin")
def create_user_route():
    try:
        create_user(
            username=(request.form.get("username") or "").strip(),
            password=request.form.get("password") or "",
            full_name=(request.form.get("full_name") or "").strip(),
            role=(request.form.get("role") or "student").strip(),
            student_code=(request.form.get("student_code") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            is_active=_bool_form("is_active"),
        )
        flash("Đã tạo người dùng mới.", "success")
    except ValueError as exc:
        message_map = {
            "required": "Vui lòng nhập đầy đủ tên đăng nhập và họ tên.",
            "student_code_required": "Tài khoản sinh viên bắt buộc phải có mã sinh viên.",
            "password_short": "Mật khẩu mới tối thiểu 6 ký tự.",
            "invalid_role": "Vai trò không hợp lệ.",
            "conflict": "Tên đăng nhập, email hoặc mã sinh viên đã tồn tại.",
        }
        flash(message_map.get(str(exc), "Không thể tạo người dùng."), "error")
    return redirect(url_for("user_management"))


@app.post("/admin/users/<int:user_id>/update")
@roles_required("admin")
def update_user_route(user_id: int):
    try:
        update_user(
            user_id,
            username=(request.form.get("username") or "").strip(),
            full_name=(request.form.get("full_name") or "").strip(),
            role=(request.form.get("role") or "student").strip(),
            student_code=(request.form.get("student_code") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            password=(request.form.get("password") or "").strip() or None,
            is_active=_bool_form("is_active"),
        )
        flash("Đã cập nhật người dùng.", "success")
    except ValueError as exc:
        message_map = {
            "required": "Vui lòng nhập đầy đủ tên đăng nhập và họ tên.",
            "student_code_required": "Tài khoản sinh viên bắt buộc phải có mã sinh viên.",
            "password_short": "Mật khẩu mới tối thiểu 6 ký tự.",
            "invalid_role": "Vai trò không hợp lệ.",
            "conflict": "Dữ liệu cập nhật bị trùng lặp.",
        }
        flash(message_map.get(str(exc), "Không thể cập nhật người dùng."), "error")
        return redirect(url_for("user_management", edit=user_id))
    return redirect(url_for("user_management"))


@app.post("/admin/users/<int:user_id>/toggle")
@roles_required("admin")
def toggle_user_route(user_id: int):
    if g.user and g.user["id"] == user_id:
        flash("Không thể khóa hoặc mở khóa chính tài khoản hiện tại.", "error")
        return redirect(url_for("user_management"))
    toggle_user_active(user_id)
    flash("Đã cập nhật trạng thái người dùng.", "success")
    return redirect(url_for("user_management"))


@app.post("/admin/users/<int:user_id>/delete")
@roles_required("admin")
def delete_user_route(user_id: int):
    if g.user and g.user["id"] == user_id:
        flash("Không thể xóa chính tài khoản hiện tại.", "error")
        return redirect(url_for("user_management"))

    user = get_user_detail(user_id)
    if not user:
        flash("Người dùng không tồn tại.", "error")
        return redirect(url_for("user_management"))

    linked_vehicles = int(
        _fetch_scalar("SELECT COUNT(*) FROM vehicles WHERE student_code = :student_code", {"student_code": user.get("student_code") or ""})
    )
    linked_logs = int(
        _fetch_scalar("SELECT COUNT(*) FROM parking_log WHERE student_code = :student_code", {"student_code": user.get("student_code") or ""})
    )
    if linked_vehicles or linked_logs:
        flash("Không thể xóa người dùng đã có phương tiện hoặc lịch sử gửi xe.", "error")
        return redirect(url_for("user_management"))

    delete_user(user_id)
    flash("Đã xóa người dùng.", "success")
    return redirect(url_for("user_management"))


@app.route("/admin/vehicles")
@roles_required("admin")
def vehicles():
    plate = (request.args.get("plate") or "").strip()
    vehicle_type = (request.args.get("type") or "").strip()
    edit_vehicle_id = (request.args.get("edit") or "").strip()
    editing_vehicle = get_vehicle_by_id(int(edit_vehicle_id)) if edit_vehicle_id.isdigit() else None
    vehicles_rows = list_vehicles(plate=plate, vehicle_type=vehicle_type)
    active_map = _active_parking_map()
    context = {
        "vehicles": vehicles_rows,
        "total": int(_fetch_scalar("SELECT COUNT(*) FROM vehicles")),
        "in_parking": int(_fetch_scalar("SELECT COUNT(*) FROM parking_log WHERE time_out IS NULL")),
        "pending": int(_fetch_scalar("SELECT COUNT(*) FROM vehicles WHERE is_active=0")),
        "approved": int(_fetch_scalar("SELECT COUNT(*) FROM vehicles WHERE is_active=1")),
        "plate_filter": plate,
        "type_filter": vehicle_type,
        "active_map": active_map,
        "editing_vehicle": editing_vehicle,
        "vehicle_form": _vehicle_form_defaults(editing_vehicle),
    }
    return render_template("vehicles.html", **context)


@app.post("/admin/vehicles/create")
@roles_required("admin")
def create_vehicle_route():
    student_code = (request.form.get("student_code") or "").strip()
    user_exists = int(_fetch_scalar("SELECT COUNT(*) FROM users WHERE student_code = :student_code", {"student_code": student_code}))
    if student_code and not user_exists:
        flash("Mã sinh viên không tồn tại trong hệ thống.", "error")
        return redirect(url_for("vehicles"))

    image_path = None
    try:
        image_path, _ = _save_upload(request.files.get("image"), prefix="vehicle_admin")
        create_vehicle(
            plate=(request.form.get("plate") or "").strip(),
            student_code=student_code,
            owner_name=(request.form.get("owner_name") or "").strip() or None,
            vehicle_type=(request.form.get("vehicle_type") or "motorbike").strip(),
            brand=(request.form.get("brand") or "").strip() or None,
            color=(request.form.get("color") or "").strip() or None,
            image_path=image_path,
            is_active=_bool_form("is_active"),
        )
        flash("Đã tạo phương tiện mới.", "success")
    except ValueError as exc:
        message_map = {
            "required": "Vui lòng nhập biển số và mã sinh viên.",
            "conflict": "Biển số xe đã tồn tại.",
            "invalid_vehicle_type": "Loại xe không hợp lệ.",
        }
        _delete_static_asset(image_path)
        flash(message_map.get(str(exc), "Không thể tạo phương tiện."), "error")
    return redirect(url_for("vehicles"))


@app.post("/admin/vehicles/<int:vehicle_id>/update")
@roles_required("admin")
def update_vehicle_route(vehicle_id: int):
    current_vehicle = get_vehicle_by_id(vehicle_id)
    if not current_vehicle:
        flash("Phương tiện không tồn tại.", "error")
        return redirect(url_for("vehicles"))
    if get_active_session_by_plate(current_vehicle["plate"]):
        flash("Không thể chỉnh sửa phương tiện khi xe đang ở trong bãi.", "error")
        return redirect(url_for("vehicles", edit=vehicle_id))

    student_code = (request.form.get("student_code") or "").strip()
    user_exists = int(_fetch_scalar("SELECT COUNT(*) FROM users WHERE student_code = :student_code", {"student_code": student_code}))
    if student_code and not user_exists:
        flash("Mã sinh viên không tồn tại trong hệ thống.", "error")
        return redirect(url_for("vehicles", edit=vehicle_id))

    image_path = None
    try:
        image_path, _ = _save_upload(request.files.get("image"), prefix="vehicle_admin")
        update_vehicle(
            vehicle_id,
            plate=(request.form.get("plate") or "").strip(),
            student_code=student_code,
            owner_name=(request.form.get("owner_name") or "").strip() or None,
            vehicle_type=(request.form.get("vehicle_type") or "motorbike").strip(),
            brand=(request.form.get("brand") or "").strip() or None,
            color=(request.form.get("color") or "").strip() or None,
            image_path=image_path,
            is_active=_bool_form("is_active"),
        )
        if image_path and current_vehicle.get("image_path") and current_vehicle["image_path"] != image_path:
            _delete_static_asset(current_vehicle["image_path"])
        flash("Đã cập nhật phương tiện.", "success")
    except ValueError as exc:
        message_map = {
            "required": "Vui lòng nhập biển số và mã sinh viên.",
            "conflict": "Biển số xe đã tồn tại.",
            "invalid_vehicle_type": "Loại xe không hợp lệ.",
        }
        _delete_static_asset(image_path)
        flash(message_map.get(str(exc), "Không thể cập nhật phương tiện."), "error")
        return redirect(url_for("vehicles", edit=vehicle_id))
    return redirect(url_for("vehicles"))


@app.post("/admin/vehicles/<int:vehicle_id>/toggle")
@roles_required("admin")
def toggle_vehicle_route(vehicle_id: int):
    vehicle = get_vehicle_by_id(vehicle_id)
    if not vehicle:
        flash("Phương tiện không tồn tại.", "error")
        return redirect(url_for("vehicles"))
    if vehicle["is_active"] and get_active_session_by_plate(vehicle["plate"]):
        flash("Không thể tạm khóa phương tiện khi xe đang ở trong bãi.", "error")
        return redirect(url_for("vehicles"))
    toggle_vehicle_active(vehicle_id)
    flash("Đã cập nhật trạng thái phương tiện.", "success")
    return redirect(url_for("vehicles"))


@app.post("/admin/vehicles/<int:vehicle_id>/approve")
@roles_required("admin")
def approve_vehicle_route(vehicle_id: int):
    vehicle = get_vehicle_by_id(vehicle_id)
    if not vehicle:
        flash("Phương tiện không tồn tại.", "error")
        return redirect(url_for("vehicles"))
    set_vehicle_active(vehicle_id, True)
    flash("Đã phê duyệt phương tiện.", "success")
    return redirect(url_for("vehicles"))


@app.post("/admin/vehicles/<int:vehicle_id>/delete")
@roles_required("admin")
def delete_vehicle_route(vehicle_id: int):
    vehicle = get_vehicle_by_id(vehicle_id)
    if not vehicle:
        flash("Phương tiện không tồn tại.", "error")
        return redirect(url_for("vehicles"))
    if get_active_session_by_plate(vehicle["plate"]):
        flash("Không thể xóa phương tiện đang ở trong bãi.", "error")
        return redirect(url_for("vehicles"))
    delete_vehicle(vehicle_id)
    _delete_static_asset(vehicle.get("image_path"))
    flash("Đã xóa phương tiện.", "success")
    return redirect(url_for("vehicles"))


@app.route("/my-vehicle")
@roles_required("student")
def my_vehicle():
    student_code = g.user.get("student_code") or ""
    vehicles_rows = list_vehicles(student_code=student_code)
    active_map = _active_parking_map()
    in_parking = sum(1 for row in vehicles_rows if active_map.get(normalize_plate(row["plate"])))
    selected_vehicle_id = (request.args.get("vehicle_id") or "").strip()
    selected_vehicle = next((row for row in vehicles_rows if str(row["id"]) == selected_vehicle_id), None)
    if not selected_vehicle:
        selected_vehicle = vehicles_rows[0] if vehicles_rows else None
    edit_mode = (request.args.get("edit") or "").strip() == "1" and bool(selected_vehicle)
    selected_history = []
    selected_active_session = None
    selected_history_count = 0
    selected_total_spent = 0
    if selected_vehicle:
        selected_active_session = get_active_session_by_plate(selected_vehicle["plate"])
        selected_history = _fetch_all(
            """
            SELECT
                pl.id,
                pl.plate,
                pl.time_in,
                pl.time_out,
                pl.fee,
                pl.status,
                pl.gate_in,
                pl.gate_out,
                COALESCE(pa.name, 'Bãi xe mặc định') AS parking_area_name
            FROM parking_log pl
            LEFT JOIN parking_areas pa ON pa.id = pl.parking_area_id
            WHERE replace(replace(replace(upper(pl.plate), '-', ''), ' ', ''), '.', '') = :plate
              AND pl.student_code = :student_code
            ORDER BY pl.id DESC
            LIMIT 12
            """,
            {"plate": normalize_plate(selected_vehicle["plate"]), "student_code": student_code},
        )
        selected_history_count = int(
            _fetch_scalar(
                """
                SELECT COUNT(*)
                FROM parking_log
                WHERE replace(replace(replace(upper(plate), '-', ''), ' ', ''), '.', '') = :plate
                  AND student_code = :student_code
                """,
                {"plate": normalize_plate(selected_vehicle["plate"]), "student_code": student_code},
            )
        )
        selected_total_spent = int(
            _fetch_scalar(
                """
                SELECT COALESCE(SUM(fee), 0)
                FROM parking_log
                WHERE replace(replace(replace(upper(plate), '-', ''), ' ', ''), '.', '') = :plate
                  AND student_code = :student_code
                """,
                {"plate": normalize_plate(selected_vehicle["plate"]), "student_code": student_code},
            )
        )
    return render_template(
        "student_my_vehicle.html",
        total_vehicle=len(vehicles_rows),
        in_parking=in_parking,
        main_vehicle=selected_vehicle,
        vehicles=vehicles_rows,
        active_map=active_map,
        edit_mode=edit_mode,
        selected_history=selected_history,
        selected_active_session=selected_active_session,
        selected_history_count=selected_history_count,
        selected_total_spent=selected_total_spent,
    )


@app.post("/my-vehicle/<int:vehicle_id>/update")
@roles_required("student")
def update_my_vehicle_route(vehicle_id: int):
    student_code = g.user.get("student_code") or ""
    current_vehicle = get_vehicle_by_id(vehicle_id)
    if not current_vehicle or current_vehicle.get("student_code") != student_code:
        flash("Phương tiện không tồn tại hoặc không thuộc về bạn.", "error")
        return redirect(url_for("my_vehicle"))
    if get_active_session_by_plate(current_vehicle["plate"]):
        flash("Không thể chỉnh sửa phương tiện khi xe đang ở trong bãi.", "error")
        return redirect(url_for("my_vehicle", vehicle_id=vehicle_id))

    image_path = None
    try:
        image_path, _ = _save_upload(request.files.get("image"), prefix="student_vehicle")
        update_vehicle(
            vehicle_id,
            plate=(request.form.get("plate") or "").strip(),
            student_code=student_code,
            owner_name=g.user.get("full_name"),
            vehicle_type=(request.form.get("vehicle_type") or "motorbike").strip(),
            brand=(request.form.get("brand") or "").strip() or None,
            color=(request.form.get("color") or "").strip() or None,
            image_path=image_path,
            is_active=bool(current_vehicle.get("is_active")),
        )
        if image_path and current_vehicle.get("image_path") and current_vehicle["image_path"] != image_path:
            _delete_static_asset(current_vehicle["image_path"])
        flash("Đã cập nhật thông tin xe.", "success")
    except ValueError as exc:
        message_map = {
            "required": "Vui lòng nhập biển số xe.",
            "conflict": "Biển số xe đã tồn tại trong hệ thống.",
            "invalid_vehicle_type": "Loại xe không hợp lệ.",
        }
        _delete_static_asset(image_path)
        flash(message_map.get(str(exc), "Không thể cập nhật xe."), "error")
        return redirect(url_for("my_vehicle", vehicle_id=vehicle_id, edit=1))
    return redirect(url_for("my_vehicle", vehicle_id=vehicle_id))


@app.post("/my-vehicle/<int:vehicle_id>/delete")
@roles_required("student")
def delete_my_vehicle_route(vehicle_id: int):
    student_code = g.user.get("student_code") or ""
    vehicle = get_vehicle_by_id(vehicle_id)
    if not vehicle or vehicle.get("student_code") != student_code:
        flash("Phương tiện không tồn tại hoặc không thuộc về bạn.", "error")
        return redirect(url_for("my_vehicle"))
    if get_active_session_by_plate(vehicle["plate"]):
        flash("Không thể xóa phương tiện khi xe đang ở trong bãi.", "error")
        return redirect(url_for("my_vehicle", vehicle_id=vehicle_id))

    delete_vehicle(vehicle_id)
    _delete_static_asset(vehicle.get("image_path"))
    flash("Đã xóa phương tiện.", "success")
    return redirect(url_for("my_vehicle"))


@app.route("/my-new-vehicle", methods=["GET", "POST"])
@roles_required("student")
def my_new_vehicle():
    form_data = {"plate": "", "vehicle_type": "motorbike", "brand": "", "color": ""}
    if request.method == "POST":
        form_data["plate"] = (request.form.get("plate") or "").strip()
        form_data["vehicle_type"] = (request.form.get("vehicle_type") or "motorbike").strip()
        form_data["brand"] = (request.form.get("brand") or "").strip()
        form_data["color"] = (request.form.get("color") or "").strip()

        image_path = None
        try:
            image_path, _ = _save_upload(request.files.get("image"), prefix="student_vehicle")
            create_vehicle(
                plate=form_data["plate"],
                student_code=g.user.get("student_code") or "",
                owner_name=g.user.get("full_name"),
                vehicle_type=form_data["vehicle_type"],
                brand=form_data["brand"] or None,
                color=form_data["color"] or None,
                image_path=image_path,
                is_active=False,
            )
            flash("Đã gửi yêu cầu đăng ký phương tiện. Vui lòng chờ phê duyệt.", "success")
            return redirect(url_for("my_vehicle"))
        except ValueError as exc:
            message_map = {
                "required": "Vui lòng nhập biển số xe.",
                "conflict": "Biển số xe đã tồn tại trong hệ thống.",
            }
            _delete_static_asset(image_path)
            flash(message_map.get(str(exc), "Không thể lưu xe mới."), "error")
            return render_template("student_new_vehicle.html", form_data=form_data), 400

    return render_template("student_new_vehicle.html", form_data=form_data)


@app.route("/student-qr", methods=["GET", "POST"])
@roles_required("student")
def student_qr():
    student_code = g.user.get("student_code") or str(g.user.get("id"))
    _expire_stale_qr_logs(student_code)
    active_sessions = _fetch_all(
        """
        SELECT
            pl.id,
            pl.plate,
            pl.time_in,
            pl.gate_in,
            COALESCE(pa.name, 'Bãi xe mặc định') AS parking_area_name
        FROM parking_log pl
        LEFT JOIN parking_areas pa ON pa.id = pl.parking_area_id
        WHERE pl.student_code=:student_code AND pl.time_out IS NULL
        ORDER BY pl.id DESC
        """,
        {"student_code": student_code},
    )
    vehicles_by_plate = {
        normalize_plate(item["plate"]): item
        for item in list_vehicles(student_code=student_code)
    }

    def _build_qr_sessions() -> list[dict]:
        sessions: list[dict] = []
        for active_session in active_sessions:
            parking_log_id = int(active_session[0])
            plate = active_session[1]
            vehicle = vehicles_by_plate.get(normalize_plate(plate)) or get_vehicle_by_plate(plate, active_only=False)
            ticket = get_active_qr_for_session(parking_log_id)
            if vehicle and not ticket:
                try:
                    _issue_session_qr(student_code, vehicle["plate"], parking_log_id)
                    ticket = get_active_qr_for_session(parking_log_id)
                except Exception:
                    ticket = None
            sessions.append(
                {
                    "session_id": parking_log_id,
                    "plate": plate,
                    "time_in": active_session[2],
                    "gate_in": active_session[3],
                    "parking_area_name": active_session[4],
                    "vehicle": vehicle,
                    "ticket": ticket,
                    "qr_image_path": ticket["qr_image_path"] if ticket else None,
                }
            )
        return sessions

    if request.method == "POST":
        session_id_raw = (request.form.get("session_id") or "").strip()
        if not session_id_raw.isdigit():
            flash("Không xác định được phiên gửi xe cần cấp lại QR.", "error")
            return redirect(url_for("student_qr"))
        parking_log_id = int(session_id_raw)
        target_session = next((item for item in active_sessions if int(item[0]) == parking_log_id), None)
        if not target_session:
            flash("Phiên gửi xe không còn hoạt động.", "error")
            return redirect(url_for("student_qr"))
        vehicle = vehicles_by_plate.get(normalize_plate(target_session[1])) or get_vehicle_by_plate(target_session[1], active_only=False)
        if not vehicle:
            flash("Không tìm thấy thông tin phương tiện cho phiên gửi xe này.", "error")
            return redirect(url_for("student_qr"))
        try:
            _issue_session_qr(student_code, vehicle["plate"], parking_log_id)
            flash(f"Đã cấp lại QR cho xe {vehicle['plate']}.", "success")
        except Exception:
            flash("Không thể tạo mã QR lúc này.", "error")
        return redirect(url_for("student_qr"))

    qr_sessions = _build_qr_sessions()

    return render_template(
        "student_qr.html",
        user=g.user,
        qr_sessions=qr_sessions,
        active_sessions_count=len(qr_sessions),
    )


@app.route("/self-dashboard")
@roles_required("student")
def self_dashboard():
    student_code = g.user.get("student_code")
    vehicles_rows = list_vehicles(student_code=student_code or "")
    recent_logs = _fetch_all(
        """
        SELECT
            pl.plate,
            pl.time_in,
            pl.time_out,
            pl.fee,
            pl.status,
            COALESCE(pa.name, 'Bãi xe mặc định') AS parking_area_name
        FROM parking_log pl
        LEFT JOIN parking_areas pa ON pa.id = pl.parking_area_id
        WHERE pl.student_code=:student_code
        ORDER BY pl.id DESC
        LIMIT 8
        """,
        {"student_code": student_code or ""},
    )
    active_vehicles = _fetch_all(
        """
        SELECT
            pl.plate,
            pl.time_in,
            COALESCE(pa.name, 'Bãi xe mặc định') AS parking_area_name
        FROM parking_log pl
        LEFT JOIN parking_areas pa ON pa.id = pl.parking_area_id
        WHERE pl.student_code=:student_code AND pl.time_out IS NULL
        ORDER BY pl.id DESC
        """,
        {"student_code": student_code or ""},
    )
    return render_template(
        "self_dashboard.html",
        user=g.user,
        current_date=datetime.now().strftime("%d/%m/%Y"),
        my_vehicles_count=len(vehicles_rows),
        vehicles_in_parking=len(active_vehicles),
        recent_sessions_count=len(recent_logs),
        recent_logs=recent_logs,
        active_vehicles=active_vehicles,
    )


@app.route("/self-history")
@roles_required("student")
def self_history():
    student_code = g.user.get("student_code")
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    where = ["pl.student_code=:student_code"]
    params = {"student_code": student_code or ""}
    if date_from:
        where.append("date(pl.time_in) >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("date(pl.time_in) <= :date_to")
        params["date_to"] = date_to

    where_sql = f"WHERE {' AND '.join(where)}"
    histories = _fetch_all(
        f"""
        SELECT
            pl.plate,
            pl.time_in,
            pl.time_out,
            pl.fee,
            pl.status,
            pl.gate_in,
            pl.gate_out,
            COALESCE(pa.name, 'Bãi xe mặc định') AS parking_area_name
        FROM parking_log pl
        LEFT JOIN parking_areas pa ON pa.id = pl.parking_area_id
        {where_sql}
        ORDER BY pl.id DESC
        LIMIT 150
        """,
        params,
    )
    return render_template(
        "self_history.html",
        user=g.user,
        histories=histories,
        total_sessions=len(histories),
        total_spent=sum(int(row[3] or 0) for row in histories),
        date_from=date_from,
        date_to=date_to,
    )


if __name__ == "__main__":
    app.run(debug=True)

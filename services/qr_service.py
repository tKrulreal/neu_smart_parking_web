import datetime as dt
from pathlib import Path

import qrcode
import time
from config import Config


def _qr_decoder():
    import cv2
    from pyzbar.pyzbar import decode

    return cv2, decode


def create_qr_asset(student_id: str, output_dir: str = None) -> tuple[str, str]:
    if output_dir is None:
        output_dir = Config.QR_FOLDER
    created_at = dt.datetime.now().isoformat(timespec="microseconds")
    payload = f"{student_id}|{created_at}"

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = created_at.replace(":", "-")
    file_path = out_dir / f"{student_id}_{safe_ts}.png"

    img = qrcode.make(payload)
    img.save(file_path)
    return payload, str(file_path)


def create_qr_for_student(student_id: str, output_dir: str = None) -> str:
    _, file_path = create_qr_asset(student_id, output_dir=output_dir)
    return file_path
def parse_qr_payload(payload: str):
    if not payload or "|" not in payload:
        return None, None
    student_id, created_at_str = payload.split("|", 1)
    return student_id.strip(), created_at_str.strip()


def is_qr_valid_time(created_at_str: str, max_age_minutes: int | None = None) -> bool:
    if not created_at_str:
        return False
    try:
        qr_created_at = dt.datetime.fromisoformat(created_at_str)
    except ValueError:
        return False
    if max_age_minutes is None:
        return True
    now = dt.datetime.now()
    age = now - qr_created_at
    if age.total_seconds() < 0:
        return False
    return age <= dt.timedelta(minutes=max_age_minutes)


def read_qr_from_image(image_path: str, qr_max_age_minutes: int | None = None):
    cv2, decode = _qr_decoder()
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"Khong doc duoc anh QR: {image_path}")
    codes = decode(img)
    if not codes:
        return None, None, False
    payload = codes[0].data.decode("utf-8").strip()
    student_id, created_at_str = parse_qr_payload(payload)
    valid_qr = is_qr_valid_time(created_at_str, max_age_minutes=qr_max_age_minutes)
    return student_id, payload, valid_qr


def scan_qr_from_camera(
    camera_index: int = 0,
    timeout_sec: int = 20,
    mirror: bool = True,
    show_guide: bool = True,
    qr_max_age_minutes: int | None = None,
):
    cv2, decode = _qr_decoder()
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        raise RuntimeError(f"Khong mo duoc camera index={camera_index}")


    start = time.time()
    payload = None

    try:
        while time.time() - start < timeout_sec:
            ok, frame = cap.read()
            if not ok:
                continue

            if mirror:
                frame = cv2.flip(frame, 1)

            h, w = frame.shape[:2]
            box_size = int(min(w, h) * 0.55)
            x1 = (w - box_size) // 2
            y1 = (h - box_size) // 2
            x2 = x1 + box_size
            y2 = y1 + box_size

            roi = frame[y1:y2, x1:x2]
            codes = decode(roi) if show_guide else []
            if not codes:
                codes = decode(frame)
            if codes:
                payload = codes[0].data.decode("utf-8").strip()
                break

            if show_guide:
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
                cv2.putText(
                    frame,
                    "Dat ma QR vao khung vang",
                    (10, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 255),
                    2,
                )
            cv2.putText(
                frame,
                "Dang quet QR... (nhan q de huy)",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )
            cv2.imshow("QR Scanner", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    if not payload:
        return None, None, False
    student_id, created_at_str = parse_qr_payload(payload)
    valid_qr = is_qr_valid_time(created_at_str, max_age_minutes=qr_max_age_minutes)
    return student_id, payload, valid_qr

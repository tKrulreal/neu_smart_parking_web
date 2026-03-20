import argparse
import re
from typing import List, Tuple

import cv2
import easyocr
from ultralytics import YOLO
MODEL_PATH = "models/license_plate_detector.pt"
ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
UPSCALE_FACTOR = 3.0
EARLY_STOP_PATTERN_SCORE = 8
EARLY_STOP_CONF = 0.75
TWO_LINE_RATIO_THRESHOLD = 1.9
FULL_PLATE_PATTERN = re.compile(
    r"([A-Z0-9]{2})\s*[-_.]?\s*([A-Z0-9]{2})\s*[-_.]?\s*(\d{4,5})"
)
NORMALIZED_PLATE_PATTERN = re.compile(r"^[A-Z0-9]{4}\d{4,5}$")

_MODEL = None
_OCR = None


def get_model():
    global _MODEL
    if _MODEL is None:
        _MODEL = YOLO(MODEL_PATH)
    return _MODEL


def get_ocr():
    global _OCR
    if _OCR is None:
        _OCR = easyocr.Reader(["en"])
    return _OCR


def normalize_plate_text(text: str) -> str:
    text = text.upper()
    replacements = {
        "O": "0",
        "Q": "0",
        "I": "1",
        "L": "1",
        "Z": "2",
        "S": "5",
        "B": "8",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"[^A-Z0-9]", "", text)


def normalize_alnum_text(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def extract_plate_by_user_format(raw_text: str) -> Tuple[str, str]:
    if not raw_text:
        return "", ""
    cleaned = raw_text.upper()
    match = FULL_PLATE_PATTERN.search(cleaned)
    if not match:
        return "", ""

    top_left, top_right, bottom = match.groups()
    normalized = f"{top_left}{top_right}{bottom}"
    pretty = f"{top_left}-{top_right} {bottom}"
    return normalized, pretty


def compose_two_line_plate(raw_top: str, raw_bottom: str) -> Tuple[str, str]:
    top = normalize_alnum_text(raw_top)
    bottom = normalize_alnum_text(raw_bottom)
    if len(top) != 4 or len(bottom) not in (4, 5) or not bottom.isdigit():
        return "", ""

    normalized = f"{top}{bottom}"
    pretty = f"{top[:2]}-{top[2:]} {bottom}"
    return normalized, pretty


def vn_plate_pattern_score(text: str) -> int:
    score = 0
    if not text:
        return score

    # Rule from your format:
    # top (4 chars) + bottom (4/5 digits) => total 8/9 chars
    if len(text) in (8, 9):
        score += 5
        if re.fullmatch(r"[A-Z0-9]{4}\d{4,5}", text):
            score += 6

    if re.match(r"^\d{2}", text):
        score += 2
    if re.search(r"\d{4,5}$", text):
        score += 1
    if re.fullmatch(r"[A-Z0-9]+", text):
        score += 1
    return score


def run_easyocr(image, allowlist: str = ALLOWLIST) -> Tuple[str, float]:
    ocr = get_ocr()
    result = ocr.readtext(
        image,
        detail=1,
        paragraph=False,
        allowlist=allowlist,
    )
    if not result:
        return "", 0.0

    raw = "".join(item[1] for item in result).strip()
    avg_conf = sum(float(item[2]) for item in result) / len(result)
    return raw, avg_conf


def preprocess_variants(plate_img):
    gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(
        gray,
        None,
        fx=UPSCALE_FACTOR,
        fy=UPSCALE_FACTOR,
        interpolation=cv2.INTER_CUBIC,
    )

    _, th1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants = [("otsu", th1)]

    blur = cv2.bilateralFilter(gray, 9, 75, 75)
    _, th2 = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(("bilateral_otsu", th2))

    th3 = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )
    variants.append(("adaptive", th3))
    return variants


def split_two_lines(img_bin):
    h = img_bin.shape[0]
    mid = h // 2
    top = img_bin[:mid, :]
    bottom = img_bin[mid:, :]
    return top, bottom


def should_use_two_line_ocr(plate_img) -> bool:
    h, w = plate_img.shape[:2]
    if h == 0:
        return False
    ratio = w / h
    return ratio <= TWO_LINE_RATIO_THRESHOLD


def append_candidate(
    candidates: List[Tuple[str, str, float, str]],
    normalized: str,
    raw: str,
    conf: float,
    source: str,
):
    if normalized:
        candidates.append((normalized, raw, conf, source))


def choose_best_candidate(candidates: List[Tuple[str, str, float, str]]) -> Tuple[str, str, float, str]:
    if not candidates:
        return "", "", 0.0, ""

    best = None
    best_score = -1.0
    for normalized, raw, conf, source in candidates:
        pattern_score = vn_plate_pattern_score(normalized)
        total_score = pattern_score * 10 + conf
        if total_score > best_score:
            best_score = total_score
            best = (normalized, raw, conf, source)
    return best


def is_valid_plate_by_user_rule(normalized: str) -> bool:
    return bool(NORMALIZED_PLATE_PATTERN.fullmatch(normalized))


def detect_plate_text(image_path: str):
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"Khong doc duoc anh: {image_path}")

    model = get_model()
    yolo_result = model(img)[0]

    best_box = None
    for box in yolo_result.boxes:
        conf = float(box.conf[0])
        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        if best_box is None or conf > best_box["conf"]:
            best_box = {"conf": conf, "xyxy": (x1, y1, x2, y2)}

    if best_box is None:
        return "", "", 0.0, "", None

    x1, y1, x2, y2 = best_box["xyxy"]
    pad_x = int((x2 - x1) * 0.08)
    pad_y = int((y2 - y1) * 0.12)

    h, w = img.shape[:2]
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)

    plate = img[y1:y2, x1:x2]
    if plate.size == 0:
        return "", "", 0.0, "", None

    variants = preprocess_variants(plate)
    use_two_line_ocr = should_use_two_line_ocr(plate)
    candidates = []
    debug_image = None

    for name, variant in variants:
        raw_full, conf_full = run_easyocr(variant)
        strict_norm, strict_pretty = extract_plate_by_user_format(raw_full)
        if strict_norm:
            append_candidate(
                candidates,
                strict_norm,
                strict_pretty,
                conf_full + 0.05,
                f"{name}_full_strict",
            )
            norm_full = strict_norm
        else:
            norm_full = normalize_plate_text(raw_full)
            append_candidate(candidates, norm_full, raw_full, conf_full, f"{name}_full")

        if use_two_line_ocr:
            top, bottom = split_two_lines(variant)
            raw_top, conf_top = run_easyocr(top)
            raw_bottom, conf_bottom = run_easyocr(bottom)

            norm_two_line, pretty_two_line = compose_two_line_plate(raw_top, raw_bottom)
            if not norm_two_line:
                raw_two_line = f"{raw_top}{raw_bottom}".strip()
                strict_norm, strict_pretty = extract_plate_by_user_format(raw_two_line)
                if strict_norm:
                    norm_two_line, pretty_two_line = strict_norm, strict_pretty
                else:
                    norm_two_line = normalize_plate_text(raw_two_line)
                    pretty_two_line = raw_two_line
            conf_two_line = (
                (conf_top + conf_bottom) / 2 if (conf_top or conf_bottom) else 0.0
            )
            append_candidate(
                candidates,
                norm_two_line,
                pretty_two_line,
                conf_two_line + (0.08 if re.fullmatch(r"[A-Z0-9]{4}\d{4,5}", norm_two_line) else 0.0),
                f"{name}_2lines",
            )

        if debug_image is None:
            debug_image = variant

        if norm_full:
            pattern_score = vn_plate_pattern_score(norm_full)
            if (
                pattern_score >= EARLY_STOP_PATTERN_SCORE
                and conf_full >= EARLY_STOP_CONF
            ):
                break

    dedup = {}
    for normalized, raw, conf, source in candidates:
        current = dedup.get(normalized)
        if current is None or conf > current[2]:
            dedup[normalized] = (normalized, raw, conf, source)
    candidates = list(dedup.values())

    normalized, raw, ocr_conf, source = choose_best_candidate(candidates)
    if not is_valid_plate_by_user_rule(normalized):
        return "", raw, 0.0, source, debug_image
    final_score = (float(best_box["conf"]) + float(ocr_conf)) / 2.0
    return normalized, raw, final_score, source, debug_image

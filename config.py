import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = "neu-smart-parking-secret-key"
    DATABASE_URL = "sqlite:///" + os.path.join(BASE_DIR, "parking.db")
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    QR_FOLDER = os.path.join(BASE_DIR, "static", "qr_out")
    EXPORT_FOLDER = os.path.join(BASE_DIR, "static", "exports")
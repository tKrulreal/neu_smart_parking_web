import os
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash
from config import Config

_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(Config.DATABASE_URL, future=True)
    return _ENGINE


def ensure_directories():
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(Config.QR_FOLDER, exist_ok=True)
    os.makedirs(Config.EXPORT_FOLDER, exist_ok=True)

def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name=:name
            """
        ),
        {"name": table_name},
    ).fetchone()
    return bool(row)


def column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(str(row[1]) == column_name for row in rows)


def users_table_allows_guard(conn) -> bool:
    sql = conn.execute(
        text(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type='table' AND name='users'
            """
        )
    ).scalar()
    return bool(sql) and "guard" in str(sql).lower()


def migrate_users_table_add_guard_role(conn) -> None:
    if not table_exists(conn, "users"):
        return
    if users_table_allows_guard(conn):
        return

    conn.execute(text("ALTER TABLE users RENAME TO users_old"))
    create_users_table(conn)
    conn.execute(
        text(
            """
            INSERT INTO users
            (id, username, password_hash, full_name, role, student_code, email, phone, is_active, created_at)
            SELECT
                id, username, password_hash, full_name, role, student_code, email, phone, is_active, created_at
            FROM users_old
            """
        )
    )
    conn.execute(text("DROP TABLE users_old"))


def migrate_qr_logs_table(conn) -> None:
    if not table_exists(conn, "qr_logs"):
        return
    if not column_exists(conn, "qr_logs", "plate"):
        conn.execute(text("ALTER TABLE qr_logs ADD COLUMN plate TEXT"))
    if not column_exists(conn, "qr_logs", "parking_log_id"):
        conn.execute(text("ALTER TABLE qr_logs ADD COLUMN parking_log_id INTEGER"))


def drop_all_tables(conn):
    conn.execute(text("DROP TABLE IF EXISTS qr_logs"))
    conn.execute(text("DROP TABLE IF EXISTS plate_scan_log"))
    conn.execute(text("DROP TABLE IF EXISTS parking_log"))
    conn.execute(text("DROP TABLE IF EXISTS vehicles"))
    conn.execute(text("DROP TABLE IF EXISTS users"))


def create_users_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('admin', 'student', 'guard')),
            student_code TEXT UNIQUE,
            email TEXT UNIQUE,
            phone TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))


def create_vehicles_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT UNIQUE NOT NULL,
            student_code TEXT NOT NULL,
            owner_name TEXT,
            vehicle_type TEXT NOT NULL DEFAULT 'motorbike',
            brand TEXT,
            color TEXT,
            image_path TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))


def create_parking_log_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS parking_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            student_code TEXT NOT NULL,
            time_in TEXT NOT NULL,
            time_out TEXT,
            gate_in TEXT,
            gate_out TEXT,
            fee INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'IN_PARKING',
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))


def create_plate_scan_log_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS plate_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_path TEXT NOT NULL,
            raw_text TEXT,
            normalized_plate TEXT,
            score REAL,
            source TEXT,
            direction TEXT,
            gate TEXT,
            status TEXT NOT NULL DEFAULT 'UNKNOWN',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))


def create_qr_logs_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS qr_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_code TEXT NOT NULL,
            qr_payload TEXT NOT NULL,
            qr_image_path TEXT,
            plate TEXT,
            parking_log_id INTEGER,
            is_valid INTEGER NOT NULL DEFAULT 1,
            used_for_exit INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))


def create_indexes(conn):
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_student_code ON users(student_code)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles(plate)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vehicles_student_code ON vehicles(student_code)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_parking_log_plate ON parking_log(plate)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_parking_log_status ON parking_log(status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_parking_log_time_in ON parking_log(time_in)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_plate_scan_log_plate ON plate_scan_log(normalized_plate)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_qr_logs_student_code ON qr_logs(student_code)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_qr_logs_parking_log_id ON qr_logs(parking_log_id)"))


def seed_default_users(conn):
    admin_password = generate_password_hash("admin123")
    guard_password = generate_password_hash("guard123")
    student_password = generate_password_hash("student123")

    existing = {row[0] for row in conn.execute(text("SELECT username FROM users")).fetchall()}

    if "admin" not in existing:
        conn.execute(
            text(
                """
                INSERT INTO users (username, password_hash, full_name, role, student_code, email, phone)
                VALUES (:username, :password_hash, :full_name, :role, :student_code, :email, :phone)
                """
            ),
            {
                "username": "admin",
                "password_hash": admin_password,
                "full_name": "System Administrator",
                "role": "admin",
                "student_code": None,
                "email": "admin@neu.edu.vn",
                "phone": "0123456789",
            },
        )

    if "guard" not in existing:
        conn.execute(
            text(
                """
                INSERT INTO users (username, password_hash, full_name, role, student_code, email, phone)
                VALUES (:username, :password_hash, :full_name, :role, :student_code, :email, :phone)
                """
            ),
            {
                "username": "guard",
                "password_hash": guard_password,
                "full_name": "Security Guard",
                "role": "guard",
                "student_code": None,
                "email": "guard@neu.edu.vn",
                "phone": "0999999999",
            },
        )

    if "student1" not in existing:
        conn.execute(
            text(
                """
                INSERT INTO users (username, password_hash, full_name, role, student_code, email, phone)
                VALUES (:username, :password_hash, :full_name, :role, :student_code, :email, :phone)
                """
            ),
            {
                "username": "student1",
                "password_hash": student_password,
                "full_name": "Nguyen Van A",
                "role": "student",
                "student_code": "20211234",
                "email": "20211234@st.neu.edu.vn",
                "phone": "0911111111",
            },
        )


def seed_default_vehicles(conn):
    conn.execute(text("""
        INSERT OR IGNORE INTO vehicles (plate, student_code, owner_name, vehicle_type, brand, color, image_path)
        VALUES (:plate, :student_code, :owner_name, :vehicle_type, :brand, :color, :image_path)
    """), {
        "plate": "29-G1 333.33",
        "student_code": "20211234",
        "owner_name": "Nguyen Van A",
        "vehicle_type": "motorbike",
        "brand": "Honda",
        "color": "Black",
        "image_path": None
    })

    conn.execute(text("""
        INSERT OR IGNORE INTO vehicles (plate, student_code, owner_name, vehicle_type, brand, color, image_path)
        VALUES (:plate, :student_code, :owner_name, :vehicle_type, :brand, :color, :image_path)
    """), {
        "plate": "30-A1 111.11",
        "student_code": "20210001",
        "owner_name": "Tran Van B",
        "vehicle_type": "motorbike",
        "brand": "Yamaha",
        "color": "Blue",
        "image_path": None
    })

    conn.execute(text("""
        INSERT OR IGNORE INTO vehicles (plate, student_code, owner_name, vehicle_type, brand, color, image_path)
        VALUES (:plate, :student_code, :owner_name, :vehicle_type, :brand, :color, :image_path)
    """), {
        "plate": "88-C3 888.88",
        "student_code": "20210003",
        "owner_name": "Le Thi C",
        "vehicle_type": "car",
        "brand": "Toyota",
        "color": "White",
        "image_path": None
    })


def init_db():
    ensure_directories()
    engine = get_engine()

    with engine.begin() as conn:
        migrate_users_table_add_guard_role(conn)
        create_users_table(conn)
        create_vehicles_table(conn)
        create_parking_log_table(conn)
        create_plate_scan_log_table(conn)
        create_qr_logs_table(conn)
        migrate_qr_logs_table(conn)
        create_indexes(conn)
        seed_default_users(conn)
        seed_default_vehicles(conn)

    print("Database initialized successfully.")


def recreate_db():
    ensure_directories()
    engine = get_engine()

    with engine.begin() as conn:
        drop_all_tables(conn)
        create_users_table(conn)
        create_vehicles_table(conn)
        create_parking_log_table(conn)
        create_plate_scan_log_table(conn)
        create_qr_logs_table(conn)
        create_indexes(conn)
        seed_default_users(conn)
        seed_default_vehicles(conn)

    print("Database created from scratch successfully.")


if __name__ == "__main__":
    recreate_db()

# NEU Smart Parking Web

NEU Smart Parking Web is a Flask-based parking management system for National Economics University (NEU). It combines role-based operations, vehicle registration, parking-area management, license plate recognition, QR-based exit verification, and reporting in a single web application backed by SQLite.

## What the system does

- Provides a public landing page with parking-area capacity and occupancy status.
- Supports three roles: `admin`, `guard`, and `student`.
- Detects vehicle license plates from uploaded images using YOLO and EasyOCR.
- Confirms gate entry only for approved vehicles and available parking areas.
- Issues a QR ticket for each active parking session.
- Validates both license plate and QR code before confirming gate exit.
- Calculates parking fees automatically.
- Tracks scan logs, parking history, and parking-area statistics.
- Exports parking history to CSV and Excel.

## Main workflows

### 1. Vehicle entry

1. A guard or admin uploads a vehicle image at `/gate-in`.
2. The system detects the license plate from the image.
3. The plate is checked against the registered vehicle list.
4. The selected parking area is validated for availability and active status.
5. If everything is valid, the parking session is created in `parking_log`.
6. A session-specific QR ticket is generated and stored in `qr_logs`.

### 2. Vehicle exit

1. A guard or admin uploads a plate image and a QR image at `/gate-out`.
2. The system extracts the plate and decodes the QR payload.
3. It verifies the QR against:
   - the vehicle owner
   - the active parking session
   - the vehicle plate
   - QR validity and reuse state
4. If validation succeeds, the parking session is closed and the QR code is invalidated.

### 3. Student self-service

- Students can register an account.
- Students can submit new vehicles for approval.
- Students can manage their own vehicle records.
- Students can review their parking history and active sessions.
- Students can re-issue a QR ticket for an active parking session.

### 4. Administration

- Manage users and roles.
- Approve, lock, edit, or delete vehicles.
- Manage parking-area metadata and capacity.
- Review occupancy, revenue, recent logs, and daily charts.

## Tech stack

- Backend: Flask
- Database: SQLite
- ORM / SQL layer: SQLAlchemy
- Templates: Jinja2
- License plate detection: Ultralytics YOLO
- OCR: EasyOCR
- QR generation: `qrcode`
- QR decoding: `pyzbar`
- Export: CSV and `openpyxl` for Excel

## Project structure

```text
neu_smart_parking_web/
|-- app.py                     # Main Flask app for local development
|-- api/
|   `-- index.py               # Vercel entrypoint
|-- config.py                  # App paths and configuration
|-- parking.db                 # SQLite database
|-- requirements.txt
|-- models/
|   `-- license_plate_detector.pt
|-- services/
|   |-- db_service.py
|   |-- parking_area_service.py
|   |-- parking_service.py
|   |-- plate_service.py
|   |-- qr_service.py
|   |-- user_service.py
|   `-- vehicle_service.py
|-- templates/                 # Jinja templates for all pages
`-- static/
    |-- css/
    |-- images/
    |-- qr_out/                # Generated QR images
    |-- uploads/               # Uploaded vehicle images
    `-- exports/               # Reserved export directory created at startup
```

## Database design

The application initializes the database automatically on startup and ensures these tables exist:

- `users`: login credentials, profile data, role, activation state
- `vehicles`: registered vehicles linked to student codes
- `parking_areas`: parking lot definitions, capacity, active status
- `parking_log`: entry/exit records, gates, fees, status, notes
- `plate_scan_log`: audit trail for plate scans and scan decisions
- `qr_logs`: QR payloads, image paths, session links, validity state

## Seeded data

On first run, the app seeds demo data automatically:

- Default users:
  - `admin` / `admin123`
  - `guard` / `guard123`
  - `student1` / `student123`
- Four default parking areas
- Three sample vehicles

These credentials are for development/demo use only and should be changed before any real deployment.

## Parking fee rule

The current implementation charges:

- `3,000 VND` per started hour
- minimum fee: `3,000 VND`

## Setup

### 1. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the app

```bash
python app.py
```

The development server starts at:

```text
http://127.0.0.1:5000
```

## Database reset

To recreate the database from scratch:

```bash
python services/db_service.py
```

This command drops all existing tables and seeds the database again.

## Important runtime behavior

- `init_db()` runs automatically when the application starts.
- The app creates these directories if they do not exist:
  - `static/uploads`
  - `static/qr_out`
  - `static/exports`
- Sessions are persistent for 7 days.
- Student-created vehicles are stored as inactive until approved by an admin.
- Guard and admin pages are protected by role checks.

## Key pages

- `/` - public landing page
- `/login` - sign in
- `/register` - student registration
- `/dashboard` - admin/guard dashboard
- `/gate-in` - entry scan and confirmation
- `/gate-out` - exit scan and confirmation
- `/history` - parking history and exports
- `/admin/users` - user management
- `/admin/vehicles` - vehicle management
- `/admin/parking-areas` - parking-area management and analytics
- `/my-vehicle` - student vehicle management
- `/student-qr` - active session QR tickets
- `/self-dashboard` - student dashboard
- `/self-history` - student parking history

## Service responsibilities

- `db_service.py`: database creation, migration helpers, seed data
- `user_service.py`: authentication and user CRUD
- `vehicle_service.py`: vehicle CRUD and plate normalization
- `parking_area_service.py`: occupancy, availability, and analytics
- `parking_service.py`: gate decision logic, fee calculation, history, exports
- `plate_service.py`: YOLO + OCR pipeline for plate recognition
- `qr_service.py`: QR creation, parsing, and decoding

## Deployment note

The repository includes `vercel.json` and a Vercel entrypoint at `api/index.py`. However, the current application relies on local persistence and local asset directories, including:

- `parking.db`
- `static/uploads`
- `static/qr_out`
- `static/exports` (created at startup)

Because of that, full production use is better suited to a stateful environment or to an architecture where SQLite and local file storage are replaced with managed services.

## Current limitations

- No automated test suite is included in the repository.
- The web workflow is file-upload based for plate and QR scans.
- The codebase contains helper functions for camera-based QR scanning, but the web UI currently uses uploaded images.
- The application uses SQLite, which is suitable for local development and demos but may be limiting for multi-user production traffic.

## Recommended next improvements

- Replace SQLite with PostgreSQL or MySQL for production.
- Move uploaded files and QR assets to object storage.
- Add automated tests for services and route behavior.
- Add environment-variable based configuration for secrets and storage paths.
- Add audit logging and stronger access controls for production use.

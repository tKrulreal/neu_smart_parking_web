from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for

from config import Config
from services.db_service import init_db
from services.user_service import authenticate, get_user_by_id, register_student

app = Flask(__name__)
app.config.from_object(Config)
app.permanent_session_lifetime = timedelta(days=7)

init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Before request / context processor
# ---------------------------------------------------------------------------

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
    return {"current_user": getattr(g, "user", None)}


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


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

        next_url = request.args.get("next")
        return redirect(next_url or url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username     = (request.form.get("username") or "").strip()
        full_name    = (request.form.get("full_name") or "").strip()
        student_code = (request.form.get("student_code") or "").strip() or None
        email        = (request.form.get("email") or "").strip() or None
        phone        = (request.form.get("phone") or "").strip() or None
        password     = request.form.get("password") or ""
        password2    = request.form.get("password2") or ""

        form_data = dict(
            username=username,
            full_name=full_name,
            student_code=student_code or "",
            email=email or "",
            phone=phone or "",
        )

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
                "required":       "Vui lòng nhập username và họ tên.",
                "password_short": "Mật khẩu tối thiểu 6 ký tự.",
            }
            flash(msg_map.get(str(exc), "Đăng ký thất bại (username/email/mã SV có thể đã tồn tại)."), "error")
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


# ---------------------------------------------------------------------------
# Authenticated routes
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    # Placeholder stats — replace with real DB queries
    stats = {
        "total_cars":      0,
        "in_today":        0,
        "out_today":       0,
        "available_slots": 0,
    }
    cars = []
    current_date = datetime.now().strftime("%d/%m/%Y")
    return render_template("dashboard.html", stats=stats, cars=cars, current_date=current_date)


# ---------------------------------------------------------------------------
# Guard / Admin routes
# ---------------------------------------------------------------------------

@app.route("/gate-in", methods=["GET", "POST"])
@roles_required("guard", "admin")
def gate_in():
    context = {
        "gate_name":     "Cổng 1",
        "status":        "ACTIVE",
        "current_time":  datetime.now().strftime("%H:%M:%S"),
        "current_date":  datetime.now().strftime("%d/%m/%Y"),
        "ocr":           {"plate": None, "confidence": None},
        "owner":         None,
        "system_status": None,
        "logs":          [],
    }
    return render_template("gate_in.html", **context)


@app.route("/gate-out", methods=["GET", "POST"])
@roles_required("guard", "admin")
def gate_out():
    context = {
        "current_time":  datetime.now().strftime("%H:%M:%S"),
        "plate":         None,
        "student_id":    None,
        "entry_time":    None,
        "student_name":  None,
        "vehicle":       None,
        "is_valid":      False,
        "fee":           0,
        "duration":      None,
        "rate":          None,
    }
    return render_template("gate_out.html", **context)


@app.route("/history")
@roles_required("guard", "admin")
def history():
    # Placeholder — wire up real DB query with pagination
    context = {
        "history":       [],
        "pagination":    None,
        "total_revenue": 0,
        "today_count":   0,
        "occupancy":     0,
    }
    return render_template("history.html", **context)


@app.route("/export-csv")
@roles_required("guard", "admin")
def export_csv():
    flash("Tính năng xuất CSV chưa được triển khai.", "error")
    return redirect(url_for("history"))


@app.route("/export-excel")
@roles_required("guard", "admin")
def export_excel():
    flash("Tính năng xuất Excel chưa được triển khai.", "error")
    return redirect(url_for("history"))


# ---------------------------------------------------------------------------
# Admin-only routes
# ---------------------------------------------------------------------------

@app.route("/admin")
@roles_required("admin")
def admin():
    return render_template("admin.html")


@app.route("/admin/users")
@roles_required("admin")
def user_management():
    context = {
        "users":        [],
        "total_users":  0,
        "active_users": 0,
        "locked_users": 0,
        "page":         int(request.args.get("page", 1)),
        "total_pages":  1,
    }
    return render_template("user_management.html", **context)


@app.route("/admin/vehicles")
@roles_required("admin")
def vehicles():
    context = {
        "vehicles":    [],
        "total":       0,
        "in_parking":  0,
        "pending":     0,
        "violation":   0,
        "page":        int(request.args.get("page", 1)),
        "total_pages": 1,
    }
    return render_template("vehicles.html", **context)


# ---------------------------------------------------------------------------
# Student self-service routes
# ---------------------------------------------------------------------------

@app.route("/my-vehicle")
@login_required
def my_vehicle():
    context = {
        "total_vehicle": 0,
        "in_parking":    0,
        "main_vehicle":  None,
        "vehicles":      [],
    }
    return render_template("my_vehicle.html", **context)


@app.route("/my-new-vehicle", methods=["GET", "POST"])
@login_required
def my_new_vehicle():
    return render_template("my_new_vehicle.html")


@app.route("/student-qr")
@login_required
def student_qr():
    context = {
        "user":    g.user,
        "vehicle": None,
        "ticket":  None,
    }
    return render_template("student_qr.html", **context)


@app.route("/self-dashboard")
@login_required
def self_dashboard():
    context = {
        "user":             g.user,
        "current_date":     datetime.now().strftime("%d/%m/%Y"),
        "total_vehicles":   0,
        "parking_status":   "Không trong bãi",
        "balance":          0,
        "activities":       [],
        "active_vehicle":   None,
        "parking_density":  [],
        "parking_location": "",
    }
    return render_template("self_dashboard.html", **context)


@app.route("/self-history")
@login_required
def self_history():
    context = {
        "user":           g.user,
        "histories":      [],
        "total_sessions": 0,
        "total_spent":    0,
        "page":           int(request.args.get("page", 1)),
        "has_next":       False,
    }
    return render_template("self_history.html", **context)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
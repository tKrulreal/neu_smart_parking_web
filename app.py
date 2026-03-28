from __future__ import annotations

from datetime import timedelta
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for

from config import Config
from services.db_service import init_db
from services.user_service import authenticate, get_user_by_id, register_student

app = Flask(__name__)
app.config.from_object(Config)
app.permanent_session_lifetime = timedelta(days=7)

init_db()


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    if not g.user:
        return redirect(url_for("login", next=request.path))
    return render_template("dashboard.html")


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
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        student_code = (request.form.get("student_code") or "").strip() or None
        email = (request.form.get("email") or "").strip() or None
        phone = (request.form.get("phone") or "").strip() or None
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if password != password2:
            flash("Mật khẩu nhập lại không khớp.", "error")
            return render_template(
                "register.html",
                username=username,
                full_name=full_name,
                student_code=student_code or "",
                email=email or "",
                phone=phone or "",
            ), 400

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
            if str(exc) == "required":
                flash("Vui lòng nhập username và họ tên.", "error")
            elif str(exc) == "password_short":
                flash("Mật khẩu tối thiểu 6 ký tự.", "error")
            else:
                flash("Đăng ký thất bại (username/email/mã SV có thể đã tồn tại).", "error")
            return render_template(
                "register.html",
                username=username,
                full_name=full_name,
                student_code=student_code or "",
                email=email or "",
                phone=phone or "",
            ), 400

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


@app.route("/admin")
@roles_required("admin")
def admin():
    return render_template("admin.html")


@app.route("/gate-in")
@roles_required("guard", "admin")
def gate_in():
    return render_template("gate_in.html")


@app.route("/gate-out")
@roles_required("guard", "admin")
def gate_out():
    return render_template("gate_out.html")


if __name__ == "__main__":
    app.run(debug=True)

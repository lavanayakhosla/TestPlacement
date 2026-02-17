import io
import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from random import randint

import pandas as pd
import pdfplumber
from flask import (
    Flask,
    flash,
    g,
    session,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
RESUME_DIR = UPLOAD_DIR / "resumes"
PDF_IMPORT_DIR = UPLOAD_DIR / "pdf_imports"

for folder in (UPLOAD_DIR, RESUME_DIR, PDF_IMPORT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{BASE_DIR / 'placement.db'}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
app.config["MAIL_FROM"] = os.environ.get("MAIL_FROM", "no-reply@placement-portal.local")

db = SQLAlchemy(app)


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_no = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    branch = db.Column(db.String(16), nullable=False, index=True)
    is_lateral_entry = db.Column(db.Boolean, default=False, nullable=False)
    current_semester = db.Column(db.Integer, default=1, nullable=False)
    cgpa = db.Column(db.Float, default=0.0, nullable=False)
    total_backlogs = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    semester_records = db.relationship(
        "SemesterPerformance", backref="student", lazy=True, cascade="all, delete-orphan"
    )
    resume_versions = db.relationship(
        "ResumeVersion", backref="student", lazy=True, cascade="all, delete-orphan"
    )
    applications = db.relationship("Application", backref="student", lazy=True)

    def active_resume(self):
        return (
            ResumeVersion.query.filter_by(student_id=self.id, is_active=True)
            .order_by(ResumeVersion.uploaded_at.desc())
            .first()
        )


class SemesterPerformance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    semester_no = db.Column(db.Integer, nullable=False)
    sgpa = db.Column(db.Float, nullable=False)
    semester_credits = db.Column(db.Float, nullable=False, default=0.0)
    backlog_count = db.Column(db.Integer, default=0, nullable=False)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    source_file = db.Column(db.String(255), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("student_id", "semester_no", name="uniq_student_semester"),
    )


class BacklogUpdate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    semester_no = db.Column(db.Integer, nullable=False)
    old_backlog = db.Column(db.Integer, nullable=False)
    new_backlog = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    student = db.relationship("Student")


class ResumeVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    file_path = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    @property
    def filename(self):
        return Path(self.file_path).name


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    eligible_branches = db.Column(db.String(255), nullable=False, default="ALL")
    min_cgpa = db.Column(db.Float, default=0.0, nullable=False)
    max_backlogs = db.Column(db.Integer, default=999, nullable=False)
    export_template_json = db.Column(db.Text, nullable=False, default="[]")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    applications = db.relationship("Application", backref="company", lazy=True)

    def branch_list(self):
        text = (self.eligible_branches or "ALL").strip()
        if text.upper() == "ALL":
            return ["ALL"]
        return [item.strip().upper() for item in text.split(",") if item.strip()]

    def export_template(self):
        try:
            parsed = json.loads(self.export_template_json or "[]")
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return []


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    status = db.Column(db.String(32), default="APPLIED", nullable=False)
    applied_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    exported_at = db.Column(db.DateTime, nullable=True)
    resume_version_id = db.Column(db.Integer, db.ForeignKey("resume_version.id"), nullable=True)

    resume_version = db.relationship("ResumeVersion", foreign_keys=[resume_version_id])

    __table_args__ = (
        db.UniqueConstraint("student_id", "company_id", name="uniq_student_company"),
    )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), nullable=False, index=True)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=True, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship("Student", lazy=True)

    def set_password(self, raw_password: str):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)


class OTPToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code = db.Column(db.String(6), nullable=False)
    purpose = db.Column(db.String(32), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")

    @property
    def is_expired(self):
        return datetime.utcnow() > self.expires_at


class NotificationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="PENDING")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


@app.before_request
def load_user():
    g.user = current_user()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Login required.")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not g.user:
                flash("Login required.")
                return redirect(url_for("login"))
            if g.user.role not in roles:
                flash("Access denied for your role.")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def generate_otp():
    return f"{randint(0, 999999):06d}"


def send_email(to_email: str, subject: str, body: str, user_id=None) -> bool:
    log = NotificationLog(user_id=user_id, email=to_email, subject=subject, body=body, status="PENDING")
    db.session.add(log)
    db.session.flush()

    server = app.config.get("MAIL_SERVER")
    username = app.config.get("MAIL_USERNAME")
    password = app.config.get("MAIL_PASSWORD")
    port = app.config.get("MAIL_PORT")
    use_tls = app.config.get("MAIL_USE_TLS")
    mail_from = app.config.get("MAIL_FROM")

    if not server:
        log.status = "NO_MAIL_SERVER_CONFIGURED"
        db.session.commit()
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to_email

    try:
        with smtplib.SMTP(server, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            if username:
                smtp.login(username, password or "")
            smtp.send_message(msg)
        log.status = "SENT"
    except Exception:
        log.status = "FAILED"
    db.session.commit()
    return log.status == "SENT"


def issue_otp(user: User, purpose: str) -> OTPToken:
    OTPToken.query.filter_by(user_id=user.id, purpose=purpose, consumed=False).update({"consumed": True})
    token = OTPToken(
        user_id=user.id,
        code=generate_otp(),
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db.session.add(token)
    db.session.commit()
    return token


def verify_otp(user: User, purpose: str, code: str):
    token = (
        OTPToken.query.filter_by(user_id=user.id, purpose=purpose, consumed=False)
        .order_by(OTPToken.created_at.desc())
        .first()
    )
    if not token:
        return False, "No active OTP found."
    if token.is_expired:
        return False, "OTP expired."
    if token.code != code:
        return False, "Invalid OTP."
    token.consumed = True
    db.session.commit()
    return True, "OTP verified."


def calculate_cgpa(student: Student) -> float:
    records = SemesterPerformance.query.filter_by(student_id=student.id).all()
    if not records:
        return 0.0

    min_sem = 3 if student.is_lateral_entry else 1
    usable = [r for r in records if r.semester_no >= min_sem and r.semester_credits > 0]
    if not usable:
        return 0.0
    weighted_sum = sum(r.sgpa * r.semester_credits for r in usable)
    total_credits = sum(r.semester_credits for r in usable)
    if total_credits <= 0:
        return 0.0
    return round(weighted_sum / total_credits, 2)


def calculate_backlog(student: Student) -> int:
    records = SemesterPerformance.query.filter_by(student_id=student.id).all()
    return sum(max(0, r.backlog_count) for r in records)


def refresh_student_metrics(student: Student) -> None:
    student.cgpa = calculate_cgpa(student)
    student.total_backlogs = calculate_backlog(student)


def parse_pdf_rows(pdf_path: Path):
    extracted = []
    roll_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/-]{4,}$")
    sgpa_re = re.compile(r"^(10(?:\.0+)?|[0-9](?:\.\d{1,2})?)$")

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                if not table or len(table) < 2:
                    continue
                header = [str(c or "").strip().lower() for c in table[0]]
                roll_idx = next(
                    (i for i, c in enumerate(header) if "roll" in c or "enroll" in c), None
                )
                sgpa_idx = next((i for i, c in enumerate(header) if "sgpa" in c), None)
                name_idx = next((i for i, c in enumerate(header) if "name" in c), None)
                backlog_idx = next(
                    (i for i, c in enumerate(header) if "backlog" in c or "kt" in c), None
                )
                if roll_idx is None or sgpa_idx is None:
                    continue

                for row in table[1:]:
                    if not row or len(row) <= sgpa_idx:
                        continue
                    roll_no = str(row[roll_idx] or "").strip().upper().replace(" ", "")
                    sgpa_raw = str(row[sgpa_idx] or "").strip()
                    if not roll_no or not sgpa_raw:
                        continue
                    if not roll_re.match(roll_no):
                        continue
                    if not sgpa_re.match(sgpa_raw):
                        continue

                    name = ""
                    if name_idx is not None and len(row) > name_idx:
                        name = str(row[name_idx] or "").strip()
                    backlog = 0
                    if backlog_idx is not None and len(row) > backlog_idx:
                        b = str(row[backlog_idx] or "").strip()
                        if b.isdigit():
                            backlog = int(b)

                    extracted.append(
                        {
                            "roll_no": roll_no,
                            "name": name,
                            "sgpa": float(sgpa_raw),
                            "backlog": backlog,
                        }
                    )
    return extracted


def allowed_for_company(student: Student, company: Company):
    branches = company.branch_list()
    if "ALL" not in branches and student.branch.upper() not in branches:
        return False, f"{student.branch} is not eligible for {company.name}"
    if student.cgpa < company.min_cgpa:
        return False, f"CGPA {student.cgpa} is below min {company.min_cgpa}"
    if student.total_backlogs > company.max_backlogs:
        return False, f"Backlogs {student.total_backlogs} exceed max {company.max_backlogs}"
    return True, "Eligible"


def resolve_source(source: str, application: Application):
    student = application.student
    resume = application.resume_version or student.active_resume()
    mapping = {
        "student.roll_no": student.roll_no,
        "student.name": student.name,
        "student.branch": student.branch,
        "student.cgpa": student.cgpa,
        "student.backlogs": student.total_backlogs,
        "student.lateral_entry": "YES" if student.is_lateral_entry else "NO",
        "application.status": application.status,
        "application.applied_at": application.applied_at.strftime("%Y-%m-%d %H:%M:%S"),
        "company.name": application.company.name,
        "resume.filename": resume.filename if resume else "",
        "resume.path": resume.file_path if resume else "",
    }
    return mapping.get(source, "")


@app.route("/")
@login_required
def dashboard():
    if g.user.role == "STUDENT":
        return render_template(
            "dashboard.html",
            student_count=1 if g.user.student_id else 0,
            company_count=Company.query.count(),
            application_count=Application.query.filter_by(student_id=g.user.student_id).count()
            if g.user.student_id
            else 0,
        )
    return render_template(
        "dashboard.html",
        student_count=Student.query.count(),
        company_count=Company.query.count(),
        application_count=Application.query.count(),
    )


@app.route("/students", methods=["GET", "POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def students():
    if request.method == "POST":
        student = Student(
            roll_no=request.form["roll_no"].strip().upper(),
            name=request.form["name"].strip(),
            branch=request.form["branch"].strip().upper(),
            is_lateral_entry=request.form.get("is_lateral_entry") == "on",
            current_semester=int(request.form.get("current_semester", "1")),
        )
        db.session.add(student)
        db.session.commit()
        flash("Student added.")
        return redirect(url_for("students"))
    records = Student.query.order_by(Student.branch, Student.roll_no).all()
    return render_template("students.html", students=records)


@app.route("/students/<int:student_id>/resume", methods=["POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR", "STUDENT")
def upload_resume(student_id: int):
    if g.user.role == "STUDENT" and g.user.student_id != student_id:
        flash("You can upload resume only for your own profile.")
        return redirect(url_for("dashboard"))
    student = Student.query.get_or_404(student_id)
    file = request.files.get("resume")
    if not file or not file.filename:
        flash("Resume file is required.")
        return redirect(url_for("students"))

    filename = secure_filename(file.filename)
    stored_name = f"{student.roll_no}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
    dest = RESUME_DIR / stored_name
    file.save(dest)

    ResumeVersion.query.filter_by(student_id=student.id, is_active=True).update({"is_active": False})
    rv = ResumeVersion(student_id=student.id, file_path=str(dest), is_active=True)
    db.session.add(rv)
    db.session.commit()
    flash("Resume uploaded. This version is now active.")
    return redirect(url_for("students"))


@app.route("/companies", methods=["GET", "POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def companies():
    if request.method == "POST":
        template_text = request.form.get("export_template_json", "[]").strip() or "[]"
        try:
            parsed = json.loads(template_text)
            if not isinstance(parsed, list):
                raise ValueError("Template must be a JSON list")
        except Exception:
            flash("Invalid export template JSON.")
            return redirect(url_for("companies"))

        company = Company(
            name=request.form["name"].strip(),
            eligible_branches=request.form.get("eligible_branches", "ALL").strip().upper() or "ALL",
            min_cgpa=float(request.form.get("min_cgpa", "0")),
            max_backlogs=int(request.form.get("max_backlogs", "999")),
            export_template_json=template_text,
        )
        db.session.add(company)
        db.session.commit()
        flash("Company added.")
        return redirect(url_for("companies"))

    records = Company.query.order_by(Company.name).all()
    return render_template("companies.html", companies=records)


@app.route("/applications", methods=["GET", "POST"])
@login_required
def applications():
    if request.method == "POST":
        if g.user.role == "STUDENT":
            if not g.user.student_id:
                flash("No student profile linked to your account.")
                return redirect(url_for("applications"))
            student = Student.query.get_or_404(g.user.student_id)
        else:
            student = Student.query.get_or_404(int(request.form["student_id"]))
        company = Company.query.get_or_404(int(request.form["company_id"]))
        eligibility, message = allowed_for_company(student, company)
        if not eligibility:
            flash(f"Application blocked: {message}")
            return redirect(url_for("applications"))

        existing = Application.query.filter_by(student_id=student.id, company_id=company.id).first()
        if existing:
            flash("Student already applied to this company.")
            return redirect(url_for("applications"))

        uploaded_file = request.files.get("resume")
        resume_version = student.active_resume()

        if uploaded_file and uploaded_file.filename:
            filename = secure_filename(uploaded_file.filename)
            stored_name = (
                f"{student.roll_no}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
            )
            dest = RESUME_DIR / stored_name
            uploaded_file.save(dest)
            ResumeVersion.query.filter_by(student_id=student.id, is_active=True).update({"is_active": False})
            resume_version = ResumeVersion(student_id=student.id, file_path=str(dest), is_active=True)
            db.session.add(resume_version)
            db.session.flush()
        elif not resume_version:
            flash("No active resume found. Upload one while applying.")
            return redirect(url_for("applications"))

        app_entry = Application(
            student_id=student.id,
            company_id=company.id,
            resume_version_id=resume_version.id if resume_version else None,
        )
        db.session.add(app_entry)
        db.session.commit()
        flash("Application submitted.")
        return redirect(url_for("applications"))

    apps = Application.query.order_by(Application.applied_at.desc()).all()
    if g.user.role == "STUDENT":
        apps = (
            Application.query.filter_by(student_id=g.user.student_id)
            .order_by(Application.applied_at.desc())
            .all()
            if g.user.student_id
            else []
        )
        students = [Student.query.get(g.user.student_id)] if g.user.student_id else []
    else:
        students = Student.query.order_by(Student.roll_no).all()
    companies = Company.query.order_by(Company.name).all()
    return render_template("applications.html", applications=apps, students=students, companies=companies)


@app.route("/imports/sgpa", methods=["GET", "POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def import_sgpa():
    if request.method == "POST":
        semester_no = int(request.form["semester_no"])
        semester_credits = float(request.form["semester_credits"])
        branch = request.form["branch"].strip().upper()
        file = request.files.get("pdf_file")
        if not file or not file.filename.lower().endswith(".pdf"):
            flash("Please upload a valid PDF.")
            return redirect(url_for("import_sgpa"))
        if semester_credits <= 0:
            flash("Semester credits must be greater than 0.")
            return redirect(url_for("import_sgpa"))

        safe_name = secure_filename(file.filename)
        saved_path = PDF_IMPORT_DIR / f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{safe_name}"
        file.save(saved_path)

        rows = parse_pdf_rows(saved_path)
        if not rows:
            flash("No valid rows found in PDF. Ensure columns include Roll and SGPA.")
            return redirect(url_for("import_sgpa"))

        updated = 0
        created_students = 0
        skipped_lateral = 0

        for row in rows:
            student = Student.query.filter_by(roll_no=row["roll_no"]).first()
            if not student:
                student = Student(
                    roll_no=row["roll_no"],
                    name=row["name"] or row["roll_no"],
                    branch=branch,
                    current_semester=semester_no,
                )
                db.session.add(student)
                db.session.flush()
                created_students += 1

            if student.branch.upper() != branch:
                continue
            if student.is_lateral_entry and semester_no < 3:
                skipped_lateral += 1
                continue

            perf = SemesterPerformance.query.filter_by(
                student_id=student.id, semester_no=semester_no
            ).first()
            if not perf:
                perf = SemesterPerformance(
                    student_id=student.id,
                    semester_no=semester_no,
                    sgpa=row["sgpa"],
                    semester_credits=semester_credits,
                    backlog_count=row["backlog"],
                    source_file=str(saved_path),
                )
                db.session.add(perf)
            else:
                perf.sgpa = row["sgpa"]
                perf.semester_credits = semester_credits
                perf.backlog_count = row["backlog"]
                perf.source_file = str(saved_path)

            student.current_semester = max(student.current_semester, semester_no)
            refresh_student_metrics(student)
            updated += 1

        db.session.commit()
        flash(
            f"Processed {updated} rows. New students: {created_students}. "
            f"Lateral-semester skips: {skipped_lateral}."
        )
        return redirect(url_for("import_sgpa"))

    return render_template("import_sgpa.html")


@app.route("/students/<int:student_id>/backlog", methods=["POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def update_backlog(student_id: int):
    student = Student.query.get_or_404(student_id)
    semester_no = int(request.form["semester_no"])
    new_backlog = int(request.form["new_backlog"])
    note = request.form.get("note", "").strip()

    perf = SemesterPerformance.query.filter_by(student_id=student.id, semester_no=semester_no).first()
    if not perf:
        flash("Semester record not found. Import SGPA first.")
        return redirect(url_for("students"))

    old_backlog = perf.backlog_count
    perf.backlog_count = new_backlog

    log = BacklogUpdate(
        student_id=student.id,
        semester_no=semester_no,
        old_backlog=old_backlog,
        new_backlog=new_backlog,
        note=note,
    )
    db.session.add(log)
    refresh_student_metrics(student)
    db.session.commit()

    flash("Backlog updated and CGPA/backlog metrics recalculated.")
    return redirect(url_for("students"))


@app.route("/exports/company/<int:company_id>.xlsx")
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def export_company(company_id: int):
    company = Company.query.get_or_404(company_id)
    applications = Application.query.filter_by(company_id=company.id).all()
    template = company.export_template()

    if not template:
        template = [
            {"header": "Roll No", "source": "student.roll_no"},
            {"header": "Name", "source": "student.name"},
            {"header": "Branch", "source": "student.branch"},
            {"header": "CGPA", "source": "student.cgpa"},
            {"header": "Backlogs", "source": "student.backlogs"},
            {"header": "Applied At", "source": "application.applied_at"},
        ]

    rows = []
    for app_entry in applications:
        row = {}
        for col in template:
            header = col.get("header", "Unknown")
            source = col.get("source", "")
            row[header] = resolve_source(source, app_entry)
        rows.append(row)
        app_entry.exported_at = datetime.utcnow()

    db.session.commit()

    df = pd.DataFrame(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Applications")
    output.seek(0)

    filename = f"{company.name.replace(' ', '_')}_applications_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/reports/backlog-history")
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def backlog_history():
    updates = BacklogUpdate.query.order_by(BacklogUpdate.updated_at.desc()).all()
    return render_template("backlog_history.html", updates=updates)


@app.route("/applications/<int:application_id>/status", methods=["POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def update_application_status(application_id: int):
    app_entry = Application.query.get_or_404(application_id)
    status = request.form.get("status", "").strip().upper()
    allowed = {"APPLIED", "SHORTLISTED", "INTERVIEW", "SELECTED", "REJECTED"}
    if status not in allowed:
        flash("Invalid status.")
        return redirect(url_for("applications"))

    app_entry.status = status
    student_user = User.query.filter_by(student_id=app_entry.student_id, role="STUDENT").first()
    db.session.commit()

    if student_user:
        subject = f"Application Status Updated - {app_entry.company.name}"
        body = (
            f"Hello {app_entry.student.name},\n\n"
            f"Your application status for {app_entry.company.name} is now: {status}.\n"
            f"Applied on: {app_entry.applied_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            "Regards,\nPlacement Cell"
        )
        delivered = send_email(student_user.email, subject, body, user_id=student_user.id)
        if delivered:
            flash("Status updated and email notification sent.")
        else:
            flash("Status updated but email delivery failed. Check mail config/logs.")
    else:
        flash("Status updated. No student user/email linked for notification.")
    return redirect(url_for("applications"))


@app.route("/auth/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        role = request.form.get("role", "STUDENT").strip().upper()
        if not g.user or g.user.role != "ADMIN":
            role = "STUDENT"

        if role not in {"ADMIN", "PLACEMENT_COORDINATOR", "STUDENT"}:
            flash("Invalid role.")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("Email already registered.")
            return redirect(url_for("register"))
        if len(password) < 8:
            flash("Password should be at least 8 characters.")
            return redirect(url_for("register"))

        student_id = None
        if role == "STUDENT":
            roll_no = request.form.get("roll_no", "").strip().upper()
            name = request.form.get("name", "").strip()
            branch = request.form.get("branch", "").strip().upper()
            if not roll_no or not name or not branch:
                flash("Roll no, name, and branch are required for student registration.")
                return redirect(url_for("register"))
            is_lateral = request.form.get("is_lateral_entry") == "on"
            student = Student.query.filter_by(roll_no=roll_no).first()
            if not student:
                student = Student(
                    roll_no=roll_no,
                    name=name,
                    branch=branch,
                    is_lateral_entry=is_lateral,
                    current_semester=1,
                )
                db.session.add(student)
                db.session.flush()
            student_id = student.id

        user = User(email=email, role=role, student_id=student_id, is_verified=False)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        token = issue_otp(user, "VERIFY_EMAIL")
        sent = send_email(
            user.email,
            "Verify your Placement Portal account",
            f"Your OTP is {token.code}. It expires in 10 minutes.",
            user_id=user.id,
        )
        session["pending_verify_user_id"] = user.id
        if sent:
            flash("Account created. OTP sent to email for verification.")
        else:
            flash(f"Account created. Email not sent (mail not configured). OTP: {token.code}")
        return redirect(url_for("verify_email"))

    return render_template("register.html")


@app.route("/auth/verify-email", methods=["GET", "POST"])
def verify_email():
    user_id = session.get("pending_verify_user_id")
    if not user_id:
        flash("No pending verification.")
        return redirect(url_for("login"))
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        code = request.form["otp"].strip()
        ok, msg = verify_otp(user, "VERIFY_EMAIL", code)
        if not ok:
            flash(msg)
            return redirect(url_for("verify_email"))
        user.is_verified = True
        db.session.commit()
        session.pop("pending_verify_user_id", None)
        flash("Email verified. Please login.")
        return redirect(url_for("login"))
    return render_template("verify_email.html", email=user.email, purpose="Verify Email")


@app.route("/auth/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Invalid credentials.")
            return redirect(url_for("login"))
        if not user.is_verified:
            session["pending_verify_user_id"] = user.id
            flash("Verify your email first.")
            return redirect(url_for("verify_email"))

        token = issue_otp(user, "LOGIN")
        sent = send_email(
            user.email,
            "Your login OTP",
            f"Your login OTP is {token.code}. It expires in 10 minutes.",
            user_id=user.id,
        )
        session["pending_login_user_id"] = user.id
        if sent:
            flash("OTP sent to email.")
        else:
            flash(f"Mail not configured; use OTP: {token.code}")
        return redirect(url_for("verify_login"))
    return render_template("login.html")


@app.route("/auth/verify-login", methods=["GET", "POST"])
def verify_login():
    user_id = session.get("pending_login_user_id")
    if not user_id:
        flash("No pending login.")
        return redirect(url_for("login"))
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        code = request.form["otp"].strip()
        ok, msg = verify_otp(user, "LOGIN", code)
        if not ok:
            flash(msg)
            return redirect(url_for("verify_login"))
        session["user_id"] = user.id
        session.pop("pending_login_user_id", None)
        flash("Logged in successfully.")
        return redirect(url_for("dashboard"))
    return render_template("verify_email.html", email=user.email, purpose="Login OTP")


@app.route("/auth/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))


@app.route("/admin/users", methods=["GET", "POST"])
@role_required("ADMIN")
def admin_users():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        role = request.form["role"].strip().upper()
        if role not in {"ADMIN", "PLACEMENT_COORDINATOR"}:
            flash("Admin can create only ADMIN or PLACEMENT_COORDINATOR here.")
            return redirect(url_for("admin_users"))
        if User.query.filter_by(email=email).first():
            flash("Email already exists.")
            return redirect(url_for("admin_users"))
        user = User(email=email, role=role, is_verified=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("User created.")
        return redirect(url_for("admin_users"))
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin_users.html", users=users)


@app.cli.command("init-db")
def init_db():
    db.create_all()
    ensure_default_admin()
    print("Database initialized.")


def ensure_default_admin():
    existing = User.query.filter_by(role="ADMIN").first()
    if existing:
        return
    user = User(email="admin@placement.local", role="ADMIN", is_verified=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        ensure_default_admin()
    app.run(debug=True)

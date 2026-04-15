import io
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from random import randint
import requests
import pandas as pd
import pdfplumber
from dotenv import load_dotenv

load_dotenv()
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
from sqlalchemy import inspect, text , JSON
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads"))
UPLOAD_DIR = Path(UPLOAD_ROOT)
PDF_IMPORT_DIR = UPLOAD_DIR / "pdf_imports"

for folder in (UPLOAD_DIR, PDF_IMPORT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def build_database_uri():
    raw = os.environ.get("DATABASE_URL")
    if raw:
        # Heroku/Render style legacy postgres:// URL compatibility
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql://", 1)
        return raw
    return f"sqlite:///{BASE_DIR / 'placement.db'}"


app.config["SQLALCHEMY_DATABASE_URI"] = build_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.config["ENVIRONMENT"] = os.environ.get("ENVIRONMENT", "development")
app.config["MAIL_SERVER"] = (os.environ.get("MAIL_SERVER") or "").strip() or None
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["PDF_TABLE_FALLBACK"] = os.environ.get("PDF_TABLE_FALLBACK", "false").lower() == "true"
app.config["BREVO_API_KEY"] = os.environ.get("BREVO_API_KEY")
app.config["MAIL_FROM"] = os.environ.get("MAIL_FROM", "khoslalavanaya@gmail.com")
if app.config["ENVIRONMENT"].lower() == "production":
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)

ELIGIBILITY_STATUSES = {
    "ELIGIBLE",
    "EXTERNAL_INTERN",
    "CAMPUS_INTERN",
    "EXTERNAL_PLACED",
    "BLOCKED_BY_POLICY",
}
SELECTION_POLICIES = {"BLOCKING", "NON_BLOCKING"}

BRANCH_CHOICES = ["CSE", "CSE AI", "ECE", "ECE AI", "IT", "MAE", "AI ML", "DMAM"]

import pytz


IST = pytz.timezone("Asia/Kolkata")

def to_ist(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(IST)
def _parse_eligible_branches(selected):
    if not selected or "ALL" in (s.strip().upper() for s in selected if s):
        return "ALL"
    branches = sorted(set(b.strip() for b in selected if b and b.strip()))
    return ",".join(branches) if branches else "ALL"



class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_no = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    branch = db.Column(db.String(16), nullable=False, index=True)
    is_lateral_entry = db.Column(db.Boolean, default=False, nullable=False)
    current_semester = db.Column(db.Integer, default=1, nullable=False)
    cgpa = db.Column(db.Float, default=0.0, nullable=False)
    total_backlogs = db.Column(db.Integer, default=0, nullable=False)
    dead_backlogs = db.Column(db.Integer, default=0, nullable=False)
    resume_link = db.Column(db.String(1024), nullable=True)
    eligibility_status = db.Column(db.String(32), default="ELIGIBLE", nullable=False, index=True)
    block_reason = db.Column(db.String(255), nullable=True)
    blocked_by_company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    semester_records = db.relationship(
        "SemesterPerformance", backref="student", lazy=True, cascade="all, delete-orphan"
    )
    applications = db.relationship("Application", backref="student", lazy=True)
    blocked_by_company = db.relationship("Company", foreign_keys=[blocked_by_company_id], lazy=True)


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


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    hiring_role = db.Column(db.String(255), nullable=True)
    apply_link = db.Column(db.String(1024), nullable=True)
    application_deadline = db.Column(db.DateTime, nullable=True)
    eligible_branches = db.Column(db.String(255), nullable=False, default="ALL")
    min_cgpa = db.Column(db.Float, default=0.0, nullable=False)
    max_backlogs = db.Column(db.Integer, default=999, nullable=False)
    allow_dead_backlogs = db.Column(db.Boolean, default=True)
    selection_policy = db.Column(db.String(32), default="NON_BLOCKING", nullable=False)
    extra_fields_json = db.Column(db.Text, nullable=False, default="[]")
    extra_fields = db.Column(JSON, default=[]) 
    export_template_json = db.Column(db.Text, default="[]") 
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
    extra_data = db.Column(db.Text, nullable=True)
    applied_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    exported_at = db.Column(db.DateTime, nullable=True)
    resume_link = db.Column(db.String(1024), nullable=True)
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
    error_message = db.Column(db.String(1024), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)

@app.template_filter("ist")
def ist_filter(dt):
    dt = to_ist(dt)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M")
    return ""
@app.before_request
def load_user():
    g.user = current_user()


@app.context_processor
def inject_branch_choices():
    return {"branch_choices": BRANCH_CHOICES}


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
    log = NotificationLog(
        user_id=user_id,
        email=to_email,
        subject=subject,
        body=body,
        status="PENDING",
    )

    db.session.add(log)
    db.session.flush()

    api_key = app.config.get("BREVO_API_KEY")
    sender_email = app.config.get("MAIL_FROM")

    if not api_key:
        log.status = "NO_API_KEY"
        log.error_message = "BREVO_API_KEY not configured."
        db.session.commit()
        return False

    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }

    payload = {
        "sender": {
            "name": "Placement Portal",
            "email": sender_email
        },
        "to": [
            {"email": to_email}
        ],
        "subject": subject,
        "textContent": body
    }

    try:
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            log.status = "SENT"
            log.error_message = None
        else:
            log.status = "FAILED"
            log.error_message = response.text[:1024]

    except Exception as exc:
        log.status = "FAILED"
        log.error_message = str(exc)[:1024]

    db.session.commit()

    return log.status == "SENT"


def mail_config_loaded():
    return bool(app.config.get("MAIL_SERVER"))


def last_mail_error_for(email: str):
    """Return the error_message of the most recent failed send to this email (for debugging)."""
    log = (
        NotificationLog.query.filter_by(email=email, status="FAILED")
        .order_by(NotificationLog.created_at.desc())
        .first()
    )
    return log.error_message if log else None


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
    
def calculate_dead_backlogs(student: Student) -> int:
    updates = BacklogUpdate.query.filter_by(student_id=student.id).all()

    dead = 0
    for u in updates:
        if u.old_backlog > 0 and u.new_backlog == 0:
            dead += u.old_backlog   # count cleared backlogs

    return dead

def refresh_student_metrics(student: Student) -> None:
    student.cgpa = calculate_cgpa(student)
    student.total_backlogs = calculate_backlog(student)
    student.dead_backlogs = calculate_dead_backlogs(student)


def parse_pdf_rows(pdf_path: Path):
    def parse_from_text() -> list[dict]:
        extracted_text_rows = []

        line_re = re.compile(
            r"^\s*\d+\s+([A-Za-z0-9/-]{5,})\s+(.+?)\s+(10(?:\.0+)?|[0-9](?:\.\d{1,2})?)\s+(.+)$"
        )

        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if not text:
                    continue

                for line in text.splitlines():
                    line = line.strip()

                    match = line_re.match(line)
                    if not match:
                        continue

                    roll_no = match.group(1).strip().upper()
                    name = match.group(2).strip()
                    sgpa = float(match.group(3))

                    # NEW PART 👇
                    grades_str = match.group(4).strip()
                    grades = grades_str.split()

                    # Count F
                    backlog = sum(1 for g in grades if g.upper() == "F")

                    extracted_text_rows.append(
                        {
                            "roll_no": roll_no,
                            "name": name,
                            "sgpa": sgpa,
                            "backlog": backlog,
                        }
                    )

        return extracted_text_rows

    fast_rows = parse_from_text()
    if fast_rows:
        return fast_rows
    if not app.config.get("PDF_TABLE_FALLBACK", False):
        return []

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

@app.route("/students/<int:student_id>/update-semester", methods=["POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def update_semester(student_id: int):
    student = Student.query.get_or_404(student_id)

    semester_no = int(request.form["semester_no"])
    new_sgpa = float(request.form["sgpa"])
    new_backlog = int(request.form.get("backlog", 0))
    credits = float(request.form.get("semester_credits", 0))

    perf = SemesterPerformance.query.filter_by(
        student_id=student.id,
        semester_no=semester_no
    ).first()

    if not perf:
        perf = SemesterPerformance(
            student_id=student.id,
            semester_no=semester_no,
            sgpa=new_sgpa,
            semester_credits=credits,
            backlog_count=new_backlog,
        )
        db.session.add(perf)
    else:
        perf.sgpa = new_sgpa
        perf.backlog_count = new_backlog
        if credits > 0:
            perf.semester_credits = credits

    refresh_student_metrics(student)
    db.session.commit()

    flash("Semester SGPA and backlog updated successfully.")
    return redirect(url_for("students"))
def allowed_for_company(student: Student, company: Company):
    if student.eligibility_status == "EXTERNAL_PLACED":
        return False, "Student is marked as already placed externally."
    if student.eligibility_status == "EXTERNAL_INTERN":
        return False, "Student is marked as already interned externally."
    if student.eligibility_status == "CAMPUS_INTERN":
        return False, "Student is marked as already interned via campus placement."
    if student.eligibility_status == "BLOCKED_BY_POLICY":
        by = student.blocked_by_company.name if student.blocked_by_company else "policy"
        reason = student.block_reason or f"Blocked after selection in {by}."
        return False, reason

    branches = company.branch_list()
    if "ALL" not in branches and student.branch.upper() not in branches:
        return False, f"{student.branch} is not eligible for {company.name}"
    if student.cgpa < company.min_cgpa:
        return False, f"CGPA {student.cgpa} is below min {company.min_cgpa}"
    if student.total_backlogs > company.max_backlogs:
        return False, f"Backlogs {student.total_backlogs} exceed max {company.max_backlogs}"
   
    # NEW DEAD BACKLOG CHECK
    if not company.allow_dead_backlogs and student.dead_backlogs > 0:
        return False, "Dead backlogs are not allowed for this company"

    return True, "Eligible"
    


def recompute_blocking_status(student: Student):
    selected_blocking = (
        db.session.query(Application)
        .join(Company, Application.company_id == Company.id)
        .filter(
            Application.student_id == student.id,
            Application.status == "SELECTED",
            Company.selection_policy == "BLOCKING",
        )
        .order_by(Application.applied_at.desc())
        .first()
    )
    if selected_blocking:
        student.eligibility_status = "BLOCKED_BY_POLICY"
        student.blocked_by_company_id = selected_blocking.company_id
        student.block_reason = f"Selected in blocking company: {selected_blocking.company.name}"
    else:
        if student.eligibility_status == "BLOCKED_BY_POLICY":
            student.eligibility_status = "ELIGIBLE"
            student.blocked_by_company_id = None
            student.block_reason = None


def resolve_source(source: str, application: Application):
    student = application.student
    mapping = {
        "student.roll_no": student.roll_no,
        "student.name": student.name,
        "student.branch": student.branch,
        "student.cgpa": student.cgpa,
        "student.backlogs": student.total_backlogs,
        "student.lateral_entry": "YES" if student.is_lateral_entry else "NO",
        "student.resume_link": student.resume_link or "",
        "student.eligibility_status": student.eligibility_status,
        "application.status": application.status,
        "application.applied_at": application.applied_at.strftime("%Y-%m-%d %H:%M:%S"),
        "company.name": application.company.name,
        "company.hiring_role": (application.company.hiring_role or "").strip(),
        "company.apply_link": (application.company.apply_link or "").strip(),
        "company.application_deadline": application.company.application_deadline.strftime("%Y-%m-%d %H:%M") if application.company.application_deadline else "",
        "resume.link": application.resume_link or student.resume_link or "",
        "resume.path": student.resume_link or "",
        "resume.filename": student.resume_link or "",
    }
    return mapping.get(source, "")


@app.route("/")
@login_required
# def dashboard():
#     reminders = []
    
#     # 1. Base Counts (Default values for Students)
#     s_count = 0
#     c_count = 0
#     a_count = 0

#     # 2. Agar user STUDENT hai
#     if g.user.role == "STUDENT" and g.user.student_id:
#         student = Student.query.get(g.user.student_id)
        
#         # Student ke liye reminders calculate karein
#         now = datetime.utcnow()
#         upcoming_deadline = now + timedelta(days=3)
#         companies = Company.query.filter(
#             Company.application_deadline != None,
#             Company.application_deadline >= now,
#             Company.application_deadline <= upcoming_deadline
#         ).all()

#         for c in companies:
#             applied = Application.query.filter_by(student_id=student.id, company_id=c.id).first()
#             if not applied:
#                 eligible, _ = allowed_for_company(student, c)
#                 if eligible:
#                     reminders.append(c)
        
#         # Student ko sirf uski apni application count dikhani hai toh:
#         a_count = Application.query.filter_by(student_id=student.id).count()

#     # 3. Agar user ADMIN/COORDINATOR hai, toh asli counts nikalein
#     else:
#         s_count = Student.query.count()
#         c_count = Company.query.count()
#         a_count = Application.query.count()

#     # 4. Template return karein
#     return render_template(
#         "dashboard.html",
#         student_count=s_count,
#         company_count=c_count,
#         application_count=a_count,
#         reminders=reminders
#     )
def dashboard():
    reminders = []
    display_applications = [] # <-- NEW: List to hold all apps for the student view
    
    # 1. Base Counts (Default values for Students)
    s_count = 0
    c_count = 0
    a_count = 0

    # 2. Agar user STUDENT hai
    if g.user.role == "STUDENT" and g.user.student_id:
        student = Student.query.get(g.user.student_id)
        now = datetime.utcnow()
        
        # ----------------------------------------------------
        # NEW LOGIC: Build display_applications for ALL companies
        # ----------------------------------------------------
        all_companies = Company.query.order_by(Company.application_deadline.asc()).all()
        student_applications = Application.query.filter_by(student_id=student.id).all()
        applied_company_ids = {app.company_id for app in student_applications}

        for comp in all_companies:
            # Check if deadline has passed
            is_passed = comp.application_deadline and now > comp.application_deadline
            
            if comp.id in applied_company_ids:
                status = "Application Submitted"
                status_color = "rgba(16, 185, 129, 0.1)" # Green
                text_color = "#10b981"
            elif is_passed:
                status = "Deadline Passed"
                status_color = "rgba(239, 68, 68, 0.1)" # Red
                text_color = "#ef4444"
            else:
                status = "Application Pending"
                status_color = "rgba(245, 158, 11, 0.1)" # Yellow
                text_color = "#f59e0b"

            display_applications.append({
                'company': comp,
                'company_name': comp.name,
                'status': status,
                'deadline': comp.application_deadline,
                'is_passed': is_passed,
                'status_color': status_color,
                'text_color': text_color
            })

        # ----------------------------------------------------
        # EXISTING LOGIC: Student ke liye 3-day reminders calculate karein
        # ----------------------------------------------------
        upcoming_deadline = now + timedelta(days=3)
        companies_for_reminders = Company.query.filter(
            Company.application_deadline != None,
            Company.application_deadline >= now,
            Company.application_deadline <= upcoming_deadline
        ).all()

        for c in companies_for_reminders:
            if c.id not in applied_company_ids: # Used the faster set we created above
                eligible, _ = allowed_for_company(student, c)
                if eligible:
                    reminders.append(c)
        
        # Student ko sirf uski apni application count dikhani hai toh:
        a_count = len(student_applications)

    # 3. Agar user ADMIN/COORDINATOR hai, toh asli counts nikalein
    else:
        s_count = Student.query.count()
        c_count = Company.query.count()
        a_count = Application.query.count()

    # 4. Template return karein
    return render_template(
        "dashboard.html",
        student_count=s_count,
        company_count=c_count,
        application_count=a_count,
        reminders=reminders,
        applications=display_applications  # <-- NEW: Pass the list to the HTML
    )

@app.route("/students", methods=["GET", "POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def students():
    if request.method == "POST":
        eligibility_status = request.form.get("eligibility_status", "ELIGIBLE").strip().upper()
        if eligibility_status not in ELIGIBILITY_STATUSES:
            flash("Invalid eligibility status.")
            return redirect(url_for("students"))
        student = Student(
            roll_no=request.form["roll_no"].strip().upper(),
            name=request.form["name"].strip(),
            branch=request.form["branch"].strip().upper(),
             cgpa=float(request.form.get("cgpa", "0")), 
            is_lateral_entry=request.form.get("is_lateral_entry") == "on",
            current_semester=int(request.form.get("current_semester", "1")),
            resume_link=request.form.get("resume_link", "").strip() or None,
            eligibility_status=eligibility_status,
            block_reason=request.form.get("block_reason", "").strip() or None,
        )
        db.session.add(student)
        db.session.commit()
        flash("Student added.")
        return redirect(url_for("students"))
    records = Student.query.order_by(Student.branch, Student.roll_no).all()
    return render_template("students.html", students=records)


@app.route("/students/<int:student_id>/resume-link", methods=["POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR", "STUDENT")
def update_resume_link(student_id: int):
    if g.user.role == "STUDENT" and g.user.student_id != student_id:
        flash("You can update resume link only for your own profile.")
        return redirect(url_for("dashboard"))
    student = Student.query.get_or_404(student_id)
    link = request.form.get("resume_link", "").strip()
    if not link:
        flash("Resume link is required.")
        return redirect(request.referrer or url_for("students"))
    student.resume_link = link
    db.session.commit()
    flash("Resume link updated.")
    if g.user.role == "STUDENT":
        return redirect(url_for("profile"))
    return redirect(url_for("students"))


@app.route("/companies", methods=["GET", "POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def companies():
    if request.method == "POST":
        selection_policy = request.form.get("selection_policy", "NON_BLOCKING").strip().upper()
        if selection_policy not in SELECTION_POLICIES:
            flash("Invalid company selection policy.")
            return redirect(url_for("companies"))
        template_text = request.form.get("export_template_json", "[]").strip() or "[]"
        try:
            parsed = json.loads(template_text)
            if not isinstance(parsed, list):
                raise ValueError("Template must be a JSON list")
        except Exception:
            flash("Invalid export template JSON.")
            return redirect(url_for("companies"))
        extra_fields_json = request.form.get("extra_fields_json", "[]").strip()

        try:
            parsed_fields = json.loads(extra_fields_json)
            if not isinstance(parsed_fields, list):
                raise ValueError()
        except:
            flash("Invalid Extra Fields JSON")
            return redirect(url_for("companies"))

        import pytz

        IST = pytz.timezone("Asia/Kolkata")

        deadline_utc = None
        raw_deadline = request.form.get("application_deadline")

        if raw_deadline:
            naive_dt = datetime.strptime(raw_deadline, "%Y-%m-%dT%H:%M")
    
    # treat input as IST
            ist_dt = IST.localize(naive_dt)

    # convert to UTC for DB
            deadline_utc = ist_dt.astimezone(pytz.utc)
        allow_dead = request.form.get("allow_dead_backlogs") == "on"
        company = Company(
            name=request.form["name"].strip(),
            hiring_role=request.form.get("hiring_role", "").strip() or None,
            apply_link=request.form.get("apply_link", "").strip() or None,
            application_deadline = deadline_utc,
            eligible_branches=_parse_eligible_branches(request.form.getlist("eligible_branches")),
            min_cgpa=float(request.form.get("min_cgpa", "0")),
            max_backlogs=int(request.form.get("max_backlogs", "999")),
            allow_dead_backlogs=allow_dead,
            selection_policy=selection_policy,
            export_template_json=template_text,
            extra_fields_json=extra_fields_json,
        )
        db.session.add(company)
        db.session.commit()
        flash("Company added.")
        return redirect(url_for("companies"))

    records = Company.query.order_by(Company.name).all()
    return render_template("companies.html", companies=records)


@app.route("/profile")
@role_required("STUDENT")
def profile():
    if not g.user.student_id:
        flash("No student profile linked to your account.")
        return redirect(url_for("dashboard"))
    student = Student.query.get_or_404(g.user.student_id)
    return render_template("profile.html", student=student)


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

        if company.application_deadline and datetime.utcnow() > company.application_deadline:
            deadline_ist = to_ist(company.application_deadline)

            flash(
                f"Application deadline for {company.name} has passed "
                f"({deadline_ist.strftime('%Y-%m-%d %H:%M')} IST)."
            )
            return redirect(url_for("applications"))

        if not student.resume_link:
            flash("No resume link found for this student. Add resume link first.")
            return redirect(url_for("applications"))
        fields = json.loads(company.extra_fields_json or "[]")
        extra_data = {}
        for field in fields:
            key = f"extra_{field['name']}"
            value = request.form.get(key)

            if field.get("required") and not value:
                flash(f"{field['label']} is required")
                return redirect(url_for("applications"))

            if field["type"] == "select":
                if value and value not in field.get("options", []):
                    flash(f"Invalid value for {field['label']}")
                    return redirect(url_for("applications"))

            extra_data[field["name"]] = value

       

        app_entry = Application(
            student_id=student.id,
            company_id=company.id,
            extra_data=json.dumps(extra_data),
            resume_link=student.resume_link
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
    company_fields = {
        c.id: json.loads(c.extra_fields_json or "[]")
        for c in companies
    }
    return render_template("applications.html", applications=apps, students=students, companies=companies, company_fields=company_fields)


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


@app.route("/students/<int:student_id>/eligibility-status", methods=["POST"])
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def update_eligibility_status(student_id: int):
    student = Student.query.get_or_404(student_id)
    status = request.form.get("eligibility_status", "").strip().upper()
    note = request.form.get("block_reason", "").strip()
    if status not in ELIGIBILITY_STATUSES:
        flash("Invalid eligibility status.")
        return redirect(url_for("students"))

    student.eligibility_status = status
    if status == "BLOCKED_BY_POLICY":
        student.block_reason = note or "Manually blocked by placement policy."
    else:
        student.block_reason = note or None
        if status != "BLOCKED_BY_POLICY":
            student.blocked_by_company_id = None

    db.session.commit()
    flash("Eligibility status updated.")
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
        extra = json.loads(app_entry.extra_data or "{}")
        for k, v in extra.items():
            row[k] = v

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
    recompute_blocking_status(app_entry.student)
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
@app.route("/exports/applicants")
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def export_applicants():
    branch = request.args.get("branch", "ALL").strip().upper()
    company_id = request.args.get("company_id")

    if not company_id:
        flash("Please select a company.")
        return redirect(url_for("applications"))

    # 🔥 JOIN Student + Application
    query = (
        db.session.query(Student)
        .join(Application, Student.id == Application.student_id)
        .filter(Application.company_id == int(company_id))
    )

    # 🔹 Apply branch filter
    if branch != "ALL":
        query = query.filter(Student.branch == branch)

    students = query.order_by(Student.roll_no).all()


        

    # # 🔹 Build Excel
    # rows = []
    # for s in students:
    #     app_entry = Application.query.filter_by(student_id=s.id, company_id=int(company_id)).first()
    #     rows.append({
    #         "Roll No": s.roll_no,
    #         "Name": s.name,
    #         "Branch": s.branch,
    #         "Semester": s.current_semester,
    #         "CGPA": s.cgpa,
    #         "Active Backlogs": s.total_backlogs,
    #         "Dead Backlogs": getattr(s, "dead_backlogs", 0),
    #         "Eligibility": s.eligibility_status,
            
    #         "Resume Link": app_entry.resume_link if app_entry and app_entry.resume_link else s.resume_link or "",
    #     })
    # 🔹 Build Excel
    rows = []
    for s in students:
        app_entry = Application.query.filter_by(student_id=s.id, company_id=int(company_id)).first()
        
        # 1. Base student data
        row_data = {
            "Roll No": s.roll_no,
            "Name": s.name,
            "Branch": s.branch,
            "Semester": s.current_semester,
            "CGPA": s.cgpa,
            "Active Backlogs": s.total_backlogs,
            "Dead Backlogs": getattr(s, "dead_backlogs", 0),
            "Eligibility": s.eligibility_status,
            "Resume Link": app_entry.resume_link if app_entry and app_entry.resume_link else s.resume_link or "",
        }

        # 2. Dynamically add the custom company fields!
        if app_entry and app_entry.extra_data:
            import json # Ensure json is imported at the top of your file
            try:
                extra_fields = json.loads(app_entry.extra_data)
                for key, value in extra_fields.items():
                    # Clean up the key so "github_link" becomes "Github Link" in the Excel Header
                    clean_header = key.replace('_', ' ').title()
                    row_data[clean_header] = value
            except Exception:
                pass # Failsafe if the JSON is somehow corrupted

        rows.append(row_data)

    df = pd.DataFrame(rows)
    # ... rest of the export code remains exactly the same ...
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Applicants")

    output.seek(0)

    filename = f"applicants_{branch}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

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
            elif User.query.filter_by(student_id=student.id).first():
                flash("An account is already registered for this roll number. Please login.")
                return redirect(url_for("register"))
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
            msg = "Account created, but OTP email failed to send."
            if mail_config_loaded():
                err = last_mail_error_for(user.email)
                msg += f" OTP: {token.code}"
                if err:
                    msg += f" Reason: {err}"
            else:
                msg = f"Account created. Set MAIL_* in .env (local) or deployment env. OTP: {token.code}"
            flash(msg)
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
        if request.form.get("resend") == "1":
            token = issue_otp(user, "VERIFY_EMAIL")
            sent = send_email(
                user.email,
                "Verify your Placement Portal account",
                f"Your new OTP is {token.code}. It expires in 10 minutes.",
                user_id=user.id,
            )
            if sent:
                flash("New OTP sent to your email.")
            else:
                msg = "Could not send email."
                err = last_mail_error_for(user.email)
                flash(f"{msg} OTP: {token.code}" + (f" Reason: {err}" if err else ""))
            return redirect(url_for("verify_email"))
        code = request.form.get("otp", "").strip()
        if not code:
            flash("Enter the 6-digit OTP.")
            return redirect(url_for("verify_email"))
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


@app.route("/auth/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Enter your email address.")
            return redirect(url_for("forgot_password"))
        user = User.query.filter_by(email=email).first()
        if not user:
            flash("If an account exists with this email, an OTP will be sent. Check your inbox.")
            return redirect(url_for("login"))
        token = issue_otp(user, "RESET_PASSWORD")
        sent = send_email(
            user.email,
            "Reset your Placement Portal password",
            f"Your OTP to reset password is {token.code}. It expires in 10 minutes.",
            user_id=user.id,
        )
        session["pending_reset_user_id"] = user.id
        if sent:
            flash("OTP sent to your email. Enter it below to set a new password.")
        else:
            msg = "Email could not be sent."
            err = last_mail_error_for(user.email)
            flash(f"{msg} OTP: {token.code}" + (f" Reason: {err}" if err else ""))
        return redirect(url_for("reset_password"))
    return render_template("forgot_password.html")


@app.route("/auth/reset-password", methods=["GET", "POST"])
def reset_password():
    user_id = session.get("pending_reset_user_id")
    if not user_id:
        flash("Start from Forgot password and enter your email first.")
        return redirect(url_for("forgot_password"))
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not code:
            flash("Enter the 6-digit OTP.")
            return redirect(url_for("reset_password"))
        if len(new_password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("reset_password"))
        if new_password != confirm:
            flash("Passwords do not match.")
            return redirect(url_for("reset_password"))
        ok, msg = verify_otp(user, "RESET_PASSWORD", code)
        if not ok:
            flash(msg)
            return redirect(url_for("reset_password"))
        user.set_password(new_password)
        db.session.commit()
        session.pop("pending_reset_user_id", None)
        flash("Password reset successfully. Please login.")
        return redirect(url_for("login"))
    return render_template("reset_password.html", email=user.email)


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
        session["user_id"] = user.id
        flash("Logged in successfully.")
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/auth/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))
@app.route("/exports/students")
@role_required("ADMIN", "PLACEMENT_COORDINATOR")
def export_students_by_branch():
    branch = request.args.get("branch", "ALL").strip().upper()

    query = Student.query

    if branch != "ALL":
        query = query.filter(Student.branch == branch)

    students = query.order_by(Student.roll_no).all()

    rows = []
    for s in students:
        rows.append({
            "Roll No": s.roll_no,
            "Name": s.name,
            "Branch": s.branch,
            "Semester": s.current_semester,
            "CGPA": s.cgpa,
            "Active Backlogs": s.total_backlogs,
            "Dead Backlogs": getattr(s, "dead_backlogs", 0),
            "Eligibility": s.eligibility_status,
            "Resume Link": s.resume_link or "",
        })

    df = pd.DataFrame(rows)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Students")

    output.seek(0)

    filename = f"students_{branch}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

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

@app.route("/applications/<int:application_id>/edit", methods=["GET", "POST"])
@login_required
def edit_application(application_id: int):
    app_entry = Application.query.get_or_404(application_id)

    # 🔐 Only owner student can edit
    if g.user.role != "STUDENT" or g.user.student_id != app_entry.student_id:
        flash("You can edit only your own application.")
        return redirect(url_for("applications"))

    company = app_entry.company

    if request.method == "POST":
        # ✅ Update resume link
        new_resume = request.form.get("resume_link", "").strip()
        if new_resume:
            app_entry.resume_link = new_resume

        # ✅ Handle dynamic fields
        extra_data = {}
        fields = json.loads(company.extra_fields_json or "[]")

        for field in fields:
            key = f"extra_{field['name']}"
            value = request.form.get(key)

            if field.get("required") and not value:
                flash(f"{field['label']} is required.")
                return redirect(request.url)

            extra_data[field["name"]] = value

        app_entry.extra_data = json.dumps(extra_data)

        db.session.commit()
        flash("Application updated successfully.")
        return redirect(url_for("applications"))

    # GET request → show existing values
    existing_data = json.loads(app_entry.extra_data or "{}")
    fields = json.loads(company.extra_fields_json or "[]")

    return render_template(
        "edit_application.html",
        application=app_entry,
        fields=fields,
        existing_data=existing_data
    )
@app.route("/applications/<int:application_id>/delete", methods=["POST"])
@login_required
def delete_application(application_id: int):
    app_entry = Application.query.get_or_404(application_id)

    # 🔐 Security check
    if g.user.role == "STUDENT":
        if g.user.student_id != app_entry.student_id:
            flash("You can delete only your own applications.")
            return redirect(url_for("applications"))

    db.session.delete(app_entry)
    db.session.commit()

    flash("Application deleted successfully.")
    return redirect(url_for("applications"))


@app.route("/admin/mail-debug")
@role_required("ADMIN")
def admin_mail_debug():
    latest = NotificationLog.query.order_by(NotificationLog.created_at.desc()).limit(10).all()
    return {
        "mail_server_loaded": bool(app.config.get("MAIL_SERVER")),
        "mail_username_loaded": bool(app.config.get("MAIL_USERNAME")),
        "mail_password_loaded": bool(app.config.get("MAIL_PASSWORD")),
        "mail_from_loaded": bool(app.config.get("MAIL_FROM")),
        "mail_port": app.config.get("MAIL_PORT"),
        "mail_use_tls": app.config.get("MAIL_USE_TLS"),
        "recent_notification_statuses": [
            {
                "id": row.id,
                "email": row.email,
                "subject": row.subject,
                "status": row.status,
                "error_message": row.error_message,
                "created_at": row.created_at.isoformat(),
            }
            for row in latest
        ],
    }, 200


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.cli.command("init-db")
def init_db():
    db.create_all()
    ensure_default_admin()
    print("Database initialized.")


def ensure_default_admin():
    existing = User.query.filter_by(role="ADMIN").first()
    if existing:
        return

    admin_email = os.environ.get("DEFAULT_ADMIN_EMAIL")
    admin_password = os.environ.get("DEFAULT_ADMIN_PASSWORD")

    if not admin_email or not admin_password:
        # Safe local fallback only when explicitly in non-production mode.
        if app.config["ENVIRONMENT"].lower() != "production":
            admin_email = "admin@placement.local"
            admin_password = "admin123"
        else:
            return

    user = User(email=admin_email.strip().lower(), role="ADMIN", is_verified=True)
    user.set_password(admin_password)
    db.session.add(user)
    db.session.commit()


def bootstrap_database():
    db.create_all()
    ensure_schema_updates()
    ensure_default_admin()


def ensure_schema_updates():
    # Lightweight compatibility migration for older databases.
    inspector = inspect(db.engine)
    student_cols = {col["name"] for col in inspector.get_columns("student")}
    if "resume_link" not in student_cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN resume_link VARCHAR(1024)"))
        db.session.commit()
    if "eligibility_status" not in student_cols:
        db.session.execute(
            text("ALTER TABLE student ADD COLUMN eligibility_status VARCHAR(32) DEFAULT 'ELIGIBLE'")
        )
        db.session.commit()
    if "block_reason" not in student_cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN block_reason VARCHAR(255)"))
        db.session.commit()
    if "blocked_by_company_id" not in student_cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN blocked_by_company_id INTEGER"))
        db.session.commit()
    if "dead_backlogs" not in student_cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN dead_backlogs INTEGER DEFAULT 0"))
        db.session.commit()

    application_cols = {col["name"] for col in inspector.get_columns("application")}
    if "extra_data" not in application_cols:
        db.session.execute(text("ALTER TABLE application ADD COLUMN extra_data TEXT"))
        db.session.commit()
    if "resume_link" not in application_cols:
        db.session.execute(text("ALTER TABLE application ADD COLUMN resume_link VARCHAR(1024)"))
        db.session.commit()





    

    company_cols = {col["name"] for col in inspector.get_columns("company")}
    if "selection_policy" not in company_cols:
        db.session.execute(
            text("ALTER TABLE company ADD COLUMN selection_policy VARCHAR(32) DEFAULT 'NON_BLOCKING'")
        )
        db.session.commit()
    if "hiring_role" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN hiring_role VARCHAR(255)"))
        db.session.commit()
    if "apply_link" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN apply_link VARCHAR(1024)"))
        db.session.commit()
    if "application_deadline" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN application_deadline TIMESTAMP"))
        db.session.commit()
    if "allow_dead_backlogs" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN allow_dead_backlogs BOOLEAN DEFAULT TRUE"))
        db.session.commit()
    if "extra_fields_json" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN extra_fields_json TEXT DEFAULT '[]'"))
        db.session.commit()
    if "extra_fields" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN extra_fields TEXT DEFAULT '[]'"))
        db.session.commit()
    if "export_template_json" not in company_cols:
        db.session.execute(text("ALTER TABLE company ADD COLUMN  export_template_json TEXT DEFAULT '[]'"))
        db.session.commit()
       

    sem_perf_cols = {col["name"] for col in inspector.get_columns("semester_performance")}
    if "semester_credits" not in sem_perf_cols:
        db.session.execute(
            text("ALTER TABLE semester_performance ADD COLUMN semester_credits FLOAT DEFAULT 0")
        )
        db.session.commit()

    notif_cols = {col["name"] for col in inspector.get_columns("notification_log")}
    if "error_message" not in notif_cols:
        db.session.execute(text("ALTER TABLE notification_log ADD COLUMN error_message VARCHAR(1024)"))
        db.session.commit()


if os.environ.get("AUTO_INIT_DB", "true").lower() == "true":
    with app.app_context():
        bootstrap_database()


if __name__ == "__main__":
    with app.app_context():
        bootstrap_database()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)

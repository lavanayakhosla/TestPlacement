# Campus Placement Portal

Placement portal for on-campus drives with:
- Student applications to companies
- Company-specific Excel export format
- SGPA import from semester PDF
- Lateral-entry student handling
- Resume versioning with auto-fallback
- Backlog updates with recalculation and audit history
- Login with role-based access (`ADMIN`, `PLACEMENT_COORDINATOR`, `STUDENT`)
- OTP-based email verification + OTP-based login
- Email notification on application status changes

## Tech Stack
- Flask + SQLAlchemy + SQLite
- `pdfplumber` for PDF table extraction
- `pandas` + `openpyxl` for Excel export

## Setup
```bash
cd /Users/lavanayakhosla/Documents/New\ project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

App runs at `http://127.0.0.1:5000`.

## Default Admin
On first run, system auto-creates:
- Email: `admin@placement.local`
- Password: `admin123`

Change this immediately after first login.

## Email/OTP Configuration
Set SMTP env vars (example):
```bash
export MAIL_SERVER="smtp.gmail.com"
export MAIL_PORT="587"
export MAIL_USERNAME="your_email@gmail.com"
export MAIL_PASSWORD="app_password"
export MAIL_USE_TLS="true"
export MAIL_FROM="no-reply@yourcollege.edu"
```

If SMTP is not configured, OTP will still be generated and shown as flash text for local testing.

## Core Workflows
1. Add students in `Students`.
2. Upload resume per student (new upload becomes active version).
3. Add companies in `Companies` with eligibility + export JSON template.
4. Import SGPA PDF in `SGPA Import` for each semester/branch.
5. Students apply in `Applications`.
6. Coordinators/Admin update application status (`SHORTLISTED`, `INTERVIEW`, `SELECTED`, etc.).
7. Student receives status email notification automatically.
8. Download company-wise Excel from `Companies`.
9. Update backlogs in `Students` and see audit trail in `Backlog History`.

## Export Template Format
Per company, set JSON list:
```json
[
  {"header":"Roll Number","source":"student.roll_no"},
  {"header":"Name","source":"student.name"},
  {"header":"Branch","source":"student.branch"},
  {"header":"CGPA","source":"student.cgpa"},
  {"header":"Backlogs","source":"student.backlogs"},
  {"header":"Resume","source":"resume.filename"},
  {"header":"Applied On","source":"application.applied_at"}
]
```

Supported `source` keys:
- `student.roll_no`
- `student.name`
- `student.branch`
- `student.cgpa`
- `student.backlogs`
- `student.lateral_entry`
- `application.status`
- `application.applied_at`
- `company.name`
- `resume.filename`
- `resume.path`

## Notes
- CGPA is recalculated as weighted average: `sum(SGPA * semester_credits) / sum(semester_credits)`.
- For lateral-entry students, semesters `< 3` are ignored in CGPA and import.
- Backlog total is recalculated after SGPA import and backlog updates.

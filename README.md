# Campus Placement Portal

Placement portal for on-campus drives with:
- Student applications to companies
- Company-specific Excel export format
- SGPA import from semester PDF
- Lateral-entry student handling
- Resume link support per student (no file upload storage dependency)
- Backlog updates with recalculation and audit history
- Student eligibility locking (`ELIGIBLE`, `EXTERNAL_INTERN`, `CAMPUS_INTERN`, `EXTERNAL_PLACED`, `BLOCKED_BY_POLICY`)
- Company selection policy (`BLOCKING` / `NON_BLOCKING`)
- Login with role-based access (`ADMIN`, `PLACEMENT_COORDINATOR`, `STUDENT`)
- Email + password login (no OTP at login)
- Production-ready deployment setup (Postgres + Gunicorn + Docker + Render/Railway configs)

## Tech Stack
- Flask + SQLAlchemy (SQLite local / Postgres production)
- `pdfplumber` for PDF table extraction
- `pandas` + `openpyxl` for Excel export

## Local Setup
```bash
cd TestPlacement
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

App runs at `http://127.0.0.1:5000`.

## Default Admin Behavior
- Local (`ENVIRONMENT != production`): if no admin exists and no env values provided, app auto-creates:
  - `admin@placement.local` / `admin123`
- Production (`ENVIRONMENT=production`): admin is created only if both are set:
  - `DEFAULT_ADMIN_EMAIL`
  - `DEFAULT_ADMIN_PASSWORD`



## Production Environment Variables
Required:
- `ENVIRONMENT=production`
- `SECRET_KEY` (strong random value)
- `DATABASE_URL` (Postgres connection string)
- `DEFAULT_ADMIN_EMAIL`
- `DEFAULT_ADMIN_PASSWORD`


Optional:
- `UPLOAD_DIR` (default `./uploads`)
- `AUTO_INIT_DB` (default `true`)
- `PDF_TABLE_FALLBACK` (default `false`; keep false on low-memory hosts)

## Core Workflows
1. Add students in `Students`.
2. Save resume link and eligibility status per student profile.
3. Add companies in `Companies` with eligibility + export JSON template.
4. Import SGPA PDF in `SGPA Import` for each semester/branch.
5. Students apply in `Applications`.
6. Configure company policy (`BLOCKING` or `NON_BLOCKING`).
7. Coordinators/Admin update application status (`SHORTLISTED`, `INTERVIEW`, `SELECTED`, etc.).
8. If selected in a `BLOCKING` company, student is auto-blocked from new applications.
9. Student receives status email notification automatically.
10. Download company-wise Excel from `Companies`.
11. Update backlogs in `Students` and see audit trail in `Backlog History`.

## Export Template Format
Per company, set JSON list:
```json
[
  {"header":"Roll Number","source":"student.roll_no"},
  {"header":"Name","source":"student.name"},
  {"header":"Branch","source":"student.branch"},
  {"header":"CGPA","source":"student.cgpa"},
  {"header":"Backlogs","source":"student.backlogs"},
  {"header":"Resume Link","source":"student.resume_link"},
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
- `student.resume_link`
- `application.status`
- `application.applied_at`
- `company.name`
- `resume.link`

## Notes
- CGPA is recalculated as weighted average: `sum(SGPA * semester_credits) / sum(semester_credits)`.
- For lateral-entry students, semesters `< 3` are ignored in CGPA and import.
- Backlog total is recalculated after SGPA import and backlog updates.


## Important Production Notes
- Do not use SQLite in production.
- `uploads/` is local filesystem; only SGPA-imported PDFs are stored there by default.
- Keep `.env`, `placement.db`, and `uploads/` out of Git.

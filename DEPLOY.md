# Deploy checklist (push & go live)

After you **push** your code, the app does **not** read your local `.env`. You must set everything on the **host** (e.g. Render).

---

## 1. Push to GitHub

- Commit and push as usual. Do **not** commit `.env` (it’s in `.gitignore`).

---

## 2. Set environment variables on the host

In your hosting dashboard (e.g. Render → your service → **Environment**), set these. Use **secret** fields for passwords and `SECRET_KEY`.

| Variable | Required | Example / note |
|----------|----------|----------------|
| `ENVIRONMENT` | Yes | `production` |
| `SECRET_KEY` | Yes | Long random string (e.g. 32+ chars). Render can generate. |
| `DATABASE_URL` | Yes | Postgres URL. On Render: add a Postgres DB and link it (or paste the **Internal** URL). |
| `DEFAULT_ADMIN_EMAIL` | Yes | Admin login email. |
| `DEFAULT_ADMIN_PASSWORD` | Yes | Admin password (strong). |
| `MAIL_SERVER` | For email | `smtp.gmail.com` |
| `MAIL_PORT` | For email | `587` |
| `MAIL_USERNAME` | For email | Your Gmail (or SMTP user). |
| `MAIL_PASSWORD` | For email | Gmail App Password (no spaces in the value, or use quotes in the dashboard if needed). |
| `MAIL_USE_TLS` | For email | `true` |
| `MAIL_FROM` | For email | Same as `MAIL_USERNAME` or your “from” address. |

- If you use **Render** with the repo’s `render.yaml`, it already defines these keys; you only need to **fill in the values** for the ones marked `sync: false` (and ensure a Postgres database is created and linked for `DATABASE_URL`).

---

## 3. Database (Render example)

- In Render, create a **PostgreSQL** database (e.g. name `campus-placement-db` if your `render.yaml` references it).
- The `DATABASE_URL` is usually set automatically when you link the DB to the web service. If not, copy the **Internal Database URL** into `DATABASE_URL`.

---

## 4. After first deploy

- Open your app URL and hit `/healthz` — should return `{"status":"ok"}`.
- Log in with `DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD`.
- Try **Register** or **Forgot password** to confirm email (OTP) works.

---

## 5. Optional

- **Uploads**: SGPA PDFs go to `uploads/`. On free tiers this is often ephemeral; for persistence you’d need a volume or external storage (not covered here).
- **Custom domain**: Configure in the host’s dashboard and point DNS as instructed.

# MediConnect — Backend overview

Django project **`pharmacybackend`** exposes the MediConnect API primarily through the **`chatbot`** application under **`/api/chatbot/`**. For ranking, AI flows, and design rationale, see **[PLATFORM_OVERVIEW.md](./PLATFORM_OVERVIEW.md)**.

---

## 1. Stack

| Layer | Technology |
|--------|------------|
| Runtime | Python 3 |
| Framework | Django ≥ 6, Django REST Framework |
| HTTP (typical prod) | Gunicorn (WSGI) |
| Real-time | Django **Channels** — use **Daphne** for ASGI (HTTP + WebSockets), not `runserver --daphne` |
| CORS / CSRF | `django-cors-headers`, Django CSRF (SPA admin uses session + `X-CSRFToken`) |
| Database | **MongoDB** is the canonical default (`MONGODB_URI`, `django-mongodb-backend`; all app/session/auth/admin data on `default`). SQL-only SQLite/PostgreSQL exists only when `USE_SQL_BACKEND=true` (or `DJANGO_USE_MONGODB=false`). Optional read-only **`LEGACY_SQLITE_PATH`** is for migrating legacy SQLite into Mongo, not primary storage. |
| AI / vision | `google-generativeai`, Pillow, OpenCV, pytesseract (prescription / OCR paths) |
| Email / MFA | Django `send_mail`, optional **`LOGIN_EMAIL_2FA`**, **`pyotp`** for TOTP |
| Config | `python-dotenv`, `django-environ` (`.env` at repo root) |

**Dependency lockfile:** root `requirements.txt`.

---

## 2. URL layout

| Prefix | Purpose |
|--------|---------|
| `/` | Short JSON “API alive” message |
| `/health/` | Health JSON |
| `/admin/` | Django admin (when not on stripped Mongo admin config) |
| `/api/chatbot/` | **All MediConnect REST routes** (see `chatbot/urls.py`) |

The `api` Django app’s URL include is reserved; **`api/urls.py`** is currently minimal.

---

## 3. Major API groups (`/api/chatbot/`)

- **Patient / chat:** `chat/` (responses include **`drug_interactions`** plus a preamble in **`response`** when the embedded ruleset flags pairs), `conversation/<uuid>/`, `upload-prescription/` (with coordinates: creates a pharmacist-visible broadcast **even when OCR returns no medicines** — image stored for manual read), `check-interactions/`, `alternatives/`, `rate-pharmacy/`, `record-purchase/`, `reserve/`, `request/.../responses/`, `request/.../ranked/` (**`envelope=true`** adds **`meta.drug_interactions`**; flat-array mode unchanged unless **`include_drug_interactions=true`** wraps `{ items, count, drug_interactions }`). **WebSocket** `ws/chatbot/<medicine_request_id>/`: `medicine_request_snapshot` when `/chat/` saves the broadcast (same `pharmacy_responses` as HTTP; includes **`drug_interactions`**); `medicine_request_ranked_update` when a pharmacist submits a quote (merged list **identical** to `GET .../ranked/` — chronology for ~2 min then MCDA + live-inventory dedupe; includes `ranking_pending`, **`drug_interactions`**, and `poll_url`); legacy `{ "event": "pharmacy_response", "medicine_request_id" }` still sent for older UIs.
- **Registration / directory:** `register/patient|pharmacy|pharmacist/`, `pharmacies/`, `pharmacists/`
- **Patient dashboard:** `patient/login/`, `patient/profile/`, `patient/requests/` (rows include **`reservations[]`** when reserves link ``request_id``), **`patient/active-request/`**, `patient/dashboard/stats/`, `patient/saved-medicines/`, `patient/notifications/`, **patient MFA** (`patient/mfa/...`). Pickup contract: **`[PATIENT_PICKUP_BACKEND_SPEC.md](./PATIENT_PICKUP_BACKEND_SPEC.md)`** (`reserve/` 409 duplicates, **`GET ranked/?envelope=true`** hints).
- **Pharmacist portal:** `pharmacist/login/` (returns JWT **`access`** / **`refresh`**), **`pharmacist/token/refresh/`**, **`GET|PATCH pharmacist/settings/`** (nested envelope + `operations.accepting_requests` — contract **[PHARMACY_SETTINGS_BACKEND_SPEC.md](./PHARMACY_SETTINGS_BACKEND_SPEC.md)**), `pharmacist/profile/`, `pharmacist/security/`, **`pharmacist/password/change/`**, `pharmacist/requests/` (each row may include **`prescription_review`**, **`prescription_image_url`**, **`needs_pharmacist_prescription_read`** when OCR left **`medicine_names`** empty — image stream: **`GET pharmacist/requests/<request_id>/prescription-image/?pharmacist_id=…`**), `pharmacist/response/<uuid>/`, `pharmacist/decline/<uuid>/`, `pharmacist/inventory/` (**GET list, POST bulk upsert, POST `{ "action": "delete", ... }`** as DELETE fallback, **PATCH** single row including optional **`new_medicine_name`** rename, **DELETE row** — see view docstring), `pharmacist/reservations/`, **pharmacist MFA** (`pharmacist/mfa/...`, Bearer), `pharmacist/<uuid>/ranking-summary/`
- **Auth helpers:** `auth/password-reset/request|confirm/`, `auth/mfa/login/complete/`, `auth/login/verify-email-otp/`
- **Admin (session):** `admin/login|logout|me|csrf/`, dashboard, ranking config, chatbot policy & safety reviews, analytics (heatmap, impact, SLA, search volume, MediBot overview), pharmacies (CRUD, verification queue, watchlist, export), pharmacists, requests/reservations status, audit logs, patients list, chatbot logs, AI report PDF, patient-by-session tools, `admin/dashboard/widgets/` (also mounted under `pharmacybackend/urls.py` for ordering)

Legacy aliases: `pharmacy/requests/`, `pharmacy/response/<uuid>/`.

---

## 4. Environment variables (common)

| Variable | Role |
|----------|------|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` | Django core |
| `MONGODB_URI`, `MONGODB_DB_NAME` | **Mongo** default database (required unless SQL fallback) |
| `USE_SQL_BACKEND` | **`true`** → SQLite (`db.sqlite3`) or Postgres via `DATABASE_URL` instead of Mongo |
| `DJANGO_USE_MONGODB` | Explicit opt-out (`false`) from Mongo stack when migrating/debugging |
| `DATABASE_URL` | Postgres when Mongo is disabled (`DJANGO_USE_MONGODB=false` or `USE_SQL_BACKEND=true`; prefer `DATABASE_URL`; else SQLite `db.sqlite3`) |
| `LEGACY_SQLITE_PATH` | Optional second SQLite alias for `import_sqlite_to_mongodb` migration tool |
| `CORS_ORIGIN`, `CSRF_TRUSTED_ORIGINS` | SPA origins |
| `OPENROUTER_API_KEY` / `GEMINI_API_KEY` | MediBot (see startup logs) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`, `EMAIL_BACKEND` | Outbound email |
| `LOGIN_EMAIL_2FA` | After password, email OTP until `auth/mfa/login/complete/` or legacy verify path |
| `JWT_ACCESS_MINUTES`, `JWT_REFRESH_DAYS` | SimpleJWT lifetime for pharmacist (and future) Bearer tokens |
| `APP_PUBLIC_URL` | Optional links inside emails |
| `AUTO_SIMULATE_RESPONSES` | Demo-only simulated pharmacy responses |

---

## 5. Core models (conceptual)

- **Chat:** `ChatConversation`, `ChatMessage`
- **Requests / responses:** `MedicineRequest`, `PharmacyResponse`, ranking snapshots
- **Directory:** `Pharmacy`, `Pharmacist`, `PharmacyInventory`, `PharmacyRating`
- **Patient app:** `PatientProfile`, `SavedMedicine`, `PatientNotification`, `Reservation`
- **Governance:** `PlatformAdminSettings`, `ChatbotSafetyReview`, `PharmacySettings` (+ history), `AdminAuditLog`

---

## 6. Email behaviour (high level)

When SMTP is configured, the backend can send: patient **medicine-request** summaries (optional flag from chat), **nearby pharmacy** notices on broadcast, **pharmacy-response** notices to patients (unless opted out), **login / password-reset** codes, and related transactional mail. If email is not configured, sends are skipped with warnings; most APIs still return success.

---

## 7. Security & auth

- **Patient / pharmacist login:** JSON responses; optional **TOTP** (enrolled via `patient/mfa/*`, `pharmacist/mfa/*`) or **email OTP** when `LOGIN_EMAIL_2FA` is enabled.
- **Admin:** Django **session** login; mutating admin requests need **CSRF** after bootstrap.
- **Password reset:** email code + `user_type` (`patient` | `pharmacist` | `admin`).

---

## 8. Deployment notes

- **Production HTTP:** Gunicorn → Django WSGI, or **Daphne** → `pharmacybackend.asgi:application` for Channels.
- **MongoDB:** Atlas network blips can surface as `DatabaseError`; use retries, stable pooling, and graceful handling where implemented (e.g. reservation list helpers).
- **HTTPS:** Recommended for geolocation, cookies, and mixed-content rules in production.

---

## 9. Testing & quality (honest)

- There is **no** bundled automated test suite under `tests/` in this repo by default. Use **manual / Postman** checks and/or add `pytest-django` or `manage.py test` for regression coverage before capstone submission if required.

---

## 10. Key source files

| Path | Role |
|------|------|
| `chatbot/views.py` | Most HTTP handlers |
| `chatbot/urls.py` | Route table |
| `chatbot/models.py` | ORM models |
| `chatbot/serializers.py` | DRF serializers |
| `chatbot/services.py` | Chatbot, ranking, location, OCR, etc. |
| `chatbot/email_service.py` | Mail + OTP cache helpers |
| `chatbot/mfa_api.py` | Patient / pharmacist TOTP enrollment |
| `chatbot/pharmacy_portal_ranking.py` | Portal leaderboard logic |
| `chatbot/admin_analytics.py` | Admin analytics bundles |
| `pharmacybackend/settings.py` | Settings |
| `pharmacybackend/asgi.py` | ASGI entry (Channels) |

---

## 11. Related doc

- **[PLATFORM_OVERVIEW.md](./PLATFORM_OVERVIEW.md)** — flows, MCDA ranking, three “ranking surfaces,” capstone framing, file map.

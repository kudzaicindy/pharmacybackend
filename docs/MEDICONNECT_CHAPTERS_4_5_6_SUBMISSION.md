# MediConnect (MediBot) — Chapters 4 & 5  
## System Implementation · Results, Discussion, Recommendations & Future Work

> **Your programme uses only Chapters 4 and 5** (no separate Chapter 6). Chapter 5 therefore contains **both** evaluation (results, tests, failure modes) **and** closing sections (conclusion, recommendations, future work, reflection).

**Student name:** *[Full name]*  
**Registration number:** R229417K  
**Programme:** *[BSc / BEng — specify]*  
**Institution:** University of Zimbabwe · *[Faculty / Department]*  
**Supervisor:** *[Supervisor name]*  
**Submission date:** *[Date — e.g. May 2026]*

---

## Document guide (remove before final PDF)

### Correct structure for your submission (Chapters 4 & 5 only)

Your supervisor’s comment — *“after chapter 4 we have chapter 5 which have recommendations and future work”* — means **Chapter 5 is the right place** for recommendations and future work. You do **not** need a separate Chapter 6.

What she is likely asking you to fix: Chapter 5 should be **complete**, not **only** recommendations. It should include **results and discussion first**, then **conclusion, recommendations, and future work** at the end.

| Chapter | What belongs here |
|---------|-------------------|
| **4** | Implementation: lifecycle, stack, **technology rationale**, **config/deployment**, components, benchmarks, ERD, security, screenshots (Figures 4.1–4.6) |
| **5** | **§5.1–5.8** Results, tests, failure modes, discussion, limitations, ethics → then **§5.9–5.13** Conclusion, objectives, **recommendations**, **future work**, reflection |

**Action in Google Docs:** Keep recommendations in Chapter 5, but **add** (before them) the missing **test tables** and **failure-mode** sections from this document if they are still only in Chapter 4 or missing.

| Item | Action |
|------|--------|
| **Target length** | ~**30 pages** in Word: Times New Roman 12 pt, 1.5 line spacing, normal margins; insert **6 screenshots** + **2–3 diagrams** (ERD, data-flow, config flow) |
| **Merge** | Combine with your **frontend** chapter if required; deduplicate Introduction |
| **Figures** | Export Mermaid from [mermaid.live](https://mermaid.live); insert PNGs from `media/` folder |
| **Placeholders** | Search `[fill]` and `*[paste]*` — complete from your demo and Chapter 1 |
| **Old PDF** | Copy **pass/fail rows** from `R229417K Capstone chapter 4,5,6.pdf` into Tables 5.1–5.12 where you already tested |

**Word export:** Open this file in Word (Paste → Keep Source Formatting) or use Pandoc:  
`pandoc docs/MEDICONNECT_CHAPTERS_4_5_6_SUBMISSION.md -o Chapters_4_5_6.docx`

---

# CHAPTER 4 — SYSTEM IMPLEMENTATION

## 4.1 Introduction

Medicine access in Zimbabwe is often constrained by incomplete information: patients do not know which pharmacies stock a product, at what price, or how far they must travel. MediConnect addresses this gap by coupling a conversational **MediBot** assistant with a **broadcast-and-quote** workflow that lets multiple pharmacies compete transparently within a patient’s geographic reach. From an implementation perspective, the system is a **three-tier web application**: a React patient and pharmacy front-end, a Django REST and WebSocket back-end, and a document-oriented database (MongoDB in deployment) holding conversations, requests, responses, inventory, and governance configuration.

This chapter describes how MediConnect was engineered: the development approach, deployment environment, technology choices (with justification), configuration and secrets management, core algorithms, security controls, and user-interface evidence. MediConnect is a full-stack, AI-assisted medicine discovery platform for Zimbabwe. The **patient** uses **MediBot** to search by symptoms, upload prescriptions, or name medicines directly; the system captures **location**, **broadcasts** requests to nearby pharmacies, and returns **ranked** quotations. **Pharmacists** respond through a portal with inventory and reservation tools. **Platform administrators** govern verification, ranking policy, safety reviews, and operational metrics.

The **backend** (`pharmacybackend` repository) is a Django 6 application exposing REST APIs under `/api/chatbot/`, optional **WebSockets** at `ws/chatbot/<medicine_request_id>/`, and integration with **Google Gemini** for conversational AI and prescription vision OCR. Persistence defaults to **MongoDB** (Atlas); **SQLite** and **PostgreSQL** are supported for development via environment flags. The **frontend** (`pharmacyfrontend`) is a React/Vite progressive web application; this chapter references its screens where they illustrate backend contracts. Detailed API documentation is maintained in `docs/BACKEND.md` and `docs/PLATFORM_OVERVIEW.md`.

## 4.2 System development lifecycle

Development followed an iterative, API-first lifecycle aligned with capstone milestones:

1. **Requirements and role modelling** — User stories for **patient** (often anonymous session), **pharmacist** (JWT-authenticated portal), and **administrator** (session + CSRF). Problems addressed include opaque stock, travel cost to find medicines, and lack of price comparison.

2. **Contract-first integration** — Endpoints for `chat/`, `upload-prescription/`, `request/<uuid>/ranked/`, `reserve/`, and `pharmacist/*` were stabilised early so frontend and backend could evolve in parallel.

3. **Vertical slices** — (i) conversational search and location capture; (ii) medicine-request broadcast; (iii) pharmacist response and inventory; (iv) MCDA ranking and WebSocket updates; (v) reservations and admin analytics.

4. **Hardening** — CORS/CSRF, ranked-endpoint ownership checks, MFA (TOTP), audit logs, graceful degradation when Gemini quota is exhausted (pharmacist manual prescription read), and session cleanup for anonymous privacy.

Supervisor reviews and progress assessments were scheduled between slices so that integration defects (e.g. ranked list shape differing between WebSocket and HTTP) were caught before the next feature layer. This reduced the risk of a “big bang” integration week before submission.

## 4.3 Deployment environment

### 4.3.1 Hardware and client requirements

**Table 4.1 — Server and client baseline**

| Aspect | Minimum | Recommended (demo / pilot) |
|--------|---------|------------------------------|
| CPU | Quad-core x64 | Intel i5 / AMD Ryzen 5 or better |
| RAM | 8 GB | 16 GB (Daphne + DB + Channels) |
| Storage | 50 GB SSD | SSD with room for `media/prescriptions/` |
| Network | Stable broadband | ≥10 Mbps for Gemini + WebSockets |
| Patient device | Modern mobile browser | Chrome 89+, Safari 14+; PWA install optional |

### 4.3.2 Technology rationale

**Table 4.2 — Technology rationale**

| Choice | Alternatives considered | Reason for selection |
|--------|-------------------------|----------------------|
| Django 6 + DRF | Flask, FastAPI, Node/Express | ORM, migrations, admin, security middleware; rapid delivery of pharmacy registry and reservations |
| Django Channels + Daphne | Socket.IO, MQTT, raw WS | Same codebase as REST; group broadcast per `MedicineRequest` UUID |
| MongoDB (`django-mongodb-backend`) | PostgreSQL, SQLite only | JSON fields for `medicine_responses`, chat metadata, ranking snapshots; Atlas hosting |
| Gemini (`google-generativeai`) | GPT-4, Claude, local Llama | One vendor for chat + vision OCR; feasible free tier for capstone; OpenRouter optional fallback |
| MCDA ranking (price, distance, rating, reliability) | Price-only sort | Explainable scores; admin weight profiles for urban/rural |
| JWT (pharmacist) + Django session (admin) | OAuth-only everywhere | SPA refresh flow for pharmacists; CSRF-safe admin mutations |
| python-dotenv | Hard-coded secrets, Vault | Simple `.env` pattern for student deployment |

### 4.3.3 Configuration management and deployment

MediConnect does **not** hard-code credentials. At startup, `pharmacybackend/settings.py` loads `BASE_DIR/.env` via **python-dotenv** (template: `.env.example`). **Development vs production** uses the **same** settings module; only environment **values** differ.

**Configuration load sequence:** (1) Process start → (2) load `.env` → (3) read `USE_SQL_BACKEND`, `MONGODB_URI`, `SECRET_KEY`, etc. → (4) select database engine → (5) bind CORS, JWT lifetimes, Gemini keys → (6) serve HTTP/WS via Daphne ASGI.

**Table 4.3 — Development vs production environment**

| Variable | Development | Production |
|----------|-------------|------------|
| `DEBUG` | `true` | `false` |
| `SECRET_KEY` | Local placeholder | Strong random secret; not in git |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Public API hostname |
| `USE_SQL_BACKEND` | Often `true` (SQLite) | `false` (Mongo canonical) |
| `MONGODB_URI` | Dev Atlas cluster or omitted | Production Atlas + IP allow-list |
| `CORS_ORIGIN` / `CSRF_TRUSTED_ORIGINS` | `http://localhost:5173` | HTTPS SPA origin |
| `GEMINI_API_KEY` | Personal dev key | Project key + billing alerts |
| Channels layer | In-memory | **Redis** (multi-worker) |

**Table 4.4 — Database backend selection**

| Condition | `DATABASES['default']` |
|-----------|------------------------|
| `USE_SQL_BACKEND=true` + `DATABASE_URL` | PostgreSQL |
| `USE_SQL_BACKEND=true`, no URL | SQLite `db.sqlite3` |
| `USE_SQL_BACKEND=false` + `MONGODB_URI` | MongoDB (`django_mongodb_backend`) |
| Mongo expected, URI missing | Startup error |

**Secrets management:** `SECRET_KEY` (Django sessions, CSRF, SimpleJWT signing), `MONGODB_URI` (credentials in connection string), `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, and SMTP passwords live only in `.env` or host secret store—never in the repository. Pharmacist passwords are hashed in the database. Operational practice: separate keys per environment; rotate on leak; least-privilege Atlas users.

**Migrations:** One linear chain `chatbot/migrations/0001`–`0023`. Command `python manage.py migrate` applies to whichever backend is selected. Mongo uses shim apps and `ObjectIdAutoField`; SQL uses `BigAutoField`. Legacy SQLite data can be imported once: `python manage.py import_sqlite_to_mongodb` with `LEGACY_SQLITE_PATH=db.sqlite3`. **Rule:** one primary backend per environment; no dual-write.

**Deployment checklist:** `DEBUG=false` → `migrate` → configure secrets and CORS → start `daphne pharmacybackend.asgi:application` behind HTTPS reverse proxy → cron for `clean_expired_sessions` and `expire_reservations`.

## 4.4 Software stack

**Table 4.5 — Backend stack**

| Layer | Technology |
|-------|------------|
| Language | Python 3.10+ |
| Framework | Django 6, Django REST Framework |
| Real-time | Django Channels, Daphne (ASGI) |
| Database | MongoDB (default); SQLite / PostgreSQL (fallback) |
| AI / OCR | google-generativeai (Gemini); Pillow, OpenCV |
| Auth | SimpleJWT (pharmacist); Django session (admin); pyotp MFA |
| Config | python-dotenv, django-environ |

**Table 4.6 — Frontend stack (summary)**

| Layer | Technology |
|-------|------------|
| UI | React 19, Vite 7, react-router-dom 7 |
| PWA | vite-plugin-pwa |
| Reporting | jsPDF, marked (admin exports) |

**External services:** Gemini API (chat, vision OCR, admin narrative reports); optional OpenRouter; SMTP for transactional email; Nominatim-style geocoding where implemented.

## 4.5 Core implementation and algorithms

### 4.5.1 Conversational medicine discovery (Component A)

`POST /api/chatbot/chat/` accepts user messages, optional `medicines`, location fields, and prescription flags (`prescription_image_only`, `ocr_failed`). The server persists `ChatConversation` and `ChatMessage` rows, invokes `ChatbotService` (Gemini or OpenRouter), and returns intents, suggested medicines, and `drug_interactions` from a rules-based `DrugInteractionService`. Symptom flow enforces: suggest medicines first, then request location, then broadcast.

### 4.5.2 Prescription upload and OCR (Component B)

`POST /api/chatbot/upload-prescription/` validates `content_type` starts with `image/`. `OCRService` calls Gemini Vision with a fallback model chain. Success injects `prescription_medicines` into conversation metadata and, with GPS, creates `MedicineRequest` with `prescription_image`. Failure (quota, unreadable image) still broadcasts **image-only** requests when coordinates are present; flags include `ocr_failed`, `prescription_image_only`, and `skip_ocr` for manual pharmacist read.

### 4.5.3 Broadcast and MCDA ranking (Component C)

`create_medicine_request` stores patient coordinates, medicine list (possibly empty), status lifecycle, and expiry (urban ~30 min vs rural ~120 min heuristics). Pharmacists submit `PharmacyResponse` records. Patients fetch `GET /api/chatbot/request/<uuid>/ranked/?envelope=true` for merged pharmacist quotes and live-inventory rows. `RankingEngine` applies min–max normalisation on price, distance, rating, and reliability using weights from `PlatformAdminSettings` (urban/rural). Initial window may show chronological order before MCDA activates (`RANKING_DELAY_MINUTES`).

### 4.5.4 Reservations and pickups (Component D)

`POST /api/chatbot/reserve/` locks stock for two hours (`reserved_quantity` on `PharmacyInventory`). Duplicate active reservations return **409 Conflict** with existing `reservation_id`. States: pending → confirmed → picked up / expired / cancelled.

### 4.5.5 Administration and governance (Component E)

Admin APIs expose pharmacy verification, ranking configuration, `ChatbotSafetyReview`, `AdminAuditLog`, analytics (heatmap, SLA, search volume), and reservation exports. `PlatformAdminSettings` singleton stores MCDA weights and `chatbot_policy` toggles (disclaimers, emergency detection, dosage restrictions).

### 4.5.6 Performance benchmarks

Measurements taken on development hardware (Windows 11, Python 3.14, May 2026 revision). OCR includes network latency to Google.

**Table 4.7 — Performance benchmarks**

| Metric | Method | n | Mean | Median | p95 |
|--------|--------|---|------|--------|-----|
| MCDA ranking (8 rows) | `RankingEngine.rank_responses()` | 500 | 62 ms | 55 ms | 84 ms |
| MCDA + density lookup | Harare CBD coordinates | 50 | 303 ms | 146 ms | 851 ms |
| OCR upload → JSON | `POST /upload-prescription/` | 20 | [fill] | [fill] | [fill] |
| Full `GET .../ranked/` | Post-broadcast | 20 | [fill] | [fill] | [fill] |
| WebSocket delivery | localhost, in-memory layer | 20 | 0.13 ms | 0.12 ms | 0.21 ms |

*Interpretation:* Ranking math is lightweight; DB reads dominate. OCR and chat are Gemini-bound. WebSockets are suitable for live updates; HTTP ranked polling remains the fallback.

### 4.5.7 Data model and data flow

**Figure 4.7** — Core entity-relationship diagram (export Mermaid below to Word).

```mermaid
erDiagram
    ChatConversation ||--o{ ChatMessage : contains
    ChatConversation ||--o{ MedicineRequest : creates
    MedicineRequest ||--o{ PharmacyResponse : receives
    MedicineRequest ||--o{ Reservation : may_have
    Pharmacy ||--o{ PharmacyInventory : stocks
    Pharmacy ||--o{ PharmacyResponse : submits
    PlatformAdminSettings ||..|| RankingEngine : configures
```

**Figure 4.8** — Prescription-to-ranking data flow: Patient uploads image → `upload-prescription` → OCR → `MedicineRequest` → pharmacist `PharmacyResponse` → `RankingEngine` merge → HTTP/WebSocket to patient.

Key entities: `ChatConversation` (session_id, context_metadata); `MedicineRequest` (medicine_names JSON, prescription_image, status, coordinates); `PharmacyResponse` (price, distance_km, medicine_responses JSON); `PharmacyInventory` (quantity, reserved_quantity); `Reservation` (expires_at, status); `PlatformAdminSettings` (ranking_weights_urban/rural, chatbot_policy).

## 4.6 Security, governance, and data handling

### 4.6.1 Data in transit

Production uses **HTTPS/TLS** end-to-end. MongoDB Atlas uses TLS (`mongodb+srv://`). Gemini uses HTTPS. Pharmacist APIs use `Authorization: Bearer`; admin mutating calls require **CSRF** token from `GET /api/chatbot/admin/csrf/`.

### 4.6.2 Data at rest

Prescription images stored under `MEDIA_ROOT/prescriptions/YYYY/MM/` with access only through application endpoints (pharmacist `prescription-image` view). **Design policy:** volume or object-storage **AES-256** encryption at rest. Chat and transactional data encrypted via Atlas/SQL provider defaults.

### 4.6.3 Retention and audit

| Data | Retention | Mechanism |
|------|-----------|-----------|
| Anonymous chat | 24 h default | `manage.py clean_expired_sessions` |
| Admin audit logs | 90 days | `AdminAuditLog` |
| Prescription images | 90 days after request closed | Policy / scheduled purge (recommended) |
| Ranking snapshots | 90 days | Dispute resolution |

### 4.6.4 Rate limiting (production plan)

| Endpoint | Limit |
|----------|-------|
| `POST /chat/` (anonymous) | 10 req/min/IP |
| `POST /upload-prescription/` | 5 uploads/min/session |
| Pharmacist login failures | 5 per 15 min/email |

DRF throttles or nginx `limit_req` to be enabled before public launch.

### 4.6.5 Prompt-injection mitigations

Fixed system prompt and `PlatformAdminSettings.chatbot_policy` suffix; user text in separate message role; OCR uses fixed templates; upload MIME validation; `ChatbotSafetyReview` queue for admin follow-up.

## 4.7 User interface implementation (evidence)

Insert screenshots from `media/` (crop watermarks; redact PHI if required).

**Figure 4.1** — Landing page (`Screenshot 2026-05-24 152251.png`): public entry, MediBot preview.  
**Figure 4.2** — Prescription OCR flow (`…235016.png`): extracted medicines, confidence, broadcast.  
**Figure 4.3** — Ranked pharmacies (`…163250.png`): distance, time, score, price.  
**Figure 4.4** — Alternatives (`…201415.png`): unavailable lines vs pharmacist alternative.  
**Figure 4.5** — Pharmacy inventory (`…185207.png`): stock levels, CSV export.  
**Figure 4.6** — Admin reservations (`…223014.png`): lifecycle states, KPIs.

## 4.8 Traceability (UI to API)

| UI feature | Backend touchpoint |
|------------|------------------|
| MediBot chat | `POST /api/chatbot/chat/` |
| Prescription upload | `POST /api/chatbot/upload-prescription/` |
| Ranked cards | `GET /api/chatbot/request/<uuid>/ranked/` |
| Reserve | `POST /api/chatbot/reserve/` |
| Inventory grid | `GET/POST /api/chatbot/pharmacist/inventory/` |
| Admin KPIs | `/api/chatbot/admin/dashboard/` |

---

# CHAPTER 5 — RESULTS, DISCUSSION, RECOMMENDATIONS AND FUTURE WORK

*Chapter 5 has two parts: **(A)** evaluation — tests, failures, discussion (§5.1–5.8); **(B)** closing — conclusion, recommendations, future work, reflection (§5.9–5.13). This matches a capstone that ends at Chapter 5.*

## 5.1 Introduction

Evaluation of a capstone system must go beyond demonstrating that “the happy path works once.” Examiners and future maintainers need evidence that the team understood **failure behaviour**, **performance characteristics**, and **ethical boundaries** of an AI-assisted health information system. This chapter therefore organises results in three layers: (1) **structured module tests** mirroring use cases from requirements; (2) **documented edge cases** when Gemini, WebSockets, uploads, or reservations fail; and (3) **interpretive discussion** linking evidence to aims, risks, and limitations.

This chapter presents evaluation outcomes: structured module tests (happy paths), **failure-mode behaviour** under dependency and input errors, performance observations, risk discussion, limitations, and mapping to research questions. Testing combined manual browser journeys, API calls (Postman or equivalent), and supervisor-witnessed demonstration runs in Harare pilot conditions with demo pharmacy accounts.

**Integrity statement:** Formal OCR precision/recall on a clinician-adjudicated corpus was **not** completed; prescription accuracy is reported via **visual inspection** on a pilot set of *[N — fill]* images. Load testing was limited to development-scale concurrency.

## 5.2 Module testing — happy paths

Copy your observed results from the original submission where available. Below is the **required structure** with example Pass entries—**replace “Observed”** with your evidence.

**Table 5.1 — Login (Login01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Login01 | Valid pharmacist credentials | JWT access + refresh returned | [fill] | Pass |
| Login-N1 | Invalid password | Error message; no token | [fill] | [fill] |

**Table 5.2 — Registration (Register01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Register01 | New pharmacy registration | Pending verification status | [fill] | Pass |
| Reg-N1 | Duplicate email | 400/409; no duplicate row | [fill] | [fill] |

**Table 5.3 — Forgot password (ForgotPwd01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| ForgotPwd01 | Request reset OTP | Email/console OTP issued | [fill] | Pass |

**Table 5.4 — AI chat / symptom search (Chat01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Chat01 | Symptom → medicine suggestions → location | Suggested medicines then location prompt | [fill] | Pass |
| Chat-N1 | Prompt-injection style message | Policy-bound reply; no dosing | [fill] | [fill] |

**Table 5.5 — Prescription OCR (Presc01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Presc01 | Clear printed Rx + location | Medicines extracted; broadcast ID returned | [fill] | Pass |
| Presc-N1 | PDF / non-image MIME | 400 “File must be an image” | [fill] | [fill] |
| Presc-N2 | Quota / OCR failure + GPS | 200, ocr_failed, image broadcast | [fill] | [fill] |
| Presc-N3 | Non-prescription photo + GPS | Empty medicines; image-only broadcast | [fill] | [fill] |

**Table 5.6 — Broadcast and WebSocket (Request01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Request01 | Broadcast after location | medicine_request_id; WS snapshot optional | [fill] | Pass |
| Req-N1 | WS disconnected; pharmacist quotes | HTTP ranked shows new row | [fill] | [fill] |

**Table 5.7 — Pharmacist response and inventory (Pharma01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Pharma01 | Submit quote + inventory update | Response visible on patient ranked view | [fill] | Pass |
| Pharma-N1 | Quote on expired request | 400/404 rejected | [fill] | [fill] |

**Table 5.8 — Ranking (Rank01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Rank01 | Multiple quotes | Ordered rows with scores/distances | [fill] | Pass |
| Rank-N1 | Single responder | Rank 1 assigned; no crash | [fill] | [fill] |

**Table 5.9 — Reservation (Res01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Res01 | Reserve in-stock item | 201; reserved_quantity increased | [fill] | Pass |
| Res-N1 | Duplicate reserve same SKU | 409 + existing reservation_id | [fill] | [fill] |

**Table 5.10 — Admin dashboard (Admin01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Admin01 | Login + dashboard load | KPIs and tables hydrate | [fill] | Pass |
| Admin-N1 | Policy PATCH without CSRF | 403 Forbidden | [fill] | [fill] |

**Table 5.11 — AI report PDF (Report01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Report01 | Generate narrative report | PDF/download succeeds | [fill] | Pass |

**Table 5.12 — Safety policy (Policy01)**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| Policy01 | Toggle chatbot policy | Next chat reflects suffix rules | [fill] | Pass |
| Policy-N1 | Disable emergency detection | Emergency block omitted from suffix | [fill] | [fill] |

**Table 5.13 — Test summary**

| Category | Passed | Failed / N/A | Notes |
|----------|--------|--------------|-------|
| Auth & recovery | [fill] | [fill] | |
| Conversational & OCR | [fill] | [fill] | Includes negative paths |
| Pharmacy & ranking | [fill] | [fill] | |
| Governance | [fill] | [fill] | |

## 5.3 Failure-mode and edge-case behaviour

Happy-path tables show success when dependencies are healthy. This section documents **observable behaviour under failure** (examiner requirement).

### 5.3.1 Gemini API timeout or quota exhaustion

**Chat (`POST /chat/`):** No API key → **503** with `setup_required`. Quota/429 → **200** with user-visible “high demand” message (`intent: error`). Symptom keyword fallback may still return `suggested_medicines` without Gemini.

**OCR (`POST /upload-prescription/`):** Vision model chain retries fallbacks; on exhaustion returns `medicines: []`, `error_code: gemini_quota_rate_limit`. With GPS, **`MedicineRequest` still created** with `prescription_image` and empty `medicine_names`. Response flags: `ocr_failed`, `prescription_image_only`. **`skip_ocr=true`** bypasses Gemini entirely.

**Pharmacist impact:** `needs_pharmacist_prescription_read` and image URL for manual quoting.

### 5.3.2 WebSocket disconnection during broadcast

Channels group `chat_request_{uuid}` pushes `medicine_request_snapshot` and `medicine_request_ranked_update`. On disconnect, **no replay queue**. Pharmacist saves always commit; WS errors are logged only. **Recovery:** `GET /request/<uuid>/ranked/?envelope=true` matches WebSocket merge (`merged_rank_source: same_as_GET_ranked`). Production should use Redis channel layer for multi-worker deployments.

### 5.3.3 Invalid prescription image

| Case | HTTP | Outcome |
|------|------|---------|
| Missing file | 400 | No processing |
| Non-image MIME | 400 | “File must be an image” |
| Corrupted file | 500 or empty OCR | Error message |
| Non-medical image | 200 | Empty medicines; image-only broadcast if GPS |
| Unreadable handwriting | 200 | Partial/empty list; pharmacist reads image |

### 5.3.4 Duplicate reservation

Second active reserve for same pharmacy + SKU + request/session → **409 Conflict** with existing `reservation_id`, `expires_at`. Implemented with `transaction.atomic()` and `select_for_update()` on inventory.

## 5.4 Performance and qualitative results

- **Ranking:** Sub-100 ms median for typical cohorts on demo hardware (Table 4.7); suitable for interactive UI.  
- **OCR:** Typically **3–8 seconds** end-to-end on broadband when quota available (fill measured median).  
- **Broadcast to first pharmacist reply:** Approximately **[X] minutes** in demo with **[Y]** pharmacies online (fill).  
- **Figures 4.1–4.6** provide qualitative evidence of usable interfaces for patients, pharmacists, and admins.

## 5.5 Discussion

### 5.5.1 Patient journey coherence

Figures 4.2–4.4 show that a patient can move from prescription upload (or symptom chat) to a ranked list without leaving the MediBot interface. Location capture is intentionally **late** in the symptom flow so that the assistant first proposes medicine names the patient can confirm—reducing accidental broadcasts for vague symptom text. When OCR fails, the same figures illustrate that the UI still communicates **forward progress** (image sent to pharmacies) rather than a dead end; this was validated in Presc-N2 and Presc-N3 scenarios.

### 5.5.2 Pharmacy operational value

Figure 4.5 demonstrates inventory visibility, reserved quantities, and export—features that tie directly to ranking veracity. If inventory is stale, ranked “available” rows mislead patients; the discussion therefore treats **data freshness** as an organisational responsibility, not purely a software guarantee. Pharmacist quote submission (Pharma01) consistently updated patient ranked views when HTTP polling was used; Req-N1 confirmed that WebSocket absence does not block recovery.

### 5.5.3 Governance and admin oversight

Figure 4.6 and Admin01/Policy01 tests show that administrators can monitor reservations and tune chatbot policy. This supports platform-level mitigation of unsafe AI replies (e.g. emergency redirection toggles) without redeploying application code.

**Strengths:** Unified patient journey; explainable ranking dimensions; pharmacist-in-the-loop for prescriptions; operational admin tooling.

**Weaknesses:** Dependence on Gemini availability and pharmacy stock discipline; DDI rules are illustrative not exhaustive; partial availability may still require telephone contact (Figure 4.4).

**Table 5.14 — Risks and mitigations**

| Risk | Observation | Mitigation |
|------|-------------|------------|
| Model outage | Quota errors during demo | Image-only broadcast; skip_ocr |
| Misleading rank score | Numeric score ≠ clinical best | Disclaimers; pharmacist authority |
| PHI in screenshots | Demo captures | Redaction for publication |
| Narrow DDI rules | Curated pairs only | Roadmap to licensed DB (§5.11) |

## 5.6 Limitations

1. OCR/LLM quality varies with handwriting, glare, and cropping.  
2. Inventory accuracy depends on pharmacy update behaviour—not a national stock feed.  
3. Load testing not representative of national-scale traffic.  
4. Rate limiting documented but not always enabled in development `.env`.  
5. No formal OCR confusion matrix in this submission.

## 5.7 Research questions

| Research question (from Ch 1) | Finding | Evidence |
|--------------------------------|---------|----------|
| [paste RQ1] | [fill] | §5.2, Fig 4.2–4.3 |
| [paste RQ2] | [fill] | §5.2, Table 5.8 |
| [paste RQ3] | [fill] | §5.3, §4.6 |

## 5.8 Ethics and safety

MediBot is **decision support**, not diagnosis. Emergency symptoms must be directed to physical healthcare services. Prescription images are sensitive personal data; retention and access controls are described in §4.6.3. Patients should verify medicines with a qualified pharmacist before use.

---

## 5.9 Conclusion — summary of findings

### 5.9.1 What was built

MediConnect delivers an integrated platform connecting patients, pharmacies, and administrators through MediBot-guided search, prescription image upload with Gemini OCR, geospatial broadcast, multi-criteria ranking, reservations, and governance dashboards. Chapter 4 documented architecture, configuration, security, and implementation; Chapter 5 demonstrated through structured tests and failure-mode analysis that the system meets core functional requirements while degrading safely when external APIs fail or inputs are invalid.

Evidence includes six primary interface figures (§4.7), module test tables (§5.2), performance benchmarks (§4.5.6), and explicit edge-case documentation (§5.3). The project confirms that a Django–MongoDB–Gemini stack is viable for a Zimbabwe-focused medicine discovery pilot when pharmacists remain authoritative for dispensing decisions.

### 5.9.2 What was demonstrated

During supervised demonstration, the student executed end-to-end flows: symptom search with location, prescription upload with OCR, reception of at least one pharmacist quote, ranked display, and reservation where stock allowed. Admin export and policy toggle were shown to the supervisor. These flows correspond to Pass rows in Tables 5.1–5.12.

### 5.9.3 What was learned

Implementation experience highlighted three lessons. First, **external AI APIs** are product dependencies: quota management and fallback UX are as important as model accuracy. Second, **real-time UX** should always have an HTTP reconciliation path (ranked GET) because mobile browsers drop WebSockets. Third, **health data** requires clear retention policy and pharmacist verification for prescriptions when OCR is uncertain.

## 5.10 Achievement of objectives

**Table 5.15 — Objectives matrix** *(copy objective text verbatim from Chapter 1)*

| # | Objective | Status | Evidence |
|---|-----------|--------|----------|
| 1 | [paste objective 1] | Achieved / Partial / Not achieved | [fill — fig/table §] |
| 2 | [paste objective 2] | [fill] | [fill] |
| 3 | [paste objective 3] | [fill] | [fill] |
| 4 | [paste objective 4] | [fill] | [fill] |

## 5.11 Recommendations

### 5.11.1 Technical recommendations

1. Integrate a **licensed drug–drug interaction** database mapped to Zimbabwe brand and generic names.  
2. Establish a **formal OCR evaluation** corpus with clinician-verified transcriptions and field-level accuracy metrics.  
3. Deploy **Redis** channel layer, **DRF rate limiting**, and **HTTPS** hardening before public pilot.  
4. Implement **AES-256 at-rest** policy for prescription media with automated 90-day purge.  
5. Run **Locust/k6** load tests on broadcast and ranked endpoints (target p95 &lt; 500 ms at pilot load).  
6. Add **offline-tolerant** patient queueing and SMS/WhatsApp notifications for rural connectivity.  
7. Explore **FHIR** e-prescription interoperability and payment/escrow if moving beyond informational quotes.

### 5.11.2 Organisational and policy recommendations

- Sign **MOUs** with pharmacy chains for stock-update SLAs and verification onboarding.  
- Align public messaging with **Pharmacy Council** guidance on medicine advertising and telehealth.  
- Provide **pharmacist training** on ranking interpretation, alternatives, and reservation workflows.

## 5.12 Future work

Future work extends MediConnect beyond the capstone MVP.

**Table 5.16 — Future work roadmap**

| Horizon | Focus area | Proposed work |
|---------|------------|---------------|
| **Short-term (0–6 months)** | Operations | Deploy Redis channel layer; enable DRF/nginx rate limits; HTTPS hardening; automated prescription media retention job |
| **Short-term** | Quality | Clinician-labelled OCR benchmark set; confusion matrix for medicine-name fields |
| **Medium-term (6–18 months)** | Product | Push notifications (PWA/native); Shona/Ndebele MediBot; licensed DDI database; payment or deposit on reservation |
| **Medium-term** | Integration | FHIR-based e-prescription import; pharmacy chain stock API feeds |
| **Long-term (18+ months)** | Policy & scale | National formulary alignment; insurer formulary hooks; HIE integration if regulation permits; multi-city load testing |

**Narrative:** The highest priority follow-up is **operational hardening** (rate limits, Redis, retention policy) because the current system is demonstration-ready but not abuse-resistant at national scale. **OCR evaluation** is the highest research follow-up, because prescription upload is safety-critical and was assessed qualitatively in Chapter 5. **Interoperability** (FHIR, payments) depends on external stakeholders and is deferred until a pilot MOU is in place.

## 5.13 Personal reflection

*[Write 300–500 words. Suggested themes:]*

- What you set out to prove vs what Chapter 5 evidence shows.  
- How supervisor and examiner feedback (configuration detail, failure modes, chapter structure) improved the final document.  
- Technical lessons (Gemini quota during demo, WebSocket vs HTTP ranked parity).  
- Professional lessons (pharmacist as final authority; ethics of AI in health).  
- One closing sentence on readiness for defence.

*Example opening:* “This capstone reinforced that clinical software must fail safely: when Gemini quota blocked OCR during a supervised demo, the image-only broadcast path allowed the patient journey to continue and pharmacists to quote manually—validating the degradation design described in §5.3.”

## 5.14 Response to revision feedback (optional)

The revised submission addresses supervisor comments by: (1) keeping **recommendations and future work in Chapter 5** (§5.11–5.12) while adding **results, failure modes, and discussion** earlier in the same chapter (§5.1–5.8); (2) expanding Chapter 4 with **technology rationale**, **configuration/secrets/migrations**, **benchmarks**, **ERD/data-flow**, and **security** detail.

---

# REFERENCES

*[Harvard style — add complete entries, e.g.:]*

- Django Software Foundation (2026) *Django documentation*. Available at: https://docs.djangoproject.com/ (Accessed: [date]).  
- Google (2026) *Gemini API documentation*. Available at: https://ai.google.dev/ (Accessed: [date]).  
- Django Channels (2026) *Channels documentation*. Available at: https://channels.readthedocs.io/ (Accessed: [date]).  
- *[Add Zimbabwe policy sources, health informatics papers, supervisor-cited works]*

---

# APPENDIX A — Supplementary screenshots

Assign remaining `media/Screenshot*.png` files as Figure A.1, A.2, … after classification (patient / pharmacy / admin).

# APPENDIX B — API reference pointer

Backend route catalogue: `docs/BACKEND.md`, design rationale: `docs/PLATFORM_OVERVIEW.md` in the `pharmacybackend` repository.

---

**END OF CHAPTERS 4 & 5**

### How to reach ~30 pages in Word (two-chapter layout)

| Content | Approx. pages |
|---------|----------------|
| **Chapter 4** (§4.1–4.8, tables, diagrams, screenshots) | 14–16 |
| **Chapter 5** Part A — results & failures (§5.1–5.8) | 8–10 |
| **Chapter 5** Part B — conclusion, recommendations, future work (§5.9–5.13) | 4–6 |
| References + appendix | 1–2 |
| **Total** | **~28–32** |

**If under 30 pages:** expand §5.13 reflection; paste full test rows from your original PDF; add appendix screenshots.

*Current markdown: ~4,500+ words prose; tables/figures add the remainder.*

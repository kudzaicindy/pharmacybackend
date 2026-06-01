# MediConnect (MediBot) — Draft write-up for Chapters 4, 5 & 6

**Purpose:** Merge this file with your **frontend** chapter draft and your existing **`CHAPTER_4_MEDICONNECT_FINAL.docx`** so the final document is one coherent story (UI + backend + evaluation).  
**Guideline source:** *CAPSTONE PROJECT WRITE UP.pdf* (Chapter 4: Implementation; Chapter 5: Results & Discussion; Chapter 6: Conclusion & Recommendations).  
**Figures:** Screenshots live in repository folder `media/` (paths below are relative to the **project root**).

> **Note:** The Word file could not be read automatically in this environment. After you merge, **delete duplicated sections** if your Chapter 4 already covers the same topics (e.g. landing page, chat UI).

**Placeholders to replace**

- `[Author Name]`, `[Registration Number]`, `[Institution / Faculty]`, `[Supervisor Name]`
- `[Date]`

---

## How to use the screenshots in Word

1. Insert figures with **Figure X.Y** captions and add them to your **List of Figures**.  
2. **Crop or blur** any **“Activate Windows”** watermark before final PDF submission.  
3. Keep **patient/pharmacy identifiable data** out of the main body if your ethics board requires it (use redacted copies for appendices).  
4. Image paths — if Word breaks on spaces in filenames, **rename copies** to e.g. `fig4-1-medibot-ranking.png` and update the paths.

---

## Suggested mapping: screenshot → chapter (back end + full-stack story)

Use any additional `media/Screenshot*.png` files as extra figures following the same pattern (admin analytics, ranking config, MFA, etc.).

| Figure (suggestion) | File | Best fit | One-line caption idea |
|---------------------|------|----------|----------------------|
| 4.1 | `media/Screenshot 2026-05-24 152251.png` | Ch 4 | MediConnect landing page: search, hero metrics, MediBot preview. |
| 4.2 | `media/Screenshot 2026-05-24 235016.png` | Ch 4 | Prescription upload flow: OCR output (medicines + dosages + confidence), location capture, broadcast to pharmacies. |
| 4.3 | `media/Screenshot 2026-05-24 163250.png` | Ch 4 | Patient chat: ranked pharmacy responses (distance, travel time, score, price/qty). |
| 4.4 | `media/Screenshot 2026-05-24 201415.png` | Ch 4 | Ranked results with **unavailable** lines vs **pharmacist alternative** and reserve/directions actions. |
| 4.5 | `media/Screenshot 2026-05-24 185207.png` | Ch 4 | Pharmacy portal: inventory management, low-stock warnings, pricing, CSV export. |
| 4.6 | `media/Screenshot 2026-05-24 223014.png` | Ch 4 | Admin portal: reservations table and platform metrics (operational view). |
| — | `media/Screenshot 2026-05-25 000011.png` | Skip / Ch 1–3 only | Email client — **not** system UI; optional for “supervision correspondence” appendix only. |

**Prescription JPEGs under `media/prescriptions/`** — cite only if your ethics/procurement policy allows real prescription imagery; otherwise use blurred samples in an appendix marked *“Redacted”*.

---

# Chapter 4 — System Implementation

*Per capstone guideline: development process; algorithms/API integration; screenshots / interfaces.*

## 4.1 Overview of implementation

MediConnect was implemented as a **full-stack AI-assisted medicine discovery platform** centred on:

- A **patient-facing** web experience (**MediBot — AI Health Assistant**) for medicine search, symptom-style dialogue, prescription image upload, and **location-aware** pharmacy matching.
- A **pharmacy portal** for live request handling, **inventory**, quotes, and operational status (**accepting requests**).
- A **platform admin** area for governance, verification, reservations, and high-level monitoring.

The **backend** (this repository) provides **REST APIs**, **optional WebSockets** for live ranked updates, **prescription OCR** (vision model pipeline), **ranking logic** for pharmacy responses, persistence (e.g. **MongoDB** in deployment), authentication patterns (e.g. **JWT** for pharmacists where enabled), and email/MFA hooks as configured.

*If your methodology chapter (Chapter 3) already names the architecture, repeat only a short paragraph here and reference that chapter.*

## 4.2 Development process

Describe your **actual process** (e.g. iterative prototyping, supervisor reviews, milestones). Typical structure:

1. Requirements from problem statement → user stories for **Patient / Pharmacy / Admin**.  
2. API-first contracts between frontend and backend (endpoints for `chat`, `upload-prescription`, `request/.../ranked/`, pharmacist inventory, admin reports).  
3. Incremental demos: landing → chat broadcast → pharmacist quote → reservation/pickup states.  
4. Hardening: CORS/auth, validation, audit/safety workflows, backups.

Adapt bullets to match **your** Gannt / progress assessment.

## 4.3 Technology stack (implementation level)

Summarise tools you **actually used** (adjust if yours differ):

| Layer | Technology |
|-------|-------------|
| Backend framework | Django, Django REST Framework |
| Real-time | Django Channels (WebSockets; ASGI via Daphne) |
| Persistence | MongoDB (default deployment path documented in `BACKEND.md`) or SQL fallback |
| AI / OCR | Gemini (and/or OpenRouter) for prescription parsing; rules-based fallbacks |
| Ranking | Multi-criteria style scoring (availability, distance/time, price, rating) with configurable weights |
| Frontend | *(Point to your frontend repo / React or other stack in the combined document)* |

## 4.4 Core backend features implemented

Brief, factual list (cross-check with API docs):

- **Conversational medicine search** (`/chat/`) — intent routing, suggested medicines, location capture, broadcasting **MedicineRequest** to nearby pharmacies.  
- **Prescription pipeline** (`/upload-prescription/`) — image validation, OCR JSON → structured medicines/dosages, optional **broadcast without OCR** paths for pharmacist-only review when vision/quota fails.  
- **Ranked results** (`GET .../ranked/`) — merged pharmacist vs live inventory style rows where applicable; **envelope** mode for SPA polling.  
- **Drug-interaction hints** — embedded rule-based checks exposed on chat, ranked metadata, upload payload, and `check-interactions/` (expandable to licensed DDI DB).  
- **Pharmacy side** — inventory sync, responses, ranking score visibility.  
- **Admin** — verification queue, reservations export (PDF/CSV as shown in UI).  
- **Operational transparency** — dashboard metrics (uptime, average response, user counts) as visible in admin UI.

## 4.5 Algorithms and technical methods (high level)

Write this in **your own words**; avoid copying vendor docs.

1. **Geospatial filtering** — patient coordinates vs pharmacy locations; Haversine (or equivalent) distance; travel-time estimates where implemented.  
2. **Ranking (MCDA-style)** — normalise competing criteria within the response set for a given request; apply **urban/rural weight profiles** where the platform distinguishes density. Chronological “grace” window before switching to weighted rank (as implemented).  
3. **OCR / structured extraction** — image → JSON items (names, strengths, dosing text) → validated list stored on conversation/request for pharmacists and chat context.  
4. **DDI checking (rule-based)** — pairwise checks against curated interaction entries; disclaimers surfaced to patient JSON; intended for clinician confirmation.

## 4.6 Security, governance, and data handling

- Role separation: **patient (often anonymous/session)**, **pharmacist (JWT)**, **admin (session)**.  
- Ownership checks on sensitive resources (e.g. ranked request access by `conversation_id` / `session_id`).  
- Prescription images: access-controlled URLs for pharmacists; audit considerations for **PII/PHI**.  
- **AI safety** — admin review hooks / policy (if you enabled them, describe briefly).

## 4.7 User interface implementation (evidence from `media/`)

### Figure 4.1 — Public landing and MediBot entry point

![MediConnect landing page](media/Screenshot%202026-05-24%20152251.png)

**Caption (example):** Figure 4.1: MediConnect public landing page highlighting Zimbabwe-focused positioning, medicine search, and embedded MediBot preview with near-CBD stock example.

### Figure 4.2 — Prescription upload, OCR, and pharmacy broadcast

![Prescription OCR and location flow](media/Screenshot%202026-05-24%20235016.png)

**Caption (example):** Figure 4.2: Patient uploads a prescription image; MediBot returns extracted medicines and dosages with model confidence; user supplies location; system confirms broadcast to pharmacies pending responses.

### Figure 4.3 — Ranked pharmacy responses (multi-pharmacy success)

![Ranked paracetamol results](media/Screenshot%202026-05-24%20163250.png)

**Caption (example):** Figure 4.3: MediBot presents multiple responding pharmacies with distance, travel and preparation times, internal rank score, and line-item price/quantity.

### Figure 4.4 — Alternatives and partial availability

![Alternatives and reservation warning](media/Screenshot%202026-05-24%20201415.png)

**Caption (example):** Figure 4.4: Ranked view when exact molecules are unavailable but a **pharmacist-suggested alternative** is in stock; UI directs user to call for reservation where online reserve is disallowed.

### Figure 4.5 — Pharmacy inventory module

![Pharmacy inventory management](media/Screenshot%202026-05-24%20185207.png)

**Caption (example):** Figure 4.5: Pharmacy portal inventory table with stock bars, low-stock alert, reserved quantity on paracetamol, and CSV export — data feeding patient-side availability and ranking.

### Figure 4.6 — Admin reservations and platform metrics

![Admin reservations dashboard](media/Screenshot%202026-05-24%20223014.png)

**Caption (example):** Figure 4.6: Administrator view of reservation lifecycle (pending, confirmed, picked up, expired) with export options and high-level platform statistics.

---

# Chapter 5 — Results & Discussion

*Per capstone guideline: performance results; evaluation metrics; analysis. Use **honest** framing: student projects often mix quantitative metrics with qualitative UX evaluation.*

## 5.1 Evaluation approach

State what you **did**:

- **Functional testing** — trace user stories (search, upload Rx, location on/off, pharmacist reply, admin export).  
- **Integration testing** — frontend ↔ API contracts; WebSocket vs HTTP ranked parity (if applicable).  
- **Usability / heuristic review** — clarity of warnings, medical disclaimers, error messages (OCR failure, quota, no pharmacies in range).  
- **Pilot / demonstration** — e.g. Harare test pharmacies, demo accounts (no fabricated production traffic).

If you did **not** run formal ML accuracy studies, say so clearly and justify **qualitative** evidence (screenshots, timed demos, supervisor sign-off).

## 5.2 Results — functional outcomes

Use subsections tied to **observable** behaviour (supported by figures):

1. **End-to-end patient flow** — Figures 4.2–4.4 show OCR success, location-gated broadcast, ranked responses, and alternative handling.  
2. **Pharmacy operations** — Figure 4.5 shows inventory-driven visibility and stock risk signalling.  
3. **Governance** — Figure 4.6 shows reservation states and exports for reporting.

**Optional table (fill with your real trial numbers):**

| Test ID | Scenario | Expected | Observed | Pass/Fail |
|---------|----------|----------|----------|-----------|
| T1 | Multi-medicine search + rank | ≥1 pharmacy row when stock exists | … | … |
| T2 | Prescription OCR + location | Medicines extracted; broadcast after coords | … | … |
| T3 | No stock / alternative | UI explains alternative + contact | … | … |
| T4 | Admin reservation export | CSV/PDF generates | … | … |

## 5.3 Results — performance and metrics

**Do not invent precision/recall** for OCR unless you measured them on a labelled set.

**Acceptable wording examples:**

- “Prescription parsing achieved **visually correct** extraction on [N] sample images in the pilot set; failures occurred when handwriting was illegible or image glare was present.”  
- “Median time from **broadcast** to **first pharmacist response** in demo runs was approximately [X] minutes (lab conditions, [Y] pharmacies online).”  
- “Admin dashboard displayed aggregate **uptime** and **average response** indicators during the evaluation window (Figure 4.6); these reflect **demo environment** telemetry, not a contractual SLA.”

Replace bracketed values with **your** measurements.

## 5.4 Discussion — interpretation

Interpret results against **aims and objectives** (from Chapter 1):

- **Strengths:** unified patient journey; explainable ranking fields; operational tooling for pharmacies and admins.  
- **Limitations:** OCR and LLM behaviour depend on model availability/cost; **DDI** layer is rules-based until a licensed database is integrated; connectivity and stock data quality affect trust; partial availability UX still requires **phone** for some alternative reservations (Figure 4.4).  
- **Risks:** over-reliance on automation for clinical decisions — mitigated by disclaimers and pharmacist in the loop.

## 5.5 Ethics, safety, and disclaimer

Reiterate: MediBot is **decision support**, not diagnosis; emergencies must use real healthcare services; prescription data is sensitive — cite your **consent** and **data retention** approach if applicable.

---

# Chapter 6 — Conclusion & Recommendations

*Per capstone guideline: summary; objectives achieved; future work.*

## 6.1 Summary of findings

In two to three paragraphs, answer:

- **What was built?** (MediConnect: patient AI assistant, pharmacy portal, admin governance, backend services.)  
- **What evidence shows it works?** (Figures 4.x; pilot tests in §5.2.)  
- **What was learned?** (integration complexity, real-world pharmacy workflows, AI limits.)

## 6.2 Achievement of objectives

Create a **mapping table** to your Chapter 1 objectives (copy objective text verbatim):

| Objective (from Ch 1) | Addressed? | Evidence (chapter/figure/test) |
|----------------------|------------|--------------------------------|
| … | Yes / Partial / No | e.g. §4.7 Fig 4.2; Test T2 |

Be explicit about **partial** achievements (e.g. “automated reservation for all alternatives — **not** completed”).

## 6.3 Recommendations — technical

1. Integrate a **licensed DDI** source (e.g. DrugBank or national formulary API) and map local brand names.  
2. **Offline / low-bandwidth** resilience and SMS/WhatsApp notification channel for rural users.  
3. **Payment** integration and **e-prescription** standards (FHIR) for interoperability.  
4. **Formal OCR evaluation** on a representative Zimbabwean prescription corpus; publish confusion matrix / field-level accuracy.  
5. **Load and security testing** (OWASP ASVS, rate limits, abuse monitoring).  
6. **Mobile native** or PWA hardening for push notifications on pharmacy replies.

## 6.4 Recommendations — organisational / policy

- Formal **agreements** with pharmacy chains; standardised stock update SLAs.  
- **Regulatory** alignment with national pharmacy council guidance on online medicine advertising and telehealth.  
- **Training** pack for pharmacists on ranking interpretation and alternative substitution rules.

## 6.5 Final reflection

One short paragraph on personal learning, teamwork, and professional growth (capstone reflection style).

---

## References (Harvard) — add in your master document

Use **one** consistent Harvard style. Examples of *types* of sources you may cite (replace with real entries):

- Django Software Foundation — Django Documentation.  
- Google — Gemini API documentation.  
- Zimbabwe Ministry of Health & Child Care — relevant policy PDFs (if used).  
- Academic papers on **recommender systems**, **clinical decision support**, **geospatial health access** (cite properly).

---

## Appendix A — Supplementary screenshots

List remaining files under `media/Screenshot *.png` and assign **Figure A.x** numbers after you classify each screenshot (patient vs pharmacy vs admin). *Exclude personal email screenshots unless required for audit trail.*

Suggested command to list files: `dir media\\Screenshot*.png` (Windows) or list folder in explorer.

---

## Appendix B — API / module pointers (backend)

Point readers to **`docs/BACKEND.md`** and **`docs/PLATFORM_OVERVIEW.md`** in this repo for endpoint lists and behavioural notes — useful when examiners ask for traceability between **UI** and **API**.

---

### Word-combination checklist

- [ ] Merge with frontend chapter (avoid duplicate Introduction).  
- [ ] Harmonise figure numbering across chapters (continuous or per chapter).  
- [ ] Add **Table of Contents** entries for 4–6 in Word’s TOC.  
- [ ] Complete **References** Harvard list.  
- [ ] Remove watermarks / redact PHI.  
- [ ] Run plagiarism / grammar check per supervisor advice.  
- [ ] Deadline reminder from guideline: aim to finish documentation before **4 June 2026** for plagiarism buffer (per PDF).

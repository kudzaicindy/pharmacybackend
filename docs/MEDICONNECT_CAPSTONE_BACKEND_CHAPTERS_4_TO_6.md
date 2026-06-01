# MediConnect / MediBot — Backend-Focused Chapters 4, 5 & 6  
## (Markdown master for merging with Frontend write-up)

**Purpose:** Detailed draft for **Chapter 4 (Implementation)**, **Chapter 5 (Results & Discussion)** and **Chapter 6 (Conclusion & Recommendations)** per [*CAPSTONE PROJECT WRITE UP.pdf*](../CAPSTONE%20PROJECT%20WRITE%20UP.pdf).  
**Companion sources:** [*CHAPTER_4_MEDICONNECT_FINAL.pdf*](../CHAPTER_4_MEDICONNECT_FINAL.pdf) (reuse tables and component narrative, but **re-split** headings to match university structure — see §0 below), **`docs/BACKEND.md`**, **`docs/PLATFORM_OVERVIEW.md`**.

**Screenshots repository folder:** [`media/`](../media/) (paths in this document are relative to **repository root**).

---

### Fill in before submission

| Field | Value |
|--------|--------|
| Author | *[Your full name]* |
| Registration No. | *[e.g. H230123]* |
| Institution / Faculty | University of Zimbabwe, *[Department]* |
| Supervisor(s) | *[Name(s)]* |
| Submission date | *[Date]* |

---

## 0. How this document aligns with your existing Chapter 4 PDF

Your [*CHAPTER_4_MEDICONNECT_FINAL.pdf*](../CHAPTER_4_MEDICONNECT_FINAL.pdf) is internally titled **“Chapter 4: Results and Discussion”** but additionally contains:

- Hardware and software specifications  
- Testing strategy and **module test-case tables** (Login → Policy)  
- Implementation of design (five components)  
- Architecture and integration  
- Screenshot placeholders (Figures 1–22+)  

The **official capstone guideline** maps these differently:

| Content in `CHAPTER_4_MEDICONNECT_FINAL.pdf` | Recommended chapter in **final combined** Word doc |
|----------------------------------------------|-----------------------------------------------------|
| Hardware / software specs, stacks, algorithms, coding & integration, interfaces / screenshots narrative | **Chapter 4 — System Implementation** |
| Testing objectives, test-case tables, pass/fail, analysis of outcomes, limitations | **Chapter 5 — Results & Discussion** |
| Achievement of objectives, recommendations, reflection | **Chapter 6 — Conclusion & Recommendations** |

**When you merge:** avoid duplicating the same table in two chapters — keep **technical specs once** (Ch 4) and **test evidence + interpretation once** (Ch 5).

---

## 1. Merging this file with the frontend Markdown

1. Open your **frontend** chapters in one editor pane and this file in another.  
2. **Figure numbering:** choose either **chapter-based** (4.1, 4.2… then 5.1…) or **global** (Figure 1… N). Renumber captions after paste.  
3. **Dedupe:** if the frontend chapter already describes the same UI screen, **keep the stronger caption** and cross-reference (“see Frontend §4.x”).  
4. **Tables:** Paste hardware/software/version tables once (usually Ch 4).  
5. **Word quirks:** filenames with spaces may break OLE links — duplicate screenshots as `media/fig-4-01-landing.png` etc.  
6. **Compliance:** crop **“Activate Windows”** watermarks; redact PHI on prescriptions if required by ethics.

---

## 2. Image inventory — suggested mapping to MediConnect journeys

Screenshots cannot be reliably auto-classified without viewing them; **adjust captions** after you open each PNG. Names below encode capture time (`2026-05-24` …).

### 2.1 Primary figures (recommended for Chapter 4 body)

| Ref | File | Suggested theme (edit after viewing) |
|-----|------|--------------------------------------|
| **Figure 4.1** | `media/Screenshot 2026-05-24 152251.png` | Public landing: search, MediBot teaser, Zimbabwe context. |
| **Figure 4.2** | `media/Screenshot 2026-05-24 235016.png` | Prescription path: OCR output, confidence/dosages, location + broadcast wording. |
| **Figure 4.3** | `media/Screenshot 2026-05-24 163250.png` | Patient chat — ranked pharmacies (distance, time, score, price line items). |
| **Figure 4.4** | `media/Screenshot 2026-05-24 201415.png` | Partial availability / **pharmacist alternative** + reservation / directions affordances. |
| **Figure 4.5** | `media/Screenshot 2026-05-24 185207.png` | Pharmacy portal — inventory quantities, alerts, CSV export toolbar. |
| **Figure 4.6** | `media/Screenshot 2026-05-24 223014.png` | Admin surface — reservations table + KPI strip / exports. |

**Markdown embeds** (URLs encode spaces as `%20` — works in many Markdown renderers):

```markdown
![Figure 4.1 MediConnect landing](media/Screenshot%202026-05-24%20152251.png)
![Figure 4.2 Prescription OCR flow](media/Screenshot%202026-05-24%20235016.png)
![Figure 4.3 Ranked pharmacy replies](media/Screenshot%202026-05-24%20163250.png)
![Figure 4.4 Alternatives / reservation UX](media/Screenshot%202026-05-24%20201415.png)
![Figure 4.5 Pharmacy inventory](media/Screenshot%202026-05-24%20185207.png)
![Figure 4.6 Admin reservations & metrics](media/Screenshot%202026-05-24%20223014.png)
```

### 2.2 Additional screenshots — assign to Chapter 4, Appendix A, or Frontend-only

Append these filenames to your **List of Figures** once classified (patient vs pharmacy vs admin vs marketing):

| File |
|------|
| `media/Screenshot 2026-05-24 153016.png` |
| `media/Screenshot 2026-05-24 153032.png` |
| `media/Screenshot 2026-05-24 171503.png` |
| `media/Screenshot 2026-05-24 171528.png` |
| `media/Screenshot 2026-05-24 171732.png` |
| `media/Screenshot 2026-05-24 171759.png` |
| `media/Screenshot 2026-05-24 172524.png` |
| `media/Screenshot 2026-05-24 172911.png` |
| `media/Screenshot 2026-05-24 202057.png` |
| `media/Screenshot 2026-05-24 212830.png` |
| `media/Screenshot 2026-05-24 212846.png` |
| `media/Screenshot 2026-05-24 212908.png` |
| `media/Screenshot 2026-05-24 213731.png` |
| `media/Screenshot 2026-05-24 213814.png` |
| `media/Screenshot 2026-05-24 214221.png` |
| `media/Screenshot 2026-05-24 222732.png` |
| `media/Screenshot 2026-05-24 222753.png` |
| `media/Screenshot 2026-05-24 222818.png` |
| `media/Screenshot 2026-05-24 222847.png` |
| `media/Screenshot 2026-05-24 222915.png` |
| `media/Screenshot 2026-05-24 223003.png` |
| `media/Screenshot 2026-05-24 223030.png` |
| `media/Screenshot 2026-05-24 223330.png` |
| `media/Screenshot 2026-05-24 234302.png` |

**Excluded from figures unless examiner requests:**  
`media/Screenshot 2026-05-25 000011.png` — plain email inbox (system UI unrelated).

**Sensitive:** prescription JPEG samples under `media/prescriptions/2026/05/` — use only if ethics-approved; otherwise blur/redact.

---

---

# Chapter 4 — System Implementation

*Official guideline expectation: development process • algorithms/API integration • system interfaces.*

## 4.1 Introduction

MediConnect is implemented as an **integrated patient–pharmacist–administrator** ecosystem. The patient journey is anchored on **MediBot**, an AI-assisted conversational layer that gathers symptoms or uploaded prescriptions, obtains **GPS or geocoded address** locations, broadcasts **medicine requests** to verified pharmacies, and presents **ranked** quotations. The [**pharmacybackend**](https://github.com/) Django project exposes REST endpoints under **`/api/chatbot/`**, supports **optional WebSockets** (`ws/chatbot/<request_id>/`) for live snapshots, persists requests and rankings, integrates **Gemini** (chat + prescription vision), and provides governance hooks (verification, audits, analytics).

This chapter summarises **how** the backend and full-stack artefacts were engineered; evidence screenshots appear in §4.8.

## 4.2 System development lifecycle

Adapt the wording to match your **actual** Gannt chart / progress submissions:

1. **Requirements shaping** — user stories derived from Zimbabwe’s fragmented medicine discovery problem (opaque stock, geographic friction, duplication of journeys). Roles: **guest/patient**, **pharmacist**, **platform admin**.  
2. **API-first collaboration** — request/response contracts for `chat/`, `upload-prescription/`, `request/<uuid>/ranked/`, `reserve/`, `pharmacist/*`, `admin/*` stabilised early so React (`pharmacyfrontend`) could iterate in parallel.  
3. **Vertical slices** — land **chat broadcast** → **pharmacist quote** → **patient ranked view** → **reservation lifecycle** → **admin metrics** in that order where possible (reduces integration risk).  
4. **Hardening** — CORS & CSRF, ownership checks (`conversation_id` / `session_id` on ranked pulls), MFA paths, logging, caching on ranked payloads, failover messaging when Gemini quota/OCR fails (pharmacist image review branch).  

## 4.3 Target deployment environment — hardware summary

Adapt values to **your deployed server** / laptop spec if different from the baseline in your Chapter 4 PDF.

| Aspect | Recommendation | Purpose |
|--------|------------------|---------|
| CPU | Modern quad-core or better (PDF: Intel i5 / Ryzen 5 class) | Concurrent HTTP + Channels workers + OCR |
| RAM | ≥ 8 GB (16 GB preferred for demos with DB + Channels) | Daphne / Django / DB / cache |
| Storage | SSD, ≥ 50 GB | Media uploads (prescriptions), logs, backups |
| Uplink | Stable broadband (~10 Mbps+ for Gemini + WS) | API & vision latency |

**Client devices:** modern mobile browsers (**Chrome 89+**, **Safari 14+** per your PDF baseline); MediConnect shipped as responsive web + optional **PWA** install.

## 4.4 Software stack (as implemented)

The following aligns with [*CHAPTER_4_MEDICONNECT_FINAL.pdf*](../CHAPTER_4_MEDICONNECT_FINAL.pdf) Tables 5–6 and repository documentation.

### 4.4.1 Backend (`pharmacybackend`)

| Software | Role |
|----------|------|
| Python 3.10+ | Application language |
| Django 6.x | MVC, admin, middleware, ORM abstraction |
| Django REST Framework | Serialisers, `\`/api/chatbot/\` routing |
| **Django Channels + Daphne** | ASGI; WebSockets for pharmacy/patient snapshots |
| **MongoDB path** (`django-mongodb-backend`) | Canonical production persistence documented in **`BACKEND.md`** (SQLite/Postgres optional fallbacks in dev or migration contexts) |
| **google-generativeai** | Chat + Gemini Vision OCR |
| Pillow / OpenCV / pytesseract | Image preprocessing hygiene before vision OCR |
| **pyotp**, email stack | MFA & transactional mail flows |
| **django-cors-headers** | SPA origin policy |

### 4.4.2 Frontend (`pharmacyfrontend`) — summarise in combined doc only

Cross-reference React 19 / Vite 7 / react-router-dom 7 / PWA plugin / jsPDF + marked narratives as documented in your PDF — frontend chapter should expand UI component names (`LandingPage.jsx`, `Chatbot.jsx`, dashboards).

### 4.4.3 External services

- **Gemini API** — conversational intents; structured prescription extraction; admin narrative reports (**note:** quota/billing outages trigger graceful degradation — skip-OCR re-upload routes to pharmacist optical read).  

## 4.5 Core backend behaviours (implementation narratives)

Reuse and tighten the **five components** from [*CHAPTER_4_MEDICONNECT_FINAL.pdf*](../CHAPTER_4_MEDICONNECT_FINAL.pdf) §4.5; correlate each with API routes.

### 4.5.1 Component A — conversational medicine discovery

- Frontend posts to **`POST /api/chatbot/chat/`** with optional `medicines`, `location_*`, prescription flags (`prescription_image_only`, `ocr_failed`) when the SPA must steer partial OCR flows.  
- Backend persists **`ChatConversation`** / **`ChatMessage`**; merges client echo lists where the LLM proposes a narrower subset after interaction checks; locks **`prescription_medicines`** in metadata for subsequent turns (see codebase comments around prescription repair helpers).  

### 4.5.2 Component B — prescription upload & OCR

- **`POST /api/chatbot/upload-prescription/`** validates MIME type, invokes **`OCRService`** (Gemini Vision path with Pillow/OpenCV chain).  
- When vision succeeds — structured JSON → **medicines, items, dosages, confidence_percent** injected into conversation metadata **and**, when coordinates accompany the upload, a **`MedicineRequest`** broadcast with **`prescription_image`** attached.  
- When vision fails (**quota**, unreadable handwriting) — `skip_ocr=true` / `pharmacist_review_only=true` allows **image-forwarding** without model calls; patient UI explains manual pharmacist read (FR-style behaviour).  

### 4.5.3 Component C — request broadcast & MCDA-style ranking

- **`create_medicine_request`** persists **`MedicineRequest`**, derives timeout from **urban/rural density heuristics**, sends email/nearby-pharmacy notifications as configured.  
- Pharmacists POST **`PharmacyResponse`** rows — availability, quoted price, substitution notes; patient polls **`GET /api/chatbot/request/<uuid>/ranked/`** (**`envelope=true`** recommended for SPA: merged live-inventory synthetic rows vs pharmacist replies, **`meta.drug_interactions`** embedding).  
- **Ranking gate:** chronological presentation for **`RANKING_DELAY_MINUTES`**, switching to weighted **RankingEngine / MCDA** normalisation intra-request (price, distance, rating, reliability) — aligns with Tables 13–14 in your PDF narrative.  

### 4.5.4 Component D — reservations & pickups

- **`POST /api/chatbot/reserve/`** freezes **`price_at_reservation`**; lifecycle states — pending → confirmed / expired / picked up; **`record-purchase`** path decrements inventory as described in PDF Table 15.  

### 4.5.5 Component E — admin analytics, audits, governance

- Summarise **`admin_analytics`**, dashboards, **`PlatformAdminSettings`**, **`ChatbotSafetyReview`**, **`AdminAuditLog`** mirroring §4.5 Component 5 in your PDF plus **`PLATFORM_OVERVIEW.md`**.  

## 4.6 Algorithms & technical integration (concise examiner-facing)

1. **Geospatial eligibility** — haversine (or documented equivalent) filtering around patient coordinates vs pharmacy registrations; suburb / acceptance flags from **`PharmacySettings`**.  
2. **MCDA-style ranking** — min–max criterion scaling inside each request’s cohort; urban vs rural weights from **`PlatformAdminSettings`**.  
3. **Drug–drug hints** — pairwise scan against **`DrugInteractionService` KNOWN_INTERACTIONS**; surfaced on **`chat`**, **`ranked/?envelope=true`**, **`upload-prescription`**, `check-interactions`; **explicit disclaimer** emphasises non-exhaustive rules — upgrade path to DrugBank/regulatory feed as future work (**Chapter 6**).  
4. **WebSocket coherence** — `medicine_request_snapshot` + `medicine_request_ranked_update` carry shapes consistent with **`GET ranked/`** (“same merged list semantics”) to minimise patient UI drift between HTTP polls and sockets.  

## 4.7 Security & governance mechanisms

Summarise (expand with citations to OWASP coursework if taught):

| Mechanism | Where |
|-----------|-------|
| Request ownership guards | **`GET ranked/`** validates `conversation_id` or `session_id` linkage |
| Role separation | Patient session/JWT pathways vs pharmacist Bearer JWT vs Django session admins |
| Prescription imagery | Controlled pharmacist **`prescription-image`** URLs; PHI minimisation discourse |
| CSRF bootstrap | `\`/admin/csrf/\`` before mutating admin writes |
| Auditing | **`AdminAuditLog`** entries on policy tweaks |
| MFA | pyotp integrations for pharmacist/patient MFA modules per PDF |

## 4.8 System interfaces — figures

### Figure 4.1 — MediConnect public landing entry point  

![Figure 4.1](media/Screenshot%202026-05-24%20152251.png)

**Caption (draft):** *Figure 4.1: MediConnect public landing page illustrating medicine search affordances and embedded MediBot marketing panel — entry into the conversational patient journey.*

### Figure 4.2 — Prescription OCR, dosing narrative, broadcast confirmation  

![Figure 4.2](media/Screenshot%202026-05-24%20235016.png)

**Caption (draft):** *Figure 4.2: Prescription image processed through Gemini Vision — structured medicines and dosage lines returned to the patient alongside location-aware broadcast messaging.*

### Figure 4.3 — Ranked pharmacies with temporal & economic signals  

![Figure 4.3](media/Screenshot%202026-05-24%20163250.png)

**Caption (draft):** *Figure 4.3: Ranked responder list exhibiting distance, travel + preparation heuristic, blended ranking score and line-item quotations enabling patient comparison shopping.*

### Figure 4.4 — Alternative suggestions & escalation to voice reservation  

![Figure 4.4](media/Screenshot%202026-05-24%20201415.png)

**Caption (draft):** *Figure 4.4: Scenario where precise molecules are scarce — pharmacist-listed alternative SKU with explicit call-to-action when automated reservation is suppressed.*

### Figure 4.5 — Pharmacist-facing inventory stewardship  

![Figure 4.5](media/Screenshot%202026-05-24%20185207.png)

**Caption (draft):** *Figure 4.5: Pharmacy dashboard inventory grid with replenishment signalling and CSV tooling — grounding truth for downstream availability surfaced to patients.*

### Figure 4.6 — Operational supervision & reservations ledger  

![Figure 4.6](media/Screenshot%202026-05-24%20223014.png)

**Caption (draft):** *Figure 4.6: Administrator vantage showing reservation statuses and aggregated activity metrics supporting governance reporting.*

*(Add Figures 4.7+ from §2.2 once you annotate each PNG.)*

## 4.9 Traceability bridge (for examiners)

| UI capability | Typical backend touchpoints |
|---------------|----------------------------|
| MediBot replies | `\`/api/chatbot/chat/\``, conversation serializers |
| Prescription ingestion | `\`/upload-prescription/\``, `OCRService` |
| Ranking cards | `\`/request/<uuid>/ranked/\``, `build_merged_ranked_rows_for_request` |
| Reserve button | `\`/reserve/\`` |
| Inventory grid | Pharmacist `\`/inventory/\`` APIs |
| KPI tiles | `\`/admin/dashboard/\`` bundle & analytics aggregates |

Detailed endpoint catalogue: **`docs/BACKEND.md`**.

---

# Chapter 5 — Results & Discussion

*Official guideline expectation: performance results • evaluation metrics • analysis • graphs.*

## 5.1 Evaluation strategy

Reuse **§4.3 Testing Strategy / Objectives** from [*CHAPTER_4_MEDICONNECT_FINAL.pdf*](../CHAPTER_4_MEDICONNECT_FINAL.pdf) but **relocating** verbatim tables here as Results evidence.

Combine:

1. **Manual integration smoke tests** (Postman collections if any + browser journeys).  
2. **Structured module tests** Tables 7–18 (rename as **Table 5.1 … 5.12** sequentially in Word).  
3. **Observation-based UX appraisal** — clarity of disclaimers when OCR misses items; behavioural response to Gemini quota outages.  

**Integrity statement:** Formal ML metrics (precision/recall/F1 on OCR fields) **were NOT** computed on an independent adjudicated corpus — cite **visual inspection** successes/failures on *N* held-out scripts if you quantify them honestly.

Suggested **extra exhibits** not yet plotted:

| Exhibit | Builds from |
|---------|-------------|
| Bar chart — module pass counts | Aggregation of Tables 7–18 pass flags |
| Timeline — demo-day broadcast → median first response | Pharmacist dashboards logs or manual timestamps |
| Heatmap excerpt | Duplicate admin map screenshot |

## 5.2 Quantitative module outcomes (migrate tables)

Paste from PDF (**do not omit Test IDs**):

- **Table 5.1** Login (Login01)  
- **Table 5.2** Registration (Register01)  
- **Table 5.3** Forgot password / OTP flow (ForgotPwd01)  
- **Table 5.4** AI chat symptom search (Chat01)  
- **Table 5.5** Prescription upload OCR (Presc01) — note hybrid reality: passes on clean prints; degraded on handwriting blur (discussion).  
- **Table 5.6** Broadcast & WebSocket signalling (Request01)  
- **Table 5.7** Pharmacist response + inventory (Pharma01)  
- **Table 5.8** Ranking (Rank01)  
- **Table 5.9** Reservation lifecycle integrity (Res01)  
- **Table 5.10** Admin dashboard hydration (Admin01)  
- **Table 5.11** Narrative AI report PDF (Report01)  
- **Table 5.12** Dynamic safety policy propagation (Policy01)  

Aggregate row (author-completed):

| Category | Passed | Failed / Not exec. | Remarks |
|---------|-------|---------------------|---------|
| Auth & recovery | *[n]* | *[n]* | |
| Conversational flows | *[n]* | *[n]* | |
| Inventory ↔ ranking interplay | *[n]* | *[n]* | |
| Governance/reporting | *[n]* | *[n]* | |

## 5.3 Qualitative performance themes

Discuss each with **referenced figures**:

1. **OCR fidelity vs operational failure modes** — link **Figure 4.2**, fallback paths.  
2. **Ranking explainability vs cognitive load** — interpret fields shown in **Figures 4.3–4.4**.  
3. **Supply transparency** — **Figure 4.5** alleviates asymmetric information but depends on conscientious CSV updates — data-quality risk.  

## 5.4 Risk, ethics & mitigation discussion

| Risk | Observation | Mitigation discussed / residual |
|------|-------------|----------------------------------|
| Model dependency / vendor outage | Symptoms of quota exhaustion surfaced with pharmacist manual read | Skip-OCR re-upload UX + ops billing |
| False assurance from ranked scores | Numeric rank ≠ clinical superiority | disclaimers & pharmacist authority |
| PII leakage via screenshots | Public demo artefacts | watermark removal & masking |
| DDI heuristic coverage | Narrow curated rules vs comprehensive DB | escalation to clinician + roadmap |

## 5.5 Limitations (explicit)

Bullets you may reuse:

1. OCR & LLM quality varies with handwriting, glare, cropping.  
2. Medicine inventory freshness remains **organisationally** bounded (no national stock pipe).  
3. Load testing scope limited to student-era concurrency.  
4. Economic quotation currency volatility (multi-currency realism) simplified in demo configs.  

## 5.6 Answers to Chapter 1 research questions (populate)

| Research question | Finding summary | Evidence § |
|-------------------|----------------|------------|
| *[Paste RQ1]* | *[Draft answer]* | 5.2–5.3 |
| *[Paste RQ2]* | | |

---

# Chapter 6 — Conclusion & Recommendations

## 6.1 Summary of achievements

Produce **three short paragraphs**:

1. **What was engineered** — full-stack MediConnect bridging patient conversational discovery, OCR-assisted prescriptions, heuristic DDI reminders, broadcaster queue, pharmacist inventory truth, reservations, audits.  
2. **Demonstrated capabilities** — reference **Chapter 5** pass tables + **figures 4.1–4.6**.  
3. **Lessons learnt** — integration discipline; fragility of black-box OCR; stakeholder dependency for stock integrity.  

## 6.2 Objectives matrix (mirror Chapter 1 verbatim)

| # | Objective (exact quote from Chapter 1) | Status — Achieved / Partial / Not Achieved | Justification |
|---|----------------------------------------|--------------------------------------------|---------------|
| 1 | *[paste]* | *[ ]* | *[Fig / Table ref]* |
| 2 | | | |
| 3 | | | |

## 6.3 Technical recommendations

1. **Licensed DDI data source** mapped to Zimbabwe brand/generic catalogue.  
2. **Formal OCR benchmarking** corpus with clinician-verified transcription + field accuracy metrics.  
3. **Synthetic load testing** — Locust replay of broadcast storms; websocket fan-out KPIs.  
4. **Offline / flaky network** tolerant queueing — retry backoff for Gemini + idempotent OCR POSTs.  
5. **Payment / escrow** integrations if moving beyond informational quotes.  

## 6.4 Institutional / regulatory recommendations

- Pilot MOUs with coordinated pharmacy clusters for **SLA-backed stock refresh**.  
- Align marketing language with Pharmacy Council ethical advertising guidance.  

## 6.5 Personal reflective paragraph

*Narrative on teamwork, supervisory feedback loops, interdisciplinary lessons (informatics + pharmacy practice), resilience when API quotas blocked demonstrations.*

---

## References (examples — harmonise Harvard)

*[Author, Year]* for: Django Documentation; Gemini API Guides; Channels docs; pertinent journal articles on **clinical decision support** & **spatial access to medicines**.

---

## Appendix A — Remaining screenshots (classify manually)

Enumerate each file from §2.2 once captioned → become **Figure A.x** or merge into Frontend chapter.

## Appendix B — Sensitive prescription media

List JPEG paths only if appendix cleared by ethics committee; otherwise omit.

---

## Appendix C — Deadline reminder (faculty guideline)

Per *CAPSTONE PROJECT WRITE UP*: aim to freeze documentation ahead of **`4 June 2026`** plagiarism-review buffer; defence window **`8–12 June 2026`**.

---

**End of Markdown template — combine with Frontend MD, migrate tables from PDF §4.4 into Chapter 5, adjust figure ordering, PDF export.**
@
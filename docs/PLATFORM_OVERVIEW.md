# MediConnect / Pharmacy backend — platform overview

This document describes how **patients**, **pharmacies**, **ranking**, and **admin** fit together in this Django app (`chatbot`). It covers **implementation**, **AI and uploads**, **why algorithms were chosen**, **how operators can tune them**, and **capstone-level contribution** for assessment.

**Contents:** high-level flow → ranking surfaces (patient chat, live inventory, portal) → mixed poll API → admin & settings → **platform build** (§9) → **AI** (§10) → **prescription uploads** (§11) → **design rationale** (§12) → **admin policy effects & preset guidance** (§13) → file map (§14).

---

## 0. Capstone framing (problem, solution, contribution)

**Problem addressed.** Patients often struggle to find **which nearby pharmacy** has a medicine at a **fair price**, especially when comparing multiple branches or when symptoms are vague. Pharmacies need **visibility** and **fair comparison**; operators need **governance** without redeploying code for every policy tweak.

**What we built.** A **Django** backend that: (1) runs a **conversational MediBot** (LLM) to guide **symptom → suggested medicines → location → medicine request** and **direct search** flows; (2) accepts **prescription images**, extracts medicines via **vision AI**, and threads them into chat context; (3) **broadcasts** requests to pharmacies and collects **`PharmacyResponse`** quotes; (4) **ranks** quotes for patients using **transparent multi-criteria decision analysis (MCDA)** with **urban vs rural** context; (5) supplements quotes with **live `PharmacyInventory`** search; (6) exposes an **admin command surface** (ranking presets, custom weights, chatbot safety policy, audits) so policy can evolve with **documented effects** on ordering.

**Why this “bridges the gap” as a capstone.** The project combines **software engineering** (REST APIs, optional WebSockets via **Django Channels**, data models, admin SPA contracts), **health informatics** (prescription capture, safety disclaimers, non-diagnostic AI guardrails), and **decision-support / OR-style ranking** (weighted criteria, normalisation within a choice set, explainable `score_breakdown`). It is not a black-box recommender: **weights are inspectable** and **admin-adjustable**, aligning with real platforms where **policy owners** must balance **price, access (distance), quality (rating), and reliability**.

**Evidence for markers.** Behaviour described here maps to code in `chatbot/views.py`, `chatbot/services.py` (`RankingEngine`, `ChatbotService`, `OCRService`), `chatbot/pharmacy_portal_ranking.py`, `chatbot/admin_analytics.py` (`RANKING_PROFILE_PRESETS`), and `PlatformAdminSettings` (see §14).

---

## 1. High-level flow

1. **Patient** uses the chatbot, may share **location**, and creates a **medicine request** (`MedicineRequest`).
2. **Pharmacies** (verified / active per your rules) see requests in their workflow and submit **`PharmacyResponse`** rows (availability, price, notes, per-medicine breakdown, etc.).
3. **Inventory** (`PharmacyInventory`) is the source of truth for **live** stock and pricing when the backend merges data for patient-facing lists.
4. **Admin** uses authenticated APIs under `/api/chatbot/admin/...` to operate the platform: stewardship of **ranking policy**, **chatbot safety**, **verification**, **governance**, and **patient/pharmacy** records.

### Chat JSON: symptom preview (patient apps)

When the turn is symptom-related, `POST /api/chatbot/chat/` can include **`request_preview`** (e.g. `Symptoms: …`), **`is_symptom_request`**, and **`symptoms`** even before a `medicine_request_id` exists. After a request is saved, the same fields are enriched from the DB (`request_type == symptom` → **`intent`** may surface as **`symptom_description`**). List endpoints for pharmacists/patients can expose **`request_preview`** / **`is_symptom_request`** for each row.

---

## 2. Ranking: three different things (important)

The word “ranking” is used in **three** places. They are **not** identical.

| Surface | Purpose | Algorithm |
|--------|---------|-----------|
| **A. Chatbot — replies to one request** | Order **this request’s** pharmacy quotes for the patient | **MCDA** via `RankingEngine.rank_responses` (see §3) |
| **B. Chatbot — live inventory search** | “Who has stock near this lat/lon?” | **Fixed** weights: 40% distance, 30% price, 20% availability depth, 10% rating (`get_live_inventory_ranked`) — **does not** read admin ranking presets |
| **C. Pharmacy dashboard — leaderboard** | Show each pharmacy how it scores vs **all** peers | **Same MCDA weight vector** as patient MCDA, but **different inputs** (aggregates / history) — `pharmacy_portal_ranking` (see §4) |

**Summary:**  
- **A** and **C** share **admin-controlled MCDA weights** (price, distance, rating, reliability) from `PlatformAdminSettings` / `RankingEngine.get_context_weights`.  
- **B** does **not**; it uses hard-coded 40/30/20/10.  
- **A** and **C** differ in **what numbers** go into price / distance / rating / reliability (per-request vs network-wide aggregates).

---

## 3. Patient request: how pharmacies are ranked in the chatbot (A)

**Function:** `get_ranked_pharmacy_responses` in `chatbot/views.py`.

### Timing

- For the first **`RANKING_DELAY_MINUTES`** (2 minutes) after the request is created, replies are shown in **chronological** order so patients see answers immediately. The payload marks `ranking_pending: true` and does not assign an MCDA score.
- **After** that window, the backend applies **full MCDA ranking**.

### MCDA (after delay)

- **Implementation:** `RankingEngine.rank_responses` in `chatbot/services.py`.
- **Criteria (4):**  
  - **Price** (from response / live merge)  
  - **Distance** (`distance_km` patient → pharmacy)  
  - **Rating** (pharmacy star rating)  
  - **Reliability** (pharmacy response-rate signal — computed match rate when available)  
- **Weights:** `RankingEngine.get_context_weights(pharmacy_density)`  
  - **Urban vs rural** is determined at the **patient request location**: count of **verified** pharmacies within **5 km**; if **≥ 3**, context is **urban**, else **rural**.  
  - Weights come from DB **overrides** (`ranking_weights_urban` / `ranking_weights_rural`) if set; else from the **`active_ranking_profile`** preset (e.g. urban_default, affordability); else built-in defaults.
- **Normalization:** Within **this request only**, each criterion is min–max normalized across **the current list of responses**, then multiplied by weights and **summed** (higher score = better).
- **Availability:** Responses are split into **medicine available** vs **not**; MCDA runs inside each group; **available** results are ordered **before** unavailable.

**So for “patient requested medicine, multiple pharmacies replied”:** ranking is **MCDA with platform weights**, **relative to competing quotes on that request**, after a short stabilization window.

### Pharmacist submission vs `PharmacyInventory` (what the patient sees)

In **`get_ranked_pharmacy_responses`**, per-requested-medicine rows in **`medicines`** / **`medicine_responses`** prefer **live inventory**: **available quantity** (quantity − reserved) and **price** come from **`PharmacyInventory`** when a name matches (with fuzzy match). The pharmacist’s submitted line item is **not** kept as the displayed source of truth for those fields when inventory has a row. **Top-level** **`price`** on the response object may still reflect the stored **`PharmacyResponse`** aggregate; UIs should rely on **`medicine_responses`** / **`medicines`** for line-level accuracy.

### Layman's terms: what `ranking_score` means on each pharmacy row

Think of each pharmacy quote as getting a **blend of four grades** (price, distance, rating, reliability). It is **not** the same as the pharmacy dashboard’s **0–100** leaderboard score.

1. **Admin policy** sets how important each grade is. That is what you see as **`weights_used`** (they sum to **1**). Example: price 22%, distance 38%, rating 25%, reliability 15% — often with **more weight on distance** when the patient is in a **rural** context (`mcda_context`: fewer verified pharmacies within **5 km** of the **patient’s** location).

2. **Only the pharmacies that replied to this request** are compared. For each of the four criteria, the backend turns raw values (price in dollars, km, stars, response rate %) into a **score between 0 and 1** for *this group only*: the **best** among the group is pushed toward **1**, the **worst** toward **0**. That is what **`score_breakdown`** reports (price / distance / rating / reliability as normalized 0–1).

3. **`ranking_score`** = weighted sum:  
   `price_w×price_norm + distance_w×distance_norm + rating_w×rating_norm + reliability_w×reliability_norm`  
   **Higher = better** for this patient on this request.

4. **Why two pharmacies differ:** e.g. one may be **cheapest** (high price_norm) but **farther** (low distance_norm) and **weaker on reliability** (low reliability_norm). If distance has a **large** weight (typical rural), the **closer** pharmacy can still win overall.

#### Worked example (structure like your API payload)

Policy (**`weights_used`**): price **0.22**, distance **0.38**, rating **0.25**, reliability **0.15** (`mcda_context`: **rural**).

| Pharmacy | `score_breakdown` (price, distance, rating, reliability) | Arithmetic | `ranking_score` |
|----------|------------------------------------------------------------|------------|-----------------|
| Citizens | 1, 0, 1, 0 | 0.22×1 + 0.38×0 + 0.25×1 + 0.15×0 = **0.47** | **0.47** |
| Belgravia | 0, 1, 1, 1 | 0.22×0 + 0.38×1 + 0.25×1 + 0.15×1 = **0.78** | **0.78** |

So Belgravia ranks **#1**: it is **closest** in this set (distance_norm = 1) and **strong on reliability** (reliability_norm = 1), while Citizens is **cheapest** (price_norm = 1) but **farthest** and **weakest on reliability** in this pair — with **38%** on distance, the closer branch wins the blend.

*(Exact decimals may differ slightly from rounding in `RankingEngine.rank_responses`.)*

---

## 4. Pharmacy dashboard leaderboard (C)

**Module:** `chatbot/pharmacy_portal_ranking.py`  
**API example:** pharmacist **ranking summary** (e.g. `GET …/pharmacist/<id>/ranking-summary/`).

### Same as chatbot MCDA?

- **Same:** The **weight vector** (price, distance, rating, reliability) is read the same way: `RankingEngine.get_context_weights` via **`get_portal_mcda_weights(pharmacy)`**.  
- **Different:** **Urban vs rural** for the portal uses **that pharmacy’s** coordinates (density around **the branch**), not the patient’s.  
- **Different:** **Inputs** are **aggregate scores 0–100**, not raw quote fields normalized within one request:

| Symbol | Meaning (dashboard) |
|--------|---------------------|
| **P** | Price competitiveness vs **network** (inventory lines vs peer median prices) |
| **D** | Proximity vs peers from **median** `distance_km` on **`PharmacyResponse`** rows (90 days, `medicine_available=True`), with backfill from patient/pharmacy coordinates when `distance_km` was missing |
| **T** | Patient rating as % of 5 stars |
| **Rel** | **Average** of response-rate % and stock-reliability % (one “reliability” pillar for MCDA) |

**Total score:** weighted sum of P, D, T, Rel, clamped 0–100; full **leaderboard** ranks **all active pharmacies** with the same formula.

**Payload notes:** Responses may include `composite_weights`, `composite_breakdown`, `formula`, `algorithm_source`, and `ranking_summary_payload_version` so the UI can show the **exact** coefficients.

---

## 5. Live inventory ranking in chatbot (B)

**Function:** `get_live_inventory_ranked` in `chatbot/views.py`.

- Builds candidates from **inventory** (stocked, verified pharmacies within max distance).  
- Ranks with **fixed** `0.40·norm_distance + 0.30·norm_price + 0.20·norm_availability + 0.10·norm_rating`.  
- **Does not** use `active_ranking_profile` or `RankingEngine.get_context_weights`.

If product goal is “one policy everywhere”, this path would need to be refactored to reuse `RankingEngine` + admin weights (not done in this doc).

---

## 5.1 Ranked poll API: pharmacist replies + live inventory (mixed list)

**Function:** `get_ranked_responses` in `chatbot/views.py` (e.g. `GET /api/chatbot/request/<id>/ranked/`).

1. **Pharmacist rows:** from **`get_ranked_pharmacy_responses`** (§3) — MCDA among **responders only**, with **`score_breakdown`**, **`weights_used`**, **`mcda_context`**.
2. **Live rows:** from **`get_live_inventory_ranked`** (§5), for pharmacies whose **`pharmacy_id`** is **not** already in the pharmacist list. These rows have **`from_live_inventory: true`**, **`medicines_breakdown`**, and the **40/30/20/10** score (no reliability pillar).
3. **Sort:** The combined list is sorted by **`ranking_score`** with **higher = better** for **both** cohorts (MCDA composite and live composite are each “higher wins” within their own math; cross-cohort ordering is **best-effort** because normalisation happened in **different peer groups**).
4. **`rank`:** After merge and sort, **`rank`** is set to **1 … limit** on the returned payload so it matches the final order (no duplicate ranks from separate sub-lists).
5. **`limit`:** Query param (default **3**) truncates the merged list.

**Meta:** Optional **`envelope`** response includes **`scoring: mcda_live_inventory_mixed`** and a short **`ranking_note`** describing the above.

---

## 5.2 Who appears: broadcast radius vs inventory vs replies (example)

These sets are **not** the same:

| Mechanism | Typical scope in code | What it does |
|-----------|------------------------|--------------|
| **Broadcast helper** | **`broadcast_to_pharmacies`** logs pharmacies within **10 km** of the patient (for future notifications / visibility notes). | Does **not** by itself cap how many rows the patient sees in chat. |
| **Live inventory search** | **`get_live_inventory_ranked`** — verified, stocked branches within **`max_distance_km`** (default **50 km** in code). | Anyone with **matching `PharmacyInventory`** can appear as a **live** row. |
| **Pharmacist replies** | Only branches that submit a **`PharmacyResponse`** for that **`MedicineRequest`**. | **2 replies ⇒ 2** pharmacist-backed rows (plus MCDA among them after the delay). |

**Example:** “7 pharmacies nearby”, **3** have matching stock in **`PharmacyInventory`**, only **2** submit a response.

- **First chat reply** after creating a request (with meds + location): the path often shows **live inventory first** (up to those **3** branches, subject to distance limit and caps), **without** merging pharmacist replies into that **first** live-only view for a brand-new request.
- **Poll / follow-up / ranked endpoint:** Build **2** rows from responders, then **append** live rows for **`pharmacy_id`s** not already present — e.g. if stock exists at **A, B, C** and **A** and **B** replied, the patient sees **A** and **B** as pharmacist rows and **C** as an extra **live-only** row (then merged sort + **`limit`**).

Branches that neither have inventory picked up by the live query **nor** respond **do not** appear.

---

## 6. How admin manages the platform

Admin features are exposed under **`/api/chatbot/admin/...`** (staff/superuser session where enforced). Main groups:

### 6.1 Ranking & algorithm stewardship

- **`GET/PATCH /admin/ranking/config/`**  
  - Reads/writes **`PlatformAdminSettings`**: `active_ranking_profile`, optional `ranking_weights_urban` / `ranking_weights_rural`, presets (`apply_preset`), optional `standard_weights` alias for urban.  
  - **PATCH** may send **only** `active_ranking_profile` (“save profile only”); weights then follow the named preset until custom JSON is saved.

- **Dashboard aggregates** (e.g. **`/admin/dashboard/data/`**, **`/admin/overview/medi-bot/`**) expose layers such as **layer3_algorithm** (profiles, `standard_weights`, `context_profiles`) for the Admin Command Center UI.

### 6.2 Chatbot safety & AI governance

- **`GET/PATCH /admin/chatbot/policy/`** — toggles merged into **`chatbot_policy`** (disclaimers, dosage restrictions, paediatric warnings, etc.).  
- **Safety reviews** — list/resolve endpoints under **`/admin/chatbot/reviews/`** for human audit of flagged interactions.  
- **Conversation logs** — **`/admin/chatbot/logs/`** (and per-conversation).
- **`POST /admin/reports/generate/`** — AI-generated narrative for admin PDF/report exports from dashboard snapshot JSON (uses configured model provider in backend).

### 6.3 Pharmacies & pharmacists

- CRUD-style operations: create/update pharmacy, **verification queue**, **watchlist**, status patches, export.  
- **Pharmacist** admin create/update/delete.  
These tie to **`verification_status`**, **`is_active`**, and governance aggregates shown in the dashboard.

### 6.4 Patients & requests (operations)

- **Patient overview / profile / saved medicines / notifications** under **`/admin/patients/<session_id>/...`**.  
- **Medicine request** and **reservation** status updates.  
- **Request detail** for support (`/admin/requests/<request_id>/`).

### 6.5 Analytics & health

- SLA metrics, geo heatmap, impact/equity, search volume, uptime report, **health** endpoint for lightweight status / badge counts.

### 6.6 Auth bootstrap

- **`/admin/csrf/`**, **`/admin/login/`**, **`/admin/logout/`**, **`/admin/me/`** for SPA admin sessions.

---

## 7. Configuration singleton

**Model:** `PlatformAdminSettings` (`singleton_id='main'`).

| Field | Role |
|--------|------|
| `active_ranking_profile` | Which named preset is “live” for stewardship UI and for weight fallback when urban/rural JSON is empty |
| `ranking_weights_urban` / `ranking_weights_rural` | Optional JSON overrides (integer percents or normalized); when present and valid, they **win** over preset rows |
| `chatbot_policy` | Safety toggles for the chatbot |
| `reported_uptime_percent` | Optional manual uptime display |

**Resolution order for MCDA weights** (`resolve_platform_mcda_weights` / `get_context_weights`): stored JSON for the context → preset matching **`active_ranking_profile`** → hard-coded defaults.

---

## 8. Quick answers to common questions

| Question | Answer |
|----------|--------|
| Is chatbot ranking the same as the pharmacy dashboard? | **Same weight policy** (admin MCDA) for the **four pillars**, but **different inputs** and **different context** (patient location vs pharmacy location for urban/rural). |
| When a patient requests medicine, is that the same as the leaderboard? | **No.** The patient sees **per-request** MCDA over **quotes**. The leaderboard is **global** peer comparison with **P/D/T/Rel** aggregates. |
| Does live inventory search follow admin ranking presets? | **No** — fixed 40/30/20/10 unless you change code to align it. |
| Is the ranked poll list one algorithm? | **No** — it’s **mixed**: MCDA on pharmacist rows + live blend on inventory-only rows; sort uses **higher `ranking_score`** for both; order across types is **approximate**. |
| Pharmacist said $1 × 15 but inventory says $2 × 10? | **`medicines` / `medicine_responses`** follow **inventory** when a row matches; top-level **`price`** may still look like the old submission. |
| 7 nearby, 3 with stock, 2 replied — how many cards? | Up to **3** meaningful options if three branches have stock (two as replies, one possible **live-only**), not seven; see §5.2. |
| Who controls the numbers? | **Admin** via ranking config + presets; **pharmacies** influence scores through **inventory**, **responses**, **ratings**, and **distance** data quality. |

---

## 9. How the platform was built (architecture)

| Layer | Role in this repository |
|--------|-------------------------|
| **API** | Django REST Framework endpoints under `chatbot/urls.py` — chat, medicine requests, pharmacy responses, reservations, admin operations, ranked poll. |
| **Real-time** | **Django Channels** + **`pharmacybackend/asgi.py`** (`ProtocolTypeRouter`: HTTP + WebSocket). See **WebSockets** below. |
| **Data** | **MongoDB** by default (`MONGODB_URI`, `django-mongodb-backend`): app models, Django sessions, and mongo-shim contrib. SQL (SQLite/`DATABASE_URL`) only when `USE_SQL_BACKEND=true` or `DJANGO_USE_MONGODB=false` — see **`pharmacybackend/settings.py`**. Optional legacy SQLite read-only alias for **`import_sqlite_to_mongodb`**. |
| **Ranking** | Patient quote MCDA in `RankingEngine`; portal leaderboard in `pharmacy_portal_ranking.py`; live inventory blend in `get_live_inventory_ranked` (fixed weights). |
| **Admin** | Staff APIs + dashboard-oriented aggregates (`admin_analytics.py`) for stewardship UI. |

### WebSockets: why **Daphne** works and **`runserver`** often does not

Your app’s **`application`** in `pharmacybackend/asgi.py` is a **`ProtocolTypeRouter`**: HTTP goes to Django’s ASGI app, **WebSocket** goes to **`URLRouter(chatbot.routing.websocket_urlpatterns)`**. That full router is only used when the process loads **`pharmacybackend.asgi:application`**.

- **`daphne pharmacybackend.asgi:application -p 8000`** starts an **ASGI** server that handles **both** HTTP and the WebSocket **upgrade** end-to-end. Channels consumers receive connections as intended.

- **`python manage.py runserver`** (stock Django) is still primarily a **WSGI-oriented** dev server in many setups. Even with **`channels`** installed, behaviour depends on version and platform: the **Channels** project supplies its own **`runserver`** command that *can* run ASGI, but on **Windows** or with certain Django/Channels combinations it is **easy to end up with HTTP-only behaviour** or broken upgrades — so WebSockets **fail** (connection errors, immediate close, or 404 on the WS URL).

**Recommendation:** use **Daphne** (or **uvicorn** with the same ASGI path) for local development whenever you need WebSockets. REST-only smoke tests can still use `runserver` if you prefer.

The design keeps **business rules** (ranking weights, chatbot safety toggles) in **data** (`PlatformAdminSettings`, policies) where possible, so operators can tune behaviour without always shipping new application code.

---

## 10. AI integration (MediBot)

**Goal.** Assist patients in **natural language** while staying within **non-diagnostic**, **pharmacy-navigation** boundaries; extract **structured** hints (intent, suggested medicines) for the rest of the pipeline.

**Providers (env-driven).** `ChatbotService` in `chatbot/services.py`:

- **OpenRouter** (`OPENROUTER_API_KEY`) — chat completions API; model configurable (e.g. Gemini-class models via OpenRouter).
- **Google Gemini** — used when **`USE_GEMINI_FOR_CHAT`** is set or when only **`GEMINI_API_KEY`** is present (direct Gemini chat path).

**Flow.** `POST /api/chatbot/chat/` loads history, calls **`ChatbotService.process_message`**, persists assistant metadata (intent, entities, suggested medicines), then **`views.chat`** applies **rule-based guards** (symptom keywords, prescription context, location) to create **`MedicineRequest`** rows, merge **live inventory**, and attach **request preview** fields for symptom flows.

**System behaviour (high level).** The system prompt encodes a **strict symptom flow**: suggest medicines first → user confirms → **then** ask for location → broadcast request. **Direct medicine search** can nudge prescription upload when appropriate. **Admin `chatbot_policy`** (via `merge_chatbot_policy`) appends **safety lines** (disclaimers, dosage restrictions, paediatric warnings, etc.) to the effective prompt.

**Why LLM + rules.** The LLM handles **language variability** (English / Shona / Ndebele intent); **deterministic code** enforces **inventory truth**, **request lifecycle**, **ranking**, and **auditability** — a common pattern in regulated-adjacent health tech.

**Vision (prescriptions).** Separate from chat text: **`OCRService`** uses **Gemini vision** on uploaded images to extract **medicine names** and related text (see §11).

---

## 11. Prescription image upload & downstream use

**Endpoint.** `upload_prescription` in `chatbot/views.py` accepts **`prescription_image`**, runs **`OCRService.extract_prescription_text`**, and returns extracted medicines.

**Pipeline.**

1. **Vision model** analyses the image with a **structured prompt** (medicine names, dosages, frequency, duration, instructions; emphasise accurate names for safety).
2. Parsed lists feed the API response; a **chat user message** is stored, e.g. `Uploaded prescription with medicines: …`, and **`conversation.context_metadata['prescription_medicines']`** is updated.
3. **`POST …/upload-prescription/` with patient coordinates**: if OCR returns **no usable medicine list** (quota, unreadable Rx, errors), the backend **still broadcasts** the same **`MedicineRequest`** with **`prescription_image`** and a **`prescription_review_snapshot`** keyed for **pharmacist manual read**, so dashboards can reply from the saved photo (**`medicine_names`** may be `[]`).
4. On later **`chat`** turns, the backend can merge those names into **medicine matching** for requests **unless** the conversation is classified as **symptom-first** (prescription metadata is ignored for pure symptom queries to avoid wrong intent).

5. When the patient broadcasts with location (**`/upload-prescription/`** immediately or **`/chat/`** after OCR + location), the **`MedicineRequest`** stores **`prescription_image`** (file) and **`prescription_review_snapshot`** (structured OCR: items, dosages, confidence, truncated raw text excerpt). Pharmacist **`GET pharmacist/requests/`** returns **`prescription_review`** plus **`prescription_image_url`**; staff open the image via **`GET pharmacist/requests/<request_id>/prescription-image/?pharmacist_id=…`** to verify dispense legitimacy against the handwritten/printed Rx.

**Ranking link.** When **`medicine_names`** is present, those names drive pharmacist tasking and **live inventory** matching (**image-only broadcasts** rely on pharmacist quotes rather than OCR-backed matching).

---

## 12. Why this ranking approach (algorithm choice)

**Why MCDA (multi-criteria) instead of a single score?** Real patients trade off **price**, **travel burden**, **trust in the branch**, and **likelihood the quote is dependable**. A **single** sort key (e.g. price only) systematically hurts **access** (remote patients) or **safety perception** (ignores reliability). **MCDA** expresses that trade-off as a **weighted sum of normalised criteria**, which is standard in **decision analysis** and easy to explain in a capstone write-up.

**Why min–max normalisation *within one request*?** Quotes only compete against **each other** on that ticket; the patient cares about **relative** cheapest / closest / best-rated **among responders**, not absolute global percentiles.

**Why urban vs rural weight profiles?** Pharmacy **density** within **5 km** of the **patient** flips context: in **dense** areas, patients can often choose on **price**; in **sparse** areas, **distance** and **reliability** matter more. Weights come from **`PlatformAdminSettings`** / presets (`resolve_platform_mcda_weights`).

**Why a separate live-inventory formula (40/30/20/10)?** “Who has stock **now**” is implemented as a **fast, deterministic** query over **`PharmacyInventory`** without waiting for pharmacist clicks. It uses a **fixed** blend so it stays **predictable** and cheap to maintain; **admin presets do not yet drive this path** (documented limitation; alignment would be future work).

**Why admin can change weights.** Stakeholders (ministry-style operators, chain HQ, research supervisors) may want to stress **affordability**, **rural access**, or **shortage-period reliability** without a developer redeploy. The backend stores effective weights and exposes them in API payloads (`weights_used`) for **transparency**.

---

## 13. Admin-configurable rankings: effects, trade-offs, “best” preset

### What admins can change

- **`active_ranking_profile`** — selects a named preset (`RANKING_PROFILE_PRESETS` in `chatbot/admin_analytics.py`).
- **`ranking_weights_urban` / `ranking_weights_rural`** — optional JSON overrides; when valid, they **override** preset percentages for that context.

Resolution order: **custom JSON** → **preset** for `active_ranking_profile` → **built-in defaults** in `RankingEngine.default_weights`.

### Effects when admin changes policy

| Change | Effect on patients (pharmacist-quote MCDA) | Effect on portal leaderboard |
|--------|--------------------------------------------|------------------------------|
| **More weight on price** | Cheaper quotes rise in **`ranking_score`** *among quotes on the same request* (after the 2-minute window). | **P** pillar (price competitiveness vs network) uses the **same weight vector** in the portal MCDA layer — emphasis shifts toward price there too. |
| **More weight on distance** | Closer pharmacies win more often; important when rural or when travel cost dominates. | **D** (distance / proximity aggregate) gains influence vs peers. |
| **More weight on reliability** | Pharmacies with stronger **response-rate** signals rank higher; useful when **stock-outs** or **ghost quotes** are a concern (`shortage_mode`-style presets). | Rel pillar gains influence. |
| **Switching urban vs rural JSON** | At a **patient** location classified **urban** (≥3 verified pharmacies in 5 km), **urban** weights apply; otherwise **rural**. Changing both columns changes behaviour in both density regimes. | Portal uses **pharmacy-centric** density for context (see §4) — patient chat and portal context can **differ** by design. |

**What does *not* change automatically.** **`get_live_inventory_ranked`** weights stay **fixed** until code is updated; **mixed ranked poll** ordering across pharmacist vs live rows remains **best-effort** (§5.1). Existing **`MedicineRequest`** rows are **not** retroactively re-sorted in the database; **new** rankings apply the **next** time the patient hits ranked/chat logic.

### Is there a single “best” ranking for admin to set?

There is **no universal optimum** — the right profile depends on **policy goals**:

- **`urban_default`** — balanced starting point when many pharmacies compete (price + distance + rating + reliability).
- **`affordability`** — stresses **price** when the population is **cost-sensitive** (watch for **underweighting distance** for patients who cannot travel far).
- **`rural_equity`** — slightly **less extreme distance** than default rural + **more rating/reliability** emphasis — useful when **quality of service** must counterbalance geography.
- **`shortage_mode`** — lifts **reliability** when **stock uncertainty** is the main risk.

**Practical recommendation for demos / capstone:** start with **`urban_default`**, switch to **`affordability`** or **`rural_equity`** in the **admin ranking config** API and show **before/after** `ranking_score` / order on the **same synthetic quotes** to demonstrate **governance**. Document the trade-off in the written report.

---

## 14. File map (implementation)

| Area | Primary code |
|--------|----------------|
| Patient quote ranking | `chatbot/views.py` — `get_ranked_pharmacy_responses` |
| MCDA core | `chatbot/services.py` — `RankingEngine`, `resolve_platform_mcda_weights` |
| Live inventory ranking | `chatbot/views.py` — `get_live_inventory_ranked` |
| Mixed ranked poll (patient) | `chatbot/views.py` — `get_ranked_responses` |
| Pharmacy portal leaderboard | `chatbot/pharmacy_portal_ranking.py` |
| Admin ranking API | `chatbot/views.py` — `admin_ranking_config` |
| Preset definitions | `chatbot/admin_analytics.py` — `RANKING_PROFILE_PRESETS` |
| Settings model | `chatbot/models.py` — `PlatformAdminSettings` |
| MediBot / MCDA / OCR core | `chatbot/services.py` — `ChatbotService`, `RankingEngine`, `OCRService` |
| Prescription upload API | `chatbot/views.py` — `upload_prescription` |
| ASGI / WebSocket | `pharmacybackend/asgi.py`, `chatbot/routing.py`, `chatbot/consumers.py` |

---

*Maintained for repository and capstone assessment; update when behaviour changes.*

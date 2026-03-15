# MediConnect — Capstone Spec to Implementation Alignment

This document maps the **formal Use Case Analysis** (Pemhiwa Kudzai, R229417K) to the current pharmacy backend implementation.

---

## 1. System Actors

| Spec Actor | Type | Backend Support |
|------------|------|-----------------|
| **Patient** | Primary Human | Anonymous sessions (`session_id`), optional `User`; `PatientProfile` for preferences |
| **Pharmacist** | Primary Human | `Pharmacist` model, JWT login, dashboard APIs |
| **System Administrator** | Human | Django admin; no custom admin dashboards per UC-A1–A6 |
| **AI Chatbot** | Automated | `ChatbotService` (OpenRouter/Gemini), symptom flow, disclaimers |

---

## 2. Patient Use Cases → Implementation

| Spec UC | Description | Status | Implementation |
|---------|-------------|--------|----------------|
| **UC-P1** | Upload Prescription | ✅ | `POST /api/chatbot/upload-prescription/`; OCR via `OCRService` (Gemini Vision); NER for medicine names; DDI check; broadcast to pharmacies |
| **UC-P2** | Describe Symptoms (AI) | ✅ | Chat flow; cache/session; Gemini/OpenRouter; NER; up to 3 medicines; DDI check; disclaimer; broadcast |
| **UC-P3** | Direct Medicine Search | ✅ | Chat with medicine name + location → request created and broadcast |
| **UC-P4** | Specify Search Preferences | ✅ | `PatientProfile`: `max_search_radius_km`, `sort_results_by` (best_match, nearest, cheapest) |
| **UC-P5** | View Pharmacy Rankings | ✅ | `GET .../request/{id}/ranked/`; MCDA ranking; top N returned |
| **UC-P6** | View Pharmacy Details & Compare | ✅ | Ranked response includes price, preparation time, availability, rating |
| **UC-P7** | Check Drug Interactions | ✅ | `POST /api/chatbot/check-interactions/`; `DrugInteractionService`; also used in upload/symptom flows |
| **UC-P8** | Access Offline Information | ⚠️ | PWA/offline is frontend concern; backend supports cached/session data |

---

## 3. Pharmacist Use Cases → Implementation

| Spec UC | Description | Status | Implementation |
|---------|-------------|--------|----------------|
| **UC-PH1** | Receive Patient Requests | ✅ | Dashboard `GET /api/chatbot/pharmacist/requests/`; no WebSocket/email/SMS yet |
| **UC-PH2** | Submit Medicine Availability | ✅ | `POST .../pharmacist/response/{request_id}/`; `medicine_available`, `medicine_responses` |
| **UC-PH3** | Provide Pricing Information | ✅ | `price` in response; validation (e.g. required for ranking) |
| **UC-PH4** | Indicate Preparation Time | ✅ | `preparation_time` (minutes) in response |
| **UC-PH5** | Suggest Alternatives | ✅ | `alternative_medicines`, `medicine_responses[].alternative` |
| **UC-PH6** | Manage Pharmacy Profile | ✅ | Pharmacy update; location, contact; pharmacist profile |
| **UC-PH7** | Update Stock Information | ✅ | `GET/POST /api/chatbot/pharmacist/inventory/`; stale data can be flagged (e.g. >30 days) |
| **UC-PH8** | View Response History | ✅ | Via request/response APIs and dashboard |

---

## 4. Administrator Use Cases

| Spec UC | Description | Status | Implementation |
|---------|-------------|--------|----------------|
| **UC-A1** | Monitor System Performance | ⚠️ | Django admin; no dedicated metrics dashboard (broadcast/response rate, latency) |
| **UC-A2** | Manage User Accounts | ⚠️ | Django admin; no custom suspend/verify flows |
| **UC-A3** | Verify Pharmacy Credentials | ⚠️ | `Pharmacy.is_active`; no explicit approval workflow |
| **UC-A4** | Configure System Parameters | ⚠️ | Env/settings; no admin UI for radius, ranking weights, rate limits |
| **UC-A5** | Generate Usage Reports | ❌ | Not implemented |
| **UC-A6** | Review Security Logs | ⚠️ | Django/auth logs; no dedicated audit view |

---

## 5. AI Chatbot Use Cases (<<include>>)

| Spec UC | Description | Status | Implementation |
|---------|-------------|--------|----------------|
| **UC-C1** | Process Natural Language Queries | ✅ | Chatbot; OpenRouter/Gemini; symptom parsing |
| **UC-C2** | Maintain Conversation Context | ✅ | Last 8 message pairs per session (`history` in chat) |
| **UC-C3** | Identify Medical Entities | ✅ | LLM + optional NER in flow; medicine/symptom extraction |
| **UC-C4** | Check Drug Interactions | ✅ | `DrugInteractionService.check_interactions()` in flows |
| **UC-C5** | Provide Safety Warnings | ✅ | Mandatory disclaimer for AI suggestions |
| **UC-C6** | Escalate Complex Queries | ⚠️ | Messaging can direct to professional care; no formal escalation API |

---

## 6. Request Lifecycle (State Flow)

Spec states: **Created → Broadcasting → Awaiting Responses → (Responses Received | SafetyHold) → Ranking → Completed | Expired**

| Spec State | Backend `MedicineRequest.status` | Notes |
|------------|----------------------------------|--------|
| Created | `created` | ✅ |
| (validated) | `validated` | ✅ Optional step |
| Broadcasting | `broadcasting` | ✅ |
| Awaiting Responses | `awaiting_responses` | ✅ |
| Responses Received | `responses_received` | ✅ |
| **SafetyHold** | ❌ | **Not in model.** Spec: high-risk DDI → pause for review. Could add `safety_hold` status and hold logic. |
| Ranking | `ranking` | ✅ |
| Completed | `completed` | ✅ |
| Expired | `expired` | ✅ (e.g. `expire_requests` management command) |
| (partial/timeout) | `partial`, `timeout` | ✅ Extra backend states |

---

## 7. Ranking Algorithm

Spec: **Multi-Criteria Optimisation**  
\( S_i = \sum_k W_k(\text{context}) \cdot N_k(R_i) \), normalised \(N_k \in [0,1]\), **higher = better**.

### Spec weight matrix

| Criteria (k) | Urban Weight | Rural Weight |
|--------------|--------------|--------------|
| Price | 0.25 | 0.40 |
| Distance | 0.15 | 0.35 |
| Availability | 0.45 | 0.15 |
| Rating | 0.15 | 0.10 |
| **Total** | 1.00 | 1.00 |

### Current implementation

- **Location:** `chatbot/services.py` (`RankingEngine`), `chatbot/views.py` (`get_ranked_pharmacy_responses` and inline MCDA).
- **Context:** Urban vs rural by pharmacy density (e.g. ≥3 pharmacies within 5 km → urban).
- **Weights used:**  
  - Urban: price 0.35, distance 0.25, rating 0.25, **reliability** 0.15.  
  - Rural: price 0.20, distance 0.45, rating 0.20, reliability 0.15.  
- **Gap:** Spec uses **Availability** (in stock) as a criterion with high urban weight (0.45). Backend uses **reliability** (response rate) and does not use the same availability weight. Align by: (1) adding availability as a normalised criterion and (2) matching urban/rural weights to the spec table above (including availability, no reliability if not in spec).

---

## 8. Key Operation Specifications

### 8.1 submitPharmacyResponse

| Spec | Implementation |
|------|----------------|
| requestID exists, request.state = 'Awaiting Responses' | Request must exist; response accepted when status is `broadcasting` or `awaiting_responses` (status updated to `awaiting_responses` on first broadcast, then `responses_received` on first response). |
| price > 0 | Validated in serializer/views; required for ranking. |
| preparationTime ≥ 0 | `preparation_time` stored; validation can be enforced explicitly. |
| No duplicate response per pharmacy/request | Enforced (one response per pharmacist/pharmacy per request). |
| PharmacyResponse created, submissionTime, responseCount | ✅ `PharmacyResponse.submitted_at`; response count derivable from related responses. |

### 8.2 processSymptomQuery

| Spec | Implementation |
|------|----------------|
| symptomDescription ≥ 10 characters | Can add explicit validation. |
| patientID/session | `session_id` / conversation. |
| patientLocation within Zimbabwe | Location validated in flow; bounds check possible. |
| conversationHistory ≤ 8 message pairs | ✅ Last 8 messages used. |
| Rate limit (e.g. 10 queries/hour/patient) | Can add throttle per session. |
| MedicineRequest type = 'symptom', 1–3 medicines, disclaimer, broadcast 15 km | ✅ Symptom flow; radius from profile or default (e.g. 10 km); spec 15 km for symptom can be made configurable. |

### 8.3 initiatePharmacyBroadcast

| Spec | Implementation |
|------|----------------|
| request.status = validated; not expired | Request created with `broadcasting`; `expires_at` set. |
| 1.0 km ≤ searchRadius ≤ 50.0 km | Profile `max_search_radius_km`; default 10 km; bounds can be enforced. |
| 1–10 distinct medicines | Medicine list length can be validated. |
| request.status → broadcasting; broadcastedAt; expectedResponseDeadline = broadcastedAt + 2 hours | ✅ Status; `expires_at` set (currently urban 30 min, rural 120 min; spec says 2-hour window). |
| Notifications to each pharmacy | Dashboard visibility; no WebSocket/email/SMS queue yet. |
| NoPharmaciesInRadius → expand 10 → 20 → 50 km | ❌ Not implemented; single radius used. |
| NotificationServiceUnavailable → fallback + retry | ❌ No notification service yet. |
| PharmacyInventoryStale → flag | Can flag when inventory `updated_at` > 30 days. |

---

## 9. Environment / APIs

| Spec / Component | Backend | .env.example |
|------------------|---------|--------------|
| OCR / NER | Gemini Vision in `OCRService` | `GEMINI_API_KEY` |
| LLM (symptom, suggestions) | OpenRouter / Gemini in `ChatbotService` | `OPENROUTER_API_KEY` (and optionally Gemini) |
| DDI | `DrugInteractionService` (built-in list) | No external DDI API key; optional if you add one later |

---

## 10. Summary of Gaps

| Priority | Gap | Action |
|----------|-----|--------|
| High | **SafetyHold** state missing | Add `safety_hold` to `MedicineRequest.status` choices; set when severe DDI detected; review flow before moving to ranking. |
| High | **Ranking weights** differ from spec | Add Availability; set Urban: Price 0.25, Distance 0.15, Availability 0.45, Rating 0.15; Rural: Price 0.40, Distance 0.35, Availability 0.15, Rating 0.10. |
| Medium | **2-hour response window** | Spec: all requests 2-hour window. Current: urban 30 min, rural 120 min. Option: make request expiry 2 hours for all. |
| Medium | **Progressive radius** 10 → 20 → 50 km | If no pharmacies in initial radius, retry with 20 km then 50 km before returning “no pharmacies”. |
| Medium | **Pharmacist notifications** | WebSocket / email / SMS for new requests (spec: broadcast via WebSocket). |
| Low | Admin dashboards (UC-A1–A6) | Custom views for metrics, verification, system params, reports, audit logs. |
| Low | **processSymptomQuery**: min 10 chars, 15 km for symptom, rate limit | Add validation and config. |

---

## 11. References

- **Capstone doc:** MediConnect Use Case Analysis — Request to Final Result (Pemhiwa Kudzai, R229417K).
- **Backend:** `USE_CASE_GAP_ANALYSIS.md`, `PHARMACY_RANKING_ALGORITHM.md`, `MEDICINE_REQUEST_FLOW.md`, `chatbot/models.py`, `chatbot/views.py`, `chatbot/services.py`.

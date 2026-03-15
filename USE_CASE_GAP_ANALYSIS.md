# Use Case Implementation Gap Analysis

This document compares the **UPDATED USE CASES** specification with the current backend implementation.

---

## ✅ IMPLEMENTED (Aligns with Use Cases)

| Use Case | Status | Notes |
|----------|--------|-------|
| **UC-P01** Access Platform Anonymously | ✅ | session_id, no login required |
| **UC-P02** Prescription Upload | ✅ | `POST /upload-prescription/`, OCR via Gemini |
| **UC-P03** Symptom Description | ✅ | Chatbot flow, suggests medicines, location required |
| **UC-P04** Direct Medicine Search | ✅ | Via chatbot, medicine name → location |
| **UC-P05** View Ranked Results | ✅ | MCDA ranking, price/distance/rating/reliability |
| **UC-P07** AI Healthcare Chatbot | ✅ | OpenRouter, symptom flow, disclaimers |
| **UC-PH01** Register Pharmacy | ✅ | `POST /register/pharmacy/` |
| **UC-PH02** Login | ✅ | `POST /pharmacist/login/` |
| **UC-PH03** Receive Patient Requests | ✅ | `GET /pharmacist/requests/` |
| **UC-PH04** Submit Response | ✅ | `POST /pharmacist/response/{id}/` |
| **UC-PH05** Decline Request | ✅ | `POST /pharmacist/decline/{id}/` |
| **UC-PH06** Suggest Alternative | ✅ | alternative_medicines in response, suggest_alternatives API |
| **UC-PH08** Update Inventory | ✅ | `GET/POST /pharmacist/inventory/` |
| **UC-S01** OCR on Prescription | ✅ | OCRService with Gemini Vision |
| **UC-S02** Conversation Context | ✅ | session-based, last 8 turns |
| **UC-S04** Pharmacy Rankings | ✅ | MCDA, urban/rural weights |
| **Result Isolation** | ✅ | `start_new_search`, `results_for_request_id` |
| **UC-P08** Check Drug Interactions | ✅ | `POST /check-interactions/` |
| **UC-P12** Rate Pharmacy | ✅ | `POST /rate-pharmacy/` |
| **UC-S05** Drug Interaction Check | ✅ | DrugInteractionService in check-interactions |
| **UC-S07** No-Response Timeout | ✅ | `python manage.py expire_requests` (run via cron) |
| **UC-S09** Clean Expired Sessions | ✅ | `python manage.py clean_expired_sessions` (run daily) |

---

## ⚠️ PARTIALLY IMPLEMENTED

| Use Case | Status | Gap |
|----------|--------|-----|
| **UC-P06** Directions to Pharmacy | ⚠️ | No mapping API (Google Maps, etc.); pharmacy address returned but no route |
| **UC-S03** Broadcast to Pharmacies | ⚠️ | Requests created and visible in dashboard; no WebSocket/email/SMS notifications to pharmacists |
| **UC-A01–A07** Admin Use Cases | ⚠️ | Django admin exists; no custom admin dashboards per use case |

---

## ❌ NOT IMPLEMENTED

| Use Case | Description | Required |
|----------|-------------|----------|
| **UC-P09** Phone for SMS Updates | Optional phone, verification, SMS notifications | Phone storage, verification API, SMS provider (Twilio, etc.) |
| **UC-P10** Create Account from Anonymous | Link session to new account, save history | Patient registration, session linking |
| **UC-P11** View Request History | Registered patient sees past requests | `GET /patient/requests/` (requires auth) |
| **UC-S10** SMS Notifications | Send SMS when responses ready | SMS provider integration |

---

## Summary

| Category | Count |
|----------|-------|
| ✅ Fully implemented | 23 |
| ⚠️ Partially implemented | 2 |
| ❌ Not implemented | 3 |

---

## Recommended Implementation Order (per use cases)

1. **UC-S07** No-Response Timeout – Mark expired requests; optionally notify (needs SMS for full flow)
2. **UC-P09 + UC-S10** Phone & SMS – Enables real-time updates for anonymous users
3. **UC-P08 + UC-S05** Drug Interactions – Safety-critical for multi-medicine requests
4. **UC-P12** Rate Pharmacy – Endpoint for patients to submit ratings
5. **UC-S09** Clean Expired Sessions – Privacy and data retention
6. **UC-P10 + UC-P11** Account creation & history – For users who want to save results

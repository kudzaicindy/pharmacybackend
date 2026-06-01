# Pharmacy portal settings — backend contract

Base path: **`/api/chatbot/`** (see `chatbot/urls.py`).

## Authentication

| Mechanism | Use |
|-----------|-----|
| **JWT Bearer** | `Authorization: Bearer <access>` |

**Login:** `POST /api/chatbot/pharmacist/login/` with email + password. On success (including after MFA completes), responses include **`access`** and **`refresh`** tokens alongside `pharmacist`.

**Refresh:** `POST /api/chatbot/pharmacist/token/refresh/` body `{ "refresh": "<refresh_token>" }` (SimpleJWT).

Pharmacists must have a linked Django `User`; otherwise login returns an error.

Env (see `pharmacybackend/settings.py`): `JWT_ACCESS_MINUTES`, `JWT_REFRESH_DAYS`.

---

## Consolidated settings

### `GET /api/chatbot/pharmacist/settings/`

- **Auth:** Bearer required.
- **Query (optional):** `pharmacist_id=<uuid>` — must match the authenticated pharmacist.

Returns a nested object:

```json
{
  "profile": {
    "pharmacist_id": "...",
    "pharmacy_id": "PHARM-001",
    "verification_status": "verified",
    "name": "Branch legal name",
    "display_name": "Shown name",
    "license_number": "",
    "tax_number": "",
    "address": "",
    "phone": "",
    "whatsapp": "",
    "email": "",
    "website": "",
    "description": "",
    "branch_name": "",
    "city": "",
    "geo_region": ""
  },
  "operations": {
    "accepting_requests": true,
    "opening_hours": {
      "weekday_open": "08:00",
      "weekday_close": "18:30",
      "opening_hours_text": "",
      "holiday_notes": ""
    },
    "weekday_open": "08:00",
    "weekday_close": "18:30",
    "opening_hours_text": "",
    "holiday_notes": "",
    "timezone": "Africa/Harare",
    "holiday_mode": false,
    "pause_outside_opening_hours": false,
    "pause_requests_outside_hours": false,
    "auto_accept_reservations": false,
    "max_reservation_window_minutes": 120,
    "low_stock_threshold_default": 5,
    "out_of_stock_behavior": "hide",
    "auto_substitute_enabled": false
  },
  "notifications": {
    "channels": { "email": true, "sms": false, "in_app": true },
    "notify_new_request": true,
    "notify_low_stock": true,
    "notify_reservation_expiry": true,
    "email_on_low_stock": true,
    "quiet_hours": {},
    "digest_frequency": "instant"
  },
  "service": {
    "radius_km": null,
    "pickup_available": true,
    "delivery_available": false,
    "areas_covered": []
  },
  "preferences": {
    "disclaimer_visible": true,
    "prescription_enforcement": false,
    "audit_logging_enabled": true,
    "ui_dark_mode": false,
    "ui_table_density": "comfortable",
    "ui_default_page_size": 25,
    "ui_default_filters": {},
    "preferred_profile": "",
    "hide_zero_quantity_in_search": false,
    "auto_suggest_shift_on_open": true
  },
  "meta": {
    "feature_flags": {
      "settings_history": true,
      "settings_reset": true,
      "test_notification": true,
      "security_panel": true
    },
    "current_ranking_profile": "urban_default",
    "settings_version": 1,
    "settings_updated_at": "2026-05-24T12:00:00+00:00",
    "mfa_totp_enabled": false
  }
}
```

**Read-only:** `profile.verification_status` (controlled by admins).

**SPA-friendly mirrors (also persisted inside JSON where noted):**

- **`operations.weekday_open` / `weekday_close` / `opening_hours_text` / `holiday_notes`** — duplicated at the top of `operations` for convenience; they are stored **inside** `operations.opening_hours` and updated when you PATCH either the nested `opening_hours` object **or** these flat keys.
- **`operations.pause_requests_outside_hours`** — alias of **`pause_outside_opening_hours`** (same value on GET/PATCH).
- **`notifications.email_on_low_stock`** — mirrors **`notify_low_stock`** (PATCH either key).
- **`preferences.hide_zero_quantity_in_search`** / **`auto_suggest_shift_on_open`** — persisted under **`preferences.ui_default_filters`**; top-level booleans are derived on GET.

---

### `PATCH /api/chatbot/pharmacist/settings/`

- **Auth:** Bearer required.
- **Body:** JSON object with **one or more** of: `profile`, `operations`, `notifications`, `service`, `preferences`. Only included keys are updated.

Examples:

Pause new patient traffic (toggle syncs with portal top bar behaviour server-side):

```json
{
  "operations": { "accepting_requests": false }
}
```

Profile save subset:

```json
{
  "profile": {
    "name": "Updated Pharmacy Ltd",
    "display_name": "Uptown Branch",
    "license_number": "REG-123",
    "tax_number": "",
    "address": "...",
    "phone": "...",
    "whatsapp": "...",
    "email": "branch@example.com",
    "website": "https://example.com",
    "description": "..."
  }
}
```

**Optimistic locking (optional):** top-level `expect_version` must equal `meta.settings_version` before patch; otherwise **409** with code `settings_version_conflict`.

Returns the **same envelope** as GET after applying changes.

Implementation: `chatbot/pharmacist_portal_settings.py` (`serialize_pharmacist_settings_envelope`, `patch_pharmacist_settings_envelope`).

---

## Side effects when `operations.accepting_requests` is false

- **Patient live inventory** (`get_live_inventory_ranked`): that pharmacy’s stock rows are omitted.
- **Broadcast helper** (`broadcast_to_pharmacies`): branch excluded from nearby notification list construction (and outbound pharmacy emails that use that list).

**Pharmacist dashboard** (`GET .../pharmacist/requests/?pharmacist_id=…`): still lists active broadcasts so staff can see pending work and respond; pause does **not** blank the inbox.

Defaults: missing `PharmacySettings` row is treated as **accepting**.

---

## Profile shortcut

- **`GET|PATCH /api/chatbot/pharmacist/profile/`** — same Bearer auth; returns or patches only the `profile` subsection (nested shape as above).

---

## Security & password

- **`GET|PATCH /api/chatbot/pharmacist/security/`** — audit prefs (`audit_logging_enabled`); GET includes `mfa_totp_enabled`. Password is **not** changed here.
- **`POST /api/chatbot/pharmacist/password/change/`** — Bearer required. Body:

```json
{
  "current_password": "...",
  "new_password": "..."
}
```

(`old_password` is accepted as an alias for `current_password`.)

Successful response includes **new** `access` / `refresh` tokens.

---

## MFA (pharmacist)

All routes **`IsAuthenticated`** (Bearer):

| Method | Path |
|--------|------|
| GET, POST | `/api/chatbot/pharmacist/mfa/status/` |
| POST | `/api/chatbot/pharmacist/mfa/setup/start/` |
| POST | `/api/chatbot/pharmacist/mfa/setup/confirm/` |
| POST | `/api/chatbot/pharmacist/mfa/disable/` |

Setup/start returns `provisioning_uri` and **`otpauth_uri`** (same value).

---

## Auxiliary settings routes

| Method | Path | Notes |
|--------|------|--------|
| POST | `/pharmacist/settings/test-notification/` | Simulated dispatch |
| GET | `/pharmacist/settings/history/` | Audit rows |
| POST | `/pharmacist/settings/reset/` | Resets configurable fields; returns full envelope |

All require Bearer.

---

## Patient settings (summary)

Still session-based (`session_id` / `conversation_id` query):

- **`GET|PATCH /api/chatbot/patient/profile/`**

PATCH accepts existing fields plus SPA aliases:

| Alias | Stored field |
|-------|----------------|
| `email_notifications` | `notify_pharmacy_responses` |
| `drug_interaction_alerts` | `notify_drug_interactions` |

**Patient MFA** remains under `/api/chatbot/patient/mfa/...` (session resolution unchanged).

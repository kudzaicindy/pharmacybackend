# Patient pickup / reservation — backend contract

This aligns the MediConnect SPA (banners, “Continue your search”, ranked polling, `localStorage` merge) with the Django API under **`/api/chatbot/`**.

Run migrations after pulling: reservation rows gain optional **`medicine_request`** linkage and **`updated_at`** so ranked-cache invalidation picks up pharmacist confirmations.

---

## 1. `POST /api/chatbot/reserve/`

**Purpose:** Create a pickup hold; optionally bind it to a specific broadcast (`MedicineRequest`) so later GETs can return `reservations[]` without relying on client-only state.

### Request body

| Field | Required | Notes |
|--------|----------|--------|
| `pharmacy_id` | ✓ | Pharmacy primary key |
| `medicine_name` | Usually ✓ | Omitted only if `conversation_id` resolves `suggested_medicines` (first medicine used) |
| `quantity` | — | Default `1` |
| `conversation_id` | Recommended | Must match `MedicineRequest` when `request_id` is sent |
| `session_id` | Fallback | Alternative ownership check vs `medicine_request.conversation.session_id` |
| `request_id` **or** `medicine_request_id` | Recommended | UUID of `MedicineRequest`; links FK on `Reservation` |
| `patient_name`, `patient_phone` | Optional | Passed through to pharmacist views |

When `request_id` / `medicine_request_id` is present, **`conversation_id` or `session_id` must match that request’s conversation** (403 otherwise).

### Success `201 Created`

Includes at least:

- `success`, `reservation_id`
- `status` — initial `pending`
- `pharmacy_id`, `pharmacy_name`
- `medicine_name`, `quantity`
- `expires_at`
- `confirmed_at` — `null` until pharmacist confirms (see §3)
- `request_id` / `medicine_request_id` when linked

### Duplicate active reservation → `409 Conflict`

If another non-expired `pending` or `confirmed` reservation already exists for the **same pharmacy + SKU** scoped by:

- the same **`medicine_request`** when FK is set (preferred), otherwise
- the same **`conversation`** or **`session_id`** as narrowed in the duplicate query,

…the response echoes the conflicting row identifiers (`reservation_id`, `status`, `expires_at`, `confirmed_at`, etc.) so the client can reconcile UI without guessing.

---

## 2. `GET /api/chatbot/patient/requests/<uuid>/`

**Purpose:** Resume the search; SPA merges `reservations` after refresh.

**Auth shape:** Existing rules — `session_id` or `conversation_id` query param tying the caller to `medicine_request.conversation.session_id`.

### Response additions

- **`reservations`** — ordered list (newest first) of pickups for **this broadcast**  
  - Primary: reservations with `medicine_request_id = request_id`
  - Legacy: same-conversation rows with null FK constrained to timestamps between this request’s creation and the next request in that chat

Each element includes:

`reservation_id`, `medicine_request_id`, `request_id` (mirror), `pharmacy_id`, `pharmacy_name`, `medicine_name`, `quantity`, `status`, `price_at_reservation`, `reserved_at`, `expires_at`, `confirmed_at`

- **`active_reservation`** — same shape plus `maps_query`, scoped to **this broadcast’s latest** pending/confirmed non-expired row (not merely “latest in conversation”).
- **`pharmacy_responses`** — unchanged ranked/MCDA list for offline ranking.

Related list endpoints:

- **`GET …/patient/requests/`** — each item gains **`reservations`** (same shape).
- **`GET …/patient/active-request/`** — adds **`reservations`** for the active request alongside **`active_reservation`**.

---

## 3. `POST /api/chatbot/pharmacist/reservations/<uuid>/confirm/`

**Purpose:** Pharmacist acknowledges stock / readiness → patient-visible **`status: confirmed`** and **`confirmed_at`** ISO timestamp.

Reservation saves bump **`Reservation.updated_at`**, which is included in the ranked API cache fingerprint so short-TTL caches reflect confirmation quickly without `skip_ranked_cache`.

---

## 4. `GET /api/chatbot/request/<uuid>/ranked/`

**Ownership:** Existing `conversation_id` or `session_id` gates.

### Flat array mode (default)

Returns the ranked pharmacy row list only (backwards compatible). Rows may carry optional keys when reconstructed from decorators:

`has_reservation`, `reservation_status`, `reservation_id`

### Envelope mode `?envelope=true`

Returns an object:

- `results` / `items` — ranked pharmacies (above hint fields merged per row).
- **`reservations`** — active (`pending`|`confirmed`) non-expired pickup snapshots **for this medicine request**, same mini-schema as §2 entries.
- `count`, `meta` — unchanged scoring notes.

Ranking cache version **`ranked_api:v6:…`**; fingerprint includes reservation `updated_at` activity so confirmations invalidate cached ranked payloads.

---

## 5. Operational notes

- **Always send `request_id` from the SPA when reserving**, so confirmations and expiry handling stay attributable to one broadcast.
- **Restart ASGI workers** (`daphne` / uvicorn) after deploy so decorators and fingerprints load.
- **Cron:** `expire_reservations` releases stock; full `Reservation.save()` keeps **`updated_at`** fresh for auditing and cache coherence.

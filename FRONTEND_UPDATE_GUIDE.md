# Frontend Update Guide

## Overview
This guide explains how to update your frontend to work with the improved pharmacy backend system. The platform supports **anonymous users** (no login required); each person should see only their own search results.

## Key Changes

### 1. Result Isolation – Each Person Sees Their Own Results

**Every user/session must see only their current search's results—never results from previous searches or other users.**

| Scenario | Frontend behavior |
|----------|-------------------|
| **New search (same device)** | Send `start_new_search: true` in the first message, or generate a new `session_id` |
| **Display pharmacy responses** | Only show responses when `results_for_request_id` or `medicine_request_id` matches the request you're displaying |
| **Polling** | Poll only for the current `medicine_request_id`; ignore poll results for a different request |
| **"New Search" button** | Call chat with `start_new_search: true` and clear any displayed pharmacy results |
| **Different person (shared device)** | Generate new `session_id` on app load or when "Start fresh" is tapped |

**Backend support:**
- `start_new_search`: When `true`, the backend creates a new session so the user gets a clean conversation with no prior results.
- `results_for_request_id`: Present when pharmacy responses are returned; use it to confirm responses belong to the current request.
- **No “old” results on new search:** The backend only returns an existing request’s pharmacy responses when the user **explicitly** asks for updates (e.g. “any updates?”, “check”, “waiting”). Bare confirmations like “yes” or “ok” do **not** trigger showing a previous request’s responses, so the user won’t see old stomach-ache results when they’ve started a new search (e.g. headache).

### 2. Request-Response Matching
- **IMPORTANT**: Each request has a unique `request_id` (UUID)
- Responses are strictly matched to requests by `request_id`
- Never show responses from one request for another request

### 3. API Endpoints

#### Chat Endpoint
**POST** `/api/chatbot/chat/`

**Request Body:**
```json
{
  "message": "I have a headache",
  "session_id": "session_123",  // Optional, auto-generated if not provided
  "conversation_id": "uuid",     // Optional, for continuing conversation
  "start_new_search": false,     // Optional: true = fresh session, no previous results (for "New Search" button)
  "location_latitude": -17.8394, // Optional, from geolocation
  "location_longitude": 31.0543,  // Optional, from geolocation
  "location_address": "Harare",  // Optional, manual entry
  "location_suburb": "Mt Pleasant",  // Optional, for pharmacist dashboard display
  "language": "sn",              // Optional: "en" (English), "sn" (Shona), "nd" (Ndebele)
  "selected_medicines": ["paracetamol", "ibuprofen"]  // Optional: medicines patient selected (symptom flow)
}
```

**Response:**
```json
{
  "response": "AI response text",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "symptom_description",
  "requires_location": true,
  "suggested_medicines": [],
  "medicine_request_id": "uuid",  // Present when request is created
  "short_request_id": "0B142651",  // First 8 chars of request_id (no dashes), matches dashboard #0b142651
  "pharmacy_responses": [         // Present when responses are available
    {
      "response_id": "uuid",
      "pharmacy_id": "simed-01",
      "pharmacy_name": "Simed Pharmacy",
      "medicine_available": true,
      "price": "5.00",
      "preparation_time": 10,
      "distance_km": 2.5,           // ✅ Always calculated
      "estimated_travel_time": 5,   // ✅ Always calculated
      "total_time_minutes": 15,     // ✅ preparation + travel
      "ranking_score": 50.0,
      "alternative_medicines": [
        {
          "medicine": "aspirin",
          "suggested_by": "Simed Pharmacy",
          "pharmacy_id": "simed-01"
        }
      ],
      "notes": "",
      "submitted_at": "2026-01-17T12:00:00Z"
    }
  ],
  "request_sent_to_pharmacies": true,
  "total_responses": 2,
  "status": "responses_received",
  "results_for_request_id": "uuid",
  "recommendation": {
    "recommended_pharmacy": "Simed Pharmacy",
    "pharmacy_id": "simed-01",
    "reason": "I recommend **Simed Pharmacy** because medicine is available, only 2.5km away, ready in 15 minutes."
  }
}
```

#### Symptom flow: “I have a headache” → suggest medicines (e.g. ibuprofen)

When the user describes symptoms (e.g. “I have a headache”), the backend follows a strict order: **suggest medicines first**, then **ask for confirmation**, then **ask for location**. The first reply must **not** ask for location.

**Step 1 – User says “I have a headache”**

Backend responds with something like:

- **Message to show:** Acknowledge the symptom, suggest specific medicines with a short reason, then ask if they want to search. Example:
  - *“Based on your symptoms, you might need: Paracetamol – for fever and headache; Ibuprofen – for pain relief; Aspirin – for pain relief. Would you like to search for these medicines? You can say yes or tell me which ones you want.”*
- **Do not** ask for location in this step.

**API response shape for Step 1 (symptom, no location yet):**

```json
{
  "response": "Based on your symptoms (headache), you might need:\n• Paracetamol – for fever and headache\n• Ibuprofen – for pain relief\n• Aspirin – for pain relief\n\nWould you like to search for these medicines? You can say yes or tell me which ones you want.",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "symptom_description",
  "requires_location": false,
  "suggested_medicines": ["paracetamol", "ibuprofen", "aspirin"],
  "medicine_request_id": null,
  "request_sent_to_pharmacies": false
}
```

**Frontend should:**
- Show `response` as the assistant message.
- Use `suggested_medicines` to show selectable pills/chips (e.g. Paracetamol, Ibuprofen, Aspirin) so the user can confirm “all” or pick some.
- Do **not** show a location input yet (`requires_location` is `false`).

**Step 2 – User confirms (e.g. “Yes”, “I want ibuprofen”, “All of them”)**

Backend then asks for location. Example response:

- *“To find pharmacies near you, I need your location. Please share your area or use your current location.”*
- `requires_location: true`, still no `medicine_request_id` until they send location.

**Step 3 – User sends location**

Backend creates the medicine request, sends to pharmacies, and returns `medicine_request_id`, `poll_url`, and later `pharmacy_responses` (or live inventory results).

So for “headache → suggest ibuprofen”: the **first** response is only the suggestion text + `suggested_medicines` and **no** location ask; the **second** response (after confirmation) is the location ask; the **third** (after location) is the request + results.

#### Get Ranked Responses
**GET** `/api/chatbot/request/{request_id}/ranked/?conversation_id={conversation_id}&limit=3`

**Query Parameters:**
- `conversation_id` (required): User's conversation ID for security
- `limit` (optional): Number of responses (default: 3)

**Response:** Same as `pharmacy_responses` in chat endpoint

#### Real-Time Response Polling (Show responses as soon as pharmacy responds)

When the chat returns `"Request has been sent. Waiting for pharmacies to respond"`, the response includes:
```json
{
  "medicine_request_id": "uuid",
  "pharmacy_responses": [],
  "total_responses": 0,
  "poll_url": "/api/chatbot/request/{request_id}/ranked/?conversation_id={uuid}&limit=3",
  "poll_interval_seconds": 10,
  "polling_enabled": true
}
```

**Frontend behavior:** When `polling_enabled` is true and `total_responses` is 0:
1. Start polling `GET {baseUrl}{poll_url}` every `poll_interval_seconds` seconds
2. When the response array has items, stop polling and display the pharmacy responses to the user immediately
3. Stop polling after request expires (e.g. 30 min urban / 2 hr rural) or when user navigates away
4. **Result isolation:** Only display poll results if they match the current `medicine_request_id`; discard results if the user has started a new search

This way users see pharmacy responses as soon as they arrive—no need to send another message.

---

### Pharmacy Responses Not Showing in Chatbot – Checklist

If pharmacy responses exist in the database but don't appear in the chat, verify:

| Check | Action |
|-------|--------|
| **1. Polling** | When the chat returns `polling_enabled: true`, start polling `poll_url` every 10 seconds. When the poll returns non‑empty data, show pharmacy responses in the UI. |
| **2. Render `pharmacy_responses`** | When the API returns `pharmacy_responses: [...]`, render them (cards, list, etc.). Don’t rely only on the `response` text. |
| **3. Session consistency** | Use the same `session_id` and `conversation_id` for the whole flow. Avoid resetting the session or using a new ID on refresh. |
| **4. Follow-up message** | Alternatively, ask the user to send a message (e.g. “any updates?”). The API will fetch and return pharmacy responses for that conversation. |
| **5. Request ownership** | Include `conversation_id` or `session_id` when polling `GET /request/{id}/ranked/`. |

**Flow summary:**
- Chat API returns data only when the frontend sends a request.
- To surface new pharmacy responses: either poll `poll_url` or send another chat message.
- When the backend returns `pharmacy_responses`, the frontend must render them.

#### Check Drug Interactions (UC-P08)
**POST** `/api/chatbot/check-interactions/`

**Request Body:**
```json
{
  "medicines": ["aspirin", "ibuprofen", "warfarin"]
}
```

**Response:**
```json
{
  "medicines": ["aspirin", "ibuprofen", "warfarin"],
  "interactions": [
    {
      "medicine_a": "aspirin",
      "medicine_b": "ibuprofen",
      "severity": "moderate",
      "description": "Increased stomach bleeding risk"
    }
  ],
  "has_interactions": true,
  "disclaimer": "This is not a substitute for professional medical advice. Consult a healthcare provider."
}
```

#### Rate Pharmacy (UC-P12)
**POST** `/api/chatbot/rate-pharmacy/`

**Request Body:**
```json
{
  "pharmacy_id": "simed-01",
  "rating": 5,
  "response_id": "uuid",
  "notes": "Fast service"
}
```

- `pharmacy_id` (required)
- `rating` (required, 1–5)
- `response_id` (optional) – links rating to a specific visit
- `notes` (optional)

**Response:**
```json
{
  "message": "Thank you for your rating",
  "pharmacy_id": "simed-01",
  "pharmacy_name": "Simed Pharmacy",
  "rating": 4.5,
  "rating_count": 12
}
```

#### Get Pharmacy Responses
**GET** `/api/chatbot/request/{request_id}/responses/?conversation_id={conversation_id}`

**Query Parameters:**
- `conversation_id` (required): User's conversation ID for security

**Response:** Array of pharmacy responses with `total_time_minutes` included

#### Pharmacist Dashboard
**GET** `/api/chatbot/pharmacist/requests/?pharmacist_id={pharmacist_id}`

**Response:**
```json
[
  {
    "request_id": "uuid",
    "short_request_id": "A1B2C3D4",
    "request_type": "symptom",
    "medicine_names": ["paracetamol", "ibuprofen"],
    "symptoms": "I have a headache",
    "location_address": "4 St Kilda, Mt Pleasant",
    "location_suburb": "Mt Pleasant",
    "location_latitude": -17.8394,
    "location_longitude": 31.0543,
    "created_at": "2026-01-17T12:00:00Z",
    "expires_at": "2026-01-17T14:00:00Z",
    "status": "broadcasting",
    "has_responded": false,
    "has_declined": false,
    "response_count": 0,
    "distance_km": 2.5
  }
]
```

Display as: `Request #A1B2C3D4 - Paracetamol 500mg | Patient nearby (Mt Pleasant) - X min ago`

#### Submit Pharmacy Response
**POST** `/api/chatbot/pharmacist/response/{request_id}/`

**Request Body:**
```json
{
  "pharmacist_id": "uuid",
  "medicine_available": true,
  "price": "5.00",
  "quantity": 100,
  "expiry_date": "2026-08-30",
  "preparation_time": 10,
  "alternative_medicines": ["aspirin"],
  "notes": "Available in stock"
}
```

**Response:**
```json
{
  "response_id": "uuid",
  "pharmacy_id": "simed-01",
  "pharmacy_name": "Simed Pharmacy",
  "medicine_available": true,
  "price": "5.00",
  "quantity": 100,
  "expiry_date": "2026-08-30",
  "preparation_time": 10,
  "distance_km": 2.5,
  "estimated_travel_time": 5,
  "alternative_medicines": ["aspirin"],
  "medicine_responses": [],
  "notes": "Available in stock",
  "submitted_at": "2026-01-17T12:00:00Z"
}
```

#### Decline Request
**POST** `/api/chatbot/pharmacist/decline/{request_id}/`

**Request Body:**
```json
{
  "pharmacist_id": "uuid",
  "reason": "Out of stock"
}
```

**Response:** `201 Created` or `200 OK` (if already declined)

#### Pharmacy Inventory
**GET** `/api/chatbot/pharmacist/inventory/?pharmacist_id={pharmacist_id}`

**Response:**
```json
{
  "pharmacy_id": "ph-001",
  "pharmacy_name": "HealthFirst Pharmacy",
  "summary": {
    "total_medicines": 45,
    "in_stock": 25,
    "low_stock": 12,
    "out_of_stock": 8
  },
  "items": [
    {
      "medicine_name": "paracetamol",
      "quantity": 50,
      "low_stock_threshold": 10,
      "status": "in_stock",
      "updated_at": "2026-03-02T12:00:00Z"
    }
  ]
}
```

**POST** `/api/chatbot/pharmacist/inventory/` (Update Inventory)

**Price is required** for each item so patients see and rank by price; keep prices updated.

**Request Body:**
```json
{
  "pharmacist_id": "uuid",
  "items": [
    { "medicine_name": "paracetamol", "quantity": 100, "low_stock_threshold": 10, "price": 5.00 },
    { "medicine_name": "ibuprofen", "quantity": 5, "low_stock_threshold": 10, "price": 3.50 }
  ]
}
```

**Response:** `200 OK` with updated items. If any item is missing `price`, the API returns `400` with an error message.

#### Record purchase (decrement stock when user buys)
When a user buys medicine from a pharmacy (e.g. collects in-store or completes an order), call this so inventory is decremented and future availability is accurate.

**POST** `/api/chatbot/record-purchase/`

**Request Body:**
```json
{
  "pharmacy_id": "uuid",
  "items": [
    { "medicine_name": "paracetamol", "quantity": 2 },
    { "medicine_name": "ibuprofen", "quantity": 1 }
  ]
}
```

Optional (for audit): `response_id`, `medicine_request_id`.

**Response:** `200 OK`
```json
{
  "message": "Purchase recorded; inventory decremented.",
  "pharmacy_id": "uuid",
  "items": [
    { "medicine_name": "paracetamol", "quantity_sold": 2, "previous_quantity": 50, "new_quantity": 48 },
    { "medicine_name": "ibuprofen", "quantity_sold": 1, "previous_quantity": 10, "new_quantity": 9 }
  ]
}
```

If a medicine has no inventory record, that item is returned with `new_quantity: 0` and a message that stock was not decremented (pharmacy should add/update stock via pharmacist inventory first).

#### Reserve medicine (lock stock for 2 hours)
When the user clicks "Reserve" on a pharmacy from **live inventory** results, call this to lock stock. Concurrency-safe.

**POST** `/api/chatbot/reserve/`

**Request Body:**
```json
{
  "pharmacy_id": "pharmacy-uuid-or-id",
  "medicine_name": "paracetamol",
  "quantity": 2,
  "conversation_id": "uuid",
  "session_id": "optional-if-no-conversation",
  "patient_phone": "optional"
}
```
- **medicine_name** can be omitted if **conversation_id** is sent: the backend uses the first medicine from that conversation’s search (e.g. from the last “pharmacy results” response). This avoids "Medicine name is missing" when the frontend only sends pharmacy_id + conversation_id.

**Response:** `201 Created`
```json
{
  "success": true,
  "reservation_id": "uuid",
  "expires_at": "2026-03-03T15:00:00Z",
  "message": "Reservation confirmed. Please pick up within 2 hours. 2 x paracetamol at Pharmacy Name.",
  "pharmacy_name": "Pharmacy Name",
  "medicine_name": "paracetamol",
  "quantity": 2
}
```

If stock is insufficient: `400` with `error`: `"Only N available (you requested M)"`.

#### Pharmacist: reservations
- **GET** `/api/chatbot/pharmacist/reservations/?pharmacist_id=uuid` – list pending/confirmed reservations for the pharmacy.
- **POST** `/api/chatbot/pharmacist/reservations/{reservation_id}/confirm/` – body `{ "pharmacist_id": "uuid" }` – confirm reservation (ready for pickup).
- **POST** `/api/chatbot/pharmacist/reservations/{reservation_id}/complete/` – body `{ "pharmacist_id": "uuid" }` – mark picked up: decrements stock and releases reserved quantity.

### Live inventory flow (implementation guide)
When the user has **location + medicine names** (e.g. after confirming symptoms or typing "I need amoxicillin 500mg" and sharing location):

1. Backend **queries live inventory** (pharmacies with stock where `quantity - reserved_quantity > 0`). Only pharmacies with **latitude and longitude** set are included; others are skipped.
2. Results are **ranked**: 40% distance, 30% price, 20% availability, 10% rating.
3. Chat response includes `pharmacy_responses` and `from_live_inventory: true` (no `medicine_request_id`).
4. Frontend shows pharmacy cards with **Reserve** buttons; on Reserve, call **POST /api/chatbot/reserve/**.
5. Pharmacist dashboard: **GET pharmacist/reservations/** for pending list; **confirm** then **complete** when patient picks up (stock is decremented on complete).

If no live inventory results, the backend falls back to creating a medicine request and waiting for pharmacy responses (existing flow).

**Symptom-based and prescription requests: live inventory + pharmacist responses**
- For symptom or prescription flows (location + suggested medicines), the backend **merges** two sources in one response:
  1. **Live inventory** – pharmacies with the requested medicines in stock (from `PharmacyInventory`).
  2. **Pharmacist responses** – any pharmacies that responded to a request for the same conversation (availability, **alternative medicines**, notes).
- Pharmacies from live inventory appear first (with `from_live_inventory: true`). Pharmacies that only responded (e.g. with alternatives, no inventory row) are appended with `from_pharmacist_response: true`. Each pharmacy appears at most once.
- When the user checks for updates (e.g. “any updates?”, or polling), the same merge runs: live inventory + latest pharmacist responses so the patient sees both stock and pharmacist suggestions (including alternatives) as soon as they respond.

**When another pharmacy updates inventory after the patient already saw results**
- **Live inventory path** (`from_live_inventory: true`): Results are a **point-in-time snapshot**. If another pharmacy adds or updates stock later, the patient will not see them until they **search again** (e.g. tap “Search again” or send another message with the same location). The frontend can offer a “Refresh results” button that re-sends the last search (same location + medicines) so the backend runs live inventory again and returns an updated list.
- **Request path** (medicine request + polling): When the backend returns `poll_url`, polling **GET** `poll_url` returns pharmacies that responded to that request. Pharmacies that later update only their **inventory** (and do not submit a response to that request) do not appear in poll results; the patient would need to start a new search to see live inventory from those pharmacies.

**Inventory: price required and kept updated**
- **POST** `/api/chatbot/pharmacist/inventory/` **requires** a numeric `price` for each item. Prices are used for ranking and patient display; keep them updated when costs change.
- **GET** pharmacist inventory returns `price_missing: true` for any item where price is null (e.g. legacy rows). The dashboard should prompt the pharmacy to set/update price so patients see correct prices and ranking works (30% weight).

### Availability and stock flow (summary)
| Step | Who | Action |
|------|-----|--------|
| User asks for medicine + location | Patient | Chat → **live inventory** queried first; if results, show ranked list with Reserve |
| No live stock | Backend | Creates medicine request → broadcast → pharmacists respond |
| Pharmacies respond | Pharmacist | `POST .../pharmacist/response/{request_id}/` with availability, price, notes |
| Displayed availability | Backend | **Live path**: from `PharmacyInventory` (quantity − reserved). **Request path**: from responses + inventory |
| Reserve | Patient | `POST .../reserve/` → locks stock for 2 hours; pharmacist sees in reservations |
| Stock inflow | Pharmacist | `POST .../pharmacist/inventory/` to set/update quantities and **price** (price required) |
| Pick-up complete | Pharmacist | `POST .../pharmacist/reservations/{id}/complete/` → decrements stock and reserved |
| Sale without reservation | App / pharmacy | `POST .../record-purchase/` with `pharmacy_id` and `items` |

Run **`python manage.py expire_reservations`** (e.g. cron every 10 min) to expire old reservations and release reserved stock.

## Frontend Implementation

### 1. Chat Flow

```javascript
// Step 1: User sends message
const sendMessage = async (message, conversationId, sessionId) => {
  const response = await fetch('http://localhost:8000/api/chatbot/chat/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
      session_id: sessionId
    })
  });
  
  const data = await response.json();
  
  // Step 2: Check if location is required
  if (data.requires_location) {
    // Show location input UI
    showLocationInput();
  }
  
  // Step 3: Check if pharmacy responses are available
  if (data.pharmacy_responses && data.pharmacy_responses.length > 0) {
    // Display pharmacy responses
    displayPharmacyResponses(data.pharmacy_responses, data.recommendation);
  }
  
  // Step 4: Store conversation_id for future requests
  if (data.conversation_id) {
    localStorage.setItem('conversation_id', data.conversation_id);
  }
  
  // Step 5: Store request_id if present
  if (data.medicine_request_id) {
    localStorage.setItem('current_request_id', data.medicine_request_id);
  }
  
  return data;
};
```

### 2. Location Handling

```javascript
// When user provides location (geolocation)
const sendLocation = async (latitude, longitude, address) => {
  const conversationId = localStorage.getItem('conversation_id');
  const sessionId = localStorage.getItem('session_id') || generateSessionId();
  
  const response = await fetch('http://localhost:8000/api/chatbot/chat/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: 'okay', // or user's confirmation
      conversation_id: conversationId,
      session_id: sessionId,
      location_latitude: latitude,
      location_longitude: longitude,
      location_address: address
    })
  });
  
  return await response.json();
};

// When user manually enters address
const sendManualAddress = async (address) => {
  const conversationId = localStorage.getItem('conversation_id');
  
  const response = await fetch('http://localhost:8000/api/chatbot/chat/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: address, // User's address text
      conversation_id: conversationId,
      location_address: address
    })
  });
  
  return await response.json();
};
```

### 3. Display Pharmacy Responses

```javascript
const displayPharmacyResponses = (responses, recommendation) => {
  // Show recommendation if available
  if (recommendation) {
    showRecommendation(recommendation);
  }
  
  // Display each pharmacy response
  responses.forEach((response, index) => {
    const card = createResponseCard({
      rank: index + 1,
      pharmacyName: response.pharmacy_name,
      available: response.medicine_available,
      price: response.price,
      distance: response.distance_km,        // ✅ Always available
      travelTime: response.estimated_travel_time, // ✅ Always available
      totalTime: response.total_time_minutes,    // ✅ Always available
      preparationTime: response.preparation_time,
      alternatives: response.alternative_medicines,
      notes: response.notes
    });
    
    document.getElementById('responses-container').appendChild(card);
  });
};

const createResponseCard = (data) => {
  const card = document.createElement('div');
  card.className = 'pharmacy-response-card';
  card.innerHTML = `
    <div class="rank-badge">#${data.rank}</div>
    <h3>${data.pharmacyName}</h3>
    <div class="status ${data.available ? 'available' : 'unavailable'}">
      ${data.available ? '✅ Available' : '❌ Not Available'}
    </div>
    ${data.available ? `
      <div class="price">Price: $${data.price}</div>
      <div class="time-info">
        <span>📍 Distance: ${data.distance?.toFixed(1) || 'N/A'} km</span>
        <span>⏱️ Travel Time: ${data.travelTime || 'N/A'} min</span>
        <span>⏳ Prep Time: ${data.preparationTime} min</span>
        <span>🕐 Total Time: ${data.totalTime || 'N/A'} min</span>
      </div>
    ` : ''}
    ${data.alternatives?.length > 0 ? `
      <div class="alternatives">
        <strong>Alternatives:</strong>
        ${data.alternatives.map(alt => `
          <div>${alt.medicine} (suggested by ${alt.suggested_by})</div>
        `).join('')}
      </div>
    ` : ''}
    ${data.notes ? `<div class="notes">${data.notes}</div>` : ''}
  `;
  return card;
};
```

### 4. Pharmacist Dashboard

**Seeing a request:** The dashboard lists requests that are (1) **within 50 km** of the pharmacist’s pharmacy and active, or (2) completed/expired where this pharmacist has responded. Each request has a **short ID** (e.g. `#0b142651`) from `short_request_id` in the API (first 8 characters of `request_id`). The **patient chat** response includes the same `short_request_id` so you can show “Request #0B142651” and it matches the dashboard. **Pending** = no pharmacy has responded yet; **Responded** = at least one pharmacy has responded. If you don’t see a request, check the **Responded** tab (once any pharmacy responds, the request moves there) and ensure your pharmacy is within 50 km of the patient location.

```javascript
// Fetch requests for pharmacist
const fetchPharmacistRequests = async (pharmacistId) => {
  const response = await fetch(
    `http://localhost:8000/api/chatbot/pharmacist/requests/?pharmacist_id=${pharmacistId}`
  );
  
  const requests = await response.json();
  
  // Display requests
  requests.forEach(request => {
    const card = createRequestCard(request);
    document.getElementById('requests-container').appendChild(card);
  });
};

const createRequestCard = (request) => {
  const card = document.createElement('div');
  card.className = 'request-card';
  card.innerHTML = `
    <div class="request-header">
      <h3>${request.request_type === 'symptom' ? 'Symptom Request' : 'Medicine Request'}</h3>
      <span class="status ${request.status}">${request.status}</span>
    </div>
    ${request.symptoms ? `<p><strong>Symptoms:</strong> ${request.symptoms}</p>` : ''}
    ${request.medicine_names?.length > 0 ? `
      <p><strong>Medicines:</strong> ${request.medicine_names.join(', ')}</p>
    ` : ''}
    <p><strong>Location:</strong> ${request.location_address}</p>
    ${request.distance_km ? `<p><strong>Distance:</strong> ${request.distance_km.toFixed(1)} km</p>` : ''}
    <p><strong>Responses:</strong> ${request.response_count}</p>
    ${request.has_responded ? '<span class="responded-badge">✓ Responded</span>' : ''}
    <button onclick="respondToRequest('${request.request_id}')">Respond</button>
  `;
  return card;
};

// Submit response
const respondToRequest = async (requestId) => {
  const pharmacistId = localStorage.getItem('pharmacist_id');
  
  const response = await fetch(
    `http://localhost:8000/api/chatbot/pharmacist/response/${requestId}/`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pharmacist_id: pharmacistId,
        medicine_available: true,
        price: document.getElementById('price-input').value,
        preparation_time: parseInt(document.getElementById('prep-time-input').value),
        alternative_medicines: getAlternatives(),
        notes: document.getElementById('notes-input').value
      })
    }
  );
  
  const data = await response.json();
  console.log('Response submitted:', data);
  // Refresh requests list
  fetchPharmacistRequests(pharmacistId);
};
```

## Important Notes

### 1. Request-Response Matching
- **ALWAYS** use `request_id` to match responses to requests
- Never show responses from one `request_id` for another `request_id`
- Store `request_id` when request is created
- Only fetch responses using the correct `request_id`

### 2. Distance and Time
- `distance_km`: Always calculated from patient location to pharmacy
- `estimated_travel_time`: Always calculated based on distance
- `total_time_minutes`: Always includes preparation + travel time
- Display "N/A" only if values are null (shouldn't happen)

### 3. Security
- Always include `conversation_id` when fetching responses
- This ensures users only see their own responses
- Use `session_id` for anonymous users

### 4. Symptom Description Flow (Step-by-Step)
1. **Patient describes symptoms** (e.g., "I have fever, headache and body pains")
   - AI analyzes symptoms, suggests medicines (e.g., Paracetamol for fever, Ibuprofen for pain)
   - Response: `suggested_medicines: ["paracetamol", "ibuprofen"]`, `requires_location: false`
   - AI asks: "Would you like to search for these medicines?"

2. **Patient selects/confirms medicines** ("Yes", "I want paracetamol and ibuprofen", "All of them")
   - AI acknowledges, then asks for location
   - Response: `requires_location: true`
   - Optionally send `selected_medicines` in the next request

3. **Patient provides location**
   - Request created with `medicine_names: [selected medicines]`, `symptoms: "..."`, `request_type: "symptom"`
   - Backend returns `medicine_request_id`, `request_sent_to_pharmacies: true`

4. **Frontend polls** `GET /request/{request_id}/ranked/` for pharmacy responses:
   - **Immediately after request**: "Request has been sent. Waiting for pharmacies to respond."
   - **As soon as 1+ pharmacies respond**: Show responses in chronological order (`ranking_pending: true`)
   - **2 minutes after request creation**: Responses are MCDA-ranked (`ranking_pending: false`)

### 5. User Flow (Legacy/Direct)
1. User says "I have a headache" → AI asks for location
2. User provides location → Request created → Sent to pharmacies
4. Backend returns `medicine_request_id` with "waiting for pharmacies to respond" (`pharmacy_responses: []`)
5. Frontend polls `GET /api/chatbot/request/{request_id}/ranked/?conversation_id=...` until responses arrive
6. Pharmacies respond → Polling returns ranked responses
7. User sees ranked responses with distance, time, price

**Note:** Each request has its own `request_id`. Use the `medicine_request_id` from the response when polling.

### 5. Patient Dashboard (MediConnect) API

All patient dashboard endpoints require **`session_id`** or **`conversation_id`** (query param or body) so the backend can scope data to the current patient.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/chatbot/patient/dashboard/stats/` | GET | Dashboard stats: active_requests, fulfilled_count, expired_count |
| `/api/chatbot/patient/requests/` | GET | List my requests (optional `?status=all\|active\|fulfilled\|expired`, `?limit=50`) |
| `/api/chatbot/patient/requests/<request_id>/` | GET | Single request with ranked pharmacy_responses |
| `/api/chatbot/patient/saved-medicines/` | GET | List saved medicines |
| `/api/chatbot/patient/saved-medicines/` | POST | Add saved medicine: `{ "medicine_name": "...", "display_name": "..." (optional) }` |
| `/api/chatbot/patient/saved-medicines/remove/?medicine_name=...` | POST/DELETE | Remove one; or body `{ "medicine_name": "..." }` |
| `/api/chatbot/patient/notifications/` | GET | List notifications (`?type=all\|pharmacy_response\|...`, `?unread_only=true`) |
| `/api/chatbot/patient/notifications/mark-read/` | POST | Mark read: body `{ "id": 1 }` or `{ "ids": [1,2] }` or empty for all |
| `/api/chatbot/patient/profile/` | GET / PATCH | Get or update patient profile and preferences |

**Dashboard stats response example:**
```json
{
  "active_requests": 3,
  "fulfilled_count": 12,
  "expired_count": 2,
  "avg_savings": null,
  "time_saved_hrs": null
}
```

**My requests list item:**
```json
{
  "request_id": "uuid",
  "short_request_id": "0B142651",
  "medicine_names": ["amoxicillin"],
  "symptoms": "",
  "location_address": "Harare CBD",
  "submitted_at": "2026-03-06T09:15:00Z",
  "response_count": 2,
  "pharmacy_names": ["HealthPlus", "MedCity"],
  "best_price": "4.20",
  "best_pharmacy_id": "healthplus-01",
  "best_pharmacy_name": "HealthPlus",
  "best_medicine_name": "amoxicillin",
  "status": "responses_received"
}
```

**Profile PATCH** accepts: `display_name`, `email`, `phone`, `date_of_birth`, `home_area`, `preferred_language`, `allergies`, `conditions`, `max_search_radius_km`, `sort_results_by`, and notification/preference flags.

---

## How the frontend should fetch data (Patient vs Pharmacy)

Use this as the single reference for **what to call** and **what to send** for each app.

### Base URL and identifiers

- **Base URL:** `{API_BASE}/api/chatbot/` (e.g. `https://yourserver.com/api/chatbot/`).
- **Patient app** identifies the user with **`session_id`** and/or **`conversation_id`**. Send one of these on every patient request (query param or body). No login required for anonymous use.
- **Pharmacy/Pharmacist app** identifies the pharmacist with **`pharmacist_id`** (UUID). Send it on every pharmacist request (query param or body). Use **pharmacist/login/** to obtain or validate the pharmacist.

---

### Patient app – what to fetch

| Data needed | How to fetch |
|-------------|--------------|
| **Register / update patient** | **POST** `/register/patient/` with optional `session_id` or `conversation_id`, and profile fields: `display_name`, `email`, `phone`, `date_of_birth`, `home_area`, `preferred_language`, `allergies`, `conditions`, etc. If neither ID is sent, backend creates a new `session_id` and returns it—store it for all later requests. |
| **Chat + search** | **POST** `/chat/` with `message`, and optionally `session_id`, `conversation_id`, `location_latitude`, `location_longitude`, `location_address`, `start_new_search`, `selected_medicines`. |
| **Conversation history** | **GET** `/conversation/{conversation_id}/` to load messages for a conversation. |
| **Pharmacy responses for a request** | **GET** `/request/{request_id}/responses/?conversation_id={conversation_id}` or **GET** `/request/{request_id}/ranked/?conversation_id={conversation_id}&limit=3`. Use the `poll_url` from chat when waiting for responses. |
| **Dashboard stats** | **GET** `/patient/dashboard/stats/?session_id=...` or `?conversation_id=...` (or in body). |
| **My requests list** | **GET** `/patient/requests/?session_id=...` (optional `?status=active|fulfilled|expired|all`, `?limit=50`). |
| **Single request detail** | **GET** `/patient/requests/{request_id}/?session_id=...` (or conversation_id). |
| **Saved medicines** | **GET** `/patient/saved-medicines/?session_id=...`; **POST** same URL with `{ "medicine_name": "...", "display_name": "..." }` to add. |
| **Remove saved medicine** | **POST** or **DELETE** `/patient/saved-medicines/remove/?medicine_name=...` (or body `{ "medicine_name": "..." }`) with session_id. |
| **Notifications** | **GET** `/patient/notifications/?session_id=...` (optional `?type=...`, `?unread_only=true`). |
| **Mark notifications read** | **POST** `/patient/notifications/mark-read/` with body `{ "id": 1 }` or `{ "ids": [1,2] }` or `{}` for all, plus session_id. |
| **Profile** | **GET** `/patient/profile/?session_id=...`; **PATCH** same URL with fields to update. |
| **Reserve medicine** | **POST** `/reserve/` with `pharmacy_id`, `medicine_name` (or `conversation_id` to use last request), `conversation_id`, optional `quantity`. |
| **Record purchase** | **POST** `/record-purchase/` with `pharmacy_id`, `conversation_id`, `items: [{ "medicine_name", "quantity", "price" }]`. |
| **Rate pharmacy** | **POST** `/rate-pharmacy/` with `pharmacy_id`, `conversation_id`, `rating`, optional `review`. |
| **Check interactions** | **POST** `/check-interactions/` with list of medicine names. |
| **Alternatives** | **POST** `/alternatives/` with `medicine_name`, `session_id` (or conversation_id). |
| **Upload prescription** | **POST** `/upload-prescription/` (multipart) for OCR flow. |

**Patient flow in short:**  
1) Persist `session_id` (and after first chat, `conversation_id`).  
2) Use **POST /chat/** for all chat and search; when the response has `polling_enabled` and `poll_url`, poll that URL until `pharmacy_responses` are returned.  
3) Use **/patient/** endpoints for dashboard, requests, saved medicines, notifications, profile—always with `session_id` or `conversation_id`.

#### Patient notifications when pharmacies respond

- **Backend behavior:**  
  - Every time a pharmacist submits a response via **POST** `/pharmacist/response/{request_id}/`, the backend also creates a `PatientNotification` with `notification_type = "pharmacy_response"`, linked to that `request_id` and `response_id`.  
  - The notification includes:  
    - `title`: e.g. `"Simed Pharmacy responded to your request #0B142651"`  
    - `body`: e.g. `"Ibuprofen 400mg available for $3.50."`
- **Frontend behavior:**  
  - Poll or fetch periodically: **GET** `/patient/notifications/?session_id=...&type=pharmacy_response&unread_only=true`.  
  - Display each notification as a clickable item, e.g.:  
    - **“Simed Pharmacy responded to your request #0B142651 – Ibuprofen 400mg available for $3.50.”**  
  - When user taps/clicks a notification:  
    - Use `related_request_id` to navigate to your **request detail / results view**, or call **GET** `/patient/requests/{request_id}/?session_id=...` and then show the ranked `pharmacy_responses`.  
  - Mark notifications read via **POST** `/patient/notifications/mark-read/` with `{ "id": notificationId }` or `{ "ids": [...] }`.

---

### Pharmacy / Pharmacist app – what to fetch

| Data needed | How to fetch |
|-------------|--------------|
| **Login** | **POST** `/pharmacist/login/` with credentials; store returned `pharmacist_id` (and pharmacy info) for subsequent calls. |
| **Pharmacist profile** | **GET** `/pharmacist/{pharmacist_id}/` to get profile and pharmacy details. |
| **Requests to respond to** | **GET** `/pharmacist/requests/?pharmacist_id={pharmacist_id}`. Optional `?pharmacy_id=...`, `?status=pending|responded|expired`. |
| **Submit response** | **POST** `/pharmacist/response/{request_id}/` with body: `pharmacist_id`, `medicine_available`, `price`, `quantity`, `expiry_date`, `preparation_time`, optional `alternative_medicines`, `notes`. |
| **Decline request** | **POST** `/pharmacist/decline/{request_id}/` with body: `pharmacist_id`, `reason`. |
| **Inventory list** | **GET** `/pharmacist/inventory/?pharmacist_id={pharmacist_id}`. |
| **Update inventory** | **POST** `/pharmacist/inventory/` with `pharmacist_id` and `items: [{ "medicine_name", "quantity", "price", "low_stock_threshold" }]`. |
| **Reservations list** | **GET** `/pharmacist/reservations/?pharmacist_id={pharmacist_id}`. |
| **Confirm reservation** | **POST** `/pharmacist/reservations/{reservation_id}/confirm/` with body `{ "pharmacist_id": "..." }`. |
| **Complete reservation (pick-up)** | **POST** `/pharmacist/reservations/{reservation_id}/complete/` with body `{ "pharmacist_id": "..." }` (decrements stock). |

#### “Get directions” button for pharmacies (frontend)

- Anywhere you show a chosen pharmacy (chat results, request detail, dashboard “best result”, or from a notification), show a **Get directions** button.  
- **Frontend implementation example (web):**

```js
const openDirections = (pharmacyName, suburbOrAddress) => {
  const query = encodeURIComponent(`${pharmacyName} ${suburbOrAddress || ''}`);
  window.open(`https://www.google.com/maps/search/?api=1&query=${query}`, '_blank');
};
```

- Wire this to the directions button on each pharmacy card / row using:
  - `pharmacy_name` from `pharmacy_responses`
  - `location_address` or `location_suburb` from the request (or pharmacy record)
- Later, if the backend adds exact coordinates for pharmacies, you can switch the URL to `...&query=lat,long` without changing the UI.

**Pharmacy flow in short:**  
1) Log in via **POST /pharmacist/login/** and store `pharmacist_id`.  
2) **GET /pharmacist/requests/** to show pending requests; **GET /pharmacist/inventory/** and **POST /pharmacist/inventory/** to manage stock.  
3) **POST /pharmacist/response/{request_id}/** or **/pharmacist/decline/{request_id}/** to respond.  
4) **GET /pharmacist/reservations/** for pending pick-ups; **confirm** then **complete** to finalise and decrement stock.

---

### 6. Prescription Flow
1. User says "I need paracetamol" → AI asks for prescription
2. User uploads prescription → OCR extracts medicines
3. Request created with extracted medicines
4. Pharmacies respond with availability and pricing

## Testing Checklist

- [ ] Chat messages send correctly
- [ ] Location is captured (geolocation and manual)
- [ ] Requests are created when location is provided
- [ ] Pharmacy responses display with distance/time
- [ ] Responses are matched to correct request_id
- [ ] Pharmacist dashboard shows requests
- [ ] Pharmacist can submit responses
- [ ] Distance and time are always calculated
- [ ] Alternative medicines display correctly
- [ ] Recommendation shows when available

## Example Complete Flow

```javascript
// 1. User starts conversation
const sessionId = generateSessionId();
localStorage.setItem('session_id', sessionId);

// 2. User says "I have a headache"
const response1 = await sendMessage("I have a headache", null, sessionId);
// Response: { requires_location: true, ... }

// 3. User provides location
const response2 = await sendLocation(-17.8394, 31.0543, "Harare");
// Response: { medicine_request_id: "uuid", request_sent_to_pharmacies: true, ... }

// 4. Pharmacies respond (polling or websocket)
const requestId = response2.medicine_request_id;
const conversationId = response2.conversation_id;

// 5. Fetch responses
const responses = await fetch(
  `http://localhost:8000/api/chatbot/request/${requestId}/ranked/?conversation_id=${conversationId}`
);
const data = await responses.json();

// 6. Display responses
displayPharmacyResponses(data, null);
```

## Error Handling

```javascript
try {
  const response = await sendMessage(message, conversationId, sessionId);
  
  if (response.error) {
    // Handle API errors
    showError(response.error);
  }
  
  if (response.requires_location && !hasLocation) {
    // Show location input
    showLocationInput();
  }
  
} catch (error) {
  console.error('Error:', error);
  showError('Failed to send message. Please try again.');
}
```

## Backend Operations (Cron Jobs)

Run these management commands for full use case support:

| Command | Schedule | Purpose |
|---------|----------|---------|
| `python manage.py expire_requests` | Every 5–10 min | UC-S07: Mark requests expired when no pharmacies respond by deadline |
| `python manage.py clean_expired_sessions` | Daily | UC-S09: Delete expired anonymous session data (privacy) |

Example cron (Linux):
```cron
*/10 * * * * cd /path/to/pharmacybackend && python manage.py expire_requests
0 2 * * * cd /path/to/pharmacybackend && python manage.py clean_expired_sessions --hours 24
```

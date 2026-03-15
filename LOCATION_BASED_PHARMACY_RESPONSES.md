# Location-Based Pharmacy Response Flow

## Overview

When a user provides their location along with a medicine request or symptom description, the system now automatically:
1. Creates a medicine request
2. Broadcasts it to nearby pharmacies (pharmacies submit responses via API)
3. Uses a ranking algorithm to select the top 3 responses
4. Returns the top 3 ranked pharmacy responses instead of just an AI chat response

## How It Works

### Step 1: User Sends Message with Location

**Request:**
```json
{
  "message": "I have a headache",
  "session_id": "session_123",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335,
  "location_address": "Harare, Zimbabwe"
}
```

### Step 2: System Creates Medicine Request

The system automatically:
- Detects medicine/symptom keywords in the message
- Creates a `MedicineRequest` with the location
- Sets status to `'broadcasting'` - ready for pharmacies to respond
- **Note**: Pharmacies must submit responses via the API endpoint (see below)

### Step 3: Ranking Algorithm

When pharmacy responses are received, they are automatically ranked using an algorithm that considers:
- **Availability** (most important - unavailable medicines rank last)
- **Total Time** (preparation + travel time)
- **Price** (lower is better)
- **Distance** (closer is better)

Only the **top 3 ranked responses** are returned to the user.

See [PHARMACY_RANKING_ALGORITHM.md](./PHARMACY_RANKING_ALGORITHM.md) for detailed algorithm documentation.

### Step 4: Response Format

**When Location is Provided and Pharmacy Responses Exist:**

```json
{
  "response": "✅ Your request has been sent to nearby pharmacies! I found 3 pharmacies with available options. Here are the responses:",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "medicine_search",
  "requires_location": false,
  "suggested_medicines": ["paracetamol"],
  "medicine_request_id": "uuid",
  "request_sent_to_pharmacies": true,
  "pharmacy_responses": [
    {
      "rank": 1,
      "response_id": "uuid",
      "pharmacy_id": "ph-001",
      "pharmacy_name": "HealthFirst Pharmacy",
      "pharmacist_id": null,
      "pharmacist_name": "Unknown",
      "medicine_available": true,
      "price": "4.50",
      "preparation_time": 15,
      "distance_km": 1.2,
      "estimated_travel_time": 7,
      "total_time_minutes": 22,
      "ranking_score": 63,
      "alternative_medicines": [],
      "notes": "",
      "submitted_at": "2026-01-12T12:20:00Z"
    },
    {
      "response_id": "uuid",
      "pharmacy_id": "ph-002",
      "pharmacy_name": "City Care Pharmacy",
      "medicine_available": true,
      "price": "3.80",
      "preparation_time": 30,
      "distance_km": 0.8,
      "estimated_travel_time": 6,
      ...
    }
  ]
}
```

**When Location is Provided but No Responses Yet:**

```json
{
  "response": "✅ Your request has been sent to nearby pharmacies! We're waiting for pharmacies to respond...",
  "medicine_request_id": "uuid",
  "pharmacy_responses": [],
  "request_sent_to_pharmacies": true,
  "total_responses": 0,
  "status": "awaiting_responses"
}
```

**When Location is NOT Provided:**

```json
{
  "response": "I understand you're experiencing a headache. To help you find medication, could you please tell me what specific medicine you are looking for, or if you have any symptoms you'd like help with?",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "symptom_description",
  "requires_location": true,
  "suggested_medicines": [],
  "medicine_request_id": null,
  "request_sent_to_pharmacies": false
}
```

## Key Changes

### 1. Automatic Request Creation
- When location is provided AND message contains medicine/symptom keywords, a medicine request is automatically created
- Request status is set to `'broadcasting'` - ready for pharmacies to respond
- No need for separate API call to create request

### 2. Pharmacy Response Submission
- Pharmacies submit responses via API: `POST /api/chatbot/pharmacist/response/{request_id}/`
- Each pharmacy can submit one response per request
- System automatically calculates distance and travel time

### 3. Ranking Algorithm
- All responses are ranked using a sophisticated algorithm
- Considers: availability, total time, price, distance
- Only top 3 responses are returned to user
- Each response includes `rank` and `ranking_score` fields

### 4. Response Flag
- `request_sent_to_pharmacies: true` indicates request was created and sent to pharmacies
- `total_responses` shows number of pharmacy responses received
- Frontend can use this to display pharmacy cards/list instead of just chat message

## Frontend Integration

### Displaying Pharmacy Responses

```javascript
// In your Chatbot component
const handleResponse = (response) => {
  if (response.request_sent_to_pharmacies && response.pharmacy_responses) {
    // Show pharmacy response cards
    setPharmacyResponses(response.pharmacy_responses);
    setShowPharmacyList(true);
  } else {
    // Show normal chat message
    addMessageToChat(response.response);
  }
};
```

### Example UI Flow

1. **User sends:** "I have a headache" (no location)
   - **Response:** AI asks for location
   - **UI:** Show chat message with location request

2. **User sends:** "I have a headache" + location
   - **Response:** Pharmacy responses included
   - **UI:** Show pharmacy cards with:
     - Pharmacy name
     - Price
     - Distance
     - Total time (preparation + travel)
     - Availability status

## Pharmacy Response Fields

Each pharmacy response includes:

- `pharmacy_name`: Name of the pharmacy
- `medicine_available`: Boolean - is the medicine available?
- `price`: Price in local currency (if available)
- `preparation_time`: Minutes to prepare the medicine
- `distance_km`: Distance from user location
- `estimated_travel_time`: Estimated travel time in minutes
- `alternative_medicines`: List of alternative medicines if main one unavailable
- `notes`: Additional notes from pharmacist

## How Pharmacies Submit Responses

Pharmacies receive medicine requests and submit responses via:

**Endpoint:** `POST /api/chatbot/pharmacist/response/{request_id}/`

**Request Body:**
```json
{
  "pharmacist_id": "uuid",
  "medicine_available": true,
  "price": 4.50,
  "preparation_time": 15,
  "alternative_medicines": ["ibuprofen"],
  "notes": "Available in stock"
}
```

The system automatically:
- Calculates distance from patient location
- Estimates travel time
- Ranks the response when user requests results

## Ranking and Sorting

Pharmacy responses are automatically ranked using a multi-factor algorithm:
1. **Availability** (most important - unavailable = +1000 penalty)
2. **Total time** = `preparation_time` + `estimated_travel_time` (weight: 2x)
3. **Price** (weight: 10x, lower is better)
4. **Distance** (weight: 5x, closer is better)

Only the **top 3 ranked responses** are returned. See [PHARMACY_RANKING_ALGORITHM.md](./PHARMACY_RANKING_ALGORITHM.md) for details.

## Testing

To test the flow:

1. **Send message without location:**
   ```bash
   curl -X POST http://localhost:8000/api/chatbot/chat/ \
     -H "Content-Type: application/json" \
     -d '{"message": "I have a headache", "session_id": "test"}'
   ```
   Should return: `requires_location: true`

2. **Send message with location:**
   ```bash
   curl -X POST http://localhost:8000/api/chatbot/chat/ \
     -H "Content-Type: application/json" \
     -d '{
       "message": "I have a headache",
       "session_id": "test",
       "location_latitude": -17.8252,
       "location_longitude": 31.0335,
       "location_address": "Harare"
     }'
   ```
   Should return: `request_sent_to_pharmacies: true` with `pharmacy_responses` array

## Next Steps

1. **Frontend:** Update UI to display pharmacy response cards when `request_sent_to_pharmacies: true`
2. **Real Pharmacies:** Replace `simulate_pharmacy_responses()` with actual pharmacy notification system
3. **Ranking:** Use the `/api/chatbot/request/{request_id}/ranked/` endpoint for advanced ranking

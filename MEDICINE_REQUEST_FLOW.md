# Medicine Request Flow

## Overview

**Key Rule: Medicine requests are ONLY created when the patient provides their location.**

This ensures that:
- We can find nearby pharmacies
- Distance can be calculated
- Travel time can be estimated
- Requests are only sent to relevant pharmacies

## Complete Flow

### Step 1: Patient Sends Message (No Location)

**Request:**
```json
{
  "message": "I have a headache",
  "location_latitude": null,
  "location_longitude": null
}
```

**What Happens:**
- ✅ AI processes the message
- ✅ AI detects symptom/medicine intent
- ✅ AI asks for location (`requires_location: true`)
- ❌ **NO medicine request created**
- ❌ Nothing appears in pharmacy dashboard

**Response:**
```json
{
  "response": "What is your location?",
  "requires_location": true,
  "medicine_request_id": null
}
```

### Step 2: Patient Provides Location

**Request:**
```json
{
  "message": "I have a headache",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335,
  "location_address": "Harare, Zimbabwe"
}
```

**What Happens:**
1. ✅ System detects location coordinates
2. ✅ System detects medicine/symptom keywords
3. ✅ **Medicine request is CREATED**
4. ✅ Request status set to `'broadcasting'`
5. ✅ System queries nearby pharmacies (within 10km)
6. ✅ Request becomes visible in pharmacy dashboard
7. ✅ Pharmacies can now see and respond to the request

**Response:**
```json
{
  "response": "✅ Your request has been sent to nearby pharmacies!",
  "medicine_request_id": "uuid-here",
  "request_sent_to_pharmacies": true,
  "pharmacy_responses": []  // Empty until pharmacies respond
}
```

### Step 3: Pharmacies See Request in Dashboard

**Pharmacist Dashboard Query:**
```
GET /api/chatbot/pharmacist/requests/?pharmacist_id={pharmacist_id}
```

**Response:**
```json
[
  {
    "request_id": "uuid-here",
    "request_type": "symptom",
    "symptoms": "I have a headache",
    "location_address": "Harare, Zimbabwe",
    "status": "broadcasting",
    "has_responded": false,
    "created_at": "2026-01-12T12:00:00Z"
  }
]
```

### Step 4: Pharmacies Submit Responses

**Pharmacy Response:**
```
POST /api/chatbot/pharmacist/response/{request_id}/
{
  "pharmacist_id": "uuid",
  "medicine_available": true,
  "price": 4.50,
  "preparation_time": 15,
  "alternative_medicines": ["ibuprofen"],
  "notes": "Available in stock"
}
```

**What Happens:**
- ✅ System calculates distance from patient location
- ✅ System estimates travel time
- ✅ Response is saved and linked to request
- ✅ Request status updated to `'awaiting_responses'` or `'responses_received'`

### Step 5: Patient Gets Ranked Responses

When patient checks for responses (or in next message), system:
1. ✅ Gets all pharmacy responses for the request
2. ✅ Ranks them using algorithm (availability, time, price, distance)
3. ✅ Returns top 3 ranked responses

**Response:**
```json
{
  "pharmacy_responses": [
    {
      "rank": 1,
      "pharmacy_name": "HealthFirst Pharmacy",
      "medicine_available": true,
      "price": "3.80",
      "total_time_minutes": 20,
      "ranking_score": 63
    },
    {
      "rank": 2,
      ...
    },
    {
      "rank": 3,
      ...
    }
  ]
}
```

## Code Implementation

### Request Creation Logic

```python
# Location is REQUIRED
if data.get('location_latitude') and data.get('location_longitude'):
    # Only then create request
    medicine_request = create_medicine_request(...)
    # Request is immediately broadcasted to pharmacies
else:
    # No request created - AI asks for location
    requires_location = True
```

### Broadcasting Function

```python
def broadcast_to_pharmacies(medicine_request):
    """
    Queries nearby pharmacies (within 10km) and makes request visible.
    Request is immediately available in pharmacy dashboard.
    """
    # Query pharmacies within 10km radius
    # Request status='broadcasting' makes it visible
    # TODO: Add push notifications/emails
```

## Key Points

✅ **Location is REQUIRED** - No location = No request  
✅ **Request created immediately** when location provided  
✅ **Status = 'broadcasting'** - Makes it visible to pharmacies  
✅ **Nearby pharmacies queried** (within 10km radius)  
✅ **Dashboard shows request** - Pharmacies can respond  
✅ **Responses ranked** - Top 3 returned to patient  

## Testing the Flow

### Test 1: Without Location (No Request)
```bash
curl -X POST http://localhost:8000/api/chatbot/chat/ \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I have a headache",
    "session_id": "test"
  }'
```
**Expected:** `medicine_request_id: null`, `requires_location: true`

### Test 2: With Location (Request Created)
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
**Expected:** `medicine_request_id: "uuid"`, `request_sent_to_pharmacies: true`

### Test 3: Check Pharmacy Dashboard
```bash
GET /api/chatbot/pharmacist/requests/?pharmacist_id={pharmacist_id}
```
**Expected:** Request appears in list with `status: "broadcasting"`

## Summary

**The flow is correct:**
- ✅ Requests ONLY created when location provided
- ✅ Request immediately broadcasted to pharmacies
- ✅ Pharmacies see request in dashboard
- ✅ Pharmacies respond via API
- ✅ Responses ranked and top 3 returned

This ensures efficient matching between patients and nearby pharmacies!

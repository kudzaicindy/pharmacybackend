# Why Requests Don't Appear in Pharmacy Dashboard

## The Issue

You sent: **"I have a headache"** without location coordinates.

**Result:**
- ❌ No medicine request was created (`medicine_request_id: null`)
- ✅ AI correctly asked for location (`requires_location: true`)
- ❌ Nothing appears in pharmacy dashboard (because no request exists)

## Why This Happens

Medicine requests are **only created when location is provided** because:
1. Pharmacies need location to calculate distance
2. Location is required to find nearby pharmacies
3. Distance/travel time are part of the ranking algorithm

## Solution: Send Location in Next Message

When the user provides location, the request will be created automatically.

### Example Flow:

**Message 1 (No Location):**
```json
{
  "message": "I have a headache",
  "location_latitude": null,
  "location_longitude": null
}
```
**Response:** AI asks for location, no request created

**Message 2 (WITH Location):**
```json
{
  "message": "I have a headache",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335,
  "location_address": "Harare, Zimbabwe"
}
```
**Response:** 
- ✅ Medicine request created
- ✅ `medicine_request_id` returned
- ✅ Request appears in pharmacy dashboard

## How to Test Pharmacy Dashboard

### Step 1: Create a Request with Location

```bash
curl -X POST http://localhost:8000/api/chatbot/chat/ \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I have a headache",
    "session_id": "test_session",
    "location_latitude": -17.8252,
    "location_longitude": 31.0335,
    "location_address": "Harare, Zimbabwe"
  }'
```

**Expected Response:**
```json
{
  "medicine_request_id": "uuid-here",
  "request_sent_to_pharmacies": true,
  ...
}
```

### Step 2: Check Pharmacy Dashboard

```bash
GET /api/chatbot/pharmacist/requests/?pharmacist_id={your_pharmacist_id}
```

**Expected Response:**
```json
[
  {
    "request_id": "uuid-here",
    "request_type": "symptom",
    "symptoms": "I have a headache",
    "status": "broadcasting",
    "has_responded": false,
    ...
  }
]
```

## Quick Test: Create Sample Request

If you want to test the dashboard immediately, you can create a request manually:

### Option 1: Via Django Admin
1. Go to `http://localhost:8000/admin/`
2. Navigate to `Chatbot > Medicine requests`
3. Click "Add Medicine request"
4. Fill in:
   - Request type: `symptom`
   - Symptoms: `I have a headache`
   - Location latitude: `-17.8252`
   - Location longitude: `31.0335`
   - Location address: `Harare, Zimbabwe`
   - Status: `broadcasting`
5. Save

### Option 2: Via Python Shell

```python
from chatbot.models import MedicineRequest, ChatConversation
from django.utils import timezone
from datetime import timedelta
import uuid

# Get or create a conversation
conversation, _ = ChatConversation.objects.get_or_create(
    session_id="test_session",
    defaults={'status': 'active'}
)

# Create medicine request
request = MedicineRequest.objects.create(
    conversation=conversation,
    request_type='symptom',
    symptoms='I have a headache',
    location_latitude=-17.8252,
    location_longitude=31.0335,
    location_address='Harare, Zimbabwe',
    status='broadcasting',
    expires_at=timezone.now() + timedelta(hours=2)
)

print(f"Created request: {request.request_id}")
```

## Verification

After creating a request with location:

1. ✅ Request should have `status = 'broadcasting'`
2. ✅ Request should have location coordinates
3. ✅ Request should appear in pharmacy dashboard
4. ✅ Pharmacist can see it and respond

## Summary

**The pharmacy dashboard is empty because:**
- No medicine request was created (location was not provided)
- Requests are only created when `location_latitude` and `location_longitude` are provided

**To fix:**
- Send a follow-up message WITH location coordinates
- Or create a test request manually for testing

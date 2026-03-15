# Testing Pharmacy Dashboard

## Issue: Requests Not Showing in Dashboard

### Problem
When a user sends a message like "I have a headache" **without location**, no medicine request is created, so pharmacies don't see it in their dashboard.

### Solution
**Medicine requests are only created when location is provided.**

## How to Test

### Step 1: Send Message WITH Location

**Request:**
```json
POST /api/chatbot/chat/
{
  "message": "I have a headache",
  "session_id": "test_session",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335,
  "location_address": "Harare, Zimbabwe"
}
```

**Expected Result:**
- Medicine request is created
- `medicine_request_id` is returned (not null)
- Request appears in pharmacy dashboard

### Step 2: Check Pharmacy Dashboard

**Request:**
```bash
GET /api/chatbot/pharmacist/requests/?pharmacist_id={pharmacist_id}
```

**Expected Result:**
- Should see the medicine request in the list
- Status should be `'broadcasting'` or `'awaiting_responses'`

## Current Flow

1. **User sends message WITHOUT location:**
   - ❌ No medicine request created
   - ✅ AI asks for location
   - ❌ Nothing appears in pharmacy dashboard

2. **User sends message WITH location:**
   - ✅ Medicine request created
   - ✅ Request appears in pharmacy dashboard
   - ✅ Pharmacies can respond

## Testing with Sample Request

To test the pharmacy dashboard, you can:

### Option 1: Use the Chat API with Location
```bash
curl -X POST http://localhost:8000/api/chatbot/chat/ \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I need paracetamol",
    "session_id": "test",
    "location_latitude": -17.8252,
    "location_longitude": 31.0335,
    "location_address": "Harare"
  }'
```

### Option 2: Create Request via Django Admin
1. Go to `http://localhost:8000/admin/`
2. Navigate to `Chatbot > Medicine requests`
3. Create a new request manually
4. Set status to `'broadcasting'`
5. Add location coordinates

### Option 3: Check Existing Requests
```bash
# List all medicine requests
GET http://localhost:8000/admin/chatbot/medicinerequest/
```

## Verification Checklist

- [ ] Message sent WITH location coordinates
- [ ] `medicine_request_id` is not null in response
- [ ] Request status is `'broadcasting'` or `'awaiting_responses'`
- [ ] Request appears in pharmacy dashboard query
- [ ] Pharmacist ID is valid and exists

## Common Issues

### Issue: "No requests found"
**Cause:** No medicine requests have been created yet (location not provided)

**Solution:** Send a message with location coordinates

### Issue: "Pharmacist not found"
**Cause:** Invalid pharmacist_id

**Solution:** 
1. Register a pharmacist first: `POST /api/chatbot/register/pharmacist/`
2. Use the returned `pharmacist_id` in dashboard query

### Issue: "Request created but not in dashboard"
**Cause:** Request status might be wrong or request expired

**Solution:**
1. Check request status (should be `'broadcasting'`, `'awaiting_responses'`, or `'responses_received'`)
2. Check request hasn't expired (`expires_at` field)
3. Verify pharmacist_id is correct

## Next Steps

After location is provided and request is created:
1. Request appears in pharmacy dashboard
2. Pharmacist can view request details
3. Pharmacist submits response via: `POST /api/chatbot/pharmacist/response/{request_id}/`
4. Response is ranked and included in top 3 for user

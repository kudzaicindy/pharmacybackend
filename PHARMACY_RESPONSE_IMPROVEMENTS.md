# Pharmacy Response Improvements

## Issues Fixed

### 1. ✅ Prevent Duplicate Response Notifications

**Problem:** Ranked responses were being sent multiple times every time the patient sent a message with location.

**Solution:**
- Added tracking to check if responses have already been shown to the patient
- Only show ranked responses if:
  - It's a NEW request (just created), OR
  - New responses have been added since last shown
- Uses message metadata to track when responses were shown
- Prevents duplicate notifications in the same conversation

**How it works:**
```python
# Check if we've already shown responses
if existing_request and not is_new_request:
    # Check recent messages for metadata
    if msg.metadata.get('pharmacy_responses_shown'):
        # Only show if new responses added since last shown
        if new_responses_count == 0:
            already_shown_responses = True  # Don't show again
```

### 2. ✅ Fix Distance and Travel Time Calculation

**Problem:** `distance_km` and `estimated_travel_time` were showing as `null` in responses.

**Solution:**
- Automatically calculate distance and travel time in `get_ranked_pharmacy_responses()`
- If response doesn't have distance/time, calculate it using pharmacy coordinates
- Updates the response object with calculated values
- Ensures all responses have distance and time data

**How it works:**
```python
# Calculate or recalculate if missing
if not response.distance_km and medicine_request.location_latitude:
    pharmacy_lat = response.pharmacy.latitude
    pharmacy_lon = response.pharmacy.longitude
    
    if pharmacy_lat and pharmacy_lon:
        distance_km = LocationService.calculate_distance(...)
        travel_time = LocationService.estimate_travel_time(...)
        
        # Update response
        response.distance_km = distance_km
        response.estimated_travel_time = travel_time
        response.save()
```

### 3. ✅ Show Pharmacy Name with Alternative Medicines

**Problem:** Alternative medicines didn't indicate which pharmacy suggested them.

**Solution:**
- Modified `get_ranked_pharmacy_responses()` to format alternatives as objects
- Each alternative now includes:
  - `medicine`: The alternative medicine name
  - `suggested_by`: The pharmacy name
  - `pharmacy_id`: The pharmacy ID

**Before:**
```json
"alternative_medicines": ["aspirin", "ibuprofen"]
```

**After:**
```json
"alternative_medicines": [
  {
    "medicine": "aspirin",
    "suggested_by": "24 Hour",
    "pharmacy_id": "24hour-2202"
  },
  {
    "medicine": "ibuprofen",
    "suggested_by": "24 Hour",
    "pharmacy_id": "24hour-2202"
  }
]
```

### 4. ✅ Add Pharmacy Recommendation

**Problem:** No guidance on which pharmacy the patient should choose.

**Solution:**
- Added `recommendation` field to chat response
- Automatically recommends the top-ranked pharmacy (rank #1)
- Includes explanation of why it's recommended
- Based on: availability, distance, time, price

**Response format:**
```json
{
  "recommendation": {
    "recommended_pharmacy": "Simed Pharmacy",
    "pharmacy_id": "simed-01",
    "reason": "I recommend **Simed Pharmacy** because medicine is available, only 2.5km away, ready in 15 minutes, best price: $5.00.",
    "ranking_score": 50.0
  }
}
```

## Complete Response Example

```json
{
  "response": "✅ Your request has been sent to nearby pharmacies! I found 2 top pharmacies with available options. Here are the top ranked responses:",
  "medicine_request_id": "uuid-here",
  "pharmacy_responses": [
    {
      "response_id": "33913709-c53f-4c38-b4e3-a0706add8891",
      "pharmacy_id": "simed-01",
      "pharmacy_name": "Simed Pharmacy",
      "pharmacist_id": null,
      "pharmacist_name": "Unknown",
      "medicine_available": true,
      "price": "5.00",
      "preparation_time": 15,
      "distance_km": 2.5,
      "estimated_travel_time": 8,
      "alternative_medicines": [],
      "notes": "",
      "submitted_at": "2026-01-17T10:55:23.460559Z",
      "total_time_minutes": 23,
      "ranking_score": 50.0,
      "rank": 1
    },
    {
      "response_id": "cd2cb2dc-2558-42b6-a686-46ef28b3556b",
      "pharmacy_id": "24hour-2202",
      "pharmacy_name": "24 Hour",
      "pharmacist_id": null,
      "pharmacist_name": "Unknown",
      "medicine_available": true,
      "price": "10.00",
      "preparation_time": 20,
      "distance_km": 5.3,
      "estimated_travel_time": 12,
      "alternative_medicines": [
        {
          "medicine": "aspirin",
          "suggested_by": "24 Hour",
          "pharmacy_id": "24hour-2202"
        }
      ],
      "notes": "",
      "submitted_at": "2026-01-17T10:46:46.344583Z",
      "total_time_minutes": 32,
      "ranking_score": 100.0,
      "rank": 2
    }
  ],
  "recommendation": {
    "recommended_pharmacy": "Simed Pharmacy",
    "pharmacy_id": "simed-01",
    "reason": "I recommend **Simed Pharmacy** because medicine is available, only 2.5km away, ready in 23 minutes, best price: $5.00.",
    "ranking_score": 50.0
  },
  "total_responses": 2,
  "request_sent_to_pharmacies": true
}
```

## Key Improvements Summary

1. ✅ **No duplicate notifications** - Responses shown only once unless new ones arrive
2. ✅ **Distance & time always calculated** - Automatically computed from pharmacy coordinates
3. ✅ **Clear alternative attribution** - Shows which pharmacy suggested each alternative
4. ✅ **Smart recommendations** - AI explains which pharmacy to choose and why

## Testing

### Test 1: Verify Distance Calculation
```bash
# Submit pharmacy response without distance
POST /api/chatbot/pharmacist/response/{request_id}/
{
  "pharmacist_id": "...",
  "medicine_available": true,
  "price": "5.00",
  "preparation_time": 15
}

# Check response - distance should be calculated automatically
GET /api/chatbot/chat/
```

### Test 2: Verify No Duplicate Responses
```bash
# Send message with location (first time)
POST /api/chatbot/chat/ 
{"message": "headache", "location_latitude": -17.8252, "location_longitude": 31.0335}
# Should show responses

# Send another message with location
POST /api/chatbot/chat/
{"message": "thanks", "location_latitude": -17.8252, "location_longitude": 31.0335}
# Should NOT show duplicate responses
```

### Test 3: Verify Alternative Format
```bash
# Submit response with alternatives
POST /api/chatbot/pharmacist/response/{request_id}/
{
  "alternative_medicines": ["aspirin", "ibuprofen"]
}

# Check response - alternatives should include pharmacy name
GET /api/chatbot/chat/
```

### Test 4: Verify Recommendation
```bash
# Get ranked responses
GET /api/chatbot/chat/

# Response should include "recommendation" field with:
# - recommended_pharmacy
# - reason (explanation)
# - ranking_score
```

## Notes

- Distance/time are calculated automatically when responses are retrieved
- Responses are only shown once unless new ones arrive
- Recommendations are based on ranking algorithm (lower score = better)
- Alternative medicines always show which pharmacy suggested them

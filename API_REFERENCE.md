# Complete API Reference

## Base URL
```
http://localhost:8000/api/chatbot
```

## Authentication
Currently, all endpoints use `AllowAny` permission. In production, you may want to add authentication.

## Endpoints

### 1. Chat Endpoint
**POST** `/api/chatbot/chat/`

Main chatbot interface for patient interactions.

**Request Body:**
```json
{
  "message": "string (required)",
  "session_id": "string (optional)",
  "conversation_id": "uuid (optional)",
  "location_latitude": "float (optional)",
  "location_longitude": "float (optional)",
  "location_address": "string (optional)"
}
```

**Response:**
```json
{
  "response": "AI response text",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "medicine_search|symptom_description|prescription_upload|general_inquiry",
  "requires_location": true|false,
  "suggested_medicines": ["medicine1", "medicine2"],
  "medicine_request_id": "uuid|null"
}
```

### 2. Upload Prescription
**POST** `/api/chatbot/upload-prescription/`

Upload prescription image for OCR processing.

**Request:** `multipart/form-data`
- `prescription_image`: File (required) - Image file (jpg, png, etc.)
- `session_id`: string (optional)
- `location_latitude`: float (optional)
- `location_longitude`: float (optional)
- `location_address`: string (optional)

**Response:**
```json
{
  "medicines": ["paracetamol", "amoxicillin"],
  "dosages": {"paracetamol": "500mg"},
  "raw_text": "Full extracted text",
  "confidence": "high|medium|low",
  "conversation_id": "uuid",
  "medicine_request_id": "uuid|null",
  "message": "Success message"
}
```

### 3. Get Conversation History
**GET** `/api/chatbot/conversation/<conversation_id>/`

Retrieve all messages in a conversation.

**Response:**
```json
{
  "conversation_id": "uuid",
  "session_id": "string",
  "status": "active|completed|abandoned",
  "created_at": "ISO datetime",
  "updated_at": "ISO datetime",
  "messages": [
    {
      "message_id": "uuid",
      "role": "user|assistant|system",
      "content": "message text",
      "created_at": "ISO datetime",
      "metadata": {}
    }
  ]
}
```

### 4. Get Pharmacy Responses (Simple)
**GET** `/api/chatbot/request/<request_id>/responses/`

Get all pharmacy responses for a request (not ranked).

**Response:**
```json
[
  {
    "response_id": "uuid",
    "pharmacy_id": "string",
    "pharmacy_name": "string",
    "medicine_available": true|false,
    "price": "decimal|null",
    "preparation_time": "integer",
    "distance_km": "float|null",
    "estimated_travel_time": "integer|null",
    "alternative_medicines": ["alt1", "alt2"],
    "notes": "string",
    "submitted_at": "ISO datetime"
  }
]
```

### 5. Get Ranked Pharmacy Responses ⭐
**GET** `/api/chatbot/request/<request_id>/ranked/`

Get pharmacy responses ranked by availability, time, price, and distance.

**Response:**
```json
[
  {
    "response_id": "uuid",
    "pharmacy_id": "string",
    "pharmacy_name": "string",
    "medicine_available": true|false,
    "price": "decimal|null",
    "preparation_time": "integer",
    "distance_km": "float|null",
    "estimated_travel_time": "integer|null",
    "total_time_minutes": "integer",
    "alternative_medicines": ["alt1", "alt2"],
    "notes": "string",
    "submitted_at": "ISO datetime",
    "ranking_score": "float",
    "rank": "integer"
  }
]
```

### 6. Get Pharmacy Requests (Dashboard)
**GET** `/api/chatbot/pharmacy/requests/?pharmacy_id=<pharmacy_id>`

Get all medicine requests for a pharmacy.

**Query Parameters:**
- `pharmacy_id`: string (required)

**Response:**
```json
[
  {
    "request_id": "uuid",
    "request_type": "symptom|prescription|direct",
    "medicine_names": ["med1", "med2"],
    "symptoms": "string",
    "location_address": "string",
    "created_at": "ISO datetime",
    "expires_at": "ISO datetime|null",
    "status": "created|broadcasting|awaiting_responses|responses_received|completed|expired",
    "has_responded": true|false
  }
]
```

### 7. Submit Pharmacy Response
**POST** `/api/chatbot/pharmacy/response/<request_id>/`

Submit pharmacy response to a medicine request.

**Request Body:**
```json
{
  "pharmacy_id": "string (required)",
  "pharmacy_name": "string (required)",
  "pharmacy_latitude": "float (optional)",
  "pharmacy_longitude": "float (optional)",
  "medicine_available": true|false (required),
  "price": "decimal (optional, required if available)",
  "preparation_time": "integer (optional, default: 0)",
  "alternative_medicines": ["alt1", "alt2"] (optional),
  "notes": "string (optional)"
}
```

**Response:**
```json
{
  "response_id": "uuid",
  "pharmacy_id": "string",
  "pharmacy_name": "string",
  "medicine_available": true|false,
  "price": "decimal|null",
  "preparation_time": "integer",
  "distance_km": "float|null",
  "estimated_travel_time": "integer|null",
  "alternative_medicines": ["alt1", "alt2"],
  "notes": "string",
  "submitted_at": "ISO datetime"
}
```

### 8. Suggest Alternatives
**POST** `/api/chatbot/alternatives/`

Get alternative medicine suggestions.

**Request Body:**
```json
{
  "medicine": "string (required)",
  "symptoms": ["symptom1", "symptom2"] (optional)
}
```

**Response:**
```json
{
  "unavailable_medicine": "string",
  "alternatives": ["alt1", "alt2", "alt3"]
}
```

## Status Codes

- `200 OK`: Request successful
- `201 Created`: Resource created successfully
- `400 Bad Request`: Invalid request data
- `404 Not Found`: Resource not found
- `500 Internal Server Error`: Server error
- `503 Service Unavailable`: Service temporarily unavailable (e.g., Gemini API unavailable)

## Error Response Format

```json
{
  "error": "Error message description"
}
```

## Data Types

- **uuid**: UUID string format
- **float**: Decimal number
- **integer**: Whole number
- **decimal**: Decimal number (for prices)
- **ISO datetime**: ISO 8601 format (e.g., "2025-01-12T10:00:00Z")
- **boolean**: true or false

## Notes

1. **Session Management**: Use `session_id` to maintain conversation context. Store it in localStorage.
2. **Location**: Always provide location when creating medicine requests for accurate distance calculations.
3. **Polling**: For pharmacy responses, poll the ranked endpoint every 5-10 seconds until responses are received.
4. **File Upload**: Prescription images should be in common formats (jpg, png, etc.) and under 10MB recommended.
5. **Ranking**: The ranking algorithm prioritizes available medicines with shortest total time (prep + travel), lowest price, and shortest distance.

# Chatbot API Documentation

## Overview
The chatbot API provides an AI-powered conversational interface for patients to search for medicines through symptoms, prescription uploads, or direct medicine searches.

## Endpoints

### 1. Chat with AI
**POST** `/api/chatbot/chat/`

Send a message to the AI chatbot and receive a response.

**Request Body:**
```json
{
  "message": "I am looking for medicine",
  "session_id": "optional-session-id",
  "conversation_id": "optional-conversation-uuid",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335,
  "location_address": "Harare, Zimbabwe"
}
```

**Response:**
```json
{
  "response": "I can help you find medicine. What is your location?",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "medicine_search",
  "requires_location": true,
  "suggested_medicines": [],
  "medicine_request_id": "uuid-or-null"
}
```

**Example Flow:**
1. User: "I am having a headache"
   - AI: "I understand you're experiencing a headache. What is your location?"
   - `requires_location: true`

2. User: "I am looking for paracetamol" (with location)
   - AI: "I'll help you find paracetamol nearby..."
   - Creates medicine request and broadcasts to pharmacies
   - Returns `medicine_request_id`

### 2. Get Conversation History
**GET** `/api/chatbot/conversation/<conversation_id>/`

Retrieve all messages in a conversation.

**Response:**
```json
{
  "conversation_id": "uuid",
  "session_id": "string",
  "status": "active",
  "messages": [
    {
      "message_id": "uuid",
      "role": "user",
      "content": "I need medicine",
      "created_at": "2025-01-12T10:00:00Z"
    },
    {
      "message_id": "uuid",
      "role": "assistant",
      "content": "What is your location?",
      "created_at": "2025-01-12T10:00:01Z"
    }
  ]
}
```

### 3. Get Pharmacy Responses
**GET** `/api/chatbot/request/<request_id>/responses/`

Get all pharmacy responses for a medicine request, sorted by total time (preparation + travel).

**Response:**
```json
[
  {
    "response_id": "uuid",
    "pharmacy_name": "HealthFirst Pharmacy",
    "medicine_available": true,
    "price": 4.50,
    "preparation_time": 15,
    "distance_km": 1.4,
    "estimated_travel_time": 8,
    "total_time_minutes": 23,
    "alternative_medicines": [],
    "notes": ""
  }
]
```

### 4. Suggest Alternatives
**POST** `/api/chatbot/alternatives/`

Get alternative medicine suggestions when a medicine is unavailable.

**Request Body:**
```json
{
  "medicine": "paracetamol",
  "symptoms": ["headache", "fever"]
}
```

**Response:**
```json
{
  "unavailable_medicine": "paracetamol",
  "alternatives": ["ibuprofen", "aspirin"]
}
```

## Usage Examples

### Example 1: Symptom-based Search
```python
# Step 1: User describes symptoms
POST /api/chatbot/chat/
{
  "message": "I am having a headache and fever"
}
# Response: AI asks for location

# Step 2: User provides location
POST /api/chatbot/chat/
{
  "message": "I am in Harare",
  "session_id": "same-session-id",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335
}
# Response: Creates medicine request, returns request_id

# Step 3: Get pharmacy responses
GET /api/chatbot/request/{request_id}/responses/
# Response: Ranked list of pharmacies with prices and times
```

### Example 2: Direct Medicine Search
```python
POST /api/chatbot/chat/
{
  "message": "I am looking for amoxicillin",
  "location_latitude": -17.8252,
  "location_longitude": 31.0335,
  "location_address": "Harare, Avondale"
}
# AI processes request, creates medicine request, returns results
```

## Features

1. **Multi-modal Input**: Supports symptom descriptions, prescription uploads, and direct medicine searches
2. **Location-aware**: Calculates distances and travel times to pharmacies
3. **Alternative Suggestions**: Suggests alternative medicines when requested medicine is unavailable
4. **Time Calculation**: Calculates total time (preparation + travel) for each pharmacy
5. **Context Retention**: Maintains conversation context across multiple turns (up to 8 messages)

## Configuration

Add to `.env`:
```
GEMINI_API_KEY=your-google-gemini-api-key
```

Get your API key from: https://makersuite.google.com/app/apikey

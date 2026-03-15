# Frontend Integration Guide

This guide provides everything you need to integrate your frontend with the Pharmacy Backend API.

## Base URL
```
http://localhost:8000/api/chatbot
```

## API Endpoints

### 1. Chat with AI
**POST** `/api/chatbot/chat/`

Send a message to the AI chatbot.

**Request:**
```json
{
  "message": "I am looking for paracetamol",
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
  "response": "I can help you find paracetamol. What is your location?",
  "conversation_id": "uuid",
  "message_id": "uuid",
  "intent": "medicine_search",
  "requires_location": true,
  "suggested_medicines": ["paracetamol"],
  "medicine_request_id": "uuid-or-null"
}
```

### 2. Upload Prescription (with OCR)
**POST** `/api/chatbot/upload-prescription/`

Upload a prescription image to extract medicine information.

**Request:** (multipart/form-data)
- `prescription_image`: File (image)
- `session_id`: string (optional)
- `location_latitude`: float (optional)
- `location_longitude`: float (optional)
- `location_address`: string (optional)

**Response:**
```json
{
  "medicines": ["paracetamol", "amoxicillin"],
  "dosages": {"paracetamol": "500mg"},
  "raw_text": "Extracted text from prescription...",
  "confidence": "high",
  "conversation_id": "uuid",
  "medicine_request_id": "uuid-or-null",
  "message": "Prescription processed successfully"
}
```

### 3. Get Ranked Pharmacy Responses
**GET** `/api/chatbot/request/<request_id>/ranked/`

Get pharmacy responses ranked by availability, time, price, and distance.

**Response:**
```json
[
  {
    "response_id": "uuid",
    "pharmacy_id": "ph-001",
    "pharmacy_name": "HealthFirst Pharmacy",
    "medicine_available": true,
    "price": 4.50,
    "preparation_time": 15,
    "distance_km": 1.4,
    "estimated_travel_time": 8,
    "total_time_minutes": 23,
    "alternative_medicines": [],
    "notes": "",
    "submitted_at": "2025-01-12T10:00:00Z",
    "ranking_score": 45.5,
    "rank": 1
  }
]
```

### 4. Get Pharmacy Requests (Dashboard)
**GET** `/api/chatbot/pharmacy/requests/?pharmacy_id=ph-001`

Get all medicine requests for a pharmacy.

**Response:**
```json
[
  {
    "request_id": "uuid",
    "request_type": "prescription",
    "medicine_names": ["paracetamol", "amoxicillin"],
    "symptoms": "",
    "location_address": "Harare, Zimbabwe",
    "created_at": "2025-01-12T10:00:00Z",
    "expires_at": "2025-01-12T12:00:00Z",
    "status": "awaiting_responses",
    "has_responded": false
  }
]
```

### 5. Submit Pharmacy Response
**POST** `/api/chatbot/pharmacy/response/<request_id>/`

Submit pharmacy response to a medicine request.

**Request:**
```json
{
  "pharmacy_id": "ph-001",
  "pharmacy_name": "HealthFirst Pharmacy",
  "pharmacy_latitude": -17.8095,
  "pharmacy_longitude": 31.0452,
  "medicine_available": true,
  "price": 4.50,
  "preparation_time": 15,
  "alternative_medicines": ["ibuprofen"],
  "notes": "Available in stock"
}
```

**Response:**
```json
{
  "response_id": "uuid",
  "pharmacy_id": "ph-001",
  "pharmacy_name": "HealthFirst Pharmacy",
  "medicine_available": true,
  "price": 4.50,
  "preparation_time": 15,
  "distance_km": 1.4,
  "estimated_travel_time": 8,
  "alternative_medicines": ["ibuprofen"],
  "notes": "Available in stock",
  "submitted_at": "2025-01-12T10:00:00Z"
}
```

## Frontend Implementation Examples

### React/JavaScript Example

```javascript
// API Configuration
const API_BASE_URL = 'http://localhost:8000/api/chatbot';

// Chat with AI
async function sendChatMessage(message, sessionId, location) {
  const response = await fetch(`${API_BASE_URL}/chat/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      message: message,
      session_id: sessionId,
      location_latitude: location?.latitude,
      location_longitude: location?.longitude,
      location_address: location?.address,
    }),
  });
  return await response.json();
}

// Upload Prescription
async function uploadPrescription(imageFile, sessionId, location) {
  const formData = new FormData();
  formData.append('prescription_image', imageFile);
  formData.append('session_id', sessionId);
  if (location) {
    formData.append('location_latitude', location.latitude);
    formData.append('location_longitude', location.longitude);
    formData.append('location_address', location.address);
  }

  const response = await fetch(`${API_BASE_URL}/upload-prescription/`, {
    method: 'POST',
    body: formData,
  });
  return await response.json();
}

// Get Ranked Responses
async function getRankedResponses(requestId) {
  const response = await fetch(`${API_BASE_URL}/request/${requestId}/ranked/`);
  return await response.json();
}

// Get Pharmacy Requests
async function getPharmacyRequests(pharmacyId) {
  const response = await fetch(
    `${API_BASE_URL}/pharmacy/requests/?pharmacy_id=${pharmacyId}`
  );
  return await response.json();
}

// Submit Pharmacy Response
async function submitPharmacyResponse(requestId, responseData) {
  const response = await fetch(
    `${API_BASE_URL}/pharmacy/response/${requestId}/`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(responseData),
    }
  );
  return await response.json();
}
```

### React Component Examples

#### Patient Chat Interface
```jsx
import React, { useState, useEffect } from 'react';

function PatientChat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [sessionId] = useState(() => localStorage.getItem('sessionId') || generateSessionId());
  const [conversationId, setConversationId] = useState(null);
  const [location, setLocation] = useState(null);
  const [requestId, setRequestId] = useState(null);

  useEffect(() => {
    localStorage.setItem('sessionId', sessionId);
    getCurrentLocation();
  }, []);

  const getCurrentLocation = () => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (position) => {
          setLocation({
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            address: 'Current Location',
          });
        },
        (error) => console.error('Location error:', error)
      );
    }
  };

  const sendMessage = async () => {
    if (!input.trim()) return;

    const userMessage = { role: 'user', content: input };
    setMessages((prev) => [...prev, userMessage]);
    setInput('');

    try {
      const response = await sendChatMessage(input, sessionId, location);
      
      setConversationId(response.conversation_id);
      if (response.medicine_request_id) {
        setRequestId(response.medicine_request_id);
      }

      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: response.response },
      ]);

      if (response.requires_location && !location) {
        getCurrentLocation();
      }
    } catch (error) {
      console.error('Error sending message:', error);
    }
  };

  return (
    <div className="chat-container">
      <div className="messages">
        {messages.map((msg, idx) => (
          <div key={idx} className={`message ${msg.role}`}>
            {msg.content}
          </div>
        ))}
      </div>
      <div className="input-area">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyPress={(e) => e.key === 'Enter' && sendMessage()}
          placeholder="Type your message..."
        />
        <button onClick={sendMessage}>Send</button>
      </div>
      {requestId && <PharmacyResults requestId={requestId} />}
    </div>
  );
}
```

#### Prescription Upload Component
```jsx
import React, { useState } from 'react';

function PrescriptionUpload() {
  const [file, setFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState(null);
  const [location, setLocation] = useState(null);

  useEffect(() => {
    // Get user location
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition((position) => {
        setLocation({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          address: 'Current Location',
        });
      });
    }
  }, []);

  const handleFileChange = (e) => {
    setFile(e.target.files[0]);
  };

  const handleUpload = async () => {
    if (!file) return;

    setUploading(true);
    try {
      const sessionId = localStorage.getItem('sessionId') || generateSessionId();
      const result = await uploadPrescription(file, sessionId, location);
      setResult(result);
      
      if (result.medicine_request_id) {
        // Redirect to results or show results
        window.location.href = `/results/${result.medicine_request_id}`;
      }
    } catch (error) {
      console.error('Upload error:', error);
      alert('Error uploading prescription. Please try again.');
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="prescription-upload">
      <h2>Upload Prescription</h2>
      <input type="file" accept="image/*" onChange={handleFileChange} />
      <button onClick={handleUpload} disabled={!file || uploading}>
        {uploading ? 'Processing...' : 'Upload & Process'}
      </button>
      {result && (
        <div className="result">
          <h3>Extracted Medicines:</h3>
          <ul>
            {result.medicines.map((med, idx) => (
              <li key={idx}>{med}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
```

#### Pharmacy Results Component
```jsx
import React, { useState, useEffect } from 'react';

function PharmacyResults({ requestId }) {
  const [responses, setResponses] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchResponses();
    // Poll for new responses every 5 seconds
    const interval = setInterval(fetchResponses, 5000);
    return () => clearInterval(interval);
  }, [requestId]);

  const fetchResponses = async () => {
    try {
      const data = await getRankedResponses(requestId);
      setResponses(data);
      setLoading(false);
    } catch (error) {
      console.error('Error fetching responses:', error);
    }
  };

  if (loading) return <div>Loading pharmacy responses...</div>;

  return (
    <div className="pharmacy-results">
      <h2>Available Pharmacies</h2>
      {responses.length === 0 ? (
        <p>No pharmacy responses yet. Please wait...</p>
      ) : (
        <div className="results-list">
          {responses.map((response) => (
            <div key={response.response_id} className="pharmacy-card">
              <div className="rank-badge">#{response.rank}</div>
              <h3>{response.pharmacy_name}</h3>
              {response.medicine_available ? (
                <>
                  <p>Price: ${response.price}</p>
                  <p>Total Time: {response.total_time_minutes} minutes</p>
                  <p>Distance: {response.distance_km} km</p>
                  <button>Select Pharmacy</button>
                </>
              ) : (
                <>
                  <p>Not Available</p>
                  {response.alternative_medicines.length > 0 && (
                    <div>
                      <p>Alternatives:</p>
                      <ul>
                        {response.alternative_medicines.map((alt, idx) => (
                          <li key={idx}>{alt}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

#### Pharmacy Dashboard Component
```jsx
import React, { useState, useEffect } from 'react';

function PharmacyDashboard({ pharmacyId, pharmacyName }) {
  const [requests, setRequests] = useState([]);
  const [selectedRequest, setSelectedRequest] = useState(null);
  const [responseForm, setResponseForm] = useState({
    medicine_available: false,
    price: '',
    preparation_time: 0,
    alternative_medicines: [],
    notes: '',
  });

  useEffect(() => {
    fetchRequests();
    const interval = setInterval(fetchRequests, 10000); // Poll every 10 seconds
    return () => clearInterval(interval);
  }, [pharmacyId]);

  const fetchRequests = async () => {
    try {
      const data = await getPharmacyRequests(pharmacyId);
      setRequests(data);
    } catch (error) {
      console.error('Error fetching requests:', error);
    }
  };

  const handleSubmitResponse = async () => {
    if (!selectedRequest) return;

    try {
      await submitPharmacyResponse(selectedRequest.request_id, {
        pharmacy_id: pharmacyId,
        pharmacy_name: pharmacyName,
        pharmacy_latitude: -17.8095, // Get from pharmacy profile
        pharmacy_longitude: 31.0452,
        ...responseForm,
      });
      alert('Response submitted successfully!');
      setSelectedRequest(null);
      fetchRequests();
    } catch (error) {
      console.error('Error submitting response:', error);
      alert('Error submitting response. Please try again.');
    }
  };

  return (
    <div className="pharmacy-dashboard">
      <h1>Pharmacy Dashboard - {pharmacyName}</h1>
      
      <div className="requests-list">
        <h2>Medicine Requests</h2>
        {requests.map((request) => (
          <div
            key={request.request_id}
            className={`request-card ${request.has_responded ? 'responded' : ''}`}
            onClick={() => setSelectedRequest(request)}
          >
            <h3>Request #{request.request_id.slice(0, 8)}</h3>
            <p>Medicines: {request.medicine_names.join(', ')}</p>
            <p>Location: {request.location_address}</p>
            <p>Status: {request.status}</p>
            {request.has_responded && <span className="badge">Responded</span>}
          </div>
        ))}
      </div>

      {selectedRequest && (
        <div className="response-modal">
          <h2>Respond to Request</h2>
          <p>Medicines: {selectedRequest.medicine_names.join(', ')}</p>
          
          <label>
            <input
              type="checkbox"
              checked={responseForm.medicine_available}
              onChange={(e) =>
                setResponseForm({ ...responseForm, medicine_available: e.target.checked })
              }
            />
            Medicine Available
          </label>

          {responseForm.medicine_available && (
            <>
              <input
                type="number"
                placeholder="Price"
                value={responseForm.price}
                onChange={(e) =>
                  setResponseForm({ ...responseForm, price: e.target.value })
                }
              />
              <input
                type="number"
                placeholder="Preparation Time (minutes)"
                value={responseForm.preparation_time}
                onChange={(e) =>
                  setResponseForm({ ...responseForm, preparation_time: parseInt(e.target.value) })
                }
              />
            </>
          )}

          <textarea
            placeholder="Alternative medicines (comma-separated)"
            value={responseForm.alternative_medicines.join(', ')}
            onChange={(e) =>
              setResponseForm({
                ...responseForm,
                alternative_medicines: e.target.value.split(',').map((s) => s.trim()),
              })
            }
          />

          <textarea
            placeholder="Notes"
            value={responseForm.notes}
            onChange={(e) =>
              setResponseForm({ ...responseForm, notes: e.target.value })
            }
          />

          <button onClick={handleSubmitResponse}>Submit Response</button>
          <button onClick={() => setSelectedRequest(null)}>Cancel</button>
        </div>
      )}
    </div>
  );
}
```

## Ranking Algorithm

Responses are ranked using a scoring system where **lower scores are better**:

1. **Availability** (most important): +1000 if not available
2. **Total Time**: (preparation_time + travel_time) × 2
3. **Price**: price × 10 (if available)
4. **Distance**: distance_km × 5

The ranking prioritizes:
1. Available medicines
2. Shortest total time (preparation + travel)
3. Lowest price
4. Shortest distance

## Error Handling

All endpoints return standard HTTP status codes:
- `200 OK`: Success
- `400 Bad Request`: Invalid input
- `404 Not Found`: Resource not found
- `500 Internal Server Error`: Server error
- `503 Service Unavailable`: Service temporarily unavailable

Error responses follow this format:
```json
{
  "error": "Error message description"
}
```

## CORS Configuration

The backend is configured to accept requests from:
- `http://localhost:3000` (default React dev server)

To add more origins, update `CORS_ALLOWED_ORIGINS` in Django settings.

## Testing

You can test the API using:
- Postman
- curl
- Your frontend application

Example curl commands:
```bash
# Chat
curl -X POST http://localhost:8000/api/chatbot/chat/ \
  -H "Content-Type: application/json" \
  -d '{"message": "I need paracetamol", "session_id": "test-123"}'

# Upload prescription
curl -X POST http://localhost:8000/api/chatbot/upload-prescription/ \
  -F "prescription_image=@prescription.jpg" \
  -F "session_id=test-123"
```

## Next Steps

1. Set up your frontend project
2. Install axios or fetch for API calls
3. Implement the components shown above
4. Add error handling and loading states
5. Style your components
6. Test the integration

For questions or issues, refer to the backend API documentation or check the Django admin panel at `http://localhost:8000/admin/`.

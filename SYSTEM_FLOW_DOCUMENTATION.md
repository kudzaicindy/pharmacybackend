# System Flow Documentation

## Overview

The Intelligent Healthcare Connection Platform connects patients with pharmacies through an AI-powered chatbot. Patients can search for medicines by symptoms, medicine names, or prescription uploads. Pharmacies receive requests and respond with availability, pricing, and delivery information.

---

## System Architecture Flow

```
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│   Patient   │────────▶│   AI Chatbot │────────▶│  Backend    │
│  (Frontend) │         │  (Gemini AI) │         │  (Django)   │
└─────────────┘         └──────────────┘         └─────────────┘
                                                          │
                                                          ▼
                                                  ┌─────────────┐
                                                  │  MongoDB    │
                                                  │ (Conversations)│
                                                  └─────────────┘
                                                          │
                                                          ▼
                                                  ┌─────────────┐
                                                  │  SQLite     │
                                                  │ (Requests & │
                                                  │  Responses) │
                                                  └─────────────┘
                                                          │
                                                          ▼
                                                  ┌─────────────┐
                                                  │ Pharmacies  │
                                                  │ Dashboard   │
                                                  └─────────────┘
```

---

## Complete User Flows

### Flow 1: Patient Medicine Search (Symptom-Based)

```
1. Patient opens app/website
   ↓
2. Patient chats with AI: "I have a headache"
   ↓
3. AI processes message (Gemini API)
   - Classifies intent: "symptom_description"
   - Extracts entities: symptoms=["headache"]
   - Suggests medicines: ["paracetamol", "ibuprofen"]
   ↓
4. AI asks for location: "What is your location?"
   ↓
5. Patient provides location (GPS or address)
   ↓
6. System creates MedicineRequest
   - request_type: "symptom"
   - medicine_names: ["paracetamol", "ibuprofen"]
   - symptoms: "I have a headache"
   - location: (lat, lon, address)
   - status: "broadcasting"
   ↓
7. System broadcasts request to pharmacies
   - Finds nearby pharmacies (within radius)
   - Creates PharmacyResponse entries (or sends notifications)
   ↓
8. Pharmacists see request in their dashboard
   ↓
9. Pharmacists respond with:
   - medicine_available: true/false
   - price: $X.XX
   - preparation_time: X minutes
   - alternative_medicines: [...]
   ↓
10. System ranks responses by:
    - Availability (most important)
    - Total time (preparation + travel)
    - Price
    - Distance
   ↓
11. Patient sees ranked list of pharmacy responses
   ↓
12. Patient selects a pharmacy
   ↓
13. System provides:
    - Pharmacy details
    - Estimated delivery time
    - Total cost
```

### Flow 2: Direct Medicine Search

```
1. Patient: "I need paracetamol"
   ↓
2. AI processes:
   - Intent: "medicine_search"
   - Extracted medicines: ["paracetamol"]
   ↓
3. AI asks for location
   ↓
4. Patient provides location
   ↓
5. System creates MedicineRequest
   - request_type: "direct"
   - medicine_names: ["paracetamol"]
   ↓
6. [Same as Flow 1, steps 7-13]
```

### Flow 3: Prescription Upload

```
1. Patient uploads prescription image
   ↓
2. OCR Service (Gemini Vision API) processes image
   - Extracts text from image
   - Identifies medicine names
   - Extracts dosages
   ↓
3. System displays extracted medicines to patient
   ↓
4. Patient confirms/edits medicines
   ↓
5. Patient provides location
   ↓
6. System creates MedicineRequest
   - request_type: "prescription"
   - medicine_names: [extracted medicines]
   ↓
7. [Same as Flow 1, steps 7-13]
```

### Flow 4: Pharmacy Registration & Setup

```
1. Pharmacy owner/admin registers pharmacy
   POST /api/chatbot/register/pharmacy/
   - pharmacy_id: "ph-001"
   - name, address, location, contact
   ↓
2. Pharmacy owner registers pharmacists (2-3 per pharmacy)
   POST /api/chatbot/register/pharmacist/
   - Links to pharmacy
   - Creates Django User account
   - Sets credentials
   ↓
3. Pharmacists can now log in
   POST /api/chatbot/pharmacist/login/
   ↓
4. Pharmacists access their dashboard
   GET /api/chatbot/pharmacist/requests/?pharmacist_id=...
```

### Flow 5: Pharmacist Dashboard & Response

```
1. Pharmacist logs in
   ↓
2. Pharmacist views dashboard
   GET /api/chatbot/pharmacist/requests/?pharmacist_id=...
   ↓
3. Dashboard shows:
   - Pending requests (not yet responded)
   - Responded requests (already handled)
   ↓
4. Pharmacist clicks on a request
   - Sees medicine names
   - Sees patient location
   - Sees symptoms (if applicable)
   ↓
5. Pharmacist checks inventory
   ↓
6. Pharmacist submits response
   POST /api/chatbot/pharmacist/response/{request_id}/
   {
     "pharmacist_id": "...",
     "medicine_available": true,
     "price": 4.50,
     "preparation_time": 15,
     "alternative_medicines": ["ibuprofen"],
     "notes": "Available in stock"
   }
   ↓
7. System calculates:
   - Distance from patient to pharmacy
   - Estimated travel time
   - Total time (preparation + travel)
   ↓
8. Response is added to ranking system
   ↓
9. Patient sees updated ranked list
```

---

## Data Flow

### 1. Conversation Flow

```
Patient Message
    ↓
Chatbot Service (Gemini AI)
    ↓
Intent Classification
    ↓
Entity Extraction
    ↓
Response Generation
    ↓
Save to MongoDB (ChatConversation, ChatMessage)
    ↓
Return to Patient
```

### 2. Medicine Request Flow

```
Location + Medicines/Symptoms
    ↓
Create MedicineRequest (SQLite)
    ↓
Status: "broadcasting"
    ↓
Find Nearby Pharmacies
    ↓
Notify Pharmacies / Create Response Slots
    ↓
Pharmacists Respond
    ↓
Status: "awaiting_responses" → "responses_received"
    ↓
Rank Responses
    ↓
Return to Patient
```

### 3. Response Ranking Algorithm

```
For each PharmacyResponse:
    ↓
Calculate Score:
    - If not available: +1000 (penalty)
    - Total time (prep + travel): + (time × 2)
    - Price: + (price × 10)
    - Distance: + (distance × 5)
    ↓
Sort by score (lower = better)
    ↓
Return ranked list
```

---

## Key System Components

### 1. AI Chatbot Service
- **Technology**: Google Gemini API (gemini-1.5-flash)
- **Functions**:
  - Natural language understanding
  - Intent classification
  - Entity extraction (medicines, symptoms, dosages)
  - Medicine suggestions
  - Alternative medicine recommendations

### 2. OCR Service
- **Technology**: Google Gemini Vision API
- **Functions**:
  - Extract text from prescription images
  - Identify medicine names
  - Extract dosages and instructions

### 3. Location Service
- **Functions**:
  - Calculate distance (Haversine formula)
  - Estimate travel time
  - Filter nearby pharmacies

### 4. Ranking System
- **Factors**:
  1. Availability (most important)
  2. Total time (preparation + travel)
  3. Price
  4. Distance
- **Algorithm**: Multi-criteria optimization

### 5. Database Structure
- **MongoDB**: Chat conversations and messages
- **SQLite**: Medicine requests, pharmacy responses, pharmacies, pharmacists

---

## API Endpoint Flow

### Patient Endpoints

```
POST /api/chatbot/chat/
    → Process message
    → Create conversation
    → Generate AI response
    → Create MedicineRequest (if location provided)

POST /api/chatbot/upload-prescription/
    → OCR processing
    → Extract medicines
    → Create MedicineRequest (if location provided)

GET /api/chatbot/request/{request_id}/ranked/
    → Get all pharmacy responses
    → Rank by algorithm
    → Return sorted list

GET /api/chatbot/request/{request_id}/responses/
    → Get all pharmacy responses
    → Return list
```

### Pharmacist Endpoints

```
POST /api/chatbot/register/pharmacy/
    → Create pharmacy record

POST /api/chatbot/register/pharmacist/
    → Create pharmacist record
    → Create Django User account

POST /api/chatbot/pharmacist/login/
    → Authenticate
    → Return pharmacist info

GET /api/chatbot/pharmacist/requests/?pharmacist_id=...
    → Get requests for pharmacist's pharmacy
    → Show pending and responded requests

POST /api/chatbot/pharmacist/response/{request_id}/
    → Submit pharmacy response
    → Calculate distance/time
    → Update request status
```

---

## State Transitions

### MedicineRequest Status Flow

```
created
    ↓
broadcasting (sent to pharmacies)
    ↓
awaiting_responses (at least one response received)
    ↓
responses_received (multiple responses)
    ↓
completed (patient selected a pharmacy)
    OR
expired (time limit reached)
```

### PharmacyResponse Flow

```
Request created
    ↓
Pharmacist views request
    ↓
Pharmacist submits response
    ↓
System calculates distance/time
    ↓
Response added to ranking
    ↓
Patient sees ranked list
```

---

## Error Handling Flow

### Common Scenarios

1. **Medicine Not Available**
   - Pharmacist sets `medicine_available: false`
   - System suggests alternatives
   - Patient sees alternatives in response

2. **No Pharmacies Nearby**
   - System expands search radius
   - Shows message to patient
   - Suggests manual pharmacy search

3. **OCR Failure**
   - Returns error message
   - Patient can retry upload
   - Fallback: manual medicine entry

4. **Pharmacist Not Responding**
   - Request expires after 2 hours
   - Status changes to "expired"
   - Patient can create new request

---

## Security & Authentication Flow

### Current State (Development)
- All endpoints use `AllowAny` permission
- No authentication required

### Production Flow (Recommended)
```
1. Patient Registration/Login
   - JWT token authentication
   - Session management

2. Pharmacist Authentication
   - Django User authentication
   - JWT token for API access
   - Role-based permissions

3. API Security
   - Rate limiting
   - CORS configuration
   - Input validation
   - SQL injection prevention
```

---

## Integration Points

### Frontend Integration

```
React/Next.js Frontend
    ↓
API Calls to Django Backend
    ↓
Real-time Updates (WebSocket/Polling)
    ↓
Display Results to User
```

### External Services

```
Google Gemini API
    ├── Chatbot Service (text generation)
    └── OCR Service (vision processing)

MongoDB Atlas
    └── Conversation storage

SQLite Database
    └── Request/Response storage
```

---

## Example Complete Flow

### Scenario: Patient with Headache

```
1. [Patient] Opens app
2. [Patient] Types: "I have a headache"
3. [System] AI processes: Intent=symptom, Entity=headache
4. [System] AI responds: "I understand you have a headache. I can suggest paracetamol or ibuprofen. What is your location?"
5. [Patient] Provides: "Harare, Zimbabwe" (or GPS coordinates)
6. [System] Creates MedicineRequest:
   - medicine_names: ["paracetamol", "ibuprofen"]
   - symptoms: "I have a headache"
   - location: (-17.8292, 31.0522, "Harare, Zimbabwe")
   - status: "broadcasting"
7. [System] Finds nearby pharmacies (within 10km radius)
8. [System] Notifies 3 pharmacies
9. [Pharmacist 1] Views request in dashboard
10. [Pharmacist 1] Checks inventory: paracetamol available
11. [Pharmacist 1] Submits: available=true, price=$4.50, prep_time=15min
12. [Pharmacist 2] Submits: available=true, price=$3.80, prep_time=30min
13. [Pharmacist 3] Submits: available=false, suggests ibuprofen
14. [System] Ranks responses:
    - Rank 1: Pharmacy 2 (lower price, closer)
    - Rank 2: Pharmacy 1 (available, good price)
    - Rank 3: Pharmacy 3 (not available, but has alternative)
15. [Patient] Sees ranked list
16. [Patient] Selects Pharmacy 2
17. [System] Updates request status to "completed"
18. [Patient] Receives pharmacy details and estimated delivery time
```

---

## Summary

The system provides an end-to-end solution connecting patients with pharmacies:

1. **Patient Side**: AI chatbot → Medicine search → Location → Request → Ranked results
2. **Pharmacy Side**: Registration → Login → Dashboard → View requests → Respond
3. **System Side**: AI processing → OCR → Location calculation → Ranking → Response delivery

The flow is designed to be intuitive, efficient, and scalable for multiple pharmacies and pharmacists.

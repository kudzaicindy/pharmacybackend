# Frontend API Configuration Guide

## Backend Server URL

Your Django backend should be running on:
```
http://localhost:8000
```

## API Base URL

All chatbot endpoints should use:
```
http://localhost:8000/api/chatbot
```

## Required Frontend Configuration

### 1. API Base URL Constant

In your frontend `api.js` or configuration file, set:

```javascript
const API_BASE_URL = 'http://localhost:8000/api/chatbot';
```

### 2. Chat Endpoint

The chat endpoint should be:
```javascript
const CHAT_ENDPOINT = `${API_BASE_URL}/chat/`;
// Full URL: http://localhost:8000/api/chatbot/chat/
```

### 3. Example API Call

```javascript
// Correct way to call the API
const response = await fetch('http://localhost:8000/api/chatbot/chat/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    message: 'I have a headache',
    session_id: 'session_123',
    conversation_id: null,
    location_latitude: null,
    location_longitude: null,
    location_address: null
  })
});
```

## Common Issues

### Issue: `ERR_CONNECTION_REFUSED`

**Cause:** Django server is not running

**Solution:**
1. Navigate to the backend directory: `cd C:\Users\HP\pharmacybackend`
2. Start the server: `python manage.py runserver`
3. Verify it's running on `http://localhost:8000`

### Issue: CORS Errors

**Cause:** Frontend origin not allowed

**Solution:** The backend is already configured to allow:
- `http://localhost:3000` (React default)
- `http://localhost:5173` (Vite default)
- `http://127.0.0.1:3000`
- `http://127.0.0.1:5173`

If your frontend runs on a different port, add it to `CORS_ALLOWED_ORIGINS` in `pharmacybackend/settings.py`

### Issue: Missing Protocol in URL

**Error:** `:8000/api/chatbot/chat/` (missing `http://localhost`)

**Solution:** Always use the full URL:
```javascript
// ❌ Wrong
fetch(':8000/api/chatbot/chat/')

// ✅ Correct
fetch('http://localhost:8000/api/chatbot/chat/')
```

## All Available Endpoints

### Chat
- **URL:** `http://localhost:8000/api/chatbot/chat/`
- **Method:** POST
- **Body:** `{ message, session_id?, conversation_id?, location_latitude?, location_longitude?, location_address? }`

### Get Conversation
- **URL:** `http://localhost:8000/api/chatbot/conversation/{conversation_id}/`
- **Method:** GET

### Get Pharmacy Responses
- **URL:** `http://localhost:8000/api/chatbot/request/{request_id}/responses/`
- **Method:** GET

### Get Ranked Responses
- **URL:** `http://localhost:8000/api/chatbot/request/{request_id}/ranked/`
- **Method:** GET

### Upload Prescription
- **URL:** `http://localhost:8000/api/chatbot/upload-prescription/`
- **Method:** POST
- **Content-Type:** `multipart/form-data`

## Testing the Backend

1. **Health Check:**
   ```
   http://localhost:8000/health/
   ```

2. **Root Endpoint:**
   ```
   http://localhost:8000/
   ```

3. **Test Chat (using curl):**
   ```bash
   curl -X POST http://localhost:8000/api/chatbot/chat/ \
     -H "Content-Type: application/json" \
     -d '{"message": "Hello"}'
   ```

## Quick Fix for Your Current Error

The error `ERR_CONNECTION_REFUSED` means the Django server isn't running. 

**To fix:**
1. Open a terminal in the backend directory
2. Run: `python manage.py runserver`
3. You should see: `Starting development server at http://127.0.0.1:8000/`
4. Then your frontend should be able to connect

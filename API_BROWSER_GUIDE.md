# How to View Your Django REST API in Browser

## Starting the Server

```bash
python manage.py runserver
```

The server will start on: **http://127.0.0.1:8000** or **http://localhost:8000**

## Available Endpoints

### 1. Root Endpoint
**URL:** http://localhost:8000/
- Shows API information

### 2. Health Check
**URL:** http://localhost:8000/health/
- Check if API is running

### 3. Django Admin
**URL:** http://localhost:8000/admin/
- Admin panel (requires superuser account)

### 4. Chatbot API Endpoints

#### Chat with AI
**URL:** http://localhost:8000/api/chatbot/chat/
**Method:** POST
**Note:** This is a POST endpoint, so you'll need to use a tool like Postman or curl to test it, OR use the browsable API interface.

#### Get Conversation
**URL:** http://localhost:8000/api/chatbot/conversation/{conversation_id}/
**Method:** GET

#### Get Pharmacy Responses
**URL:** http://localhost:8000/api/chatbot/request/{request_id}/responses/
**Method:** GET

#### Suggest Alternatives
**URL:** http://localhost:8000/api/chatbot/alternatives/
**Method:** POST

## Using Django REST Framework Browsable API

Django REST Framework provides a **browsable API** interface that lets you interact with your API directly in the browser!

### For GET Endpoints:
Simply visit the URL in your browser. For example:
- http://localhost:8000/
- http://localhost:8000/health/

### For POST Endpoints:
1. Visit the endpoint URL (e.g., http://localhost:8000/api/chatbot/chat/)
2. You'll see a form where you can:
   - Enter JSON data
   - Submit the request
   - See the response

### Example: Testing Chat Endpoint

1. Open browser: http://localhost:8000/api/chatbot/chat/
2. You'll see a form with fields for:
   - `message` (required)
   - `session_id` (optional)
   - `location_latitude` (optional)
   - `location_longitude` (optional)
   - `location_address` (optional)
3. Fill in the form and click "POST"
4. See the JSON response

## Quick Test URLs

Copy and paste these in your browser:

```
http://localhost:8000/
http://localhost:8000/health/
http://localhost:8000/api/chatbot/chat/
```

## Using Browser Developer Tools

For POST requests, you can also use browser console:

```javascript
// In browser console (F12)
fetch('http://localhost:8000/api/chatbot/chat/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    message: 'I am looking for medicine',
    session_id: 'test-session-123'
  })
})
.then(response => response.json())
.then(data => console.log(data));
```

## Troubleshooting

If you see errors:
1. Make sure the server is running: `python manage.py runserver`
2. Check for CORS issues if accessing from different origin
3. Verify your `.env` file has required variables
4. Check terminal/console for error messages

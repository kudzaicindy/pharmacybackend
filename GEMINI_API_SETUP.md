# Google Gemini API Setup Guide

The chatbot service requires a Google Gemini API key to function. Follow these steps to set it up.

## Step 1: Get Your API Key

1. Go to [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Sign in with your Google account
3. Click **"Create API Key"** or **"Get API Key"**
4. Select or create a Google Cloud project
5. Copy the generated API key

## Step 2: Add API Key to .env File

1. Open the `.env` file in your project root directory
2. Add or update the `GEMINI_API_KEY` line:

```env
GEMINI_API_KEY=your-actual-api-key-here
```

**Example:**
```env
GEMINI_API_KEY=AIzaSyBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Step 3: Restart Django Server

After adding the API key, restart your Django development server:

```bash
# Stop the server (Ctrl+C)
# Then restart:
python manage.py runserver
```

## Step 4: Verify Setup

Test the chat endpoint to verify it's working:

```bash
curl -X POST http://localhost:8000/api/chatbot/chat/ \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Hello"
  }'
```

You should receive a response from the AI chatbot instead of a 503 error.

## Troubleshooting

### Error: "GEMINI_API_KEY not found in environment variables"

**Solution:**
1. Make sure the `.env` file exists in the project root
2. Check that `GEMINI_API_KEY=your-key` is in the file
3. Ensure there are no spaces around the `=` sign
4. Restart the Django server after adding the key

### Error: "Failed to import google.generativeai"

**Solution:**
1. Install the required package:
   ```bash
   pip install google-generativeai
   ```
2. Or reinstall all dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Error: "API key is invalid"

**Solution:**
1. Verify you copied the entire API key (no extra spaces)
2. Check that the API key is active in Google AI Studio
3. Make sure you're using the correct API key for the Gemini API

## Free Tier Limits

Google Gemini API has a free tier with generous limits:
- **Free tier**: 60 requests per minute
- **Rate limits**: May vary by model
- **No credit card required** for free tier

## Security Notes

⚠️ **Important:**
- Never commit your `.env` file to version control
- The `.env` file is already in `.gitignore`
- Keep your API keys secure
- Don't share your API keys publicly

## Alternative: Using Environment Variables Directly

If you prefer not to use a `.env` file, you can set the environment variable directly:

**Windows (PowerShell):**
```powershell
$env:GEMINI_API_KEY="your-api-key-here"
python manage.py runserver
```

**Windows (Command Prompt):**
```cmd
set GEMINI_API_KEY=your-api-key-here
python manage.py runserver
```

**Linux/Mac:**
```bash
export GEMINI_API_KEY="your-api-key-here"
python manage.py runserver
```

## Need Help?

If you continue to have issues:
1. Check the Django server console for error messages
2. Verify the API key is correct in Google AI Studio
3. Check that `python-dotenv` is installed: `pip install python-dotenv`
4. Ensure the `.env` file is in the same directory as `manage.py`

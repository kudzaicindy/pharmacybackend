# Quick Fix for Gemini API Quota Issue

## The Problem
Your API key shows **limit: 0** for all free tier quotas, meaning it has no free tier access enabled.

## Fastest Solution (5 minutes)

### Step 1: Create a New API Key
1. Go to: **https://makersuite.google.com/app/apikey**
2. Click **"Create API Key"** (or "Get API Key")
3. **Important**: When prompted, select **"Create API key in new project"** OR choose a project that doesn't have quota restrictions
4. Copy the new API key

### Step 2: Update Your .env File
```env
GEMINI_API_KEY=your-new-api-key-here
```

### Step 3: Restart Django Server
```bash
# Stop server (Ctrl+C)
python manage.py runserver
```

## If New Key Still Has Quota Issues

### Enable API in Google Cloud Console

1. **Go to Google Cloud Console**
   - Visit: https://console.cloud.google.com/
   - Make sure you're signed in with the same Google account

2. **Select Your Project**
   - If you created a new project, select it from the project dropdown
   - Or create a new project: Click project dropdown → "New Project"

3. **Enable Generative Language API**
   - Go to: **APIs & Services** > **Library**
   - Search for: **"Generative Language API"**
   - Click on it
   - Click **"Enable"** button

4. **Check Quotas**
   - Go to: **APIs & Services** > **Enabled APIs**
   - Click on **Generative Language API**
   - Go to **Quotas** tab
   - Look for quotas with "Free Tier" in the name
   - They should show limits like:
     - 60 requests per minute
     - 1,500 requests per day
     - 32,000 tokens per minute

5. **If Quotas Show 0**
   - You may need to link a billing account
   - Go to: **Billing** in Google Cloud Console
   - Link a billing account (you won't be charged for free tier usage)
   - Free tier quotas should become available

## Verify It's Working

After updating the API key, test it:

```bash
python -c "import os; from dotenv import load_dotenv; load_dotenv(); import google.generativeai as genai; api_key = os.getenv('GEMINI_API_KEY'); genai.configure(api_key=api_key); model = genai.GenerativeModel('gemini-2.0-flash'); response = model.generate_content('Say hello'); print('SUCCESS:', response.text[:50])"
```

If you see "SUCCESS:" with text, the quota issue is fixed!

## Common Issues

### Issue: "API key not found"
- Make sure you copied the entire key (39 characters)
- Check for extra spaces in .env file
- Restart Django server after updating .env

### Issue: "Still getting quota errors"
- Wait 1-2 minutes after creating new key
- Try creating key in a completely new Google Cloud project
- Check that Generative Language API is enabled in that project

### Issue: "Billing account required"
- Some regions require billing account even for free tier
- Link billing account (you won't be charged for free tier)
- Free tier limits will become available

## Next Steps

Once quota is fixed:
1. Restart Django server
2. Test chat endpoint - should work now
3. Medicine requests will be created and broadcast to pharmacies

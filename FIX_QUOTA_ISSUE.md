# How to Fix Gemini API Quota Issue

## What is the Quota Issue?

The error you're seeing:
```
429 You exceeded your current quota
Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0
```

This means your Google Gemini API key has **no free tier quota available** (limit: 0). This can happen for several reasons:

1. **Free tier not enabled** - Your API key doesn't have free tier access enabled
2. **Quota exhausted** - You've used up your daily/monthly free tier quota
3. **API key not properly configured** - The key needs to be set up in Google Cloud Console
4. **Billing account required** - Some regions require a billing account even for free tier

## How to Fix It

### Option 1: Enable Free Tier (Recommended)

1. **Go to Google AI Studio**
   - Visit: https://makersuite.google.com/app/apikey
   - Sign in with your Google account

2. **Check Your API Key**
   - Find your API key in the list
   - Click on it to see details

3. **Enable Free Tier**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Navigate to **APIs & Services** > **Enabled APIs**
   - Search for "Generative Language API"
   - Make sure it's enabled
   - Check the **Quotas** tab to see your limits

4. **Verify Free Tier Status**
   - In Google Cloud Console, go to **IAM & Admin** > **Quotas**
   - Search for "Generative Language API"
   - Look for "Free Tier" quotas
   - Ensure they're not set to 0

### Option 2: Create a New API Key

If your current key doesn't have free tier access:

1. **Create New Key**
   - Go to: https://makersuite.google.com/app/apikey
   - Click **"Create API Key"**
   - Select or create a Google Cloud project
   - Copy the new API key

2. **Update Your .env File**
   ```env
   GEMINI_API_KEY=your-new-api-key-here
   ```

3. **Restart Django Server**
   ```bash
   python manage.py runserver
   ```

### Option 3: Set Up Billing (If Required)

Some regions require a billing account even for free tier:

1. **Go to Google Cloud Console**
   - Visit: https://console.cloud.google.com/
   - Navigate to **Billing**

2. **Link Billing Account**
   - Add a billing account (you won't be charged for free tier usage)
   - Free tier still applies - you only pay if you exceed free limits

3. **Verify Quota**
   - After linking billing, check quotas again
   - Free tier limits should now be available

### Option 4: Wait for Quota Reset

Free tier quotas typically reset:
- **Daily quotas**: Reset at midnight Pacific Time
- **Per-minute quotas**: Reset every minute

If you've exhausted today's quota, wait until it resets.

## Check Your Current Quota Status

### Method 1: Google Cloud Console

1. Go to: https://console.cloud.google.com/
2. Navigate to **APIs & Services** > **Enabled APIs**
3. Click on **Generative Language API**
4. Go to **Quotas** tab
5. Check your usage vs limits

### Method 2: API Usage Dashboard

1. Visit: https://ai.dev/rate-limit
2. Sign in with your Google account
3. View your current usage and limits

## Free Tier Limits

Google Gemini API free tier typically includes:
- **60 requests per minute** (RPM)
- **1,500 requests per day** (RPD)
- **32,000 tokens per minute** (input)
- **32,000 tokens per minute** (output)

These limits vary by model and region.

## Troubleshooting Steps

### Step 1: Verify API Key is Valid
```bash
# Test your API key
python -c "import os; from dotenv import load_dotenv; load_dotenv(); import google.generativeai as genai; api_key = os.getenv('GEMINI_API_KEY'); genai.configure(api_key=api_key); print('API Key is valid')"
```

### Step 2: Check Quota in Console
- Go to Google Cloud Console
- Check if Generative Language API is enabled
- Verify quota limits are not 0

### Step 3: Try a Different Model
If `gemini-2.0-flash` has quota issues, try:
- `gemini-flash-latest` (always uses latest available)
- `gemini-2.5-flash` (newer version)

### Step 4: Create New Project
Sometimes creating a fresh Google Cloud project helps:
1. Create new project in Google Cloud Console
2. Enable Generative Language API
3. Create new API key
4. Update your .env file

## Quick Fix Summary

**Fastest Solution:**
1. Go to https://makersuite.google.com/app/apikey
2. Create a **new API key**
3. Update `.env` file: `GEMINI_API_KEY=new-key-here`
4. Restart Django server

**If that doesn't work:**
1. Go to https://console.cloud.google.com/
2. Enable **Generative Language API**
3. Check **Quotas** - ensure free tier is enabled
4. Link billing account if required (won't charge for free tier)

## Alternative: Use Mock Responses (Development Only)

If you need to continue development while fixing the quota issue, you can temporarily add a fallback that provides mock responses:

```python
# In chatbot/services.py - temporary fallback
if api_quota_exceeded:
    return {
        'response': "I understand you have a headache. I can help you find pain relief medication. What is your location?",
        'intent': 'symptom_description',
        'suggested_medicines': ['paracetamol', 'ibuprofen'],
        'requires_location': True
    }
```

But this is only for development - you'll need a working API key for production.

## Still Having Issues?

If none of these solutions work:
1. Check Google AI Studio status: https://status.google.com/
2. Verify your Google account has access to Gemini API
3. Try creating API key in a different Google Cloud project
4. Contact Google Cloud Support if quota shows 0 after enabling free tier

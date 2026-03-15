# How to Enable Free Tier for Gemini API

## The Problem

Your API key shows **limit: 0** for free tier quotas. This means:
- ❌ Free tier is **NOT enabled** for your Google Cloud project
- ❌ The project has no free tier access configured
- ✅ This is a **configuration issue**, not a usage issue

## Solution: Enable Free Tier in Google Cloud Console

### Step 1: Find Your Project

1. Go to: **https://makersuite.google.com/app/apikey**
2. Find your API key in the list
3. Note which **Google Cloud project** it's associated with
   - It might show the project name or ID

### Step 2: Go to Google Cloud Console

1. Visit: **https://console.cloud.google.com/**
2. **Select the project** that your API key belongs to
   - Use the project dropdown at the top

### Step 3: Enable Generative Language API

1. Go to: **APIs & Services** > **Library**
2. Search for: **"Generative Language API"**
3. Click on it
4. Click **"Enable"** button
5. Wait for it to enable (may take 1-2 minutes)

### Step 4: Check Quotas

1. Go to: **APIs & Services** > **Enabled APIs**
2. Click on **Generative Language API**
3. Click **"Quotas"** tab
4. Look for quotas with **"Free Tier"** in the name:
   - `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`
   - `GenerateRequestsPerDayPerProjectPerModel-FreeTier`
   - `GenerateContentInputTokensPerModelPerMinute-FreeTier`

### Step 5: If Quotas Still Show 0

You may need to **link a billing account**:

1. Go to: **Billing** in Google Cloud Console
2. Click **"Link a billing account"**
3. Add a billing account (you won't be charged for free tier usage)
4. **Important**: Free tier still applies - you only pay if you exceed free limits
5. After linking, check quotas again - they should show proper limits

### Step 6: Verify Quota Limits

After enabling, quotas should show:
- **60 requests per minute** (not 0)
- **1,500 requests per day** (not 0)
- **32,000 tokens per minute** (not 0)

## Alternative: Create New API Key in New Project

If the above doesn't work, create a fresh project:

1. Go to: **https://console.cloud.google.com/**
2. Click project dropdown → **"New Project"**
3. Enter project name (e.g., "Pharmacy Chatbot")
4. Click **"Create"**
5. Wait for project to be created
6. Go to: **https://makersuite.google.com/app/apikey**
7. Click **"Create API Key"**
8. **Select the new project** you just created
9. Copy the new API key
10. Update your `.env` file
11. Restart Django server

New projects usually have free tier enabled by default.

## Quick Test After Fixing

Test if quota is enabled:

```bash
python -c "import os; from dotenv import load_dotenv; load_dotenv(); import google.generativeai as genai; api_key = os.getenv('GEMINI_API_KEY'); genai.configure(api_key=api_key); model = genai.GenerativeModel('gemini-2.0-flash'); response = model.generate_content('Hello'); print('SUCCESS - Quota is working!' if response.text else 'Still has issues')"
```

If you see "SUCCESS", the quota issue is fixed!

## Why This Happens

Some Google Cloud projects are created without free tier quotas enabled, especially if:
- The project was created through certain methods
- The project is in a region that requires billing
- The Generative Language API wasn't properly enabled

## Summary

**The issue**: Your project has free tier quotas set to **0** (disabled)

**The fix**: Enable Generative Language API and link billing account in Google Cloud Console

**Fastest solution**: Create a new project and new API key (usually has free tier enabled by default)

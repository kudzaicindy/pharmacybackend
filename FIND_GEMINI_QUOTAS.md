# How to Find Gemini API Quotas in Google Cloud Console

## You're Already on the Right Page!

You're on the **Quotas** page for the "pharmacy" project. Now you need to find the Generative Language API quotas.

## Step-by-Step Instructions

### Step 1: Filter the Quota Table

In the **Filter** box at the top of the quota table, type:
```
Generative Language API
```

Or search for:
```
generativelanguage
```

This will filter the table to show only Generative Language API quotas.

### Step 2: Look for Free Tier Quotas

After filtering, you should see rows like:
- **Service**: Generative Language API
- **Name**: "Generate requests per minute per project per model - Free Tier"
- **Value**: Should show a number (like 60) - **NOT 0**

### Step 3: If You Don't See Generative Language API

If the filter returns no results, the API is **not enabled**:

1. Go to: **APIs & Services** > **Library** (in the left sidebar)
2. Search for: **"Generative Language API"**
3. Click on it
4. Click **"Enable"** button
5. Wait 1-2 minutes for it to enable
6. Go back to **Quotas** page
7. Filter again for "Generative Language API"

### Step 4: Check the Quota Values

Once you find the Generative Language API quotas, check the **Value** column:

**What you should see:**
- ✅ **60** requests per minute (Free Tier)
- ✅ **1,500** requests per day (Free Tier)
- ✅ **32,000** tokens per minute (Free Tier)

**What you're seeing (the problem):**
- ❌ **0** for all free tier quotas

### Step 5: If Values Are Still 0

If the quotas show **0** even after enabling the API:

1. **Link a Billing Account:**
   - Go to: **Billing** (in left sidebar)
   - Click **"Link a billing account"**
   - Add a billing account
   - **Note**: You won't be charged for free tier usage

2. **Wait 5-10 minutes** for quotas to update

3. **Refresh the Quotas page**

4. Filter again for "Generative Language API"

5. Check if values are now non-zero

## Quick Filter Tips

In the quota table filter box, you can also try:
- `Free Tier` - to see all free tier quotas
- `generate_content` - to find generation-related quotas
- `gemini` - might show some Gemini-related quotas

## What to Look For

After filtering, you should see rows with:
- **Service**: "Generative Language API" or "Generative Language API (v1beta)"
- **Name**: Contains "Free Tier" in the name
- **Type**: "Quota"
- **Value**: Should be **60**, **1500**, or **32000** (not 0)

## If Still Not Working

If you've enabled the API and linked billing but quotas still show 0:

1. **Create a new project** (fresh start)
2. **Enable Generative Language API** in the new project
3. **Create a new API key** for the new project
4. **Update your .env file** with the new key

New projects usually have free tier enabled by default.

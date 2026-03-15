# Gemini 2.5 Flash Lite Update

## Summary

The chatbot has been updated to use **Gemini 2.5 Flash Lite** model, which provides the most generous free tier allowance (up to 1,000 requests per day) and is optimized for better free access.

## Changes Made

### 1. ChatbotService (OpenRouter API)
- **Model Updated**: Changed from `google/gemini-2.0-flash-exp:free` to `google/gemini-2.5-flash-lite`
- **Location**: `chatbot/services.py` line 216
- **API**: Still using OpenRouter API (no changes to API endpoint)

### 2. OCRService (Direct Gemini API)
- **Model Updated**: Changed to `gemini-2.5-flash-lite`
- **Library Support**: Added support for new `google.genai` library with fallback to old `google.generativeai`
- **Location**: `chatbot/services.py` OCRService class

## Model Details

### Gemini 2.5 Flash Lite
- **Free Tier**: Up to 1,000 requests per day
- **Rate Limit**: 15 requests per minute
- **Best For**: Free tier usage, general chatbot interactions
- **Available Via**: 
  - OpenRouter: `google/gemini-2.5-flash-lite` (no `:free` suffix needed)
  - Direct Gemini API: `gemini-2.5-flash-lite`

## Testing

The model has been tested and confirmed working:
- ✅ OpenRouter API: `google/gemini-2.5-flash-lite` works correctly
- ✅ Model responds to chat messages
- ✅ No 404 errors

## Next Steps

1. **Restart Django Server**: 
   ```bash
   python manage.py runserver
   ```

2. **Test Chatbot**:
   ```bash
   curl -X POST http://localhost:8000/api/chatbot/chat/ \
     -H "Content-Type: application/json" \
     -d '{"message": "I have a headache"}'
   ```

3. **Verify Response**: You should receive a helpful response from the AI instead of error messages.

## Troubleshooting

### If you still get 429 errors:
1. Check that your OpenRouter API key is valid
2. Verify the model name is exactly `google/gemini-2.5-flash-lite` (no `:free` suffix)
3. Check OpenRouter dashboard for rate limit status

### If OCR fails:
1. Ensure `GEMINI_API_KEY` is set in your `.env` file
2. The code will automatically use the old library if the new one isn't available
3. Install/update: `pip install google-generativeai`

## Benefits

✅ **Better Free Tier**: More requests per day (1,000 vs previous limits)  
✅ **More Reliable**: Flash Lite is optimized for free tier usage  
✅ **No Billing Required**: Works without linking billing account (in most cases)  
✅ **Backward Compatible**: Falls back to old library if new one unavailable  

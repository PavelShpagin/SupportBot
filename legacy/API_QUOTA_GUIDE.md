# 🔑 API Quota Guide

## Current Status: ✅ QUOTA IS AVAILABLE

Your Gemini API quota has reset and is working now!

---

## Quick Check

Run this anytime to check quota status:
```bash
cd /home/pavel/dev/SupportBot
source .venv/bin/activate
python test/check_quota.py
```

---

## Understanding Gemini API Quotas

### Free Tier Limits (as of your key)

**Gemini 2.5 Flash Lite:**
- Requests per minute: 15
- Requests per day: 1,500
- Tokens per minute: 1 million

**Gemini 3 Pro Preview:**
- Requests per minute: 15
- Requests per day: 1,500 (shared across all models)

### Why Evaluation Hit Quota

Your evaluation uses:
- 75 messages
- 4-5 API calls per message (decide, respond, embed, judge)
- Total: ~300-375 API calls

This consumed ~20-25% of your daily quota (1,500 requests).

**The issue:** You likely ran multiple evaluations today, or other API calls consumed the quota.

---

## How to Fix Quota Issues

### Option 1: Wait for Reset ⏰
- Quotas reset **daily** at midnight Pacific Time (PST)
- Your quota just reset (confirmed ✅)

### Option 2: Get a New API Key 🆓
If you need more capacity immediately:

1. Go to https://aistudio.google.com/apikey
2. Click "Create API Key"
3. Copy the new key
4. Update `.env`:
   ```bash
   GOOGLE_API_KEY=your_new_key_here
   ```

### Option 3: Upgrade to Paid Plan 💰
For production use:

1. Go to https://ai.google.dev/pricing
2. Enable billing in Google Cloud Console
3. Get higher quotas:
   - Pay-as-you-go pricing
   - Much higher rate limits
   - No daily caps

---

## Optimizing API Usage

### For Development/Testing:

1. **Test on fewer messages:**
   ```bash
   # Edit test/run_streaming_eval.py
   # Change to process only first 30 messages instead of all 75
   ```

2. **Use cheaper models:**
   - `gemini-2.5-flash-lite` is cheapest
   - Already configured for decision/extraction

3. **Cache results:**
   - Evaluation already caches in `test/data/streaming_eval/`
   - Rerunning uses cached KB embeddings

### Monitor Usage:

Check your usage at: https://ai.google.dev/gemini-api/docs/rate-limits

---

## Today's Quota Usage (Estimated)

Based on your runs today:

| Action | API Calls | % of Daily Quota |
|--------|-----------|------------------|
| Run 1 (complete) | ~375 calls | ~25% |
| Run 2 (complete) | ~375 calls | ~25% |
| Run 3 (partial, 58/75) | ~290 calls | ~19% |
| **Total** | **~1,040 calls** | **~69%** |

**Remaining quota:** ~460 calls (~31%)

This means you can run **one more full evaluation** today before hitting the limit again.

---

## Recommendations

### For Today:
✅ You can rerun evaluation now (quota available)
⚠️ But this will be your last full run today (~30% quota left)

### For Tomorrow:
1. Run evaluation once per day max
2. Or get a second API key for development
3. Or upgrade to paid plan for unlimited testing

---

## Quick Commands

**Check quota:**
```bash
python test/check_quota.py
```

**Run evaluation:**
```bash
python test/run_streaming_eval.py
```

**See results:**
```bash
cat test/data/streaming_eval/eval_summary.json
```

---

## Error Messages to Watch For

### Quota Exceeded:
```
Error code: 429 - quota exceeded
generativelanguage.googleapis.com/generate_requests_per_model_per_day
```
**Fix:** Wait for reset or get new key

### Rate Limit:
```
Error code: 429 - rate limit exceeded
generativelanguage.googleapis.com/generate_requests_per_minute
```
**Fix:** Add delays between calls (already handled in eval script)

### Invalid Key:
```
Error code: 401 - invalid API key
```
**Fix:** Check `.env` has correct `GOOGLE_API_KEY`

---

## Current Configuration

**API Key:** `AIzaSyBbCQVFJNJ...AnsiU` (working ✅)

**Models in use:**
- Decision: `gemini-2.5-flash-lite` (cheap, fast)
- Response: `gemini-3-pro-preview` (quality)
- Extraction: `gemini-3-pro-preview` (quality)
- Embedding: `text-embedding-004` (vectors)

**Status:** All models working, quota available ✅

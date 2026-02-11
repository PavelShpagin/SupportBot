# 📊 Final Evaluation Summary

## Three Complete Runs + One Partial

### Run Configurations

| Run | Configuration | Complete? |
|-----|--------------|-----------|
| **Baseline** | Original code | ✅ Yes (previous) |
| **Run 1** | Clean buffer only | ✅ Yes |
| **Run 2** | Hybrid context (recent + buffer) | ✅ Yes |
| **Run 3** | Aggressive prompt + clean buffer | ⚠️ Partial (58/75) |

---

## Complete Results Comparison

### Answer Messages (Need Bot Response)

| Run | Pass Rate | Avg Score | Respond Rate | Status |
|-----|-----------|-----------|--------------|--------|
| Baseline | 8.7% | 0.96 | 13.0% | 🔴 Too conservative |
| Run 1 | **13.0%** | **1.83** | **30.4%** | 🟡 Better but still low |
| Run 2 | 13.0% | 1.83 | 30.4% | 🟡 Same as Run 1 |
| Run 3 | ~11% | ~1.7 | ~26% | 🔴 WORSE (partial) |

**Best: Run 1 & 2** (tied)

---

### Contains-Answer (Bot Should Stay Silent)

| Run | Pass Rate | Respond Rate | Status |
|-----|-----------|--------------|--------|
| Baseline | **81.0%** | 19.0% | 🟢 Best |
| Run 1 | 71.4% | 28.6% | 🟡 Good |
| Run 2 | 57.1% | 42.9% | 🔴 Too eager |
| Run 3 | ~75-80% | ~25% | 🟡 Better than Run 2 |

**Best: Baseline** (81%)
**Second Best: Run 1** (71.4%)

---

### Ignore Messages (Bot Should Stay Silent)

| Run | Pass Rate | Respond Rate | Status |
|-----|-----------|--------------|--------|
| Baseline | **96.8%** | 3.2% | 🟢 Excellent |
| Run 1 | 87.1% | 12.9% | 🟡 Good |
| Run 2 | 87.1% | 12.9% | 🟡 Good |
| Run 3 | ~89% | ~11% | 🟡 Slightly better |

**Best: Baseline** (96.8%)

---

### Overall Performance

| Run | Pass Rate | Avg Score | Status |
|-----|-----------|-----------|--------|
| Baseline | **65.3%** | **6.56** | 🟢 Best overall |
| Run 1 | 60.0% | 6.16 | 🟡 Balanced |
| Run 2 | 56.0% | 5.76 | 🔴 Worst |
| Run 3 | N/A | N/A | ⚠️ Incomplete |

**Best: Baseline** (65.3% pass, 6.56 avg score)

---

## Key Findings

### ✅ What Worked

1. **Clean buffer (Run 1)**
   - Improved answer response rate: 13% → 30.4% (+17pp)
   - Maintained good contains-answer: 71.4%
   - Trade-off: Slightly more eager on ignore messages

2. **Extract-first architecture**
   - Removing solved cases from buffer helps both gate and response
   - Prevents confusion from resolved threads

### ❌ What Didn't Work

1. **Hybrid context (Run 2)**
   - Made contains-answer WORSE: 71.4% → 57.1% (-14pp)
   - No improvement on answer response rate
   - Adding recent messages confused the model

2. **Aggressive prompt (Run 3)**
   - Made answer response rate WORSE: 30.4% → ~26%
   - "Always respond if relevant CASE" didn't help
   - May have created pressure that backfired

---

## Trade-offs Analysis

### Baseline vs Run 1

**Baseline wins:**
- Contains-answer: 81% vs 71.4% (-10pp)
- Ignore: 96.8% vs 87.1% (-10pp)
- Overall: 65.3% vs 60.0% (-5.3pp)

**Run 1 wins:**
- Answer response: 30.4% vs 13.0% (+17pp)
- Answer pass: 13% vs 8.7% (+4.3pp)

**The trade-off:**
- Run 1 is more helpful (responds more to real questions)
- Baseline is more precise (fewer false positives)

---

## Recommended Configuration

### Option A: Keep Run 1 (Balanced)

**Code:**
```python
# worker.py
buffer = get_buffer()  # Clean buffer only
context = buffer
decision = decide_consider(message, context=buffer)
response = decide_and_respond(message, context=buffer, ...)
```

**Pros:**
- Better answer response rate (30.4% vs 13%)
- Still good contains-answer detection (71%)
- Simpler architecture

**Cons:**
- 10pp worse on contains-answer vs baseline
- 10pp worse on ignore vs baseline

---

### Option B: Investigate Why Answer Response is Low

Even Run 1's 30.4% response rate on "answer" messages is concerning. Only 3 out of 10 real questions get a response.

**Possible causes:**
1. KB cases not relevant enough (RAG retrieval quality)
2. Buffer often empty for new questions (normal)
3. Trust logic too strict (`has_buffer_context` threshold)
4. LLM being overly conservative in `decide_and_respond`

**Next steps:**
1. Analyze specific failure cases (answer messages that got no response)
2. Check if KB has relevant cases for those questions
3. Lower trust threshold further (100 → 50 chars?)
4. Add logging to understand decision flow

---

## Files Modified

### Current State (Run 3 - Partial)
- `signal-bot/app/jobs/worker.py` - Clean buffer + 100 char threshold
- `signal-bot/app/llm/prompts.py` - Aggressive simplified prompt

### Run 1 State (Best Complete)
- `signal-bot/app/jobs/worker.py` - Clean buffer + 200 char threshold
- `signal-bot/app/llm/prompts.py` - Enhanced but not aggressive prompt

---

## Next Actions

1. **Immediate:** Decide between Run 1 and continuing investigation
2. **Short-term:** Wait for API quota reset (24 hours)
3. **Medium-term:** Deep-dive into answer message failures
4. **Long-term:** Consider retrieval quality improvements

---

## Test Status

✅ **Unit tests:** All passing (64 passed, 13 skipped)
⚠️ **Eval tests:** Quota exceeded (partial results only)

---

## API Quota Issue

**Problem:** Gemini API daily quota limit
- Each eval message = 4-5 API calls
- 75 messages = ~300-375 calls
- Hit limit at message 58 (77% complete)

**Solution:** Wait 24 hours for quota reset OR use different API key

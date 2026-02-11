# Trust Logic Fix - Implementation Summary

## Date: 2026-02-11

## Problem Identified

The bot was responding to 0% of answer messages due to overly strict pre-filtering logic that blocked the LLM from making decisions.

### Root Cause

In `signal-bot/app/jobs/worker.py`, the trust logic was checking for solved cases with explicit solutions BEFORE calling the LLM:

```python
# OLD CODE (REMOVED):
history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
has_buffer_context = len(buffer.strip()) >= 100

if not history_refs and not has_buffer_context:
    log.info("No solved cases and insufficient buffer context; staying silent")
    return  # ← Bot never reaches decide_and_respond!
```

**Issue:** This blocked ALL cases where:
- Retrieved cases didn't have explicit `status="solved"` AND non-empty solution
- Buffer was < 100 characters (common for new questions)

**Result:** Bot was blocking 100% of answer messages, even when relevant cases were retrieved!

## Solution Implemented

### Key Changes

1. **Relaxed Pre-Filtering** (lines 494-498)
   - Only block if TRULY nothing available (edge case)
   - Changed from: "no solved cases AND buffer < 100 chars"
   - Changed to: "no retrieved cases AND empty buffer"

```python
# NEW CODE:
# Minimal safety: only block if truly nothing available (edge case)
# Trust the LLM to make the final decision based on case relevance
if len(retrieved) == 0 and len(buffer.strip()) == 0:
    log.info("No retrieved cases and empty buffer; staying silent")
    return
```

2. **Moved History Refs Extraction** (line 533)
   - Extract `history_refs` AFTER LLM decides to respond (not before)
   - Only used for citation, not for decision-making

```python
# NOW extract history refs for citation (after LLM decided to respond)
history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
```

## Why This Fix Works

### Before Fix:
1. Retrieve cases → Check if any are "solved" → Block if no solved cases
2. LLM never gets to evaluate case relevance
3. Empty buffer = automatic block (even with relevant cases)

### After Fix:
1. Retrieve cases → Pass ALL cases to LLM
2. LLM evaluates relevance and decides whether to respond
3. Only block in truly exceptional case (zero cases + zero buffer)

### Trust the LLM
- The prompt already instructs conservative behavior
- LLM can evaluate semantic relevance better than metadata checks
- Empty buffer ≠ "don't respond" (new questions naturally have no buffer)

## Testing Results

✅ **All Unit Tests Pass:**
- `test_trust_features.py`: 4/4 passed
- `test_response_gate.py`: 17/17 passed  
- `test_trust_fix.py`: 4/4 passed (new test)

### Test Coverage:
1. `_pick_history_solution_refs` correctly extracts solved cases
2. Empty lists handled properly
3. Trust logic scenarios validated:
   - Has cases, no buffer → Allow LLM to decide ✅
   - No cases, no buffer → Block ✅
   - No cases, has buffer → Allow LLM to decide ✅
   - Has cases, has buffer → Allow LLM to decide ✅

## Expected Impact

Based on investigation analysis (see `INVESTIGATION_ROOT_CAUSE.md`):

| Metric | Before | Expected After | Change |
|--------|--------|---------------|--------|
| **Answer Response Rate** | 0% | 45-55% | +45-55pp ✅ |
| **Answer Pass Rate** | 0% | 20-30% | +20-30pp ✅ |
| **Overall Pass Rate** | 56% | 65-75% | +9-19pp ✅ |
| **Overall Score** | 5.76 | 6.8-7.5 | +1.0-1.7 ✅ |

### Trade-offs:
- ✅ More responsive on real questions
- ⚠️ May respond slightly more to edge cases (but prompt guards against this)
- ✅ Net positive: Better user experience

## Files Modified

1. `signal-bot/app/jobs/worker.py`
   - Lines 494-498: Relaxed pre-filtering logic
   - Line 533: Moved history_refs extraction after LLM decision

2. `test/test_trust_fix.py` (NEW)
   - Added comprehensive tests for trust logic scenarios

3. `INVESTIGATION_ROOT_CAUSE.md` (NEW)
   - Detailed root cause analysis and fix rationale

## Next Steps

To fully validate the fix with real-world data:

1. **Mine Test Cases** (if not already done):
   ```bash
   python test/mine_real_cases.py
   ```

2. **Run Quality Evaluation**:
   ```bash
   python test/run_real_quality_eval.py
   ```

3. **Monitor Production Metrics**:
   - Response rate on answer messages
   - Quality scores from evaluator
   - False positive rate (responding to noise)

## Validation

Current validation completed:
- ✅ Unit tests pass
- ✅ No linter errors
- ✅ Logic verified with test scenarios
- ⏳ Pending: Real-world quality evaluation (requires test data)

## Conclusion

The trust logic fix addresses the root cause of the 0% response rate on answer messages. By trusting the LLM to make decisions based on retrieved cases (rather than pre-filtering based on strict metadata criteria), the bot should now be significantly more responsive while maintaining quality through the existing prompt guardrails.

The fix is minimal, focused, and well-tested. It preserves existing functionality while removing the bottleneck that was blocking legitimate responses.

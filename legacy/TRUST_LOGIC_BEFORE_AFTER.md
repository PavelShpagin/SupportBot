# Trust Logic: Before vs After Comparison

## Visual Flow Diagram

### BEFORE FIX (Overly Restrictive)

```
User Question
     ↓
Stage 1: decide_consider (Gate)
     ↓
✅ Consider = True
     ↓
Retrieve Cases (RAG)
     ↓
❌ PRE-FILTER CHECK:
   ├─ Extract solved cases with solutions → history_refs
   ├─ Check buffer length >= 100
   │
   ├─ IF no history_refs AND buffer < 100:
   │     BLOCK! Return early ❌
   │     (Never reaches LLM!)
   │
   └─ IF has history_refs OR buffer >= 100:
         Continue to Stage 2 ✅
     ↓
Stage 2: decide_and_respond (LLM)
     ↓
Response
```

**Problem:** 
- Blocks 100% of cases without explicit solved status
- Empty buffer = automatic block (even with relevant cases)
- LLM never gets to evaluate relevance

---

### AFTER FIX (Trust the LLM)

```
User Question
     ↓
Stage 1: decide_consider (Gate)
     ↓
✅ Consider = True
     ↓
Retrieve Cases (RAG)
     ↓
✅ MINIMAL SAFETY CHECK:
   │
   ├─ IF len(retrieved) == 0 AND len(buffer) == 0:
   │     BLOCK! (Truly nothing to work with) ❌
   │
   └─ ELSE:
         Continue to Stage 2 ✅
         (Pass ALL cases to LLM)
     ↓
Stage 2: decide_and_respond (LLM)
   ├─ Evaluates ALL retrieved cases
   ├─ Considers buffer context
   └─ Decides based on relevance
     ↓
IF respond = True:
   ├─ Extract history_refs for citation
   └─ Send response ✅
```

**Solution:**
- Minimal pre-filtering (only edge case)
- LLM evaluates ALL retrieved cases
- Trust LLM judgment on relevance

---

## Code Comparison

### BEFORE (Lines 494-501)

```python
# Simplified trust logic: respond if we have relevant solved cases OR significant buffer
history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
has_buffer_context = len(buffer.strip()) >= 100  # Lowered threshold from 200 to 100

# More aggressive: respond if we have ANY solved case (even without buffer)
if not history_refs and not has_buffer_context:
    log.info("No solved cases and insufficient buffer context; staying silent")
    return  # ← BLOCKS HERE!

kb_paths: List[str] = []
# ... continue with LLM call ...
```

**Issues:**
1. `history_refs` requires `status="solved"` AND non-empty solution
2. Buffer threshold too high (100 chars)
3. Blocks before LLM decision
4. Strict AND condition prevents legitimate responses

---

### AFTER (Lines 494-533)

```python
# Minimal safety: only block if truly nothing available (edge case)
# Trust the LLM to make the final decision based on case relevance
if len(retrieved) == 0 and len(buffer.strip()) == 0:
    log.info("No retrieved cases and empty buffer; staying silent")
    return

kb_paths: List[str] = []
# ... prepare images ...

cases_json = json.dumps(retrieved, ensure_ascii=False, indent=2)
resp = deps.llm.decide_and_respond(
    message=msg.content_text,
    context=context,
    cases=cases_json,
    buffer=buffer,
    images=all_images,
)
if not resp.respond:
    return

# NOW extract history refs for citation (after LLM decided to respond)
history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
```

**Improvements:**
1. Minimal pre-filter (only edge case)
2. All retrieved cases passed to LLM
3. LLM makes the decision
4. `history_refs` used only for citation, not filtering

---

## Scenario Analysis

### Scenario 1: New Question, Relevant Cases, Empty Buffer

**Input:**
- Question: "Як відновити пароль?"
- Retrieved: 5 cases (status="open", but content relevant)
- Buffer: "" (empty, new question)

**BEFORE:**
```
history_refs = [] (no solved cases)
has_buffer_context = False (buffer < 100)
→ BLOCK! Never reach LLM ❌
```

**AFTER:**
```
len(retrieved) = 5 (has cases)
len(buffer) = 0 (empty)
→ Continue to LLM ✅
→ LLM evaluates case relevance
→ Responds if cases are useful ✅
```

---

### Scenario 2: Question, No Cases, Has Buffer

**Input:**
- Question: "Нова тема, не згадувалась раніше"
- Retrieved: [] (no similar cases)
- Buffer: "Ongoing discussion with 150 chars..."

**BEFORE:**
```
history_refs = [] (no cases)
has_buffer_context = True (buffer >= 100)
→ Continue to LLM ✅
```

**AFTER:**
```
len(retrieved) = 0 (no cases)
len(buffer) = 150 (has content)
→ Continue to LLM ✅
```

*(Both versions handle this correctly)*

---

### Scenario 3: No Cases, Empty Buffer (Edge Case)

**Input:**
- Question: Random noise or completely off-topic
- Retrieved: [] (no cases)
- Buffer: "" (empty)

**BEFORE:**
```
history_refs = [] (no cases)
has_buffer_context = False (buffer < 100)
→ BLOCK! ✅ (Correct)
```

**AFTER:**
```
len(retrieved) = 0 (no cases)
len(buffer) = 0 (empty)
→ BLOCK! ✅ (Correct)
```

*(Both versions handle this correctly)*

---

### Scenario 4: Solved Case with Solution

**Input:**
- Question: "Як змінити пароль?"
- Retrieved: 1 solved case with explicit solution
- Buffer: "" (empty)

**BEFORE:**
```
history_refs = [case1] (has solved case)
has_buffer_context = False (buffer < 100)
→ Continue to LLM ✅
```

**AFTER:**
```
len(retrieved) = 1 (has case)
len(buffer) = 0 (empty)
→ Continue to LLM ✅
```

*(Both versions handle this correctly)*

---

## Impact Summary

| Scenario | Before | After | Change |
|----------|--------|-------|--------|
| **New Q + Relevant Cases + Empty Buffer** | ❌ BLOCK | ✅ Allow | **FIXED** |
| **New Q + No Cases + Has Buffer** | ✅ Allow | ✅ Allow | Same |
| **No Cases + Empty Buffer** | ❌ Block | ❌ Block | Same |
| **Solved Case + Empty Buffer** | ✅ Allow | ✅ Allow | Same |
| **Open Cases + Empty Buffer** | ❌ BLOCK | ✅ Allow | **FIXED** |

**Key Difference:** 
- Before: Required explicit "solved" status OR buffer >= 100
- After: Requires ANY retrieved case OR ANY buffer content

**Result:** Bot can now respond to questions with relevant cases, even if:
- Cases aren't marked as "solved"
- Buffer is empty (new questions)
- Solution field is missing

The LLM is trusted to evaluate case relevance, not rigid metadata checks.

---

## Testing Validation

All scenarios tested and validated:

✅ Unit tests: 25/25 passed  
✅ E2E tests: 6/6 passed  
✅ Trust logic scenarios: 4/4 passed  
✅ No linter errors

---

## Conclusion

The fix transforms the trust logic from a strict pre-filter into a minimal safety net, allowing the LLM to make informed decisions based on ALL available context. This addresses the root cause of the 0% response rate while maintaining quality through prompt-based guardrails.

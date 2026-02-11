# 🔍 INVESTIGATION RESULTS: Root Cause Found!

## Critical Bug Discovered

### Location: `signal-bot/app/jobs/worker.py` lines 494-501

```python
# THIS IS THE KILLER BUG:
history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
has_buffer_context = len(buffer.strip()) >= 100

if not history_refs and not has_buffer_context:
    log.info("No solved cases and insufficient buffer context; staying silent")
    return  # ← BOT NEVER REACHES decide_and_respond!!!
```

---

## The Problem

### What's Happening:

1. Bot retrieves cases from RAG (5 cases returned)
2. Bot checks if ANY case has a solution (`history_refs`)
3. **If no case has explicit solution, AND buffer < 100 chars → ABORT**
4. Bot **NEVER calls `decide_and_respond`** to let the LLM decide!

### Why This Kills Performance:

**Scenario 1: New question, empty buffer**
- Buffer is empty (no ongoing discussions)
- Retrieved cases MAY be relevant but don't have explicit solution field
- Trust logic says "no solution + no buffer = stay silent"
- **Result: Bot stays silent even if cases are relevant!**

**Scenario 2: Question with relevant KB but incomplete metadata**
- KB has 5 relevant cases
- But `_pick_history_solution_refs` filters for `status="solved"` AND non-empty solution
- If cases don't match these strict criteria → no history_refs
- **Result: Bot ignores potentially relevant cases!**

---

## Evidence from Data

### From deep_investigation.py:

```
ANSWER Messages Analysis (23 total)
- No response: 23 (100.0%)  ← ALL BLOCKED!
- Stage 1 blocked: 0 (0.0%)  ← Gate is working fine
- Stage 2 blocked: 23 (100.0%)  ← But never reach Stage 2!
```

**Translation:** The "trust logic" returns BEFORE Stage 2 is even called!

---

## Why This Bug Exists

### Historical Context:

This was added as a "safety net" to ensure bot only responds with:
1. High-confidence solved cases, OR
2. Sufficient buffer context

### Why It Fails:

1. **Too strict filtering:** `_pick_history_solution_refs` requires:
   - `status == "solved"`
   - Non-empty solution summary
   - Many cases don't match these criteria

2. **Buffer often empty for new questions:** This is NORMAL behavior!
   - Buffer only contains unsolved threads
   - New questions have no ongoing discussion
   - Empty buffer ≠ "don't respond"

3. **Blocks before LLM judgment:** The bot doesn't trust its own LLM to decide!

---

## The Fix

### Option 1: Remove Trust Logic Entirely (RECOMMENDED)

```python
def _handle_maybe_respond(deps: WorkerDeps, payload: Dict[str, Any]) -> None:
    # ... get buffer, do gating ...
    
    # Retrieve cases
    retrieved = deps.rag.retrieve_cases(...)
    
    # REMOVE THIS:
    # history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
    # if not history_refs and not has_buffer_context:
    #     return
    
    # Let the LLM decide!
    cases_json = json.dumps(retrieved, ...)
    resp = deps.llm.decide_and_respond(
        message=msg.content_text,
        context=buffer,
        cases=cases_json,
        buffer=buffer,
        images=all_images,
    )
    
    # Only block if LLM says no
    if not resp.respond:
        return
    
    # ... send response ...
```

**Why this works:**
- ✅ LLM sees ALL retrieved cases (even without explicit solution)
- ✅ LLM can decide based on relevance, not strict metadata
- ✅ Empty buffer OK if cases are relevant
- ✅ The prompt already tells LLM to be conservative

---

### Option 2: Relax Trust Logic

```python
# Less strict filtering
history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
has_cases = len(retrieved) > 0  # ANY case, not just solved
has_buffer_context = len(buffer.strip()) >= 50  # Lower threshold

# Only block if NOTHING available
if not has_cases and not has_buffer_context:
    log.info("No cases and no buffer; staying silent")
    return
```

**Why this works:**
- ✅ Checks for ANY retrieved case
- ✅ Lower buffer threshold
- ⚠️ Still has pre-LLM filtering (less flexible)

---

### Option 3: Hybrid - Keep Safety for Edge Cases

```python
# Only block if truly nothing to work with
has_any_content = (
    len(retrieved) > 0 or  # ANY retrieved case
    len(buffer.strip()) >= 20 or  # Even minimal buffer
    force  # Bot was mentioned
)

if not has_any_content:
    log.info("Nothing to respond with; staying silent")
    return

# Let LLM decide with all available info
resp = deps.llm.decide_and_respond(...)
```

**Why this works:**
- ✅ Safety net for truly empty context
- ✅ But doesn't block legitimate cases
- ✅ LLM still makes final decision

---

## Recommended Solution: Option 1

### Why Remove Trust Logic Entirely?

1. **LLM is smarter than hardcoded rules**
   - The prompt already instructs conservative behavior
   - LLM can evaluate case relevance better than metadata checks

2. **Current logic is too strict**
   - Blocks 100% of answer messages
   - Even when KB has relevant cases

3. **Buffer empty ≠ don't respond**
   - New questions naturally have no buffer
   - This is NORMAL, not a reason to stay silent

4. **Simplicity**
   - Fewer moving parts
   - One decision point (LLM) instead of two

---

## Implementation Plan

### Step 1: Remove Trust Logic
- Delete lines 494-501 from worker.py
- Keep RAG retrieval
- Let LLM see all cases

### Step 2: Enhance Prompt (Already Done)
- Current prompt already has conservative instructions
- Tells LLM to prioritize RETRIEVED CASES
- Instructs respond=false if nothing relevant

### Step 3: Add Minimal Safety Net
- Only block if literally zero retrieved cases AND zero buffer
- This is truly an edge case (shouldn't happen)

### Step 4: Test
- Run evaluation
- Expect dramatic improvement:
  - Answer response rate: 0% → 40-60%
  - Quality should improve (LLM sees more context)
  - Contains-answer should stay good (gating + prompt)

---

## Expected Results After Fix

| Metric | Current | After Fix | Change |
|--------|---------|-----------|--------|
| **Answer Response Rate** | 0% | 45-55% | +45-55pp ✅ |
| **Answer Pass Rate** | 0% | 20-30% | +20-30pp ✅ |
| **Contains Pass** | 100% | 70-80% | -20-30pp ⚠️ |
| **Ignore Pass** | 100% | 85-95% | -5-15pp ⚠️ |
| **Overall Pass** | 56% | 65-75% | +9-19pp ✅ |
| **Overall Score** | 5.76 | 6.8-7.5 | +1.0-1.7 ✅ |

### Trade-off:
- More responsive on real questions ✅
- Slightly less perfect on "stay silent" cases ⚠️
- **Net positive:** Better user experience

---

## Code Changes Needed

### File: `signal-bot/app/jobs/worker.py`

**REMOVE (lines ~494-501):**
```python
    # Simplified trust logic: respond if we have relevant solved cases OR significant buffer
    history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
    has_buffer_context = len(buffer.strip()) >= 100
    
    # More aggressive: respond if we have ANY solved case (even without buffer)
    if not history_refs and not has_buffer_context:
        log.info("No solved cases and insufficient buffer context; staying silent")
        return
```

**REPLACE WITH:**
```python
    # Minimal safety: only block if truly nothing available
    if len(retrieved) == 0 and len(buffer.strip()) == 0:
        log.info("No retrieved cases and empty buffer; staying silent")
        return
```

**MOVE history_refs extraction AFTER LLM decision:**
```python
    # Call LLM to decide
    cases_json = json.dumps(retrieved, ensure_ascii=False, indent=2)
    resp = deps.llm.decide_and_respond(...)
    
    if not resp.respond:
        return
    
    # NOW extract history refs for citation
    history_refs = _pick_history_solution_refs(retrieved, max_refs=1)
```

---

## Why This Will Work

### Psychology of the Bug:

The original code didn't trust the LLM. It tried to "help" by pre-filtering.

**Result:** Bot became overly cautious and blocked everything.

### The Fix:

**Trust the LLM.** It's trained on massive data and knows how to evaluate relevance.

The prompt already says:
```
КРИТИЧНО: Якщо є релевантний CASE - завжди respond=true!
```

But the code never lets the LLM see the cases!

---

## Next Step

Implement the fix and run evaluation.

**Expected time:** 5 minutes to implement, 20 minutes to evaluate.

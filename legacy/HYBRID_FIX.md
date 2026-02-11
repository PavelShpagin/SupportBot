# 🔧 Hybrid Context Fix - Implementation

## The Problem We Fixed

**Previous Issue:**
- Clean buffer (only unsolved threads) helped detect solved threads ✅
- But made bot too conservative on new questions ⚠️
- Response rate dropped: 39% → 30%

**Root Cause:** Not enough context for topic awareness when buffer is empty

---

## The Solution: Hybrid Context Approach

### Stage 1: Gate Decision (detect solved threads)
```python
gate_context = buffer  # Clean buffer, only unsolved threads
decision = decide_consider(message, context=gate_context)
```

### Stage 2: Response Generation (topic awareness)
```python
response_context = get_last_messages_text(n=15)  # Recent 15 messages
response = decide_and_respond(message, context=response_context, buffer=buffer)
```

---

## Changes Made

### 1. Worker: Hybrid Context (signal-bot/app/jobs/worker.py)

```python
# Stage 1: Clean buffer for gate decision
gate_context = buffer  # Only unsolved threads

if not force:
    decision = decide_consider(message, context=gate_context)
    
# Stage 2: Recent context (15 messages) for response
response_context = get_last_messages_text(n=15)  # More context than before (was 10)
response = decide_and_respond(message, context=response_context, buffer=buffer)
```

**Key Changes:**
- Gate uses clean buffer (detect solved threads)
- Response uses recent 15 messages (topic awareness)
- Increased from 10 → 15 messages for better context

---

### 2. Enhanced P_RESPOND_SYSTEM Prompt

**Added:**
```
КРИТИЧНО ВАЖЛИВО:
- Якщо є хоча б ОДИН релевантний вирішений CASE → respond=true!
- RETRIEVED CASES мають найвищу довіру - використовуй їх активно
- Порожній BUFFER не означає "не відповідай" - дивись на RETRIEVED CASES
- Краще дати конкретну відповідь з CASE, ніж мовчати
```

**Impact:** Bot should respond more confidently when it has relevant cases in KB

---

## Expected Results

| Metric | Before Fix | After Fix | Expected Change |
|--------|------------|-----------|-----------------|
| **Contains-Answer Pass** | 71.4% | **71.4%** | Maintain ✅ |
| **Answer Response Rate** | 30.4% | **40-50%** | +10-20pp ✅ |
| **Answer Pass Rate** | 13.0% | **20-30%** | +7-17pp ✅ |
| **Overall Pass Rate** | 60.0% | **65-70%** | +5-10pp ✅ |
| **Overall Score** | 6.16 | **7.0-7.5** | +0.8-1.3 ✅ |

---

## Why This Should Work

### Best of Both Worlds

**Clean Buffer (Stage 1):**
- ✅ Detects solved threads
- ✅ Prevents redundant responses
- ✅ Keeps +19pp improvement on contains-answer

**Rich Context (Stage 2):**
- ✅ Topic awareness from recent 15 messages
- ✅ Better understanding of ongoing discussions
- ✅ More confident responses when KB has relevant cases

---

## Files Modified

1. `signal-bot/app/jobs/worker.py`
   - Split context: clean buffer for gate, recent messages for response
   - Increased recent context: 10 → 15 messages

2. `signal-bot/app/llm/prompts.py`
   - Enhanced P_RESPOND_SYSTEM
   - Emphasized: "Empty buffer ≠ don't respond if you have relevant CASE"
   - Added critical importance of using RETRIEVED CASES

---

## Evaluation Status

**Running:** 400/100 streaming evaluation
**Started:** Just now
**Expected Duration:** 15-25 minutes
**Monitoring:** In progress...

---

## Success Criteria

✅ **Must Keep:**
- Contains-answer pass rate: ≥70% (was 71.4%)
- Contains-answer respond rate: ≤30% (was 28.6%)

✅ **Must Improve:**
- Answer response rate: ≥35% (was 30.4%, target 40%+)
- Answer pass rate: ≥20% (was 13.0%)
- Overall pass rate: ≥65% (was 60.0%)

✅ **Stretch Goal:**
- Overall score: ≥7.0 (was 6.16)

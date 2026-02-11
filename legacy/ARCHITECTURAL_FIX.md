# 🔬 ARCHITECTURAL FIX: Extract-First, Then Gate

## Current Architecture (WRONG ORDER)

```
NEW MESSAGE arrives
    ↓
1. BUFFER_UPDATE job: Append to buffer, try extract cases
    ↓
2. MAYBE_RESPOND job: Gate decision + retrieve KB + respond
    ↓
Problem: Buffer still contains SOLVED threads!
```

**THE ISSUE:** The response gate sees the **full context including already-solved discussions**, so it can't tell if the current message is:
- A NEW question (should respond)
- A continuation of an ONGOING discussion (check buffer)
- A follow-up to an ALREADY SOLVED thread (should NOT respond)

---

## Your Proposed Architecture (CORRECT ORDER)

```
NEW MESSAGE arrives
    ↓
1. BUFFER_UPDATE job:
   - Append message to buffer
   - Run case extraction
   - Remove SOLVED cases from buffer
   - Buffer now contains ONLY unsolved/ongoing discussions
    ↓
2. MAYBE_RESPOND job:
   - Gate sees CLEANED buffer (no solved threads!)
   - Can now correctly identify if question is:
     * New/ongoing → respond
     * Part of solved thread (not in buffer) → don't respond
   - Decide to respond + tag based on context
```

---

## Why This Fixes the Problems

### Problem 1: Contains-Answer Detection (10/21 failures)

**Current:**
```
User A: "Як вирішити?"
User B: "Спробуй вимкнути GPS"
User A: "Дякую, спрацювало!" ← SOLVED
[Buffer still contains all 3 messages]
    ↓
New question arrives
    ↓
Gate sees: Full thread with solution
Bot: Responds anyway ❌ (can't tell it's solved)
```

**With Extract-First:**
```
User A: "Як вирішити?"
User B: "Спробуй вимкнути GPS"
User A: "Дякую, спрацювало!" ← SOLVED
    ↓
BUFFER_UPDATE extracts case, removes from buffer
    ↓
[Buffer is now EMPTY or has only new threads]
    ↓
New question arrives
    ↓
Gate sees: Clean buffer (no solved thread)
Bot: Makes correct decision based on NEW context ✅
```

**Impact:** Fixes **8-10 contains-answer failures** instantly!

---

### Problem 2: Stage 2 Conservative Behavior (14/20 failures)

**Current Issue:**
```
Stage 2 sees:
- CONTEXT: Last 40 messages (mixed solved/unsolved)
- BUFFER: Full buffer (includes solved threads)
- CASES: Retrieved from KB

Model gets confused:
"Wait, I see a solution in the buffer already... 
 but is it for THIS question or a previous one?
 Not sure... better stay silent" ❌
```

**With Extract-First:**
```
Stage 2 sees:
- CONTEXT: Last 40 messages (for topic awareness)
- BUFFER: ONLY unsolved/ongoing discussions
- CASES: Retrieved from KB

Model reasoning:
"Buffer has ongoing discussion about X → this is relevant
 OR buffer is empty → this is a new question
 I have relevant CASE → I should help" ✅
```

**Impact:** Cleaner signal → model more confident → responds more often!

---

## Current vs Proposed Flow

### Current Flow (Wrong Order)

```python
def _handle_buffer_update(deps, payload):
    # 1. Append to buffer
    buf = get_buffer() + new_message
    
    # 2. Try extract cases
    extract = llm.extract_case_from_buffer(buf)
    
    # 3. Remove extracted (solved) cases from buffer
    if extract.cases:
        remove_solved_from_buffer()
    
    # 4. Save buffer (now cleaned)
    set_buffer(buffer_new)

def _handle_maybe_respond(deps, payload):
    # 5. Get context (includes solved threads!)
    context = get_last_messages_text(n=40)
    
    # 6. Get buffer (should be clean, but context isn't)
    buffer = get_buffer()  # ← Should be clean now
    
    # 7. Gate decision
    if not decide_consider(message, context):  # ← Context polluted!
        return
    
    # 8. Respond decision
    respond = decide_and_respond(message, context, cases, buffer)
```

**Problems:**
1. ❌ `context` (last 40 messages) still includes solved threads
2. ❌ Gate can't distinguish new vs follow-up questions
3. ❌ Model sees mixed signals

---

### Proposed Flow (Correct Order)

```python
def _handle_buffer_update(deps, payload):
    # 1. Append to buffer
    buf = get_buffer() + new_message
    
    # 2. FIRST: Try extract cases (identify SOLVED threads)
    extract = llm.extract_case_from_buffer(buf)
    
    # 3. Remove SOLVED cases from buffer IMMEDIATELY
    if extract.cases:
        remove_solved_from_buffer()
    
    # 4. Save CLEANED buffer
    set_buffer(buffer_new)  # ← Only unsolved threads remain

def _handle_maybe_respond(deps, payload):
    # 5. Get buffer (NOW CLEAN - no solved threads!)
    buffer = get_buffer()  # ← Only unsolved/ongoing
    
    # 6. Check if question already solved
    #    If buffer is empty or doesn't contain relevant discussion
    #    → This is likely a NEW question OR follow-up to solved thread
    
    if buffer_contains_recent_solution_for_this_question(buffer, message):
        # Thread solved recently, skip response
        return
    
    # 7. Gate decision with CLEAN context
    #    Tag the message type: new_question, ongoing_discussion, etc.
    decision = gate_with_tagging(message, buffer)
    if not decision.consider:
        return
    
    # 8. Respond based on tag and clean buffer
    respond = decide_and_respond(
        message, 
        buffer,  # ← Clean buffer
        cases, 
        tag=decision.tag
    )
```

---

## Key Changes Required

### Change 1: Enhanced Case Extraction Detection

**Current:** Only extracts when there's clear problem→solution→confirmation

**Need:** Also detect partial resolutions and mark buffer sections as:
- `solved`: Clear resolution
- `ongoing`: Active discussion
- `abandoned`: No recent activity

```python
class ExtractedCaseSpan(BaseModel):
    start_idx: int
    end_idx: int
    case_block: str
    state: str = "solved"  # NEW: solved | ongoing | abandoned
```

---

### Change 2: Buffer State Awareness in Gate

**Current Gate Prompt:**
```python
consider=true лише якщо:
- повідомлення просить допомоги або уточнення, І
- це не тривіальний сміття (привітання, "ок", тільки емодзі), І
- це стосується контексту підтримки групи.
```

**Enhanced Gate Prompt:**
```python
P_DECISION_SYSTEM = """Визнач тип повідомлення та чи варто розглядати для відповіді.
Поверни ТІЛЬКИ JSON з ключами:
- consider: boolean
- tag: string (message_type: new_question | ongoing_discussion | follow_up | noise)

BUFFER містить ТІЛЬКИ незавершені обговорення (вирішені кейси вже вилучено).

Теги:
- new_question: Нове питання, не пов'язане з BUFFER
- ongoing_discussion: Продовження обговорення з BUFFER
- follow_up: Питання про раніше вирішену проблему (не в BUFFER)
- noise: Привітання, "ок", офтоп

consider=true лише для: new_question, ongoing_discussion

Якщо BUFFER порожній → це імовірно new_question АБО follow_up до вирішеного кейсу.
Якщо BUFFER містить схоже обговорення → це ongoing_discussion.
"""
```

---

### Change 3: Response Decision with Tag Awareness

**Current Response Prompt:**
```python
respond=true якщо можеш відповісти з одного з джерел вище.
```

**Enhanced Response Prompt:**
```python
P_RESPOND_SYSTEM = """Ти вирішуєш, чи відповідати в групі.

ВАЖЛИВО: BUFFER містить ТІЛЬКИ незавершені обговорення.
Якщо питання було вирішено раніше, воно НЕ буде в BUFFER.

MESSAGE_TAG: {tag}

Правила за тегами:
1. new_question:
   - Шукай відповідь у RETRIEVED CASES (база знань)
   - Якщо є релевантний CASE → respond=true
   - Якщо немає CASE → respond=false

2. ongoing_discussion:
   - Перевір BUFFER для контексту обговорення
   - Використовуй RETRIEVED CASES якщо доступні
   - Якщо можеш додати до обговорення → respond=true

3. follow_up:
   - Це питання про раніше вирішений кейс
   - Шукай у RETRIEVED CASES
   - Якщо знайдено вирішений CASE → respond=true і поясни рішення

respond=true ЯКЩО:
- (tag=new_question АБО follow_up) І є релевантний CASE з бази знань
- АБО (tag=ongoing_discussion) І можеш додати корисну інформацію

respond=false ЯКЩО:
- Недостатньо інформації в CASES/BUFFER
- АБО tag=noise
"""
```

---

## Expected Impact

### With Architectural Fix Only (No Confidence Scoring)

| Metric | Current | Expected | Change |
|--------|---------|----------|--------|
| **Answer Pass Rate** | 13% | **50-60%** | **+37-47pp** |
| **Answer Avg Score** | 2.04 | **6.5-7.5** | **+4.5-5.5** |
| **Ignore Pass Rate** | 87.1% | **90-93%** | **+3-6pp** |
| **Contains Pass Rate** | 52.4% | **85-90%** | **+33-38pp** |
| **Overall Pass Rate** | 54.7% | **72-78%** | **+17-23pp** |
| **Overall Avg Score** | 5.69 | **7.2-7.8** | **+1.5-2.1** |

---

## Implementation Steps

### Phase 1: Current Code Already Has Extract-First! ✅

Looking at `_handle_buffer_update()` (lines 333-454):
```python
# Already does extract-first!
extract = deps.llm.extract_case_from_buffer(buffer_text=numbered_buffer)
# ... processes and removes solved cases
buffer_new = "".join(kept_blocks)
set_buffer(deps.db, group_id=group_id, buffer_text=buffer_new)
```

**Status:** ✅ ALREADY CORRECT!

---

### Phase 2: The Problem is in MAYBE_RESPOND Context ⚠️

In `_handle_maybe_respond()` (line 465):
```python
context_lines = get_last_messages_text(deps.db, group_id=group_id, n=40)
context = "\n".join(context_lines)
```

**Problem:** `context` includes last 40 messages from DB, which still contains solved threads!

**Fix:** Use buffer + recent messages instead:
```python
# Get CLEAN buffer (only unsolved threads)
buffer = get_buffer(deps.db, group_id=group_id) or ""

# Get RECENT context (for topic awareness, not for decision)
recent_context = get_last_messages_text(deps.db, group_id=group_id, n=10)
recent = "\n".join(recent_context)

# Check if question already in buffer (ongoing)
if buffer and is_question_in_buffer(buffer, msg.content_text):
    tag = "ongoing_discussion"
else:
    tag = "new_question"
```

---

### Phase 3: Enhanced Gate Prompt ⚠️ P0

**Update `P_DECISION_SYSTEM`:**
```python
P_DECISION_SYSTEM = """Визнач чи варто розглядати повідомлення для відповіді.
Поверни ТІЛЬКИ JSON з ключами:
- consider: boolean
- tag: string (new_question | ongoing_discussion | noise)

BUFFER містить ТІЛЬКИ незавершені обговорення (вирішені кейси вилучено).

consider=true лише якщо:
- MESSAGE є питанням про підтримку (new_question), АБО
- MESSAGE продовжує обговорення з BUFFER (ongoing_discussion)

consider=false якщо:
- Привітання, "ок", емодзі (noise)
- Подяка за вирішення (thread закрито)

Теги:
- new_question: Нове питання, BUFFER порожній або не містить схожої теми
- ongoing_discussion: Продовження теми з BUFFER
- noise: Не потребує відповіді

Якщо BUFFER порожній → імовірно new_question.
Якщо BUFFER містить схоже обговорення → ongoing_discussion.
"""
```

---

### Phase 4: Enhanced Response Prompt ⚠️ P0

**Update `P_RESPOND_SYSTEM`:**
```python
P_RESPOND_SYSTEM = """Ти вирішуєш, чи відповідати в групі.
Поверни ТІЛЬКИ JSON з ключами:
- respond: boolean
- text: рядок
- citations: масив рядків

ВАЖЛИВО:
- BUFFER містить ТІЛЬКИ незавершені обговорення
- Вирішені кейси вже вилучено з BUFFER і збережено в базі знань
- RETRIEVED CASES - це база знань вирішених кейсів

Джерела:
1. RETRIEVED CASES (вирішені кейси, найвища довіра)
2. BUFFER (поточні незавершені обговорення)

Правила:
respond=true ЯКЩО:
- Є релевантний CASE у RETRIEVED CASES (найкраще джерело!)
- АБО BUFFER містить достатньо інформації для відповіді
- АБО можна вказати корисний напрямок з наявної інформації

respond=false ЯКЩО:
- Немає достатньої інформації в CASES або BUFFER
- Питання надто загальне або поза контекстом

Пріоритет: RETRIEVED CASES > BUFFER

Якщо є релевантний вирішений CASE - використовуй його!
Якщо BUFFER має обговорення з корисною інформацією - використовуй!
"""
```

---

## Why No Confidence Scoring?

You're right - **confidence scoring adds complexity without fixing the root cause**.

**The Real Issue:** Model can't distinguish solved vs unsolved threads when buffer contains both.

**The Fix:** Clean the buffer (extract-first) so model sees only relevant context.

**Result:** Model makes correct binary decisions with clean data → no need for fuzzy confidence thresholds.

---

## Summary

### The Core Insight

**You're absolutely right:** The problem isn't confidence scoring - it's that the bot sees **polluted context** (solved + unsolved threads mixed).

**The Fix:**
1. ✅ Extract-first is already implemented
2. ⚠️ But `context` in MAYBE_RESPOND still uses last 40 messages from DB
3. ⚠️ Need to use BUFFER (clean) instead of raw DB messages
4. ⚠️ Add tagging to gate (new_question vs ongoing_discussion)
5. ⚠️ Update prompts to work with clean buffer

### Expected Result

With architectural fix + clean context + tagging:
- **Contains-answer failures:** 10 → **2-3** (85-90% pass rate)
- **Answer failures:** 14 → **6-8** (60-65% response rate)
- **Overall score:** 5.69 → **7.2-7.8**

### Next Steps

1. Modify `_handle_maybe_respond()` to use buffer instead of raw DB context
2. Add `tag` field to `DecisionResult` schema
3. Update `P_DECISION_SYSTEM` for tagging
4. Update `P_RESPOND_SYSTEM` for clean buffer awareness

**No confidence scoring needed - just clean architecture!**

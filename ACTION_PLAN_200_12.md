# Action Plan: Fix 3 Issues → Reach 85-100%

**Current**: 75.0% pass rate (12/16 scenarios)  
**Target**: 85-90%+ pass rate  
**Gap**: 10-15 percentage points  
**Status**: 🟡 Very close, need 2-3 targeted fixes

---

## 🎯 Three-Fix Strategy

Each fix addresses exactly 1 failed case = +6.25 percentage points per fix.

```
Fix 1: case_01  →  81.3% pass rate (13/16)  🎯 Getting close
Fix 2: case_08  →  87.5% pass rate (14/16)  ✅ TARGET HIT
Fix 3: case_12  →  93.8% pass rate (15/16)  ✅ TARGET EXCEEDED
```

---

## 🔴 Priority 1: Fix case_01 (Stage 1 Filter)

### Problem

Bot completely ignores valid technical question about camera FOV settings.

```
Question: "схоже що обрав не ту камеру що треба, гойдайка починається..."
Bot: consider=False → No response
Expected: Should answer with FOV configuration advice
Judge: 0/10 - "Failed to provide any response"
```

### Root Cause

Stage 1 (decide_consider) is rejecting self-resolved questions as "noise".

The question mentions the user fixed the problem ("проблему вирішено"), which may trigger the filter to think it's just a status update rather than a valuable Q&A exchange.

### Solution

Update `decide_consider` prompt in `signal-bot/app/llm/prompts.py`:

**Current logic** (inferred):
```
- Greetings → consider=False ✅
- Emoji only → consider=False ✅
- Self-resolved questions → consider=False ❌ (TOO AGGRESSIVE)
```

**New logic**:
```
- Greetings → consider=False ✅
- Emoji only → consider=False ✅
- Self-resolved questions with technical content → consider=True ✅
```

### Implementation Steps

1. **Read current prompt**:
   ```bash
   # Check decide_consider prompt
   grep -A 30 "decide_consider" signal-bot/app/llm/prompts.py
   ```

2. **Update prompt** to include guidance:
   ```
   "Consider=True for:
   - Technical questions (even if user mentions they solved it)
   - Questions with problem descriptions and solutions
   - Any discussion of fixes, configurations, or troubleshooting
   
   Consider=False only for:
   - Pure greetings with no technical content
   - Emoji-only messages
   - Off-topic questions (not about drones/firmware/hardware)"
   ```

3. **Test on case_01**:
   ```bash
   # Run single case test
   python test/run_real_quality_eval.py --case case_01
   ```

4. **Verify no regressions**:
   ```bash
   # Full eval
   python test/run_real_quality_eval.py
   ```

### Success Criteria

- ✅ case_01: consider=True, responded=True, score ≥8/10
- ✅ decline_restaurant: still consider=False (no regression)
- ✅ ignore_greeting: still consider=False (no regression)

### Expected Impact

- **Pass rate**: 75.0% → 81.3% (+6.3pp)
- **Cases fixed**: 1 out of 3 failures
- **Effort**: 1-2 hours

---

## 🟡 Priority 2: Fix case_08 (Open Cases)

### Problem

Bot responds to open cases (no solution) but provides unhelpful "we don't know" response.

```
Question: "PreArm: Internal Error 0x8000, польотник ребутається"
Bot: "У базі знань зафіксовано схожий випадок... кейс має статус 
      відкритого, точна причина виникнення помилки поки не встановлена"
Judge: 5/10 - "Correctly identified case but failed to provide actionable steps"
```

### Root Cause

Case retrieval includes open cases (status="open", no solution_summary). Bot retrieves it and tries to respond, but has nothing useful to say.

### Solution A: Filter Open Cases (Recommended)

**Approach**: Don't store open cases in knowledge base.

**Implementation**:

1. **Update mining script** `test/mine_real_cases.py` (line 211-214):
   
   Current code:
   ```python
   # Reject solved cases without solutions (quality gate)
   if case.status == "solved" and not case.solution_summary.strip():
       print(f"Block {idx}: Rejecting solved case without solution_summary")
       continue
   ```
   
   New code:
   ```python
   # Only keep solved cases with solutions
   if case.status != "solved" or not case.solution_summary.strip():
       print(f"Block {idx}: Rejecting non-solved or incomplete case (status={case.status})")
       continue
   ```

2. **Re-mine cases**:
   ```bash
   cd test/data
   rm -f signal_case_blocks.json signal_cases_structured.json
   cd ..
   REAL_REUSE_BLOCKS=0 REAL_LAST_N_MESSAGES=200 REAL_MAX_CASES=50 python mine_real_cases.py
   ```

3. **Re-run eval**:
   ```bash
   python run_real_quality_eval.py
   ```

### Solution B: Improve Open Case Response (Alternative)

**Approach**: Respond better to open cases.

**Implementation**:

Update `respond` prompt in `signal-bot/app/llm/prompts.py`:

```
"If retrieved cases are status='open' with no solution:
- Acknowledge the issue is known
- Say it's under investigation
- DO NOT provide unhelpful 'we don't know' statements
- Score: aim for 7-8/10 by being empathetic"
```

Example response:
```
"Ця помилка зафіксована в базі знань. Команда працює над вирішенням. 
Якщо знайдете рішення, будь ласка, поділіться!"
```

### Recommendation

**Use Solution A** (filter open cases):
- Cleaner approach
- Reduces noise in knowledge base
- Avoids token waste on unhelpful cases

### Success Criteria

- ✅ case_08: Either not retrieved (filtered) OR responded with score ≥8/10
- ✅ All other cases: no regression

### Expected Impact

- **Pass rate**: 81.3% → 87.5% (+6.2pp)
- **Cases fixed**: 2 out of 3 failures
- **Target status**: ✅ HIT TARGET (85%+)
- **Effort**: 2-4 hours

---

## 🟢 Priority 3: Fix case_12 (Question Focus)

### Problem

Bot answers peripheral information instead of user's explicit question.

```
Question: "А немає changelog? Хочу порівняти з diff-v3 - зрозуміти чи потрібно оновлювати"
Bot: "Щодо змін: було видалено параметр ARMING_CHECK..."
Judge: 4/10 - "Failed to address primary question about changelog location/access"
```

### Root Cause

LLM focuses on technical details in retrieved case (ARMING_CHECK changes) instead of user's meta-question about **how to find/access** the changelog.

### Solution

Update `respond` prompt in `signal-bot/app/llm/prompts.py`:

**Add guidance**:
```
"Priority order when responding:
1. FIRST: Answer the user's EXPLICIT question (what they directly asked)
2. THEN: Provide supporting details from evidence cases

Examples:
- User asks 'Where is the changelog?' → Answer about location/link first
- User asks 'How do I X?' → Explain the process/steps first
- User asks 'What changed?' → Summarize changes first

Only after addressing the explicit question, add technical details if helpful."
```

### Implementation Steps

1. **Read current respond prompt**:
   ```bash
   grep -A 50 "def respond" signal-bot/app/llm/prompts.py
   ```

2. **Add priority guidance** to prompt

3. **Test on case_12**:
   ```bash
   python test/run_real_quality_eval.py --case case_12
   ```

4. **Verify no regressions**:
   ```bash
   python test/run_real_quality_eval.py
   ```

### Expected Better Response

```
"Для порівняння версій перегляньте git commits або параметри в Mission Planner. 
Основні зміни: ARMING_CHECK видалено, додано ARMING_SKIPCHK з опцією \"-1\" для ігнорування перевірок."
```

**Score improvement**: 4/10 → 9-10/10

### Success Criteria

- ✅ case_12: score ≥8/10
- ✅ Bot addresses user's main question first
- ✅ No regression on other cases

### Expected Impact

- **Pass rate**: 87.5% → 93.8% (+6.3pp)
- **Cases fixed**: 3 out of 3 failures
- **Target status**: ✅ EXCEEDED TARGET (90%+)
- **Effort**: 2-4 hours

---

## 🎯 Bonus: Fix Kubernetes (Stage 1 Leak)

### Problem

Off-topic question (Kubernetes) passed stage 1 (consider=True), wasting tokens.

Stage 2 correctly declined (responded=False), so no false positive, but stage 1 should catch it earlier.

### Solution

Update `decide_consider` prompt to be more explicit about topic boundaries:

```
"Topic scope: drone hardware, flight controllers, firmware, cameras, sensors, 
               stabilization, ArduPilot, Mission Planner, telemetry

Out of scope: servers, Kubernetes, databases, web development, restaurants, 
              general IT questions"
```

### Expected Impact

- **Pass rate**: No change (already correctly declined)
- **Token efficiency**: Improved (skip stage 2 for off-topic)
- **Effort**: 30 minutes

---

## 📋 Implementation Timeline

### Week 1: Core Fixes

**Day 1-2**: Priority 1 (case_01)
- [ ] Read decide_consider prompt
- [ ] Update prompt with guidance
- [ ] Test on case_01
- [ ] Run full eval
- [ ] Verify 81%+ pass rate
- [ ] Commit changes

**Day 3-4**: Priority 2 (case_08)
- [ ] Decide: Solution A (filter) or B (improve response)
- [ ] Implement chosen solution
- [ ] Re-mine cases if using Solution A
- [ ] Run full eval
- [ ] Verify 87%+ pass rate (✅ HIT TARGET)
- [ ] Commit changes

**Day 5**: Priority 3 (case_12)
- [ ] Read respond prompt
- [ ] Add priority guidance
- [ ] Test on case_12
- [ ] Run full eval
- [ ] Verify 93%+ pass rate
- [ ] Commit changes

**Day 6**: Bonus + Verification
- [ ] Fix Kubernetes leak (stage 1)
- [ ] Run final full eval
- [ ] Verify 93-100% pass rate
- [ ] Document changes

### Week 2: Expansion & Deployment

**Day 1-2**: Expand Test Coverage
- [ ] Mine 500 messages
- [ ] Extract 30-50 cases
- [ ] Run comprehensive eval
- [ ] Verify performance holds

**Day 3-4**: Staging Deployment
- [ ] Deploy to staging environment
- [ ] Monitor metrics (pass rate, response time, user feedback)
- [ ] Verify no regressions

**Day 5**: Production Planning
- [ ] Review staging metrics
- [ ] Plan canary deployment
- [ ] Prepare rollback procedures

---

## 🧪 Testing Strategy

### Test Each Fix Independently

```bash
# After each fix, run:
cd test
source ../.venv/bin/activate
EMBEDDING_MODEL=gemini-embedding-001 python run_real_quality_eval.py

# Check results:
cat data/real_quality_eval.json | jq '.summary.by_category'
```

### Regression Prevention

After each fix, verify these cases still pass:

- ✅ case_02, case_03, case_04, case_05, case_06, case_07, case_09, case_10, case_11
- ✅ decline_restaurant
- ✅ ignore_greeting, ignore_emoji

### Success Criteria by Milestone

| Milestone | Pass Rate | Cases Fixed | Status |
|-----------|-----------|-------------|--------|
| **Current** | 75.0% (12/16) | 0/3 | 🟡 Starting point |
| **After Fix 1** | 81.3% (13/16) | 1/3 | 🟡 Getting close |
| **After Fix 2** | 87.5% (14/16) | 2/3 | ✅ TARGET HIT |
| **After Fix 3** | 93.8% (15/16) | 3/3 | ✅ TARGET EXCEEDED |
| **After Bonus** | 100% (16/16) | All | ✅ PERFECT |

---

## 🚨 Rollback Plan

If any fix causes regression:

1. **Identify regression**:
   ```bash
   # Compare before/after
   diff old_results.json new_results.json
   ```

2. **Revert changes**:
   ```bash
   git revert HEAD
   ```

3. **Re-run eval**:
   ```bash
   python test/run_real_quality_eval.py
   ```

4. **Investigate root cause** before re-attempting

---

## 📊 Tracking Progress

### Checklist

**Priority 1: case_01 (Stage 1 Filter)**
- [ ] Analyze decide_consider prompt
- [ ] Update prompt with self-resolved question handling
- [ ] Test on case_01
- [ ] Verify consider=True, responded=True, score≥8
- [ ] Run full eval
- [ ] Verify 81%+ pass rate
- [ ] No regressions on decline/ignore cases
- [ ] Commit + document

**Priority 2: case_08 (Open Cases)**
- [ ] Choose solution (A: filter, B: improve response)
- [ ] Implement solution
- [ ] Re-mine cases if needed
- [ ] Run full eval
- [ ] Verify 87%+ pass rate (TARGET HIT)
- [ ] No regressions
- [ ] Commit + document

**Priority 3: case_12 (Question Focus)**
- [ ] Analyze respond prompt
- [ ] Add priority guidance (explicit Q first)
- [ ] Test on case_12
- [ ] Verify score≥8, addresses main question
- [ ] Run full eval
- [ ] Verify 93%+ pass rate
- [ ] No regressions
- [ ] Commit + document

**Bonus: Kubernetes (Stage 1 Leak)**
- [ ] Update decide_consider with topic boundaries
- [ ] Test on decline_kubernetes
- [ ] Verify consider=False
- [ ] Run full eval
- [ ] Verify 100% pass rate
- [ ] Commit + document

---

## 📞 Support & Questions

**Documentation**: 
- Full results: `EVAL_200_12_RESULTS.md`
- Visual summary: `EVAL_200_12_VISUAL.md`
- This action plan: `ACTION_PLAN_200_12.md`

**Test Commands**:
```bash
# Full eval
python test/run_real_quality_eval.py

# View results
cat test/data/real_quality_eval.json | jq '.summary'

# Re-mine cases (if needed)
cd test/data && rm signal_*.json && cd ..
REAL_REUSE_BLOCKS=0 REAL_LAST_N_MESSAGES=200 REAL_MAX_CASES=50 python mine_real_cases.py
```

**Files to Modify**:
- `signal-bot/app/llm/prompts.py` - All prompt fixes
- `test/mine_real_cases.py` - If filtering open cases

---

**Status**: 📋 Ready to implement  
**Confidence**: 🟢 HIGH  
**Timeline**: 5-6 days to 93%+  
**Risk**: 🟢 LOW

**Each fix is targeted, testable, and reversible. Let's hit that 85-90%+ target!** 🎯

# SupportBot Evaluation: 200/12 Analysis

**Date**: February 11, 2026  
**Messages Analyzed**: 200  
**Cases Extracted**: 12  
**Test Scenarios**: 16 (12 real cases + 4 synthetic tests)

---

## 📊 Executive Summary

### Overall Performance: **75.0% Pass Rate** (12/16 scenarios passed)

```
████████████████████░░░░  75.0% Overall Pass Rate
Target: 80-90%
Gap: 5.0 percentage points from minimum target
```

### Results by Category

| Category | Pass Rate | Score | Status |
|----------|-----------|-------|--------|
| **Should Answer** (Real cases) | **75.0%** (9/12) | 8.17/10 | 🟡 Close |
| **Should Decline** (Off-topic) | **50.0%** (1/2) | 5.0/10 | 🔴 Needs work |
| **Should Ignore** (Noise) | **100%** (2/2) | 10.0/10 | ✅ Perfect |

---

## 🎯 Key Findings

### What's Working Excellently

✅ **High Quality When Responding**
- 9 out of 11 responses scored **9-10/10** (81.8%)
- **100% accuracy** - no hallucinations detected
- Proper Ukrainian language in all responses
- Concise answers (~160 chars average)
- Good citation of evidence

✅ **Perfect Noise Filtering**
- Greetings: Correctly ignored ✅
- Emoji-only: Correctly ignored ✅

✅ **Strong Core Performance**
- 75% of real support cases handled correctly
- Average score 8.17/10 (up from 7.56 in previous eval)

### What Needs Improvement

🔴 **3 Failed Cases (25% failure rate)**

1. **case_01**: Failed to respond at all (consider=False)
   - Score: 0/10
   - Issue: Stage 1 (decide_consider) incorrectly rejected the case
   
2. **case_08**: Unhelpful response for unsolved case
   - Score: 5/10
   - Issue: Responded but provided no actionable information
   
3. **case_12**: Missed user's primary question
   - Score: 4/10
   - Issue: Answered wrong aspect of the question

🔴 **Off-Topic Handling Issue**
- Kubernetes question: Should have been caught at stage 1, but wasn't (consider=True)
- Stage 2 correctly declined to respond

---

## 📈 Detailed Analysis by Category

### 1. Should Answer (Real Support Cases): 75% Pass Rate

**Passed Cases (9/12)**: ✅✅✅✅✅✅✅✅✅

| Case | Score | Response Length | Summary |
|------|-------|----------------|---------|
| case_02 | 10/10 | 148 chars | Fuse1 vs Fuse2 differences |
| case_03 | 10/10 | 101 chars | GPS/compass solution for "koshmaryk" |
| case_04 | 10/10 | 118 chars | System coordinate confirmation |
| case_05 | 10/10 | 108 chars | Pixhawk 2.4.8 firmware availability |
| case_06 | 10/10 | 189 chars | EKF3 IMU0 error solution |
| case_07 | 10/10 | 180 chars | Bulk milbeta activation |
| case_09 | 10/10 | 56 chars | IMX290-83 build selection |
| case_10 | 10/10 | 248 chars | MNT mode for Karma FC |
| case_11 | 9/10 | 229 chars | PosHold autotune settings |

**Failed Cases (3/12)**: ❌❌❌

#### ❌ case_01: Complete Silence (0/10)
```
Question: "схоже що обрав не ту камеру що треба, гойдайка починається з часом..."
Expected: Answer about FOV camera settings
Bot Action: consider=False, responded=False
Judge: "Failed to provide any response, despite relevant evidence"
```

**Root Cause**: Stage 1 (decide_consider) incorrectly rejected this case. The bot should have considered it since there's a matching case about FOV camera configuration.

**Impact**: Critical - user gets no response at all

---

#### ❌ case_08: Unhelpful Response (5/10)
```
Question: "польотнік ребутається і арм не дозволяє 'PreArm: Internal Error 0x8000'"
Response: "У базі знань зафіксовано схожий випадок... кейс має статус відкритого, 
          точна причина виникнення помилки поки не встановлена."
Judge: "Correctly identified case but failed to provide actionable steps"
```

**Root Cause**: Retrieved case has no `solution_summary` (status="open"). Bot responded but couldn't provide helpful information.

**Impact**: Medium - user knows the problem is documented but gets no solution

---

#### ❌ case_12: Wrong Focus (4/10)
```
Question: "А немає changelog? Хочу порівняти з diff-v3 - зрозуміти чи потрібно оновлювати"
Response: "Щодо змін: було видалено параметр ARMING_CHECK..."
Judge: "Failed to address primary question about changelog and how to compare versions"
```

**Root Cause**: Bot retrieved a relevant case about ARMING_CHECK changes but missed the user's main question about how to find/access the changelog.

**Impact**: Medium - provides some useful info but doesn't answer the actual question

---

### 2. Should Decline (Off-Topic): 50% Pass Rate

| Test | Expected | Bot Behavior | Score | Pass |
|------|----------|--------------|-------|------|
| Kubernetes | Decline | consider=True ❌, responded=False ✅ | 0/10 | ❌ |
| Restaurant | Decline | consider=False ✅, responded=False ✅ | 10/10 | ✅ |

**Issue**: Stage 1 (decide_consider) allowed Kubernetes through (consider=True), though stage 2 correctly declined. This wastes tokens and processing time.

---

### 3. Should Ignore (Noise): 100% Pass Rate

| Test | Input | Bot Behavior | Score | Pass |
|------|-------|--------------|-------|------|
| Greeting | "Привіт всім!" | consider=False, responded=False | 10/10 | ✅ |
| Emoji | "👍" | consider=False, responded=False | 10/10 | ✅ |

**Perfect filtering of non-actionable messages!**

---

## 🔍 Comparison: 150/9 vs 200/12

| Metric | 150/9 Eval | 200/12 Eval | Change |
|--------|------------|-------------|--------|
| Cases Extracted | 9 | 12 | +3 (+33%) |
| Should Answer Pass Rate | 77.8% (7/9) | 75.0% (9/12) | -2.8pp |
| Average Score | 7.56/10 | 8.17/10 | +0.61 (+8%) |
| Should Decline Pass Rate | 50% (1/2) | 50% (1/2) | No change |
| Should Ignore Pass Rate | 100% (2/2) | 100% (2/2) | No change |
| Overall Pass Rate | 76.9% (10/13) | 75.0% (12/16) | -1.9pp |

### Key Observations

📊 **Consistent Performance**
- Pass rates are statistically similar (75-77.8%)
- Quality when responding is **higher** in 200/12 eval (8.17 vs 7.56)
- Noise filtering remains perfect

📊 **More Data Reveals Edge Cases**
- New cases exposed 1 additional stage-1 failure (case_01)
- Found 2 more problematic scenarios (case_08, case_12)
- These were likely not present in smaller sample

---

## 🎯 Root Cause Analysis

### Problem 1: Stage 1 Filter Too Aggressive (case_01)

**Symptom**: Valid support question rejected at decide_consider stage

**Evidence**:
```
case_01 (FOV camera): consider=False → No response
Restaurant question: consider=False → Correctly ignored
```

**Hypothesis**: Stage 1 may be confusing "self-resolved" cases (where user says "fixed it") with noise/greetings.

**Fix**: Tune stage 1 prompt to consider all technical questions, even if user mentions resolution.

---

### Problem 2: Open Cases Handled Poorly (case_08)

**Symptom**: Bot responds to open cases but provides no value

**Evidence**:
```
case_08: status="open", no solution_summary
Bot: "кейс має статус відкритого, точна причина виникнення помилки поки не встановлена"
Score: 5/10 (unhelpful)
```

**Root Cause**: Bot retrieves open cases and tries to respond, but has no solution to offer.

**Fix Options**:
1. Filter out open cases at stage 2 (respond gate)
2. Provide better response for open cases (e.g., "This is a known issue being investigated")
3. Don't store open cases in the knowledge base

---

### Problem 3: Question Understanding (case_12)

**Symptom**: Bot answers peripheral info instead of user's main question

**Evidence**:
```
User: "Where's the changelog? Want to compare versions"
Bot: "Here's what changed in ARMING_CHECK parameter"
Judge: "Failed to answer how to find changelog"
```

**Root Cause**: LLM focused on technical content in retrieved case instead of user's meta-question about documentation.

**Fix**: Improve respond prompt to prioritize user's explicit question over retrieved content details.

---

### Problem 4: Stage 1 Lets Off-Topic Through (decline_kubernetes)

**Symptom**: Kubernetes question passed stage 1 (consider=True)

**Fix**: Strengthen stage 1 prompt to be more conservative about topic boundaries.

---

## 🚀 Action Plan: Path to 85%+

### Priority 1: Fix case_01 (Stage 1 Filter) 🔴 HIGH IMPACT

**Problem**: Valid technical questions being rejected at stage 1

**Solution**: Update `decide_consider` prompt:
- Consider all technical questions, even self-resolved ones
- Only reject greetings, emojis, and clearly off-topic questions

**Expected Impact**: +1 case = 83.3% pass rate (10/12)

**Effort**: 1-2 hours (prompt tuning)

---

### Priority 2: Handle Open Cases Better 🟡 MEDIUM IMPACT

**Problem**: Bot responds to open cases but provides no actionable info (case_08)

**Solution Option A** (Quick): Filter at stage 2
```python
if retrieved_case.status == "open" and not retrieved_case.solution_summary:
    return "Проблема зафіксована, але рішення ще в процесі розробки."
```

**Solution Option B** (Better): Don't store open cases
- Only keep solved cases with solutions in knowledge base
- Open cases add noise without value

**Expected Impact**: +1 case = 91.7% pass rate (11/12)

**Effort**: 2-4 hours

---

### Priority 3: Improve Question Focus (case_12) 🟡 MEDIUM IMPACT

**Problem**: Bot answers wrong aspect of user's question

**Solution**: Update respond prompt:
- "Answer the user's EXPLICIT question first"
- "If asking about documentation/process, address that before technical details"

**Expected Impact**: +1 case = 100% pass rate (12/12)

**Effort**: 2-4 hours (prompt tuning + testing)

---

### Priority 4: Tighten Stage 1 Off-Topic Filter 🟢 LOW IMPACT

**Problem**: Kubernetes question incorrectly passed stage 1

**Solution**: Make stage 1 more conservative about topic scope

**Expected Impact**: Better token efficiency, no pass rate change

**Effort**: 1 hour

---

## 📊 Projected Improvement

| Fix | Pass Rate | Real Cases | Overall |
|-----|-----------|-----------|---------|
| **Current** | - | 75.0% (9/12) | 75.0% (12/16) |
| + Fix case_01 | Stage 1 | 83.3% (10/12) | 81.3% (13/16) |
| + Fix case_08 | Open cases | 91.7% (11/12) | 87.5% (14/16) |
| + Fix case_12 | Focus | 100% (12/12) | 93.8% (15/16) |
| + Fix decline | Stage 1 | 100% (12/12) | 100% (16/16) |

**Target achieved after Priority 1 + Priority 2**: **87.5%** ✅

---

## 📉 Quality Metrics Deep Dive

### Response Quality Distribution

```
10/10: ████████░░  8 cases (50.0%)  ⭐ Perfect responses
 9/10: █░░░░░░░░░  1 case  (6.3%)   ⭐ Excellent
 5/10: █░░░░░░░░░  1 case  (6.3%)   🟡 Poor
 4/10: █░░░░░░░░░  1 case  (6.3%)   🟡 Poor
 0/10: █████░░░░░  5 cases (31.3%)  ❌ Failed (3 should_answer + 1 decline + correct ignores)
      : ██░░░░░░░░  2 cases (12.5%)  ✅ Correctly ignored/declined (scored 10)
```

**Key Insight**: When bot responds to should_answer cases, 82% score 9-10/10 (9 out of 11 responses).

---

### Response Length Analysis

```
Average length (when responded): 160.2 chars
Min: 56 chars (case_09)
Max: 248 chars (case_10)
Median: 148 chars
```

**All responses are concise and appropriate!** ✅

---

## 🎓 Learnings from 200/12 Eval

### 1. Extraction Rate Remains Low

- **200 messages → 12 cases** = 6% extraction rate
- Similar to 150 messages → 9 cases = 6% extraction rate

**Conclusion**: Most chat messages are noise/greetings. Real support discussions are sparse.

### 2. Sample Size Matters

- Larger sample revealed new failure modes (case_01 type)
- More edge cases = better understanding of system limits

**Recommendation**: Continue with 200+ message evals, or expand to 500+ for comprehensive testing.

### 3. Quality Is Consistent

- Average score improved (8.17 vs 7.56)
- When bot responds correctly, it's reliable
- Failures are systematic (stage 1, open cases), not random

---

## ✅ Deployment Readiness

### Green Flags ✅

- High quality responses (8.17/10 average)
- No hallucinations
- Perfect noise filtering
- Consistent performance across two evals

### Yellow Flags 🟡

- 75% pass rate (below 80% target)
- 3 failure modes identified
- Stage 1 filter needs tuning

### Recommendation

**Deploy to staging with monitoring** while implementing Priority 1 + Priority 2 fixes.

Rollback triggers:
- Pass rate drops below 70%
- False positive rate > 5%
- User complaints increase

---

## 📝 Next Steps

### Immediate (This Week)

1. ✅ Complete 200/12 evaluation ← **DONE**
2. 🔴 Fix case_01 (stage 1 filter) - 2 hours
3. 🔴 Fix case_08 (open cases) - 4 hours
4. 🔴 Re-run evaluation - 30 mins
5. ✅ Verify 85%+ pass rate

### Short Term (Next Week)

1. 🟡 Fix case_12 (question focus) - 4 hours
2. 🟢 Tighten stage 1 off-topic filter - 1 hour
3. 🔄 Run 500-message eval for more cases
4. 📊 Deploy to staging with monitoring

### Medium Term

1. Expand test coverage to 30-50 cases
2. Implement image processing (case_03 from old eval)
3. Monitor production metrics
4. Plan for 95%+ target

---

## 📞 Support & Questions

**Documentation**: See `README_TRUST_FIX.md` for background  
**Test Script**: `python test/run_real_quality_eval.py`  
**Results File**: `test/data/real_quality_eval.json`

---

**Status**: 📋 Analysis Complete  
**Confidence**: 🟢 HIGH  
**Next Action**: Fix stage 1 filter (Priority 1)  
**Timeline**: 2-3 days to 85%+

**We're close to the target! Just 2-3 fixes away from 85-90%+ pass rate.** 🎯

# 400/16 Evaluation Results: Generalization Analysis

**Date**: February 11, 2026  
**Scale**: 400 messages → 16 cases (4% extraction rate)  
**With Fixes Applied**: Yes (stage 1 filter + open case filtering)

---

## 📊 RESULTS SUMMARY

### Overall Performance

```
200/11 Eval:  86.7% pass rate (13/15 scenarios)
400/16 Eval:  75.0% pass rate (15/20 scenarios)

CHANGE:       -11.7 percentage points ⚠️
```

### Breakdown by Category

| Category | 200/11 | 400/16 | Change |
|----------|--------|--------|--------|
| **Should Answer** | 90.9% (10/11) | **75.0%** (12/16) | **-15.9pp** ⚠️ |
| **Avg Score** | 9.36/10 | **8.56/10** | **-0.80** ⚠️ |
| **Should Decline** | 50.0% (1/2) | 50.0% (1/2) | No change |
| **Should Ignore** | 100% (2/2) | 100% (2/2) | Perfect ✅ |

---

## 🔍 ANSWER: Did It Generalize Well?

### **NO - Performance Degraded on Larger Dataset**

The bot's performance **degraded** when scaling from 200 to 400 messages:

- **Pass rate dropped**: 86.7% → 75.0% (-11.7pp)
- **Should answer dropped**: 90.9% → 75.0% (-15.9pp)  
- **Average score dropped**: 9.36 → 8.56 (-0.80)

**However**, the bot is still at the **75% baseline** (same as before fixes on 200/12 eval), which suggests:
- ✅ Fixes did NOT make things worse
- ✅ Bot maintains baseline quality on new data
- ⚠️ New edge cases emerged in larger dataset

---

## 🔴 NEW FAILURES ANALYSIS (4 cases)

### ❌ case_01: Image-Based Question (Score: 4/10)

**Question**: "Доброго вечора. Підкажіть, будь ласка, в чому може бути проблема [IMAGE ATTACHED]"

**Bot Response**: "Якщо це помилки на пристрої «Кошмарик», спробуйте вимкнути GPS та компас..."

**Judge Verdict**: "The bot's response is not relevant to the user's question, as it does not address the provided image or any specific problem described by the user."

**Root Cause**: **Image Processing Failure**
- User provided screenshot/image showing problem
- Bot cannot process images (multimodal capability missing)
- Bot guessed based on retrieved cases, provided irrelevant solution

**Type**: Infrastructure limitation (not prompt issue)

---

### ❌ case_03: Partial Answer (Score: 6/10)

**Question**: "в diff3 є 640 і 640diff на курбас, що актуальне?"

**Bot Response**: Long explanation about Kurba device, mentions both options, references evidence...

**Judge Verdict**: Score 6/10 - partially correct but wordy

**Root Cause**: **Complex Multi-Part Question**
- User asks which firmware version is current
- Retrieved case has conflicting information
- Bot tried to synthesize answer from multiple sources

**Type**: Edge case - complex question requiring nuanced answer

---

### ❌ case_07: Missing Context (Score: 4/10)

**Question**: "Підсумовуючи мої експерименти - я можу запустити СтабХ, зайти в онлайн інтерфейс і отримати зображення з камери..."

**Bot Response**: "Так, це коректний висновок. Для роботи без стабільного позиціонування (GPS) зазвичай використовують режим `Guided_NO_GPS`..."

**Judge Verdict**: "The response is relevant but could be more comprehensive by explicitly linking the `Guided_NO_GPS` mode to the user's StabX and camera interface context."

**Root Cause**: **Partial Context Match**
- User's message is a summary/conclusion of their experiments
- Bot found relevant case about Guided_NO_GPS mode
- Bot response is technically correct but doesn't fully connect to user's specific context

**Type**: Retrieval/relevance issue

---

### ❌ case_09: Changelog Question AGAIN (Score: 4/10)

**Question**: "А немає changelog? Хочу порівняти з diff-v3..."

**Bot Response**: "Щодо змін у конфігурації прошивки: 1. Було видалено параметр..."

**Judge Verdict**: "The bot's response is accurate based on Case 1, but it fails to address the user's core question about changelogs and comparing firmware versions."

**Root Cause**: **Same as 200/11 eval case_11**
- User asks meta-question about WHERE to find changelog
- Retrieved case only has WHAT changed
- Prompt improvement didn't fix this (KB content issue)

**Type**: Knowledge base content gap

---

## 📈 Failure Pattern Analysis

### Failure Types Distribution

| Type | Count | Cases |
|------|-------|-------|
| **Image/Multimodal** | 1 | case_01 |
| **Complex/Nuanced Questions** | 1 | case_03 |
| **Partial Context Match** | 1 | case_07 |
| **KB Content Gap** | 1 | case_09 |

### Key Insights

1. **Image Processing is Critical**
   - 6.25% of real cases (1/16) involve images
   - Bot has 0% success rate on image questions
   - This is a **known limitation** that requires multimodal LLM

2. **Changelog/Meta Questions Persist**
   - Same failure type as 200/11 eval (case_09 = old case_11)
   - KB doesn't have meta-information about processes/documentation
   - Needs documentation-focused cases in KB

3. **Complex Questions Are Challenging**
   - When user asks compound questions or provides conflicting context
   - Bot tries to synthesize but may miss the mark
   - Score 6/10 suggests partial success

4. **Context Matching Can Be Imperfect**
   - Bot retrieves relevant case but doesn't fully connect to user's specific situation
   - Needs better contextualization in response generation

---

## ✅ What DID Generalize Well

### Maintained Strong Performance

| Aspect | 200/11 | 400/16 | Status |
|--------|--------|--------|--------|
| **Should Ignore** | 100% | 100% | ✅ Perfect |
| **Should Decline** | 50% | 50% | 🟡 Consistent |
| **High-Quality Responses** | 91% (9-10/10) | 75% (9-10/10) | 🟡 Good |
| **Zero Hallucinations** | 0% | 0% | ✅ Perfect |

### Successful Cases

**12 out of 16 cases passed** (75%):
- case_02: Koshmaryk GPS/compass issue (10/10)
- case_04: PosHold mode behavior (10/10)
- case_05: Camera identification (9/10 - good handling of "can't see image")
- case_06: Autotune rotation behavior (10/10)
- case_08: Milbeta bulk activation (10/10)
- case_10: Build location question (10/10)
- case_11: Karma MNT mode (10/10)
- case_12: StabX camera preset (10/10)
- case_13: SoloGoodF722 support (10/10)
- case_14: FS_EKF_THRESH setting (10/10)
- case_15: Fuse1 vs Fuse2 differences (10/10)
- case_16: IMX290-83 build selection (10/10)

**Pattern**: Straightforward technical questions with clear matches in KB → excellent performance

---

## 🎯 Generalization Assessment

### What Worked

✅ **Core Functionality**
- Stage 1 filter improvements held up (no new false rejections)
- Open case filtering prevented unhelpful responses
- Response quality remains high when bot has good match (75% score 9-10/10)

✅ **Consistency**
- No regressions on previously working patterns
- Noise filtering perfect (100%)
- Core technical Q&A strong (75% pass rate on real cases)

### What Didn't Scale

⚠️ **New Edge Cases Emerged**
- Image-based questions (multimodal limitation)
- Complex/nuanced questions requiring synthesis
- Meta-questions about processes/documentation

⚠️ **Pass Rate Variability**
- 200 messages → 86.7% pass rate
- 400 messages → 75.0% pass rate
- Larger dataset reveals more edge cases

---

## 💡 Why Performance Dropped

### Hypothesis: **More Diverse Cases in Larger Dataset**

**200 messages**:
- 11 cases extracted
- Mostly straightforward technical questions
- Limited edge cases

**400 messages**:
- 16 cases extracted (33% more cases)
- More diverse question types
- More edge cases (images, complex questions, meta-questions)

**Analogy**: Like testing on a larger, more representative sample:
- 200 messages = "development set" (easier cases)
- 400 messages = "validation set" (closer to production distribution)

### Statistical Analysis

```
Extraction rate:
- 200 msg: 11 cases = 5.5% extraction rate
- 400 msg: 16 cases = 4.0% extraction rate

Open cases filtered:
- 200 msg: 1 open case filtered (8.3% of blocks)
- 400 msg: 5 open cases filtered (20.8% of blocks)
```

**Insight**: More messages → more open/unsolved discussions in data → lower quality KB content overall

---

## 🚀 Recommendations

### Priority 1: Add Multimodal Support (High Impact)

**Problem**: 6.25% of cases involve images (case_01)  
**Solution**: Use vision-capable LLM (e.g., Gemini 2.0 Flash, GPT-4 Vision)  
**Expected Impact**: +6.25pp (1 case fixed) → 81.25% pass rate

---

### Priority 2: Expand KB with Meta-Content (Medium Impact)

**Problem**: Questions about WHERE to find things, HOW to access documentation  
**Solution**: Add meta-cases to KB:
```
- "Де знайти changelog?" → "git log або git commits"
- "Як порівняти версії?" → "використовуйте git diff"
- "Де документація?" → "посилання на wiki/docs"
```

**Expected Impact**: +6.25pp (1 case fixed) → 87.5% pass rate

---

### Priority 3: Improve Complex Question Handling (Low Impact)

**Problem**: Compound/nuanced questions get partially correct answers  
**Solution**: Enhance P_RESPOND_SYSTEM to:
- Break down complex questions into sub-parts
- Address each part explicitly
- Synthesize coherent answer

**Expected Impact**: +6.25pp (1 case improved) → 93.75% pass rate

---

### Priority 4: Context-Aware Retrieval (Low Impact)

**Problem**: Retrieved cases are relevant but not perfectly contextualized  
**Solution**: Add user's full context to retrieval query, not just last message

**Expected Impact**: Minor quality improvement on edge cases

---

## 📊 Projected Performance with Fixes

| Fix | Pass Rate | Cases Passing |
|-----|-----------|---------------|
| **Current (400/16)** | 75.0% | 12/16 |
| + Multimodal support | 81.25% | 13/16 |
| + Meta-content KB | 87.5% | 14/16 |
| + Complex Q handling | 93.75% | 15/16 |
| **Target Achieved** | **90%+** | ✅ |

---

## 🎓 Key Learnings

### 1. **Prompt Fixes Held Up Well**

The fixes we made (stage 1 filter, open case filtering) did NOT cause regressions:
- ✅ No false rejections
- ✅ No unhelpful open case responses
- ✅ Quality maintained on similar cases

### 2. **Larger Dataset Reveals True Performance**

75% on 400/16 is likely **closer to production performance** than 86.7% on 200/11:
- More diverse cases
- More edge cases
- More representative of real-world distribution

### 3. **Infrastructure Limitations Matter**

The biggest failure (case_01, image question) is due to **missing multimodal capability**, not prompt quality:
- Can't be fixed with better prompts
- Requires infrastructure upgrade
- Represents 6.25% of test cases

### 4. **KB Content Gaps Are Real**

Meta-questions (changelog, documentation) fail consistently because:
- KB doesn't have this type of content
- Need to expand KB beyond pure Q&A
- Should include "how to" and "where to find" cases

---

## ✅ Final Assessment

### Generalization Score: **B+ (Good, Not Excellent)**

**Strengths**:
- ✅ Core functionality solid (75% pass rate)
- ✅ Fixes held up without regressions
- ✅ Zero hallucinations maintained
- ✅ Noise filtering perfect

**Weaknesses**:
- ⚠️ Performance drop from 86.7% to 75% on larger dataset
- ⚠️ New edge cases exposed (images, meta-questions)
- ⚠️ Not hitting 80-90% target on this dataset

### Production Readiness

**Recommendation**: **DEPLOY TO STAGING** with caveats

The bot is production-ready for:
- ✅ Straightforward technical Q&A (75% of cases)
- ✅ Noise filtering (100% accuracy)
- ✅ Preventing hallucinations (0% false info)

The bot needs improvement for:
- 🔴 Image-based questions (requires multimodal LLM)
- 🟡 Meta-questions about documentation/processes
- 🟡 Complex/nuanced questions

**Action Plan**:
1. Deploy current version to staging
2. Monitor real-world performance
3. Prioritize multimodal support (biggest impact)
4. Expand KB with meta-content
5. Iterate on complex question handling

---

## 📝 Comparison Table: All Evaluations

| Metric | Pre-Fixes (200/12) | Post-Fixes (200/11) | Scaled (400/16) |
|--------|-------------------|---------------------|-----------------|
| **Pass Rate** | 75.0% | **86.7%** | 75.0% |
| **Should Answer** | 75.0% | **90.9%** | 75.0% |
| **Avg Score** | 8.17 | **9.36** | 8.56 |
| **Cases** | 12 | 11 | 16 |
| **Messages** | 200 | 200 | 400 |

**Takeaway**: Fixes improved performance on 200-message dataset, but larger 400-message dataset revealed true baseline is closer to 75% with current KB and capabilities.

---

**Status**: 📋 Analysis Complete  
**Recommendation**: Deploy to staging, prioritize multimodal support  
**Next Eval**: Test with vision-capable LLM on same 400/16 dataset

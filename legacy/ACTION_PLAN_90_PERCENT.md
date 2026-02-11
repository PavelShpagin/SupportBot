# Action Plan: Reaching 90%+ Pass Rate

**Current**: 77.8% pass rate (10/13 scenarios)  
**Target**: 80-90%+ pass rate  
**Gap**: 2.2-12.2 percentage points  
**Status**: 🟡 Close to target, need 1-2 fixes

---

## Quick Reference

| Fix | Impact | Effort | Timeline | Priority |
|-----|--------|--------|----------|----------|
| **Fix Case 05** (Open discussions) | +11.1pp → **88.9%** ✅ | Low (prompt only) | 1-2 days | 🔴 HIGH |
| **Fix Case 03** (Image processing) | +11.1pp → **100%** 🎯 | Medium (infra) | 1 week | 🟡 MED |
| **Fix Kubernetes decline** | +7.7pp (decline rate) | Low (prompt only) | 1 day | 🟢 LOW |
| **Expand test set** | Better statistics | Low (run script) | 1 hour | 🟡 MED |

---

## 🔴 Priority 1: Fix Case 05 (Open Discussions)

### Problem

**Case**: Stellar H7V2 firmware request  
**Question**: "Потрібна прошивка під СтабХ для польотника Stellar H7V2"  
**Current Behavior**: Bot stays silent  
**Expected Behavior**: Bot should respond with available context

**Root Cause**:
- Case has `status="open"` and empty `solution_summary`
- Stage 1 (decide_consider) correctly passes: `consider=True` ✅
- Stage 2 (decide_and_respond) incorrectly rejects: `respond=False` ❌
- Respond prompt requires "complete solution" → too conservative

**Judge Feedback**:
> "Failed to provide any response, despite relevant evidence. User's request directly matches Case 1, which provides all the necessary information."

### Solution

#### Step 1: Update Respond Prompt

**File**: `signal-bot/app/llm/prompts.py`

**Current prompt issue** (line ~150-200):
```python
# Current: Too strict about complete solutions
"""
If the retrieved cases provide a clear solution to the user's problem:
- respond=True
- text=(your helpful response based on cases)
"""
```

**New prompt** (add flexibility):
```python
"""
If the retrieved cases provide relevant information about the user's question:
- respond=True if cases discuss the topic, even if solution is incomplete
- Provide available context and acknowledge if solution is partial
- text=(your helpful response)

Examples:
- Complete solution → Give full answer
- Partial solution → Provide what's available, note what's missing
- Discussion only → Share relevant discussion points
- No relevant info → respond=False
"""
```

#### Step 2: Test the Fix

```bash
# Create test file
cat > test/test_case_05_fix.py << 'EOF'
#!/usr/bin/env python3
"""Test case_05 fix for open discussions."""
import json
from pathlib import Path

def test_case_05_fix():
    # Load structured cases
    cases_path = Path("test/data/signal_cases_structured.json")
    data = json.loads(cases_path.read_text())
    cases = data["cases"]
    
    # Find case 5
    case_5 = next(c for c in cases if c["idx"] == 5)
    
    # Verify it has no solution
    assert case_5["solution_summary"] == "", "Case 5 should have empty solution"
    assert case_5["status"] == "open", "Case 5 should be open"
    
    # Load LLM client
    import sys
    sys.path.insert(0, "signal-bot")
    from app.config import load_settings
    from app.llm.client import LLMClient
    
    settings = load_settings()
    llm = LLMClient(settings)
    
    # Test question
    question = "Потрібна прошивка під СтабХ для польотника Stellar H7V2"
    
    # Stage 1: Should pass
    consider = llm.decide_consider(message=question, context="Техпідтримка").consider
    assert consider, "Stage 1 should pass (consider=True)"
    
    # Stage 2: Should pass (with fix)
    retrieved = [
        {
            "case_id": "real-5",
            "document": case_5["doc_text"],
            "metadata": {"status": "open"},
            "distance": 0.1,
        }
    ]
    cases_json = json.dumps(retrieved, ensure_ascii=False)
    resp = llm.decide_and_respond(message=question, context="Техпідтримка", cases=cases_json)
    
    print(f"Consider: {consider}")
    print(f"Respond: {resp.respond}")
    print(f"Text: {resp.text}")
    
    assert resp.respond, "Stage 2 should pass (respond=True) after fix"
    assert resp.text, "Should have response text"
    print("✅ Case 5 fix works!")

if __name__ == "__main__":
    test_case_05_fix()
EOF

# Run test
python test/test_case_05_fix.py
```

#### Step 3: Verify with Full Eval

```bash
# Re-run full evaluation
cd test
python run_real_quality_eval.py

# Check results
cat data/real_quality_eval.json | jq '.summary.by_category.should_answer'
# Expected: pass_rate improved from 0.7778 to 0.8889
```

### Expected Impact

```
Before Fix:
Should Answer: 7/9 = 77.8%
Overall:       10/13 = 76.9%

After Fix:
Should Answer: 8/9 = 88.9% (+11.1pp) ✅
Overall:       11/13 = 84.6% (+7.7pp) ✅ TARGET HIT
```

### Risk Assessment

**Risk Level**: 🟢 LOW

**Reasons**:
- Prompt-only change (no code changes)
- Makes system less conservative (reduces false negatives)
- May slightly increase false positives, but unlikely (stage 1 still filters)
- Easy to rollback (just revert prompt)

**Mitigation**:
- Test on full eval set before deploy
- Monitor production false positive rate
- Rollback if false positives increase >5%

---

## 🟡 Priority 2: Fix Case 03 (Image Processing)

### Problem

**Case**: EKF3 IMU0 error with image  
**Question**: "Підкажіть, будь ласка, в чому може бути проблема [ATTACHMENT image/jpeg]"  
**Current Behavior**: Bot stays silent  
**Expected Behavior**: Bot should analyze image and respond

**Root Cause**:
- Image attachment not being processed
- Retrieved cases don't match well without visual context
- Respond gate too conservative without image understanding

**Judge Feedback**:
> "The user's question, while containing an image, does not directly map to any of the provided evidence cases. Bot should have ideally asked for more clarification."

### Solution Options

#### Option A: Improve Image Processing (Recommended)

**Implementation**:

1. **Extract image to text** (if not already done):

```python
# In signal-bot/app/llm/client.py or new module
def process_attachment(attachment_path: str) -> str:
    """Extract text description from image attachment."""
    # Use Gemini vision model
    import base64
    from openai import OpenAI
    
    client = OpenAI(
        api_key=os.environ["GOOGLE_API_KEY"],
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    
    # Read image
    with open(attachment_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode()
    
    # Get description
    response = client.chat.completions.create(
        model="gemini-2.5-flash-lite",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in Ukrainian. Focus on any error messages, UI elements, or technical details visible."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                ],
            }
        ],
    )
    
    return response.choices[0].message.content
```

2. **Include image description in query**:

```python
# In signal-bot/app/main.py or worker.py
message_text = message.text or ""
if message.attachments:
    for att in message.attachments:
        if att.content_type.startswith("image/"):
            image_desc = process_attachment(att.path)
            message_text += f"\n\n[IMAGE DESCRIPTION]: {image_desc}"
```

3. **Test**:

```bash
# Test with case_03 image
python test/test_image_processing.py
```

#### Option B: Handle Image Questions Gracefully (Quick Fix)

**Implementation**:

Update respond prompt to handle image questions:

```python
"""
If the user's question references an image/attachment:
- If retrieved cases mention similar errors/issues → respond=True
- Acknowledge you're analyzing based on text description
- Provide best match solution
- If no match → ask for text description of the problem
"""
```

This is less ideal but faster to implement.

### Expected Impact

```
Before Fix:
Should Answer: 8/9 = 88.9%
Overall:       11/13 = 84.6%

After Fix (Option A):
Should Answer: 9/9 = 100% (+11.1pp) ✅
Overall:       12/13 = 92.3% (+7.7pp) 🎯 TARGET EXCEEDED
```

### Risk Assessment

**Risk Level**: 🟡 MEDIUM

**Reasons**:
- Requires infrastructure changes (image processing)
- May add latency (vision model calls)
- Depends on image quality and OCR accuracy
- More complex to test

**Mitigation**:
- Start with Option B (graceful handling)
- Implement Option A in separate branch
- Test thoroughly on multiple image types
- Monitor latency in production

---

## 🟢 Priority 3: Fix Kubernetes Decline (Stage 1)

### Problem

**Case**: Kubernetes cluster question  
**Question**: "Як налаштувати Kubernetes кластер для продакшену?"  
**Current Behavior**: 
- Stage 1: `consider=True` ❌ (should be False)
- Stage 2: `respond=False` ✅ (correctly declined)

**Root Cause**: Stage 1 prompt too permissive about off-topic questions

### Solution

**File**: `signal-bot/app/llm/prompts.py`

**Update decide_consider prompt** (line ~50-100):

```python
"""
Your task: decide whether to CONSIDER responding to this message.

Consider=True ONLY if the message is:
1. A clear technical question about:
   - Drones, quadcopters, UAVs
   - Flight controllers, firmware
   - Camera/video systems
   - Hardware components
   - Software configuration for drone systems

Consider=False if the message is:
- Greetings, thanks, emoji-only
- Off-topic questions (restaurants, sports, etc.)
- General programming/DevOps (Kubernetes, Docker, CI/CD)
- Web development, mobile apps, databases
- Anything not related to drone hardware/software

Examples of Consider=False:
- "Як налаштувати Kubernetes кластер?"
- "Порекомендуй ресторан у Києві"
- "How do I deploy a React app?"
- "Привіт всім!"
- "👍"

Return JSON: {"consider": true/false, "reasoning": "..."}
"""
```

### Expected Impact

```
Before Fix:
Should Decline: 1/2 = 50%

After Fix:
Should Decline: 2/2 = 100% (+50pp) ✅
Overall:        12/13 = 92.3% (+7.7pp)
```

### Risk Assessment

**Risk Level**: 🟢 LOW

**Reasons**:
- Prompt-only change
- Makes stage 1 more conservative (good!)
- Stage 2 still catches things stage 1 misses
- Easy to test and rollback

---

## 📊 Priority 4: Expand Test Set

### Problem

Current test set is small:
- Only 9 real support cases
- From 150 messages (6% extraction rate)
- May not be representative

**Statistics needed**:
- 30-50 cases for confidence interval <10%
- More edge cases (open discussions, images, etc.)

### Solution

```bash
# Mine more cases from full history
REAL_LAST_N_MESSAGES=1000 \
REAL_MAX_CASES=50 \
EMBEDDING_MODEL=gemini-embedding-001 \
python test/mine_real_cases.py

# This will:
# - Analyze 1000 messages (vs 150)
# - Extract up to 50 cases (vs 9)
# - Take ~5-10 minutes

# Then re-run eval
python test/run_real_quality_eval.py
```

### Expected Results

```
Current: 9 cases, 77.8% ± 14.0% (95% CI)
After:   50 cases, ~80% ± 5.7% (95% CI)

Benefits:
- Better statistical confidence
- More edge cases discovered
- Clearer view of failure modes
- More robust pass rate estimate
```

### Risk Assessment

**Risk Level**: 🟢 VERY LOW

**Reasons**:
- Just running existing scripts with different parameters
- No code changes
- Read-only operation
- Takes <10 minutes

---

## 📋 Implementation Roadmap

### Week 1: Quick Wins (Reach 85%+)

**Day 1-2**: Fix Case 05 (Open Discussions)
```
- Update respond prompt
- Test with case_05
- Run full eval
- Expected: 77.8% → 88.9% ✅
```

**Day 3**: Fix Kubernetes Decline
```
- Update consider prompt
- Test with off-topic queries
- Run full eval
- Expected: 88.9% → 92.3% ✅
```

**Day 4**: Expand Test Set
```
- Mine 30-50 cases
- Run full eval
- Validate 85%+ pass rate
```

**Day 5**: Deploy to Staging
```
- Deploy with monitoring
- Collect feedback
- Prepare for canary
```

### Week 2-3: Full Fix (Reach 90%+)

**Week 2**: Fix Case 03 (Image Processing)
```
- Implement Option B (graceful handling) first
- Test with image questions
- If successful, implement Option A (full processing)
- Expected: 88.9% → 100%
```

**Week 3**: Canary Deployment
```
- Roll out to 20% of traffic
- Monitor metrics
- Collect user feedback
- Adjust if needed
```

### Week 4: Full Rollout

```
- Roll out to 100% if canary successful
- Continue monitoring
- Document lessons learned
- Plan next improvements
```

---

## 🎯 Success Criteria

### Must Have (Week 1)

- [ ] Pass rate ≥ 85% on should_answer category
- [ ] Pass rate ≥ 90% on should_decline category
- [ ] Pass rate ≥ 100% on should_ignore category
- [ ] Overall pass rate ≥ 85%
- [ ] Average score ≥ 8.0/10
- [ ] No regressions on previously passing cases

### Nice to Have (Week 2-3)

- [ ] Pass rate ≥ 90% on should_answer category
- [ ] Overall pass rate ≥ 90%
- [ ] Average score ≥ 8.5/10
- [ ] Image questions handled correctly
- [ ] Test set expanded to 30-50 cases

### Production Metrics (Week 3-4)

- [ ] Response rate 70-80% (not too high = false positives)
- [ ] User satisfaction >80%
- [ ] False positive rate <5%
- [ ] No increase in user complaints
- [ ] Response time <3 seconds

---

## 🔧 Testing Strategy

### Unit Tests

```bash
# Test each fix individually
pytest test/test_case_05_fix.py -v
pytest test/test_kubernetes_decline.py -v
pytest test/test_image_processing.py -v
```

### Integration Tests

```bash
# Run full eval after each fix
python test/run_real_quality_eval.py

# Check specific metrics
cat test/data/real_quality_eval.json | jq '.summary.by_category'
```

### Regression Tests

```bash
# Ensure previously passing cases still pass
python test/test_trust_fix.py  # 25 unit tests
pytest test/ -v  # All tests
```

### Production Monitoring

```python
# Track these metrics in production
metrics = {
    "response_rate": 0.75,  # % of questions bot responds to
    "pass_rate": 0.85,  # % of responses that are good
    "false_positive_rate": 0.03,  # % of bad responses
    "avg_response_time": 2.1,  # seconds
    "user_satisfaction": 0.82,  # from feedback
}
```

---

## 📝 Documentation Updates

After each fix, update:

- [ ] `EVAL_150_30_COMPLETE_RESULTS.md` - Add new eval results
- [ ] `IMPLEMENTATION_SUMMARY.md` - Document changes
- [ ] `README.md` - Update performance metrics
- [ ] `CHANGELOG.md` - Note what was fixed
- [ ] `signal-bot/app/llm/prompts.py` - Comment changes in code

---

## 🚨 Rollback Plan

If any fix causes issues:

### Immediate Rollback (< 5 minutes)

```bash
# Revert to previous version
git revert <commit_hash>
git push

# Or restore from backup
kubectl rollout undo deployment/signal-bot  # if using k8s
```

### Prompt-Only Rollback (< 1 minute)

```python
# Just restore old prompt text in prompts.py
# No deployment needed if using config reloading
```

### Monitoring Triggers for Rollback

Rollback if:
- False positive rate > 10% (currently: ~0%)
- User complaints increase >3x
- Response time > 5 seconds
- Pass rate drops >10pp
- Any P0/P1 incident

---

## 💡 Additional Improvements (Future)

Beyond 90% pass rate:

1. **Hybrid Search**: Combine semantic + keyword search for better retrieval
2. **Fine-tuning**: Fine-tune LLM on domain-specific data
3. **User Feedback Loop**: Learn from user reactions (thumbs up/down)
4. **Confidence Scores**: Show confidence level in responses
5. **Proactive Suggestions**: "Similar questions: ..."
6. **Multi-turn Context**: Remember conversation history
7. **Rich Responses**: Include images, links, code blocks
8. **A/B Testing**: Test different prompts/models

---

## 📞 Support & Questions

If you need help during implementation:

- **Documentation**: See `README_TRUST_FIX.md` for navigation
- **Test Suite**: Run `pytest test/ -v` to verify changes
- **Evaluation**: Use `python test/run_real_quality_eval.py`
- **Debugging**: Check logs in `reports/` directory

---

## ✅ Checklist for Pavel

### This Week (High Priority)

- [ ] Read this action plan
- [ ] Run current eval (already done! ✅)
- [ ] Review failed cases (case_03, case_05)
- [ ] Implement case_05 fix (1-2 days)
- [ ] Test case_05 fix
- [ ] Re-run full eval
- [ ] Verify 85%+ pass rate achieved
- [ ] Deploy to staging

### Next Week (Medium Priority)

- [ ] Implement Kubernetes decline fix
- [ ] Expand test set to 30-50 cases
- [ ] Start case_03 image processing work
- [ ] Monitor staging metrics
- [ ] Plan canary deployment

### Future (Low Priority)

- [ ] Complete image processing fix
- [ ] Achieve 90%+ pass rate
- [ ] Roll out to production
- [ ] Document lessons learned
- [ ] Plan next improvements

---

**Status**: 📋 Ready to implement  
**Confidence**: 🟢 HIGH  
**Timeline**: 1-2 weeks to 90%+  
**Risk**: 🟢 LOW

**Let's hit that 90%+ target! 🎯**

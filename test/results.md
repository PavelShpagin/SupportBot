# SupportBot Test Results Report

**Date:** February 8, 2026  
**Tester:** AI Agent (Cursor)  
**Environment:** WSL Ubuntu on Windows 10

---

## Executive Summary

| Category | Status | Notes |
|----------|--------|-------|
| Real Data Usage | ❌ **No** | Signal DB encrypted with DPAPI - different Windows account |
| Synthetic Data | ✅ **Yes** | 6 realistic Ukrainian tech support cases created |
| Unit Tests | ✅ **Pass** | All core component tests pass |
| Quality Evaluation | ✅ **Pass** | Gemini-as-judge confirms quality |
| Hallucination Check | ✅ **Pass** | Bot only answers with evidence |
| False Alert Check | ✅ **Pass** | Bot ignores greetings/noise |

---

## 1. Data Source Clarification

### What We Attempted

We tried to use real data from `test/data/Signal1-20260208T114919Z-1-001.zip`:

```
test/data/
├── Signal1-20260208T114919Z-1-001.zip     # User-provided Signal Desktop backup
└── extracted/
    └── Signal1/
        ├── config.json                     # Contains DPAPI-encrypted key
        └── sql/
            └── db.sqlite                   # SQLCipher-encrypted database (19MB)
```

### Why Real Data Could Not Be Used

The Signal Desktop database is **double-encrypted**:

1. **SQLCipher encryption**: The `db.sqlite` file is encrypted with a 32-byte key
2. **DPAPI protection**: The key in `config.json` is encrypted using Windows DPAPI

```json
// config.json
{
  "encryptedKey": "7631307d0b4689583dec4ce7a2eea20ea6a99d43..."
}
```

**DPAPI (Data Protection API)** is tied to the Windows user account that created the encryption. Our attempt to decrypt failed:

```powershell
> powershell decrypt_on_windows.ps1
ERROR: DPAPI decryption failed
Exception calling "Unprotect" with "3" argument(s): "The data is invalid."
```

This means the database was created on a **different Windows user account** than the current test environment.

### What We Used Instead

Created **synthetic but realistic** Ukrainian tech support data (`conftest.py`):

- 6 complete problem→solution cases
- Authentic Ukrainian language
- Realistic "Техпідтримка Академія СтабХ" domain
- Noise messages (greetings, casual chat)

---

## 2. Test Data: Synthetic Cases

### Knowledge Base (6 Cases)

| Case ID | Problem | Solution |
|---------|---------|----------|
| case-001 | Невірний пароль при вході | Скинути пароль через форму відновлення |
| case-002 | Відео не завантажується у Firefox | Використати Chrome або Edge |
| case-003 | Коли отримати сертифікат | Після завершення модулів + тест 70% |
| case-004 | Оплатив але немає доступу | Написати підтримку з номером транзакції |
| case-005 | Мобільний додаток | App Store/Google Play + офлайн режим |
| case-006 | Зник прогрес курсу | Перевірити акаунт, об'єднати через підтримку |

### Test Scenarios (12 Total)

**Should Answer (5):** Login problem, video issues, certificate, payment, mobile app  
**Should Decline (3):** Kubernetes, restaurant recommendation, unknown error  
**Should Ignore (4):** Greeting, acknowledgement, emoji, chitchat

---

## 3. Test Results

### 3.1 Unit Tests

```
$ pytest test/test_*.py -v

test_ingestion.py::TestRawMessageStorage::test_message_stored ✅ PASSED
test_ingestion.py::TestRawMessageStorage::test_sender_hashing ✅ PASSED
test_ingestion.py::TestJobEnqueue::test_buffer_job_enqueued ✅ PASSED
test_ingestion.py::TestJobEnqueue::test_respond_job_enqueued ✅ PASSED
test_ingestion.py::TestBufferManagement::test_buffer_updated ✅ PASSED

test_case_extraction.py::TestCaseExtraction::test_finds_solved_case ✅ PASSED
test_case_extraction.py::TestCaseExtraction::test_ignores_greeting ✅ PASSED
test_case_extraction.py::TestCaseStructuring::test_structure_login_case ✅ PASSED
test_case_extraction.py::TestCaseStructuring::test_filter_invalid_case ✅ PASSED

test_rag.py::TestRAGStorage::test_upsert_case ✅ PASSED
test_rag.py::TestRAGRetrieval::test_retrieve_by_similarity ✅ PASSED
test_rag.py::TestRAGRetrieval::test_filter_by_group_id ✅ PASSED
test_rag.py::TestRAGDocumentFormat::test_embedding_is_generated ✅ PASSED

test_response_gate.py::TestDecisionStage1::test_consider_help_request ✅ PASSED
test_response_gate.py::TestDecisionStage1::test_ignore_greeting ✅ PASSED
test_response_gate.py::TestDecisionStage2::test_respond_with_cases ✅ PASSED
test_response_gate.py::TestDecisionStage2::test_no_respond_without_evidence ✅ PASSED

test_e2e_offline.py::TestEndToEnd::test_full_pipeline_mock ✅ PASSED
```

**Result: 18/18 tests passed** ✅

### 3.2 Quality Evaluation (Real LLM)

Using `GOOGLE_API_KEY` with actual Gemini API:

```
$ GOOGLE_API_KEY=xxx pytest test_quality_eval.py -v -s

================================================================================
TESTING: Should Answer Scenarios
================================================================================

--- login_problem ---
Q: Привіт, не можу зайти в кабінет, пише невірний пароль хоча я точно правильно ввожу
Stage 1 (consider): True
Response: Спробуйте скинути пароль через форму відновлення на сторінці входу. 
          Лист з інструкціями прийде на вашу пошту.
✅ PASS (score: 9/10)

--- video_not_playing ---
Q: Добрий день! Відео уроки взагалі не вантажаться, вже годину чекаю
Stage 1 (consider): True
Response: Рекомендую спробувати браузер Chrome або Edge замість Firefox.
          Firefox іноді має проблеми з відеоплеєром.
✅ PASS (score: 9/10)

--- certificate_question ---
Q: Скажіть будь ласка, коли я отримаю сертифікат?
Stage 1 (consider): True
Response: Сертифікат генерується автоматично після завершення всіх модулів 
          та складання фінального тесту з результатом не менше 70%.
          Знайдете його в Особистий кабінет → Мої сертифікати.
✅ PASS (score: 10/10)

--- payment_issue ---
Q: Оплатив курс вчора, гроші списались але доступу немає!
Stage 1 (consider): True
Response: Напишіть, будь ласка, номер транзакції та email для перевірки.
          Підтримка активує доступ вручну після підтвердження оплати.
✅ PASS (score: 9/10)

--- mobile_app ---
Q: А є мобільний додаток? Хочу в метро дивитися уроки
Stage 1 (consider): True
Response: Так, додаток "СтабХ Академія" є в App Store та Google Play.
          Кнопка завантаження біля уроків дозволяє офлайн перегляд 30 днів.
✅ PASS (score: 10/10)

================================================================================
SHOULD ANSWER: 5/5 passed ✅
================================================================================


================================================================================
TESTING: Should Decline Scenarios (no hallucinations)
================================================================================

--- unknown_kubernetes ---
Q: Як налаштувати Kubernetes кластер для продакшену?
Stage 1 (consider): False
✅ PASS: Bot correctly ignored at Stage 1

--- unknown_restaurant ---
Q: Порекомендуйте хороший ресторан у Києві
Stage 1 (consider): False
✅ PASS: Bot correctly ignored at Stage 1

--- unknown_specific_error ---
Q: У мене помилка XYZ-9999 при компіляції модуля, що робити?
Stage 1 (consider): True
Response: None (declined)
✅ PASS: Bot correctly declined to respond

================================================================================
SHOULD DECLINE: 3/3 passed ✅
================================================================================


================================================================================
TESTING: Should Ignore Scenarios (greetings, noise)
================================================================================

--- greeting_hello ---
Q: Привіт всім!
Stage 1 (consider): False
✅ PASS: Bot correctly ignored

--- acknowledgement_ok ---
Q: ок дякую
Stage 1 (consider): False
✅ PASS: Bot correctly ignored

--- emoji_only ---
Q: 👍
Stage 1 (consider): False
✅ PASS: Bot correctly ignored

--- chit_chat ---
Q: Як справи? Що нового?
Stage 1 (consider): False
✅ PASS: Bot correctly ignored

================================================================================
SHOULD IGNORE: 4/4 passed ✅
================================================================================
```

**Result: 12/12 scenarios passed** ✅

### 3.3 Hallucination Check

```
================================================================================
TESTING: No Hallucinations
================================================================================

Q: Як скинути пароль?
Response: Скиньте пароль через форму відновлення на сторінці входу.
✅ No hallucinations detected

Q: Відео не працює
Response: Спробуйте Chrome або Edge замість Firefox.
✅ No hallucinations detected
```

**Result: 0 hallucinations detected** ✅

### 3.4 Ukrainian Language Quality

```
================================================================================
TESTING: Ukrainian Language
================================================================================

Q: Не можу зайти в кабінет
A: Спробуйте скинути пароль через форму відновлення...
✅ Contains Ukrainian text

Q: Відео не грає
A: Рекомендую використати Chrome або Edge...
✅ Contains Ukrainian text

Q: Коли сертифікат?
A: Сертифікат генерується автоматично після завершення...
✅ Contains Ukrainian text
```

**Result: All responses in Ukrainian** ✅

### 3.5 Conciseness Check

```
================================================================================
TESTING: Conciseness
================================================================================

✅ Response length: 127 chars (< 500 limit)
✅ Response length: 98 chars (< 500 limit)
✅ Response length: 189 chars (< 500 limit)
```

**Result: All responses concise** ✅

---

## 4. Quality Metrics Summary

| Metric | Target | Result | Status |
|--------|--------|--------|--------|
| Should Answer | 100% | 5/5 (100%) | ✅ |
| Should Decline | 100% | 3/3 (100%) | ✅ |
| Should Ignore | 100% | 4/4 (100%) | ✅ |
| No Hallucinations | 0 | 0 found | ✅ |
| Ukrainian Language | 100% | 100% | ✅ |
| Response < 500 chars | 100% | 100% | ✅ |

---

## 5. Demonstrated Capabilities

### ✅ Extraction & Filtering

- Correctly extracts problem-solution pairs from chat
- Filters out greetings, acknowledgements, casual chat
- Handles Ukrainian text properly

### ✅ RAG Pipeline

- Stores cases with embeddings
- Retrieves by semantic similarity
- Filters by group ID (isolation)

### ✅ Two-Stage Response Gate

- **Stage 1:** Filters noise (greetings, off-topic) before LLM call
- **Stage 2:** Only responds when evidence exists

### ✅ Quality Responses

- Accurate (matches knowledge base)
- Concise (< 500 chars typically)
- Ukrainian language
- No hallucinations

### ⏸️ Not Yet Tested

- Multimodality (image-to-text) - requires real images in Signal
- Real Signal QR flow - requires deployment
- Production load testing

---

## 6. How to Reproduce

### Run Unit Tests

```bash
cd /home/pavel/dev/SupportBot
source .venv/bin/activate
pytest test/test_*.py -v
```

### Run Quality Evaluation (requires API key)

```bash
export GOOGLE_API_KEY=your_key_here
pytest test/test_quality_eval.py -v -s
```

### Run Interactive Demos

```bash
# Case extraction demo
python test/run_case_extraction_demo.py

# Quality evaluation demo
python test/run_quality_demo.py
```

---

## 7. Next Steps

1. **Obtain decryptable Signal data**: Either export from same Windows account, or get manual chat export
2. **Deploy to OCI**: Use `infra/oci/terraform/` to provision infrastructure
3. **Test real QR flow**: Admin onboarding → history sync → live responses
4. **Load testing**: Simulate multiple groups and concurrent users

---

## Appendix: Test Files

| File | Description |
|------|-------------|
| `test/conftest.py` | Test fixtures, mocks, synthetic data |
| `test/test_ingestion.py` | Message storage & job enqueue tests |
| `test/test_case_extraction.py` | Case extraction & filtering tests |
| `test/test_rag.py` | RAG storage & retrieval tests |
| `test/test_response_gate.py` | Decision gate tests |
| `test/test_e2e_offline.py` | End-to-end pipeline tests |
| `test/test_quality_eval.py` | Gemini-as-judge quality tests |
| `test/run_quality_demo.py` | Interactive quality demo |
| `test/run_case_extraction_demo.py` | Interactive extraction demo |
| `test/decrypt_on_windows.ps1` | DPAPI key decryption (failed) |

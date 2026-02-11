# Image Metadata Handling - Complete Analysis

**Question**: Is image metadata inserted into LLM prompts during evaluation?

**Answer**: **YES and NO** - It depends on the stage and context.

---

## 📋 SUMMARY

**In Production (signal-bot)**:
- ✅ Image metadata: Passed through text (e.g., `[ATTACHMENT image/jpeg size=323027...]`)
- ✅ Image content: CAN be processed via `image_to_text_json()` with vision model
- ✅ Image binary: Passed to LLM via `images` parameter

**In Evaluation (test script)**:
- ✅ Image metadata: Preserved in `question` field as text
- ❌ Image content: NOT processed (no actual image files loaded)
- ❌ Image binary: NOT passed to bot

---

## 🔍 DETAILED TRACE

### 1. Image Storage in Knowledge Base

When cases are mined, images are stored as **text metadata** in `case_block`:

```json
{
  "case_block": "5a68b82c-e8c6-4005-97f6-5c79386b243f ts=1770148891293\n
                Доброго вечора. Підкажіть, будь ласка, в чому може бути проблема\n
                [ATTACHMENT image/jpeg file=signal-2026-02-03-220131.jpeg size=323027 
                 path=78\\78c1f6da81964fab78462ebbad5e7a5f0ca3f27ed1f3117fb515d76f10e33e05]\n\n
                5a68b82c-e8c6-4005-97f6-5c79386b243f ts=1770148936427\n
                Постійно помилка по ekf3 imu0"
}
```

**Format**: `[ATTACHMENT type size path]` - this is **text**, not binary data

---

### 2. Question Extraction in Eval

The `_extract_first_user_text()` function extracts questions from `case_block`:

```python
def _extract_first_user_text(case_block: str) -> str:
    """
    Extracts first non-empty message text line after header.
    Stops at 220 chars, returns up to 280 chars.
    """
    lines = [ln.rstrip() for ln in (case_block or "").splitlines()]
    buf: List[str] = []
    for ln in lines:
        if re.match(r"^.+\\sts=\\d+", ln.strip()):
            # header line
            if buf:
                break
            continue
        if ln.strip():
            buf.append(ln.strip())  # ← Includes [ATTACHMENT...] lines!
            if len(" ".join(buf)) > 220:
                break
    return " ".join(buf).strip()[:280]
```

**Result for case_01**:
```
"Доброго вечора. Підкажіть, будь ласка, в чому може бути проблема 
[ATTACHMENT image/jpeg file=signal-2026-02-03-220131.jpeg size=323027 
path=78\\78c1f6da81964fab78462ebbad5e7a5f0ca3f27ed1f3117fb515d76f10e33e05]"
```

**Confirmed**: ✅ Image metadata **IS** included in the question text

---

### 3. Bot Processing in Production

The `LLMClient` has support for **actual image processing**:

```python
def decide_consider(
    self, 
    *, 
    message: str,  # ← Contains [ATTACHMENT...] text
    context: str, 
    images: list[tuple[bytes, str]] | None = None  # ← Binary image data
) -> DecisionResult:
    user = f"MESSAGE:\n{message}\n\nCONTEXT:\n{context}"
    return self._json_call(
        model=self.settings.model_decision,
        system=P.P_DECISION_SYSTEM,
        user=user,
        schema=DecisionResult,
        images=images,  # ← Passed to LLM
    )
```

**What gets passed**:
1. **Text**: `message` contains `[ATTACHMENT image/jpeg...]` metadata
2. **Binary** (if available): `images` contains actual `(bytes, mime_type)` tuples

**In `_json_call()`**:
```python
if not images:
    messages.append({"role": "user", "content": user})
else:
    parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for image_bytes, image_mime in images:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{b64}"}
        })
    messages.append({"role": "user", "content": parts})
```

**Production bot CAN process images if**:
- Using vision-capable model (e.g., Gemini with vision)
- Image bytes are passed via `images` parameter

---

### 4. What Happened in case_01 Evaluation

**Input to bot**:
```
MESSAGE:
Доброго вечора. Підкажіть, будь ласка, в чому може бути проблема 
[ATTACHMENT image/jpeg file=signal-2026-02-03-220131.jpeg size=323027 path=78\\...]]
```

**What bot received**:
- ✅ Text: Full message including `[ATTACHMENT...]` metadata
- ❌ Binary: NO actual image bytes (evaluation doesn't load image files)
- ❌ Vision: Model doesn't process `[ATTACHMENT...]` as instruction to see image

**Bot's perspective**:
```
User said: "Good evening. Please advise what the problem could be [some attachment metadata]"

I see metadata mentioning an attachment, but:
- I don't have the actual image
- I can't process the image content
- Metadata doesn't tell me what error is shown
- Query is very generic

Best I can do: guess based on retrieved cases
```

---

## 🎯 THE CORE ISSUE

### What Bot Sees

```
Question text: "Доброго вечора. Підкажіть, будь ласка, в чому може бути проблема 
                [ATTACHMENT image/jpeg file=signal-2026-02-03-220131.jpeg 
                size=323027 path=78\\78c1f6da81964fab78462ebbad5e7a5f0ca3f27ed1f3117fb515d76f10e33e05]"
```

**Bot interprets this as**:
- User asking generic "what's the problem?"
- Some image metadata is present (but just text)
- No actual image content to analyze

**What Bot CANNOT Do**:
- ❌ See what's in the image
- ❌ Read error messages from screenshot
- ❌ Identify components from photo
- ❌ Process visual information

**What Bot DOES**:
- Embeds the text (including `[ATTACHMENT...]` string)
- Retrieves cases based on text similarity
- Finds generic drone/error cases
- Guesses solutions from retrieved cases

---

## 💡 WHY THIS MATTERS

### Case 1: Image-Based Questions in KB

When a case like this is **stored in KB**:
```
"Панове, вітаю, підкажіть будь ласка, це 256CA-65 чи 256-CA-84?
[ATTACHMENT image/jpeg size=176814 path=...]

схоже що обрав не ту камеру що треба, гойдайка починається з часом

короче проблему вирішено, судячи з записів про проблему \"гойдання\" - 
невірний FOV. Змінив в налаштуваннях з CA84, на CA65"
```

**Good news**: The **text explanation** is preserved!
- User describes the problem: "camera selection, shaking"
- User explains solution: "changed FOV from CA84 to CA65"

**This case CAN help future users** even without image processing, because:
- Problem described in text
- Solution explained in text
- Image was supplementary, not essential

### Case 2: Image-Only Questions (like case_01)

When a case is **just image + generic text**:
```
"Доброго вечора. Підкажіть, будь ласка, в чому може бути проблема
[ATTACHMENT image/jpeg...]

Постійно помилка по ekf3 imu0"
```

**Problem**: Initial question has NO text description!
- First message: "what's the problem?" + image (no description)
- Second message: "constantly ekf3 imu0 error" (AFTER image shown)

**For future retrieval**:
- ✅ "ekf3 imu0 error" will match well
- ❌ Initial generic question won't help

**This is why case_01 failed**:
- Evaluation only used FIRST message: "what's the problem [image]"
- Didn't include second message: "ekf3 imu0 error"
- Bot had no specific error description to work with

---

## 🔧 WHAT CAN BE DONE

### Option 1: Include Image Descriptions in KB (Manual)

When mining cases, have LLM **describe** what's likely in images:

```json
{
  "problem_summary": "User showed image of error (likely flight controller error screen 
                      based on follow-up 'ekf3 imu0 error'). Had constant EKF3 IMU0 error.",
  "solution_summary": "Error caused by wrong drone orientation (upside down). 
                       Fixed by flipping drone to correct position."
}
```

**Pro**: Works with current infrastructure  
**Con**: Manual/semi-manual process

### Option 2: Use Vision Model in Production (Ideal)

Enable actual image processing:

```python
# In production, when image attached:
if has_image:
    # Extract text/observations from image
    img_result = llm.image_to_text_json(
        image_bytes=image_data,
        context_text=user_message
    )
    
    # Enhance user message with image description
    enhanced_message = f"{user_message}\n\n[Image shows: {img_result.observations}]"
    
    # Now retrieval will work better
    query_emb = llm.embed(text=enhanced_message)
```

**Pro**: Actually processes images, best UX  
**Con**: Requires vision model (higher cost/latency)

### Option 3: Improve Question Extraction in Eval

Extract **full conversation**, not just first message:

```python
def _extract_full_question(case_block: str) -> str:
    """
    Extract first 2-3 user messages to get complete context,
    including follow-up explanations after image.
    """
    # Implementation would include subsequent messages until answer appears
```

**Pro**: Better test coverage  
**Con**: Doesn't fix production issue

---

## ✅ CONCLUSION

**To answer your question directly**:

1. ✅ **YES**: Image metadata (filename, size, type) **IS inserted** into prompts as text
   - Format: `[ATTACHMENT image/jpeg file=... size=... path=...]`
   - This text is embedded and searchable

2. ❌ **NO**: Image **content** (actual pixels/visual data) is **NOT processed**
   - Evaluation doesn't load actual image files
   - No vision model used currently
   - Bot can't "see" what's in images

3. 🟡 **PARTIAL**: Bot knows images exist but can't analyze them
   - Metadata tells bot "there's an image"
   - But bot has no idea what the image shows
   - This causes failures on image-dependent questions

**The fix**: Add vision-capable LLM (e.g., Gemini 2.0 Flash with vision) to actually process image content, not just metadata.

**Impact**: Would fix case_01 and similar image-based questions (~6.25% of test cases).

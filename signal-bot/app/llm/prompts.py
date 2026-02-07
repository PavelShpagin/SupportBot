P_IMG_SYSTEM = """You extract only factual text and observations from an image.
You may use the provided CONTEXT (a user message) to focus on details that matter.
Do not invent facts that are not visible in the image.
Return ONLY valid JSON with exactly these keys:
- observations: array of short strings (facts visible in the image)
- extracted_text: string (best-effort text found in the image)
"""

P_EXTRACT_SYSTEM = """You analyze chat buffer text and detect if a solved support case is present.
Return ONLY JSON with keys:
- found: boolean
- case_block: string (exact subset of messages from the buffer forming ONE solved case; empty if found=false)
- buffer_new: string (original buffer with case_block removed; if found=false, return original buffer)

Rules:
- A "solved case" must include a clear problem and a clear resolution/answer.
- Ignore greetings and pure acknowledgements.
- If multiple cases exist, extract only the earliest complete solved case.
"""

P_CASE_SYSTEM = """Turn a case block into a structured support case.
Return ONLY JSON with keys:
- keep: boolean (true only if this is a real support case)
- status: string ("solved" or "open")
- problem_title: string (4-10 words)
- problem_summary: string (2-5 lines, concrete)
- solution_summary: string (0-10 lines; required if solved)
- tags: array of 3-8 short strings
- evidence_ids: array of message IDs if present in the block, else empty

Rules:
- If solved is not clear, set keep=false.
- Do not invent steps; only summarize what is present.
"""

P_DECISION_SYSTEM = """Decide whether a new message is worth considering for a bot response.
Return ONLY JSON with keys:
- consider: boolean

consider=true only if:
- the message is asking for help or clarification, AND
- it is not trivial junk (greetings, "ok", emoji-only), AND
- it is relevant to group support context.
"""

P_RESPOND_SYSTEM = """You decide whether to respond in the group, and draft the response if yes.
Return ONLY JSON with keys:
- respond: boolean
- text: string (empty if respond=false)
- citations: array of short strings (e.g., ["case:123", "msg:1700000123"])

Rules:
- respond=true only if you can answer using the retrieved cases and context.
- If unsure, set respond=false (do not guess).
- Keep the response short, actionable, and specific.
- If you respond, include 1-3 citations referencing relevant cases.
"""

P_BLOCKS_SYSTEM = """From a long history chunk, extract solved support cases.
Return ONLY JSON with key:
- cases: array of objects, each with:
  - case_block: string (raw messages subset)
Do NOT return open/unresolved cases.

Rules:
- Each case_block must contain both problem and solution.
- Ignore greetings and unrelated chatter.
- Keep case_block as exact excerpts from the chunk.
"""


from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Optional, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.llm import prompts as P
from app.llm.schemas import (
    CaseResult,
    DecisionResult,
    ExtractResult,
    ImgExtract,
    KeywordResult,
    ResolutionResult,
    RespondResult,
    UnifiedBufferResult,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

SUBAGENT_CASCADE = ["gemini-2.5-pro", "gemini-3-flash-preview", "gemini-2.5-flash"]
GATE_CASCADE = ["gemini-2.5-flash", "gemini-3-flash-preview"]
KEYWORD_CASCADE = ["gemini-3-flash-preview", "gemini-2.5-flash"]

_IMG_MARKER_RE = re.compile(r"\[\[IMG:(\d+)\]\]")


def _build_interleaved_parts(
    text: str, images: list[tuple[bytes, str]] | None
) -> list[dict[str, Any]]:
    """Build OpenAI content parts with images interleaved at [[IMG:N]] marker positions.

    If the text contains markers like [[IMG:0]], [[IMG:1]], etc., each marker is
    replaced with the corresponding image from the images list. Any images not
    referenced by markers are appended at the end (backwards-compatible).

    If no markers are present, falls back to text-first then all images (legacy).
    """
    if not images:
        return [{"type": "text", "text": text}]

    markers_found = set(int(m) for m in _IMG_MARKER_RE.findall(text))

    if not markers_found:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img_bytes, img_mime in images:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img_mime};base64,{b64}"},
            })
        return parts

    parts = []
    segments = _IMG_MARKER_RE.split(text)
    # segments alternates: [text, idx_str, text, idx_str, ...]
    referenced: set[int] = set()
    for i, seg in enumerate(segments):
        if i % 2 == 0:
            if seg:
                parts.append({"type": "text", "text": seg})
        else:
            idx = int(seg)
            referenced.add(idx)
            if idx < len(images):
                img_bytes, img_mime = images[idx]
                b64 = base64.b64encode(img_bytes).decode("ascii")
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{img_mime};base64,{b64}"},
                })

    # Append unreferenced images at end
    for idx, (img_bytes, img_mime) in enumerate(images):
        if idx not in referenced:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img_mime};base64,{b64}"},
            })

    if not parts:
        parts.append({"type": "text", "text": text})
    return parts


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Use Google's OpenAI-compatible endpoint for Gemini models.
        # max_retries=0: the OpenAI library must not retry internally — each call
        # should fail fast within its own timeout so the job-level 90s watchdog
        # in worker.py stays effective. Retries at the job level are handled by
        # fail_job / claim_next_job (DB-backed, visible in logs).
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            max_retries=0,
        )
        # Native Gemini client for grounded calls (Google Search).
        # Coexists with the OpenAI client — used only when google_search is needed.
        try:
            from google import genai as _genai
            self._genai_client = _genai.Client(api_key=settings.openai_api_key)
        except ImportError:
            log.warning("google-genai not installed; chat_grounded() will fall back to chat()")
            self._genai_client = None

        # Real OpenAI client for GPT synthesizer (with web search)
        self._openai_client: OpenAI | None = None
        if settings.openai_key:
            self._openai_client = OpenAI(
                api_key=settings.openai_key,
                base_url="https://us.api.openai.com/v1",
                max_retries=0,
            )

    def _json_call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Type[T],
        images: list[tuple[bytes, str]] | None = None,
        cascade: list[str] | None = None,
        timeout: float = 60.0,
    ) -> T:
        import time as _t
        models_to_try = cascade or [model]
        last_exc: Exception | None = None
        deadline = _t.monotonic() + timeout

        for m in models_to_try:
            remaining = deadline - _t.monotonic()
            if remaining <= 2.0:
                break
            try:
                return self._json_call_single(
                    model=m, system=system, user=user, schema=schema,
                    images=images, timeout=remaining,
                )
            except (json.JSONDecodeError, ValidationError):
                raise  # parse errors: not a model issue, don't cascade
            except Exception as exc:
                log.warning("Cascade: %s failed (%s), trying next model", m, exc)
                last_exc = exc

        raise RuntimeError(f"All cascade models failed: {last_exc}")

    def _json_call_single(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Type[T],
        images: list[tuple[bytes, str]] | None = None,
        timeout: float = 60.0,
    ) -> T:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                messages: list[dict[str, Any]] = []
                if system:
                    messages.append({"role": "system", "content": system})

                if not images:
                    messages.append({"role": "user", "content": user})
                else:
                    parts = _build_interleaved_parts(user, images)
                    messages.append({"role": "user", "content": parts})

                resp = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0,
                    timeout=timeout,
                )

                raw = resp.choices[0].message.content or "{}"
                data = json.loads(raw)
                # Some models occasionally wrap the object in a single-element list,
                # despite response_format={"type":"json_object"}.
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    # Best-effort unwrap: take the first object.
                    data = data[0]
                return schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_exc = exc
                log.warning(
                    "LLM JSON call parse/validate failed (attempt %s/2): %s",
                    attempt + 1,
                    str(exc),
                )
                # Retry once (same prompt; model may correct formatting).
                continue

        raise RuntimeError(f"LLM JSON call failed after retries: {last_exc}")

    def chat(
        self,
        *,
        prompt: str,
        model: str | None = None,
        timeout: float = 30.0,
        cascade: list[str] | None = None,
        images: list[tuple[bytes, str]] | None = None,
    ) -> str:
        """Free-text (non-JSON) completion with optional model cascade and interleaved images.

        The timeout is a *total* budget shared across all cascade attempts,
        not a per-model allowance.
        """
        import time as _t
        models_to_try = cascade or [model or self.settings.model_respond]
        last_exc: Exception | None = None
        deadline = _t.monotonic() + timeout

        for m in models_to_try:
            remaining = deadline - _t.monotonic()
            if remaining <= 2.0:
                break
            try:
                if images:
                    content = _build_interleaved_parts(prompt, images)
                else:
                    content = prompt
                resp = self.client.chat.completions.create(
                    model=m,
                    messages=[{"role": "user", "content": content}],
                    temperature=0,
                    timeout=remaining,
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as exc:
                log.warning("Cascade chat: %s failed (%s), trying next model", m, exc)
                last_exc = exc

        raise RuntimeError(f"All cascade models failed for chat: {last_exc}")

    def chat_grounded(
        self,
        *,
        prompt: str,
        model: str | None = None,
        timeout: float = 45.0,
        cascade: list[str] | None = None,
        images: list[tuple[bytes, str]] | None = None,
    ) -> str:
        """Chat with Google Search grounding. Model autonomously decides when to search.

        Falls back to regular chat() if google-genai is not available.
        """
        if self._genai_client is None:
            return self.chat(prompt=prompt, model=model, timeout=timeout, cascade=cascade, images=images)

        from google.genai import types as _gt
        import time as _t

        models_to_try = cascade or [model or self.settings.model_respond]
        last_exc: Exception | None = None
        deadline = _t.monotonic() + timeout

        for m in models_to_try:
            remaining = deadline - _t.monotonic()
            if remaining <= 2.0:
                break
            try:
                contents: list[Any] = []
                if images:
                    # Build interleaved content parts for native genai SDK
                    segments = _IMG_MARKER_RE.split(prompt)
                    referenced: set[int] = set()
                    for i, seg in enumerate(segments):
                        if i % 2 == 0:
                            if seg:
                                contents.append(seg)
                        else:
                            idx = int(seg)
                            referenced.add(idx)
                            if idx < len(images):
                                img_bytes, img_mime = images[idx]
                                contents.append(_gt.Part.from_bytes(data=img_bytes, mime_type=img_mime))
                    for idx, (img_bytes, img_mime) in enumerate(images):
                        if idx not in referenced:
                            contents.append(_gt.Part.from_bytes(data=img_bytes, mime_type=img_mime))
                    if not contents:
                        contents = [prompt]
                else:
                    contents = [prompt]

                response = self._genai_client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=_gt.GenerateContentConfig(
                        tools=[_gt.Tool(google_search=_gt.GoogleSearch())],
                        temperature=0.4,
                        http_options=_gt.HttpOptions(timeout=int(remaining * 1000)),
                    ),
                )
                # Log Google Search grounding details
                search_used = False
                search_queries = []
                grounding_sources = []
                try:
                    for candidate in (response.candidates or []):
                        gc = getattr(candidate, 'grounding_metadata', None)
                        if gc:
                            # web_search_queries (what Gemini searched for)
                            for sq in getattr(gc, 'web_search_queries', None) or []:
                                search_queries.append(str(sq))
                            # grounding_chunks (actual web sources returned)
                            for chunk in getattr(gc, 'grounding_chunks', None) or []:
                                web = getattr(chunk, 'web', None)
                                if web:
                                    grounding_sources.append(f"{getattr(web, 'title', '?')}: {getattr(web, 'uri', '?')}")
                            # grounding_supports (text segments backed by sources)
                            supports = getattr(gc, 'grounding_supports', None) or []
                            if search_queries or grounding_sources or supports:
                                search_used = True
                except Exception as _e:
                    log.warning("chat_grounded: failed to parse grounding metadata: %s", _e)
                log.info("chat_grounded: model=%s search=%s queries=%s sources=%s",
                         m, search_used, search_queries[:5], grounding_sources[:5])
                return (response.text or "").strip()
            except Exception as exc:
                log.warning("Cascade chat_grounded: %s failed (%s), trying next model", m, exc)
                last_exc = exc

        log.warning("chat_grounded cascade exhausted, falling back to chat()")
        return self.chat(prompt=prompt, model=model, timeout=max(2.0, deadline - _t.monotonic()), cascade=cascade, images=images)

    def chat_openai_grounded(
        self,
        *,
        prompt: str,
        model: str = "gpt-5.4",
        timeout: float = 45.0,
        images: list[tuple[bytes, str]] | None = None,
    ) -> str:
        """Chat with OpenAI GPT model + web_search tool via Responses API.

        Falls back to Gemini chat_grounded if OpenAI client is not configured.
        """
        if self._openai_client is None:
            log.warning("chat_openai_grounded: no OpenAI key, falling back to Gemini")
            return self.chat_grounded(prompt=prompt, timeout=timeout, images=images)

        import time as _t

        try:
            # Build input — Responses API uses input_text/input_image types
            input_content: list[dict[str, Any]] = []
            if images:
                # Convert Chat-style parts to Responses API format
                chat_parts = _build_interleaved_parts(prompt, images)
                resp_parts: list[dict[str, Any]] = []
                for p in chat_parts:
                    if p.get("type") == "text":
                        resp_parts.append({"type": "input_text", "text": p["text"]})
                    elif p.get("type") == "image_url":
                        url = p["image_url"]["url"]
                        resp_parts.append({"type": "input_image", "image_url": url})
                input_content.append({"role": "user", "content": resp_parts})
            else:
                input_content.append({"role": "user", "content": prompt})

            response = self._openai_client.responses.create(
                model=model,
                input=input_content,
                tools=[{"type": "web_search"}],
                timeout=timeout,
            )
            text = response.output_text or ""
            log.info("chat_openai_grounded: model=%s len=%d", model, len(text))
            return text.strip()
        except Exception as exc:
            log.warning("chat_openai_grounded failed (%s), falling back to Gemini", exc)
            return self.chat_grounded(prompt=prompt, timeout=timeout, images=images)

    def extract_keywords(self, *, message: str) -> KeywordResult:
        """Extract search keywords from a user message using a fast model."""
        return self._json_call(
            model=KEYWORD_CASCADE[0],
            system=P.P_KEYWORD_SYSTEM,
            user=message,
            schema=KeywordResult,
            cascade=KEYWORD_CASCADE,
            timeout=15.0,
        )

    def embed(self, *, text: str) -> list[float]:
        resp = self.client.embeddings.create(model=self.settings.embedding_model, input=[text])
        return resp.data[0].embedding

    def embed_batch(self, *, texts: list[str], batch_size: int = 100) -> list[list[float]]:
        """Embed multiple texts in batched API calls. Returns list of vectors in same order as input."""
        if not texts:
            return []
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            resp = self.client.embeddings.create(model=self.settings.embedding_model, input=chunk)
            ordered = sorted(resp.data, key=lambda d: d.index if d.index is not None else 999999)
            all_embeddings.extend([d.embedding for d in ordered])
        return all_embeddings

    def image_to_text_json(self, *, image_bytes: bytes, context_text: str) -> ImgExtract:
        if not self.settings.model_img:
            raise ValueError("MODEL_IMG is required for image extraction")
        user = f"CONTEXT (may be empty):\n{context_text}\n\nTASK: Extract observations and text from the attached image."
        return self._json_call(
            model=self.settings.model_img,
            system=P.P_IMG_SYSTEM,
            user=user,
            schema=ImgExtract,
            images=[(image_bytes, "image/png")],
            cascade=SUBAGENT_CASCADE,
        )

    def extract_case_from_buffer(
        self,
        *,
        buffer_text: str,
        existing_cases: list[dict] | None = None,
    ) -> ExtractResult:
        parts = [f"BUFFER:\n{buffer_text}"]
        if existing_cases:
            lines = []
            for ec in existing_cases:
                lines.append(f"- {ec.get('title', '')} | {ec.get('summary', '')}")
            parts.append(
                "\nВЖЕ ВИТЯГНУТІ КЕЙСИ (НЕ витягувати повторно!):\n"
                + "\n".join(lines)
            )
        user = "\n".join(parts)
        return self._json_call(
            model=self.settings.model_extract,
            system=P.P_EXTRACT_SYSTEM,
            user=user,
            schema=ExtractResult,
            cascade=SUBAGENT_CASCADE,
        )

    def check_case_resolved(
        self, *, case_title: str, case_problem: str, buffer_text: str
    ) -> ResolutionResult:
        """Check if a recommendation case has been confirmed/resolved by messages in the buffer."""
        user = (
            f"ПРОБЛЕМА:\nЗаголовок: {case_title}\nОпис: {case_problem}\n\n"
            f"БУФЕР (B2):\n{buffer_text}"
        )
        return self._json_call(
            model=self.settings.model_case,
            system=P.P_RESOLVE_SYSTEM,
            user=user,
            schema=ResolutionResult,
            cascade=SUBAGENT_CASCADE,
        )

    def make_case(self, *, case_block_text: str, images: list[tuple[bytes, str]] | None = None) -> CaseResult:
        user = f"CASE_BLOCK:\n{case_block_text}"
        return self._json_call(
            model=self.settings.model_case,
            system=P.P_CASE_SYSTEM,
            user=user,
            schema=CaseResult,
            images=images,
            cascade=SUBAGENT_CASCADE,
        )

    def unified_buffer_analysis(
        self,
        *,
        buffer_text: str,
        existing_cases: list[dict] | None = None,
        recommendation_cases: list[dict] | None = None,
        images: list[tuple[bytes, str]] | None = None,
    ) -> UnifiedBufferResult:
        """Single LLM call: extract new cases + promote recommendations + update existing."""
        parts = [f"БУФЕР:\n{buffer_text}"]
        if existing_cases:
            lines = []
            for ec in existing_cases:
                ev_ids = ec.get('evidence_ids') or []
                ev_str = f" | evidence_ids={','.join(str(e) for e in ev_ids)}" if ev_ids else ""
                sol = ec.get('solution_summary') or ec.get('summary', '')
                lines.append(
                    f"- case_id={ec.get('case_id', '')} | {ec.get('title', '')} "
                    f"| Рішення: {sol[:300]}{ev_str}"
                )
            parts.append(
                "\nІСНУЮЧІ КЕЙСИ (НЕ витягувати повторно, але можна оновити через updates!):\n"
                + "\n".join(lines)
            )
        if recommendation_cases:
            lines = []
            for rc in recommendation_cases:
                lines.append(
                    f"- case_id={rc.get('case_id', '')} | {rc.get('problem_title', '')} "
                    f"| Порада: {rc.get('solution_summary', '')[:200]}"
                )
            parts.append(
                "\nІСНУЮЧІ RECOMMENDATION КЕЙСИ (перевір чи підтверджені в буфері):\n"
                + "\n".join(lines)
            )
        user = "\n".join(parts)
        return self._json_call(
            model=self.settings.model_case,
            system=P.P_UNIFIED_BUFFER_SYSTEM,
            user=user,
            schema=UnifiedBufferResult,
            images=images,
            cascade=SUBAGENT_CASCADE,
            timeout=90.0,
        )

    def decide_consider(
        self, *, message: str, context: str, images: list[tuple[bytes, str]] | None = None
    ) -> DecisionResult:
        user = f"MESSAGE:\n{message}\n\nCONTEXT (незавершені обговорення з buffer):\n{context}"
        return self._json_call(
            model=self.settings.model_decision,
            system=P.P_DECISION_SYSTEM,
            user=user,
            schema=DecisionResult,
            images=images,
            cascade=GATE_CASCADE,
        )

    def batch_gate(
        self, *, unprocessed: str, context: str,
        images: list[tuple[bytes, str]] | None = None,
    ) -> "BatchGateResult":
        from app.llm.schemas import BatchGateResult
        user = f"CONTEXT (оброблені повідомлення):\n{context}\n\nUNPROCESSED (нові повідомлення):\n{unprocessed}"
        return self._json_call(
            model=self.settings.model_decision,
            system=P.P_BATCH_GATE_SYSTEM,
            user=user,
            schema=BatchGateResult,
            images=images,
            cascade=GATE_CASCADE,
        )

    def decide_and_respond(
        self,
        *,
        message: str,
        context: str,
        cases: str,
        buffer: str = "",
        images: list[tuple[bytes, str]] | None = None,
    ) -> RespondResult:
        parts = [
            f"MESSAGE:\n{message}",
            f"CONTEXT (last messages):\n{context}",
            f"RETRIEVED CASES (top-K):\n{cases}",
        ]
        if buffer.strip():
            parts.append(f"BUFFER (ongoing discussions):\n{buffer}")
        
        user = "\n\n".join(parts)
        return self._json_call(
            model=self.settings.model_respond,
            system=P.P_RESPOND_SYSTEM,
            user=user,
            schema=RespondResult,
            images=images,
            cascade=SUBAGENT_CASCADE,
        )


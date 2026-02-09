from __future__ import annotations

import base64
import json
import logging
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
    RespondResult,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Use Google's OpenAI-compatible endpoint for Gemini models
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

    def _json_call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Type[T],
        images: list[tuple[bytes, str]] | None = None,
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
                    parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
                    for image_bytes, image_mime in images:
                        b64 = base64.b64encode(image_bytes).decode("ascii")
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{image_mime};base64,{b64}"},
                            }
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": parts,
                        }
                    )

                resp = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0,
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

    def embed(self, *, text: str) -> list[float]:
        resp = self.client.embeddings.create(model=self.settings.embedding_model, input=[text])
        return resp.data[0].embedding

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
        )

    def extract_case_from_buffer(self, *, buffer_text: str) -> ExtractResult:
        user = f"BUFFER:\n{buffer_text}"
        return self._json_call(
            model=self.settings.model_extract,
            system=P.P_EXTRACT_SYSTEM,
            user=user,
            schema=ExtractResult,
        )

    def make_case(self, *, case_block_text: str) -> CaseResult:
        user = f"CASE_BLOCK:\n{case_block_text}"
        return self._json_call(
            model=self.settings.model_case,
            system=P.P_CASE_SYSTEM,
            user=user,
            schema=CaseResult,
        )

    def decide_consider(
        self, *, message: str, context: str, images: list[tuple[bytes, str]] | None = None
    ) -> DecisionResult:
        user = f"MESSAGE:\n{message}\n\nCONTEXT (last messages):\n{context}"
        return self._json_call(
            model=self.settings.model_decision,
            system=P.P_DECISION_SYSTEM,
            user=user,
            schema=DecisionResult,
            images=images,
        )

    def decide_and_respond(
        self,
        *,
        message: str,
        context: str,
        cases: str,
        images: list[tuple[bytes, str]] | None = None,
    ) -> RespondResult:
        user = (
            f"MESSAGE:\n{message}\n\n"
            f"CONTEXT (last messages):\n{context}\n\n"
            f"RETRIEVED CASES (top-K):\n{cases}"
        )
        return self._json_call(
            model=self.settings.model_respond,
            system=P.P_RESPOND_SYSTEM,
            user=user,
            schema=RespondResult,
            images=images,
        )


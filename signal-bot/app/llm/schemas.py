from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field, model_validator


class ImgExtract(BaseModel):
    observations: List[str] = Field(default_factory=list)
    extracted_text: str = ""


class ExtractedCaseSpan(BaseModel):
    start_idx: int
    end_idx: int
    start_line: int | None = None
    end_line: int | None = None
    case_block: str = ""


class ExtractResult(BaseModel):
    cases: List[ExtractedCaseSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_spans(self) -> "ExtractResult":
        if not self.cases:
            return self

        prev_end = -1
        for i, c in enumerate(self.cases):
            if c.start_idx < 0 or c.end_idx < 0:
                raise ValueError(f"Case span {i} has negative indexes")
            if c.start_idx > c.end_idx:
                raise ValueError(f"Case span {i} has start_idx > end_idx")
            if c.start_line is not None and c.end_line is not None and c.start_line > c.end_line:
                raise ValueError(f"Case span {i} has start_line > end_line")
            if c.start_idx <= prev_end:
                raise ValueError("Case spans must be sorted and non-overlapping")
            prev_end = c.end_idx
        return self


class CaseResult(BaseModel):
    keep: bool
    status: Literal["solved", "open"] = "open"
    problem_title: str = ""
    problem_summary: str = ""
    solution_summary: str = ""
    tags: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)


class DecisionResult(BaseModel):
    consider: bool
    tag: Literal["new_question", "ongoing_discussion", "noise", "statement"] = "new_question"


class RespondResult(BaseModel):
    respond: bool
    text: str = ""
    citations: List[str] = Field(default_factory=list)


class BlocksCase(BaseModel):
    case_block: str


class BlocksResult(BaseModel):
    cases: List[BlocksCase] = Field(default_factory=list)


class ResolutionResult(BaseModel):
    """Result of checking whether a B1 open case has been resolved by current B2 buffer."""
    resolved: bool
    solution_summary: str = ""  # Non-empty only when resolved=True


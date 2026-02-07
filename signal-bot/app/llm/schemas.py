from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class ImgExtract(BaseModel):
    observations: List[str] = Field(default_factory=list)
    extracted_text: str = ""


class ExtractResult(BaseModel):
    found: bool
    case_block: str = ""
    buffer_new: str = ""


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


class RespondResult(BaseModel):
    respond: bool
    text: str = ""
    citations: List[str] = Field(default_factory=list)


class BlocksCase(BaseModel):
    case_block: str


class BlocksResult(BaseModel):
    cases: List[BlocksCase] = Field(default_factory=list)


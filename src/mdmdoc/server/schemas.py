#!/usr/bin/env python3
"""schemas.py — pydantic request/response models for the mdmdoc API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FieldCorrection(BaseModel):
    action: Literal["keep", "set", "clear"] = "keep"
    value: Any = None


class ReviewSubmission(BaseModel):
    fields: dict[str, FieldCorrection] = Field(default_factory=dict)
    doc_type_gold: str = ""
    verdict_gold: str = ""
    notes: str = ""
    reviewer: str = "egor"
    scenarios: list[str] = Field(default_factory=list)  # scenario tags (see scenarios.py)
    error_source: str = ""   # ocr_missed | model_mapped_wrong | rule_wrong | ...
    retrain: bool = True     # save-and-retrain is the default feedback flow


class FewshotIn(BaseModel):
    k: int = 2


class ModelfileIn(BaseModel):
    apply: bool = False


class LoraIn(BaseModel):
    min_labels: int = 100
    force: bool = False
    split: float = 0.85


class EvalIn(BaseModel):
    only: Literal["bank", "w9"] | None = None
    limit: int | None = None
    tag: str = ""
    scenario: str | None = None   # only labels carrying this scenario tag

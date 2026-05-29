from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ClassificationResult(BaseModel):
    topic: Literal["external_galaxy_evolution", "ml_in_astronomy", "both", "neither"]
    relevance_score: int = Field(ge=0, le=5)
    rationale: str
    keywords: list[str] = Field(default_factory=list)

"""Input validation models and the research-report JSON schema.

Every tool input is validated against a strict pydantic model — bounded lengths,
numeric ranges, and enums — before any network call is made (NSA: validate
parameters). Oversized, malformed, or missing fields are rejected early.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Upper bound on free-text inputs (~4 KB) to bound memory and resist DoS.
MAX_QUERY_CHARS = 4096
MAX_SYSTEM_PROMPT_CHARS = 4096


class SonarModel(StrEnum):
    """Allowed Sonar synthesis models (enum prevents arbitrary model strings)."""

    sonar = "sonar"
    sonar_pro = "sonar-pro"


class _StrictModel(BaseModel):
    # Reject unexpected fields so callers can't smuggle extra parameters through.
    model_config = ConfigDict(extra="forbid")


class SearchInput(_StrictModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    max_results: int = Field(default=5, ge=1, le=20)
    max_tokens_per_page: int = Field(default=1024, ge=128, le=4096)


class SonarAskInput(_StrictModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    model: SonarModel = SonarModel.sonar
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)


class DeepResearchInput(_StrictModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    num_subquestions: int = Field(default=4, ge=1, le=8)
    model: SonarModel = SonarModel.sonar_pro
    max_results_per_subquestion: int = Field(default=5, ge=1, le=10)
    # Opt-in: ask Sonar to decompose the question instead of the deterministic
    # angle list (one extra model call; falls back to deterministic on failure).
    use_model_decomposition: bool = False


def research_report_schema() -> dict[str, Any]:
    """JSON schema handed to Sonar via ``response_format`` for structured output.

    Mirrors the reference architecture: an answer plus key findings, open
    questions, and claims each carrying their supporting URLs so citations can be
    validated against retrieval metadata rather than trusted from free text.
    """
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "key_findings": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "supporting_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low", "uncertain"],
                        },
                    },
                    "required": ["claim", "supporting_urls"],
                },
            },
        },
        "required": ["answer", "key_findings", "open_questions", "claims"],
    }

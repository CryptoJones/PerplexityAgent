"""Input validation models and the research-report JSON schema.

Every tool input is validated against a strict pydantic model — bounded lengths,
numeric ranges, and enums — before any network call is made (NSA: validate
parameters). Oversized, malformed, or missing fields are rejected early.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

# Upper bound on free-text inputs (~4 KB) to bound memory and resist DoS.
MAX_QUERY_CHARS = 4096
MAX_SYSTEM_PROMPT_CHARS = 4096
MAX_INSTRUCTIONS_CHARS = 16_384
MAX_MODEL_CHARS = 128
MAX_TOOL_DESCRIPTION_CHARS = 4096

BoundedInputText = Annotated[str, Field(min_length=1, max_length=MAX_INSTRUCTIONS_CHARS)]
DomainFilter = Annotated[str, Field(min_length=1, max_length=253)]
LanguageCode = Annotated[str, Field(pattern=r"^[a-z]{2}$")]
AgentModelId = Annotated[str, Field(min_length=1, max_length=MAX_MODEL_CHARS)]
FinanceHint = Annotated[str, Field(min_length=1, max_length=128)]


class SonarModel(StrEnum):
    """Allowed Sonar synthesis models (enum prevents arbitrary model strings)."""

    sonar = "sonar"
    sonar_pro = "sonar-pro"


class _StrictModel(BaseModel):
    # Reject unexpected fields so callers can't smuggle extra parameters through.
    model_config = ConfigDict(extra="forbid")


class _ResponseModel(BaseModel):
    """Provider response models retain forward-compatible fields."""

    model_config = ConfigDict(extra="allow")


class SearchInput(_StrictModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    max_results: int = Field(default=5, ge=1, le=20)
    max_tokens_per_page: int = Field(default=1024, ge=128, le=4096)
    search_domain_filter: list[DomainFilter] | None = Field(default=None, max_length=20)
    search_language_filter: list[LanguageCode] | None = Field(default=None, max_length=10)
    search_recency_filter: Literal["hour", "day", "week", "month", "year"] | None = None
    search_after_date_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")
    search_before_date_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")
    last_updated_after_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")
    last_updated_before_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")


class PeopleSearchInput(SearchInput):
    """Direct Search API request routed through the people-search index."""


class FetchUrlInput(_StrictModel):
    urls: list[HttpUrl] = Field(..., min_length=1, max_length=10)
    max_urls: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def enforce_cap(self) -> FetchUrlInput:
        if len(self.urls) > self.max_urls:
            raise ValueError("number of URLs exceeds max_urls")
        return self


class ReasoningConfig(_StrictModel):
    effort: Literal["low", "medium", "high", "xhigh", "max"]


class ResponseFormat(_StrictModel):
    """OpenAI-compatible structured-output selector."""

    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_schema(self) -> ResponseFormat:
        if self.type == "json_schema" and self.json_schema is None:
            raise ValueError("json_schema is required when response_format.type is 'json_schema'")
        if self.type != "json_schema" and self.json_schema is not None:
            raise ValueError("json_schema is only valid when response_format.type is 'json_schema'")
        return self


class InputText(_StrictModel):
    type: Literal["input_text"] = "input_text"
    text: str = Field(..., min_length=1, max_length=MAX_INSTRUCTIONS_CHARS)


class InputImage(_StrictModel):
    type: Literal["input_image"] = "input_image"
    image_url: HttpUrl
    detail: Literal["auto", "low", "high"] = "auto"


InputContent = Annotated[InputText | InputImage, Field(discriminator="type")]


class InputMessage(_StrictModel):
    type: Literal["message"] = "message"
    role: Literal["user", "assistant", "system", "developer"]
    content: BoundedInputText | list[InputContent]


class FunctionCallInput(_StrictModel):
    type: Literal["function_call"] = "function_call"
    call_id: str = Field(..., min_length=1, max_length=256)
    name: str = Field(..., min_length=1, max_length=128)
    arguments: str = Field(..., max_length=MAX_INSTRUCTIONS_CHARS)
    thought_signature: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS_CHARS)


class FunctionCallOutputInput(_StrictModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str = Field(..., min_length=1, max_length=256)
    output: str = Field(..., max_length=MAX_INSTRUCTIONS_CHARS)


ResponseInputItem = Annotated[
    InputMessage | FunctionCallInput | FunctionCallOutputInput,
    Field(discriminator="type"),
]


class WebSearchFilters(_StrictModel):
    search_domain_filter: list[DomainFilter] | None = Field(default=None, max_length=20)
    search_language_filter: list[LanguageCode] | None = Field(default=None, max_length=10)
    search_recency_filter: Literal["day", "week", "month", "year"] | None = None
    search_after_date_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")
    search_before_date_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")
    last_updated_after_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")
    last_updated_before_filter: str | None = Field(default=None, pattern=r"^\d{1,2}/\d{1,2}/\d{4}$")


class WebSearchTool(_StrictModel):
    type: Literal["web_search"] = "web_search"
    filters: WebSearchFilters | None = None
    search_context_size: Literal["low", "medium", "high"] | None = None
    max_tokens_per_page: int | None = Field(default=None, ge=1, le=1_000_000)


class FinanceSearchTool(_StrictModel):
    type: Literal["finance_search"] = "finance_search"


class PeopleSearchTool(_StrictModel):
    type: Literal["people_search"] = "people_search"


class FetchUrlTool(_StrictModel):
    type: Literal["fetch_url"] = "fetch_url"


class FunctionTool(_StrictModel):
    type: Literal["function"] = "function"
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = Field(default=None, max_length=MAX_TOOL_DESCRIPTION_CHARS)
    parameters: dict[str, Any]
    strict: bool = True

    @field_validator("parameters")
    @classmethod
    def bound_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str)) > 65_536:
            raise ValueError("function parameter schema exceeds 65536 characters")
        return value


AgentTool = Annotated[
    WebSearchTool | FinanceSearchTool | PeopleSearchTool | FetchUrlTool | FunctionTool,
    Field(discriminator="type"),
]


class ResponseCreateInput(_StrictModel):
    """Validated request body for Perplexity's OpenAI-compatible Agent API."""

    input: BoundedInputText | list[ResponseInputItem]
    model: str | None = Field(default=None, min_length=1, max_length=MAX_MODEL_CHARS)
    models: list[AgentModelId] | None = Field(default=None, min_length=1, max_length=5)
    preset: str | None = Field(default=None, min_length=1, max_length=64)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS_CHARS)
    language_preference: LanguageCode | None = None
    reasoning: ReasoningConfig | None = None
    response_format: ResponseFormat | None = None
    tools: list[AgentTool] | None = Field(default=None, max_length=16)
    previous_response_id: str | None = Field(default=None, min_length=1, max_length=256)
    store: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_steps: int | None = Field(default=None, ge=1, le=10)
    stream: bool = False

    @model_validator(mode="after")
    def validate_model_selection(self) -> ResponseCreateInput:
        if self.model and self.models:
            raise ValueError("model and models are mutually exclusive")
        selected = [self.model] if self.model else (self.models or [])
        if (
            any(model.startswith("anthropic/") for model in selected)
            and self.max_output_tokens is None
        ):
            raise ValueError("max_output_tokens is required for Anthropic models")
        return self


class ResponseRetrieveInput(_StrictModel):
    response_id: str = Field(..., min_length=6, max_length=256, pattern=r"^resp_[A-Za-z0-9_-]+$")


class RegisteredFunctionInput(_StrictModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str)) > MAX_INSTRUCTIONS_CHARS:
            raise ValueError("function arguments exceed the input limit")
        return value


class FinanceSearchInput(_StrictModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    categories: list[FinanceHint] | None = Field(default=None, max_length=20)
    tickers: list[FinanceHint] | None = Field(default=None, max_length=20)
    model: str = Field(default="perplexity/sonar", min_length=1, max_length=MAX_MODEL_CHARS)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class OutputText(_ResponseModel):
    type: Literal["output_text"]
    text: str
    annotations: list[dict[str, Any]] = Field(default_factory=list)


class MessageOutputItem(_ResponseModel):
    type: Literal["message"]
    id: str
    role: str
    content: list[OutputText]


class SearchResultsOutputItem(_ResponseModel):
    type: Literal["search_results"]
    results: list[dict[str, Any]] = Field(default_factory=list)


class FinanceResultsOutputItem(_ResponseModel):
    type: Literal["finance_results"]
    results: list[dict[str, Any]] = Field(default_factory=list)


class PeopleSearchResultsOutputItem(_ResponseModel):
    type: Literal["people_search_results"]
    results: list[dict[str, Any]] = Field(default_factory=list)


class FetchUrlResultsOutputItem(_ResponseModel):
    type: Literal["fetch_url_results"]
    contents: list[dict[str, Any]] = Field(default_factory=list)


class FunctionCallOutputItem(_ResponseModel):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str
    thought_signature: str | None = None


ResponseOutputItem = Annotated[
    MessageOutputItem
    | SearchResultsOutputItem
    | FinanceResultsOutputItem
    | PeopleSearchResultsOutputItem
    | FetchUrlResultsOutputItem
    | FunctionCallOutputItem,
    Field(discriminator="type"),
]


class ResponsesResponse(_ResponseModel):
    created_at: int
    id: str
    model: str
    object: Literal["response"]
    output: list[ResponseOutputItem]
    status: Literal["completed", "failed", "in_progress", "requires_action"]
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


class ResponseStreamEvent(_ResponseModel):
    type: str
    response: ResponsesResponse | None = None


class SonarAskInput(_StrictModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    model: str = Field(default=SonarModel.sonar.value, min_length=1, max_length=MAX_MODEL_CHARS)
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    reasoning: ReasoningConfig | None = None
    response_format: ResponseFormat | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if value not in {item.value for item in SonarModel} and "/" not in value:
            raise ValueError("model must be sonar, sonar-pro, or a provider/model Agent API ID")
        return value

    @model_validator(mode="after")
    def require_anthropic_limit(self) -> SonarAskInput:
        if self.model.startswith("anthropic/") and self.max_output_tokens is None:
            raise ValueError("max_output_tokens is required for Anthropic models")
        return self


class DeepResearchInput(_StrictModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)
    num_subquestions: int = Field(default=4, ge=1, le=8)
    model: SonarModel = SonarModel.sonar_pro
    max_results_per_subquestion: int = Field(default=5, ge=1, le=10)
    # Opt-in: ask Sonar to decompose the question instead of the deterministic
    # angle list (one extra model call; falls back to deterministic on failure).
    use_model_decomposition: bool = False


class RetrieveInput(_StrictModel):
    # The retrieve_key is a 24-char hex content hash from an offload envelope.
    key: str = Field(..., min_length=1, max_length=128)


def research_report_schema(version: str = "v1") -> dict[str, Any]:
    """JSON schema handed to Sonar via ``response_format`` for structured output.

    Mirrors the reference architecture: an answer plus key findings, open
    questions, and claims each carrying their supporting URLs so citations can be
    validated against retrieval metadata rather than trusted from free text.

    ``version`` selects from a small registry, so the schema can evolve without
    breaking a caller pinned to an older shape (``ValueError`` on an unknown one).
    """
    try:
        builder = _SCHEMA_VERSIONS[version]
    except KeyError:
        raise ValueError(
            f"Unknown research_report_schema version {version!r}; known: {sorted(_SCHEMA_VERSIONS)}"
        ) from None
    return builder()


def _research_report_schema_v1() -> dict[str, Any]:
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
                            # format:uri so an obviously non-URL value is rejected
                            # at the structured-output boundary.
                            "items": {"type": "string", "format": "uri"},
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


# Registry of report-schema versions. Add a new builder under a new key rather
# than mutating v1 so pinned callers keep their shape.
_SCHEMA_VERSIONS = {"v1": _research_report_schema_v1}

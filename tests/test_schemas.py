import pytest
from pydantic import ValidationError

from perplexity_agent.schemas import (
    DeepResearchInput,
    FetchUrlInput,
    InputImage,
    ResponseCreateInput,
    ResponseRetrieveInput,
    ResponsesResponse,
    SearchInput,
    SonarAskInput,
    SonarModel,
    research_report_schema,
)


def test_search_defaults_and_bounds():
    s = SearchInput(query="hello")
    assert s.max_results == 5
    assert s.max_tokens_per_page == 1024


@pytest.mark.parametrize("bad", [{"query": ""}, {"query": "x" * 5000}])
def test_search_rejects_bad_query(bad):
    with pytest.raises(ValidationError):
        SearchInput(**bad)


@pytest.mark.parametrize("n", [0, 21, -3])
def test_search_rejects_out_of_range_max_results(n):
    with pytest.raises(ValidationError):
        SearchInput(query="ok", max_results=n)


def test_search_rejects_extra_fields():
    with pytest.raises(ValidationError):
        SearchInput(query="ok", evil="payload")


def test_search_accepts_current_filter_contract():
    value = SearchInput(
        query="ok",
        search_domain_filter=["nasa.gov"],
        search_recency_filter="week",
        search_after_date_filter="7/1/2026",
    )
    assert value.search_recency_filter == "week"
    with pytest.raises(ValidationError):
        SearchInput(query="ok", search_recency_filter="decade")


def test_sonar_ask_enum():
    assert SonarAskInput(question="q", model="sonar-pro").model == SonarModel.sonar_pro.value
    with pytest.raises(ValidationError):
        SonarAskInput(question="q", model="gpt-4")


def test_sonar_ask_accepts_agent_model_and_reasoning():
    value = SonarAskInput(
        question="q",
        model="openai/gpt-5.5",
        reasoning={"effort": "high"},
        max_output_tokens=100,
    )
    assert value.reasoning and value.reasoning.effort == "high"


def test_deep_research_bounds():
    assert DeepResearchInput(question="q").num_subquestions == 4
    with pytest.raises(ValidationError):
        DeepResearchInput(question="q", num_subquestions=99)


def test_deep_research_model_decomposition_defaults_off():
    assert DeepResearchInput(question="q").use_model_decomposition is False
    assert DeepResearchInput(question="q", use_model_decomposition=True).use_model_decomposition


def test_agent_response_supports_multimodal_and_function_chaining():
    value = ResponseCreateInput.model_validate(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe this"},
                        {"type": "input_image", "image_url": "https://example.com/image.png"},
                    ],
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "42"},
            ],
            "model": "openai/gpt-5.5",
            "reasoning": {"effort": "high"},
            "tools": [
                {"type": "web_search", "filters": {"search_domain_filter": ["nasa.gov"]}},
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
            "store": True,
            "previous_response_id": "resp_123",
        }
    )
    assert value.store is True
    assert value.previous_response_id == "resp_123"
    assert value.tools and value.tools[1].type == "function"


def test_input_image_requires_an_http_url():
    assert str(InputImage(image_url="https://example.com/image.png").image_url).startswith(
        "https://"
    )
    with pytest.raises(ValidationError):
        InputImage(image_url="not a URL")
    with pytest.raises(ValidationError):
        InputImage(image_url="data:image/png;base64,AAAA")


def test_agent_response_requires_anthropic_token_limit():
    with pytest.raises(ValidationError, match="max_output_tokens"):
        ResponseCreateInput(input="hello", model="anthropic/claude-sonnet-4-6")
    value = ResponseCreateInput(
        input="hello", model="anthropic/claude-sonnet-4-6", max_output_tokens=1024
    )
    assert value.max_output_tokens == 1024


def test_agent_response_rejects_conflicting_model_selection():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        ResponseCreateInput(input="hello", model="openai/gpt-5.5", models=["xai/grok-4.3"])


def test_agent_response_bounds_text_and_filter_items():
    with pytest.raises(ValidationError):
        ResponseCreateInput(input="x" * 20_000, model="openai/gpt-5.5")
    with pytest.raises(ValidationError):
        SearchInput(query="ok", search_domain_filter=["x" * 254])


def test_response_retrieve_requires_response_id_shape():
    assert ResponseRetrieveInput(response_id="resp_123").response_id == "resp_123"
    with pytest.raises(ValidationError):
        ResponseRetrieveInput(response_id="not-a-response")


def test_fetch_url_enforces_explicit_multi_url_cap():
    value = FetchUrlInput(urls=["https://a.example", "https://b.example"], max_urls=2)
    assert len(value.urls) == 2
    with pytest.raises(ValidationError, match="exceeds max_urls"):
        FetchUrlInput(urls=["https://a.example", "https://b.example"], max_urls=1)


def test_responses_response_parses_typed_output_items():
    response = ResponsesResponse.model_validate(
        {
            "created_at": 1,
            "id": "resp_1",
            "model": "openai/gpt-5.5",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ],
        }
    )
    assert response.output[0].type == "function_call"


def test_function_tool_schema_size_is_bounded():
    with pytest.raises(ValidationError, match="parameter schema"):
        ResponseCreateInput(
            input="hello",
            model="openai/gpt-5.5",
            tools=[
                {
                    "type": "function",
                    "name": "oversized",
                    "parameters": {"description": "x" * 70_000},
                }
            ],
        )


def test_research_report_schema_shape():
    schema = research_report_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"answer", "key_findings", "open_questions", "claims"}


def test_research_report_schema_supporting_urls_have_uri_format():
    items = research_report_schema()["properties"]["claims"]["items"]
    assert items["properties"]["supporting_urls"]["items"]["format"] == "uri"


def test_research_report_schema_versioning():
    assert research_report_schema("v1") == research_report_schema()
    with pytest.raises(ValueError, match="Unknown research_report_schema version"):
        research_report_schema("v999")

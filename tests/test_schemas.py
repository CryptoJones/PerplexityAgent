import pytest
from pydantic import ValidationError

from perplexity_agent.schemas import (
    DeepResearchInput,
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


def test_sonar_ask_enum():
    assert SonarAskInput(question="q", model="sonar-pro").model is SonarModel.sonar_pro
    with pytest.raises(ValidationError):
        SonarAskInput(question="q", model="gpt-4")


def test_deep_research_bounds():
    assert DeepResearchInput(question="q").num_subquestions == 4
    with pytest.raises(ValidationError):
        DeepResearchInput(question="q", num_subquestions=99)


def test_research_report_schema_shape():
    schema = research_report_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"answer", "key_findings", "open_questions", "claims"}

"""FastMCP server exposing the Perplexity tools.

Three tools are registered — ``perplexity_search``, ``sonar_ask`` and
``deep_research``. Every invocation runs through the same control path: validate
input against a strict schema, apply the token-bucket rate limit, call the API,
then emit a redacted audit record (NSA: validate parameters, DoS guard, logging).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import PerplexityClient
from .config import Settings, load_settings
from .research import deep_research as _deep_research
from .schemas import DeepResearchInput, SearchInput, SonarAskInput, SonarModel
from .security import AuditLogger, RequestGuard, TokenBucket, content_hash


def build_server(settings: Settings | None = None) -> tuple[FastMCP, Settings]:
    """Construct the FastMCP server and return it alongside the loaded settings."""
    settings = settings or load_settings()
    audit = AuditLogger(settings.audit_log_path)
    bucket = TokenBucket(settings.rate_per_minute, settings.rate_burst)
    guard = RequestGuard(bucket, audit)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        client = PerplexityClient(settings)
        try:
            yield {"client": client}
        finally:
            await client.aclose()

    mcp = FastMCP(
        "perplexity-agent",
        instructions=(
            "Tools for web research via the Perplexity API. Use perplexity_search "
            "for raw ranked results, sonar_ask for a grounded answer, and "
            "deep_research for a multi-step, validated, cited report."
        ),
        lifespan=lifespan,
        host=settings.http_host,
        port=settings.http_port,
    )

    def _client(ctx: Context) -> PerplexityClient:
        client: PerplexityClient = ctx.request_context.lifespan_context["client"]
        return client

    def _guard(tool: str, params: dict[str, Any]) -> None:
        """Rate-limit and audit the start of a tool call (params redacted)."""
        guard.acquire("tool_call", tool=tool, params=params)

    @mcp.tool()
    async def perplexity_search(
        ctx: Context,
        query: str,
        max_results: int = 5,
        max_tokens_per_page: int = 1024,
    ) -> dict[str, Any]:
        """Search the web via Perplexity's Search API and return ranked results."""
        args = SearchInput(
            query=query, max_results=max_results, max_tokens_per_page=max_tokens_per_page
        )
        _guard("perplexity_search", {"query": args.query, "max_results": args.max_results})
        result = await _client(ctx).search(
            args.query, args.max_results, args.max_tokens_per_page
        )
        audit.record("tool_result", tool="perplexity_search", result_hash=content_hash(result))
        return result

    @mcp.tool()
    async def sonar_ask(
        ctx: Context,
        question: str,
        model: str = "sonar",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Ask Sonar for a grounded answer (OpenAI-compatible chat completion)."""
        args = SonarAskInput(
            question=question, model=SonarModel(model), system_prompt=system_prompt
        )
        _guard("sonar_ask", {"question": args.question, "model": args.model.value})
        messages: list[dict[str, str]] = []
        if args.system_prompt:
            messages.append({"role": "system", "content": args.system_prompt})
        messages.append({"role": "user", "content": args.question})
        result = await _client(ctx).chat(messages, model=args.model.value)
        audit.record("tool_result", tool="sonar_ask", result_hash=content_hash(result))
        return result

    @mcp.tool()
    async def deep_research(
        ctx: Context,
        question: str,
        num_subquestions: int = 4,
        model: str = "sonar-pro",
        max_results_per_subquestion: int = 5,
    ) -> dict[str, Any]:
        """Run a multi-step research pipeline returning a validated, cited report."""
        args = DeepResearchInput(
            question=question,
            num_subquestions=num_subquestions,
            model=SonarModel(model),
            max_results_per_subquestion=max_results_per_subquestion,
        )
        _guard(
            "deep_research",
            {"question": args.question, "num_subquestions": args.num_subquestions},
        )
        result = await _deep_research(
            _client(ctx),
            args.question,
            num_subquestions=args.num_subquestions,
            model=args.model.value,
            max_results_per_subquestion=args.max_results_per_subquestion,
        )
        audit.record(
            "tool_result",
            tool="deep_research",
            result_hash=content_hash(result),
            validation_passed=result["validation_report"]["passed"],
        )
        return result

    return mcp, settings

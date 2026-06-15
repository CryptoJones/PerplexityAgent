"""FastMCP server exposing the Perplexity tools.

Tools: ``perplexity_search``, ``sonar_ask``, ``deep_research``, and ``retrieve``.
Every invocation runs through the same control path: validate input against a
strict schema, apply the token-bucket rate limit, call the API, **bound the
result to the output budget** (offloading an oversized value behind a
``retrieve_key`` the ``retrieve`` tool can fetch), then emit a redacted audit
record (NSA: validate parameters, DoS guard, logging; context-flood guard).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from mcp.server.fastmcp import Context, FastMCP

from .client import PerplexityClient
from .config import Settings, load_settings
from .efficiency import OffloadStore, bound_result
from .metrics import MetricsCollector
from .research import deep_research as _deep_research
from .schemas import DeepResearchInput, RetrieveInput, SearchInput, SonarAskInput, SonarModel
from .security import AuditLogger, RateLimitError, TokenBucket, content_hash


def build_server(settings: Settings | None = None) -> tuple[FastMCP, Settings]:
    """Construct the FastMCP server and return it alongside the loaded settings."""
    settings = settings or load_settings()
    audit = AuditLogger(settings.audit_log_path)
    bucket = TokenBucket(settings.rate_per_minute, settings.rate_burst)
    # One offload store for the server's lifetime: when a result is over budget,
    # it's stashed here and the agent gets a retrieve_key to fetch it on demand.
    store = OffloadStore()
    metrics = MetricsCollector()

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

    def _guard(tool: str, params: dict[str, Any]) -> str:
        """Rate-limit and audit the start of a tool call (params redacted).

        Returns a correlation id shared by this call's ``tool_call`` and
        ``tool_result`` audit events, so a SIEM can pair them.
        """
        call_id = uuid.uuid4().hex
        try:
            bucket.acquire()
        except RateLimitError:
            metrics.record_rate_limited(tool)
            audit.record("rate_limited", tool=tool, call_id=call_id, params=params)
            raise
        audit.record("tool_call", tool=tool, call_id=call_id, params=params)
        return call_id

    def _result_fields(
        tool: str, call_id: str, started: float, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Common ``tool_result`` audit fields (correlation, latency, token usage);
        also records the call's latency in the metrics collector."""
        duration_ms = round((time.monotonic() - started) * 1000, 1)
        metrics.record_call(tool, duration_ms)
        fields: dict[str, Any] = {
            "call_id": call_id,
            "duration_ms": duration_ms,
            "result_hash": content_hash(result),
        }
        usage = result.get("usage")
        if usage:
            fields["usage"] = usage
        return fields

    def _bound(result: dict[str, Any]) -> dict[str, Any]:
        """Bound a result to the char budget, offloading the full value if over.

        For a dict input ``bound_result`` returns either the dict unchanged or a
        ``{truncated, content, retrieve_key}`` envelope — always a dict.
        """
        return cast(
            dict[str, Any],
            bound_result(result, max_chars=settings.max_tool_output_chars, store=store),
        )

    def _truncated(bounded: Any) -> bool:
        return isinstance(bounded, dict) and bool(bounded.get("truncated"))

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
        call_id = _guard(
            "perplexity_search", {"query": args.query, "max_results": args.max_results}
        )
        started = time.monotonic()
        result = await _client(ctx).search(
            args.query, args.max_results, args.max_tokens_per_page
        )
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="perplexity_search",
            truncated=_truncated(bounded),
            **_result_fields("perplexity_search", call_id, started, result),
        )
        return bounded

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
        call_id = _guard("sonar_ask", {"question": args.question, "model": args.model.value})
        started = time.monotonic()
        messages: list[dict[str, str]] = []
        if args.system_prompt:
            messages.append({"role": "system", "content": args.system_prompt})
        messages.append({"role": "user", "content": args.question})
        result = await _client(ctx).chat(messages, model=args.model.value)
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="sonar_ask",
            truncated=_truncated(bounded),
            **_result_fields("sonar_ask", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def deep_research(
        ctx: Context,
        question: str,
        num_subquestions: int = 4,
        model: str = "sonar-pro",
        max_results_per_subquestion: int = 5,
        use_model_decomposition: bool = False,
    ) -> dict[str, Any]:
        """Run a multi-step research pipeline returning a validated, cited report."""
        args = DeepResearchInput(
            question=question,
            num_subquestions=num_subquestions,
            model=SonarModel(model),
            max_results_per_subquestion=max_results_per_subquestion,
            use_model_decomposition=use_model_decomposition,
        )
        call_id = _guard(
            "deep_research",
            {"question": args.question, "num_subquestions": args.num_subquestions},
        )
        started = time.monotonic()
        result = await _deep_research(
            _client(ctx),
            args.question,
            num_subquestions=args.num_subquestions,
            model=args.model.value,
            max_results_per_subquestion=args.max_results_per_subquestion,
            use_model_decomposition=args.use_model_decomposition,
        )
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="deep_research",
            validation_passed=result["validation_report"]["passed"],
            truncated=_truncated(bounded),
            **_result_fields("deep_research", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def retrieve(key: str) -> dict[str, Any]:
        """Fetch the full value previously offloaded behind a ``retrieve_key``.

        When a tool result is over the output budget it's bounded and the full
        value is stashed; pass the ``retrieve_key`` here to get the original back.
        """
        args = RetrieveInput(key=key)
        call_id = _guard("retrieve", {"key": args.key})
        started = time.monotonic()
        payload = store.retrieve(args.key)
        result: dict[str, Any] = (
            {"error": f"no offloaded value for key {args.key!r} (expired or unknown)"}
            if payload is None
            else {"key": args.key, "content": payload}
        )
        audit.record(
            "tool_result", tool="retrieve", **_result_fields("retrieve", call_id, started, result)
        )
        return result

    @mcp.tool()
    async def server_metrics() -> dict[str, Any]:
        """Report request/latency/rate-limit counters for this server process."""
        call_id = _guard("server_metrics", {})
        started = time.monotonic()
        result = metrics.snapshot()
        audit.record(
            "tool_result",
            tool="server_metrics",
            **_result_fields("server_metrics", call_id, started, result),
        )
        return result

    return mcp, settings

"""FastMCP server exposing the Perplexity tools.

Tools include Search/Sonar, the Responses-compatible Agent API, specialized
finance/people/URL search, deep research, retrieval, and metrics.
Every invocation runs through the same control path: validate input against a
strict schema, apply the token-bucket rate limit, call the API, **bound the
result to the output budget** (offloading an oversized value behind a
``retrieve_key`` the ``retrieve`` tool can fetch), then emit a redacted audit
record (NSA: validate parameters, DoS guard, logging; context-flood guard).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast

from mcp.server.fastmcp import Context, FastMCP

from .client import FunctionHandler, PerplexityClient, PerplexityError
from .config import Settings, load_settings
from .efficiency import OffloadStore, bound_result
from .fetch import PageFetcher
from .memory import Store
from .metrics import MetricsCollector
from .research import deep_research as _deep_research
from .schemas import (
    DeepResearchInput,
    FetchUrlInput,
    FinanceSearchInput,
    FunctionTool,
    PeopleSearchInput,
    RegisteredFunctionInput,
    ResponseCreateInput,
    ResponseRetrieveInput,
    RetrieveInput,
    SearchInput,
    SonarAskInput,
    SonarModel,
)
from .security import AuditLogger, RateLimitError, TokenBucket, content_hash


def build_server(
    settings: Settings | None = None,
    *,
    function_registry: Mapping[str, FunctionHandler] | None = None,
) -> tuple[FastMCP, Settings]:
    """Construct the FastMCP server and return it alongside the loaded settings."""
    settings = settings or load_settings()
    audit = AuditLogger(settings.audit_log_path)
    bucket = TokenBucket(settings.rate_per_minute, settings.rate_burst)
    # One offload store for the server's lifetime: when a result is over budget,
    # it's stashed here and the agent gets a retrieve_key to fetch it on demand.
    offload_store = OffloadStore()
    metrics = MetricsCollector()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        client = PerplexityClient(settings)
        fetcher = PageFetcher(settings)
        response_store = Store.from_settings(settings)
        try:
            yield {"client": client, "fetcher": fetcher, "response_store": response_store}
        finally:
            response_store.close()
            await fetcher.aclose()
            await client.aclose()

    mcp = FastMCP(
        "perplexity-agent",
        instructions=(
            "Tools for web research via the Perplexity API. Use perplexity_search "
            "for raw ranked results, sonar_ask for a grounded answer, and "
            "deep_research for a multi-step, validated, cited report. Use "
            "responses_create for the Responses-compatible Agent API."
        ),
        lifespan=lifespan,
        host=settings.http_host,
        port=settings.http_port,
    )

    def _client(ctx: Context) -> PerplexityClient:
        client: PerplexityClient = ctx.request_context.lifespan_context["client"]
        return client

    def _fetcher(ctx: Context) -> PageFetcher:
        fetcher: PageFetcher = ctx.request_context.lifespan_context["fetcher"]
        return fetcher

    def _response_store(ctx: Context) -> Store:
        response_store: Store = ctx.request_context.lifespan_context["response_store"]
        return response_store

    def _session_id(ctx: Context) -> str:
        # The transport session object is unique per connection. ``client_id`` is
        # client-declared metadata and can be reused or spoofed across sessions.
        return f"session:{id(ctx.session)}"

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
            bound_result(result, max_chars=settings.max_tool_output_chars, store=offload_store),
        )

    def _truncated(bounded: Any) -> bool:
        return isinstance(bounded, dict) and bool(bounded.get("truncated"))

    @mcp.tool()
    async def perplexity_search(
        ctx: Context,
        query: str,
        max_results: int = 5,
        max_tokens_per_page: int = 1024,
        search_domain_filter: list[str] | None = None,
        search_language_filter: list[str] | None = None,
        search_recency_filter: str | None = None,
        search_after_date_filter: str | None = None,
        search_before_date_filter: str | None = None,
        last_updated_after_filter: str | None = None,
        last_updated_before_filter: str | None = None,
    ) -> dict[str, Any]:
        """Search the web via Perplexity's Search API and return ranked results."""
        args = SearchInput.model_validate(
            {
                "query": query,
                "max_results": max_results,
                "max_tokens_per_page": max_tokens_per_page,
                "search_domain_filter": search_domain_filter,
                "search_language_filter": search_language_filter,
                "search_recency_filter": search_recency_filter,
                "search_after_date_filter": search_after_date_filter,
                "search_before_date_filter": search_before_date_filter,
                "last_updated_after_filter": last_updated_after_filter,
                "last_updated_before_filter": last_updated_before_filter,
            }
        )
        call_id = _guard(
            "perplexity_search", {"query": args.query, "max_results": args.max_results}
        )
        started = time.monotonic()
        result = await _client(ctx).search(
            args.query,
            args.max_results,
            args.max_tokens_per_page,
            **args.model_dump(
                include={
                    "search_domain_filter",
                    "search_language_filter",
                    "search_recency_filter",
                    "search_after_date_filter",
                    "search_before_date_filter",
                    "last_updated_after_filter",
                    "last_updated_before_filter",
                },
                exclude_none=True,
            ),
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
    async def responses_create(
        ctx: Context,
        input: str | list[dict[str, Any]],
        model: str | None = None,
        models: list[str] | None = None,
        preset: str | None = None,
        instructions: str | None = None,
        language_preference: str | None = None,
        reasoning: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        store: bool | None = None,
        max_output_tokens: int | None = None,
        max_steps: int | None = None,
        stream: bool = False,
        auto_execute_functions: bool = False,
        max_function_rounds: int = 8,
    ) -> dict[str, Any]:
        """Create an Agent API response, including multimodal and function-call inputs.

        With ``stream=true``, SSE events are parsed and returned as a bounded event
        list because MCP tool calls themselves are request/response operations.
        """
        args = ResponseCreateInput.model_validate(
            {
                "input": input,
                "model": model,
                "models": models,
                "preset": preset,
                "instructions": instructions,
                "language_preference": language_preference,
                "reasoning": reasoning,
                "response_format": response_format,
                "tools": tools,
                "previous_response_id": previous_response_id,
                "store": store,
                "max_output_tokens": max_output_tokens,
                "max_steps": max_steps,
                "stream": stream,
            }
        )
        payload = args.model_dump(mode="json", exclude_none=True)
        session_id = _session_id(ctx)
        if args.previous_response_id:
            owner = _response_store(ctx).response_owner(args.previous_response_id)
            if owner is not None and owner != session_id:
                raise PerplexityError("previous_response_id belongs to another MCP session")
        if not 1 <= max_function_rounds <= 10:
            raise ValueError("max_function_rounds must be between 1 and 10")
        call_id = _guard(
            "responses_create",
            {
                "model": args.model,
                "preset": args.preset,
                "stream": args.stream,
                "previous_response_id": args.previous_response_id,
            },
        )
        started = time.monotonic()
        result: dict[str, Any]
        if args.stream and auto_execute_functions:
            raise ValueError("auto_execute_functions cannot be combined with stream")
        if auto_execute_functions:
            if not function_registry:
                raise PerplexityError("No function handlers are registered")
            requested = {tool.name for tool in (args.tools or []) if isinstance(tool, FunctionTool)}
            unavailable = requested - function_registry.keys()
            if unavailable:
                raise PerplexityError(
                    f"Unregistered function handlers requested: {sorted(unavailable)}"
                )
            result = await _client(ctx).run_response_with_tools(
                payload, function_registry, max_rounds=max_function_rounds
            )
        elif args.stream:
            events = [event async for event in _client(ctx).stream_response(payload)]
            completed = next(
                (
                    event.get("response")
                    for event in reversed(events)
                    if event.get("type") == "response.completed"
                ),
                None,
            )
            result = {"object": "response.stream", "events": events}
            if isinstance(completed, dict):
                result["response"] = completed
        else:
            result = await _client(ctx).create_response(payload)
        persisted = result.get("response") if result.get("object") == "response.stream" else result
        if args.store is not False and isinstance(persisted, dict) and persisted.get("id"):
            _response_store(ctx).save_response(
                str(persisted["id"]), persisted, session_id=session_id, now=time.time()
            )
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="responses_create",
            truncated=_truncated(bounded),
            **_result_fields("responses_create", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def responses_retrieve(ctx: Context, response_id: str) -> dict[str, Any]:
        """Retrieve a stored Agent API response snapshot by its ``resp_`` ID."""
        args = ResponseRetrieveInput(response_id=response_id)
        session_id = _session_id(ctx)
        response_store = _response_store(ctx)
        cached = response_store.response(args.response_id, session_id=session_id)
        owner = response_store.response_owner(args.response_id)
        if cached is None and owner is not None and owner != session_id:
            raise PerplexityError("response_id belongs to another MCP session")
        call_id = _guard("responses_retrieve", {"response_id": args.response_id})
        started = time.monotonic()
        result = cached or await _client(ctx).retrieve_response(args.response_id)
        if cached is None:
            response_store.save_response(
                args.response_id, result, session_id=session_id, now=time.time()
            )
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="responses_retrieve",
            truncated=_truncated(bounded),
            **_result_fields("responses_retrieve", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def finance_search(
        ctx: Context,
        query: str,
        categories: list[str] | None = None,
        tickers: list[str] | None = None,
        model: str = "perplexity/sonar",
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Answer a finance query using the Agent API's structured finance tool."""
        args = FinanceSearchInput(
            query=query,
            categories=categories,
            tickers=tickers,
            model=model,
            max_output_tokens=max_output_tokens,
        )
        # Validate the equivalent Agent request too, including Anthropic's
        # required max_output_tokens rule, before making the convenience call.
        validated = ResponseCreateInput.model_validate(
            {
                "input": args.query,
                "model": args.model,
                "tools": [{"type": "finance_search"}],
                "max_output_tokens": args.max_output_tokens,
            }
        )
        call_id = _guard("finance_search", {"query": args.query, "model": args.model})
        started = time.monotonic()
        result = await _client(ctx).search_finance(
            args.query,
            categories=args.categories,
            tickers=args.tickers,
            model=validated.model or args.model,
            max_output_tokens=validated.max_output_tokens,
        )
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="finance_search",
            truncated=_truncated(bounded),
            **_result_fields("finance_search", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def people_search(
        ctx: Context,
        query: str,
        max_results: int = 5,
        max_tokens_per_page: int = 1024,
    ) -> dict[str, Any]:
        """Search Perplexity's people index for structured professional profiles."""
        args = PeopleSearchInput(
            query=query,
            max_results=max_results,
            max_tokens_per_page=max_tokens_per_page,
        )
        call_id = _guard("people_search", {"query": args.query})
        started = time.monotonic()
        result = await _client(ctx).people_search(
            args.query, args.max_results, args.max_tokens_per_page
        )
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="people_search",
            truncated=_truncated(bounded),
            **_result_fields("people_search", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def fetch_url(
        ctx: Context,
        url: str | None = None,
        urls: list[str] | None = None,
        max_urls: int = 1,
    ) -> dict[str, Any]:
        """Fetch a bounded set of public URLs through the hardened page fetcher."""
        combined = ([url] if url else []) + (urls or [])
        args = FetchUrlInput.model_validate({"urls": combined, "max_urls": max_urls})
        call_id = _guard("fetch_url", {"urls": [str(item) for item in args.urls]})
        started = time.monotonic()
        pages = await asyncio.gather(*(_fetcher(ctx).fetch(str(item)) for item in args.urls))
        result = {
            "contents": [
                {
                    "url": page.final_url,
                    "title": page.title,
                    "snippet": page.text,
                    "injection_flags": page.injection_flags,
                }
                for page in pages
            ]
        }
        bounded = _bound(result)
        audit.record(
            "tool_result",
            tool="fetch_url",
            truncated=_truncated(bounded),
            **_result_fields("fetch_url", call_id, started, result),
        )
        return bounded

    @mcp.tool()
    async def sonar_ask(
        ctx: Context,
        question: str,
        model: str = "sonar",
        system_prompt: str | None = None,
        reasoning: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Ask Sonar or an Agent API provider model for a grounded answer."""
        args = SonarAskInput.model_validate(
            {
                "question": question,
                "model": model,
                "system_prompt": system_prompt,
                "reasoning": reasoning,
                "response_format": response_format,
                "max_output_tokens": max_output_tokens,
            }
        )
        call_id = _guard("sonar_ask", {"question": args.question, "model": args.model})
        started = time.monotonic()
        use_agent = "/" in args.model or args.reasoning is not None
        if use_agent:
            agent_model = args.model if "/" in args.model else f"perplexity/{args.model}"
            payload = ResponseCreateInput(
                input=args.question,
                model=agent_model,
                instructions=args.system_prompt,
                reasoning=args.reasoning,
                response_format=args.response_format,
                max_output_tokens=args.max_output_tokens,
            ).model_dump(mode="json", exclude_none=True)
            result = await _client(ctx).create_response(payload)
        else:
            messages: list[dict[str, str]] = []
            if args.system_prompt:
                messages.append({"role": "system", "content": args.system_prompt})
            messages.append({"role": "user", "content": args.question})
            legacy_format = (
                args.response_format.model_dump(mode="json", exclude_none=True)
                if args.response_format
                else None
            )
            result = await _client(ctx).chat(
                messages, model=args.model, response_format=legacy_format
            )
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
        payload = offload_store.retrieve(args.key)
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

    if function_registry:

        @mcp.tool(name="registered_function_call")
        async def registered_function_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            """Invoke one server-operator-registered, allowlisted Python function."""
            args = RegisteredFunctionInput(name=name, arguments=arguments)
            handler = function_registry.get(args.name)
            if handler is None:
                raise PerplexityError(f"Function {args.name!r} is not registered")
            call_id = _guard("registered_function_call", {"name": args.name})
            started = time.monotonic()
            value = handler(args.arguments)
            if inspect.isawaitable(value):
                value = await value
            safe_value = json.loads(json.dumps(value, default=str))
            result = {"name": args.name, "output": safe_value}
            bounded = _bound(result)
            audit.record(
                "tool_result",
                tool="registered_function_call",
                truncated=_truncated(bounded),
                **_result_fields("registered_function_call", call_id, started, result),
            )
            return bounded

    return mcp, settings

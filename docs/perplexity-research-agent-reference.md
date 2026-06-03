# Perplexity Research Agent Reference Architecture

This document outlines a practical reference architecture for building a research agent on top of the Perplexity API. It focuses on a retrieval-first workflow, structured outputs, citation handling, validation, and implementation patterns suitable for local or self-managed AI stacks.[cite:10][cite:16][cite:22][cite:37]

## Recommended architecture

A robust implementation separates retrieval from synthesis. Perplexity’s Search API is designed for ranked web results, while Sonar provides grounded answer generation through an OpenAI-compatible chat completions interface.[cite:10][cite:16][cite:37]

Recommended components:

- API gateway: a local FastAPI or Express service that stores the `PERPLEXITY_API_KEY` server-side and exposes an internal `/research` endpoint.[cite:6][cite:30]
- Retrieval lane: Perplexity Search API for focused sub-question evidence gathering and source collection.[cite:10]
- Synthesis lane: Sonar or Sonar Pro for grounded answer generation and structured final reports.[cite:16][cite:17]
- Cache: Redis or SQLite keyed by normalized query plus freshness window to reduce repeated search calls.[cite:17]
- Validation layer: a post-processing step that checks claims against citation fields and retrieval results before the response is returned.[cite:22]

## Request flow

A dependable research workflow starts by decomposing the user’s question into narrower sub-questions, then runs targeted searches for each one before synthesizing across the collected evidence.[cite:10][cite:17] This pattern improves transparency and makes debugging easier because retrieval errors and synthesis errors are isolated from one another.[cite:10][cite:16]

Suggested flow:

1. Decompose the question into 3 to 8 focused sub-questions based on topic, timeframe, and evidence needs.[cite:17]
2. Query Search API for each sub-question and collect ranked results with snippets and URLs.[cite:10]
3. Deduplicate by canonical URL and cluster sources by theme or claim area.[cite:10]
4. Pass a compact evidence summary into Sonar or Sonar Pro for structured synthesis.[cite:16][cite:22]
5. Validate the structured output and return the answer, evidence, unresolved gaps, and confidence notes.[cite:22]

## Implementation example

Perplexity documents Search as a dedicated API and Sonar as an OpenAI-compatible chat completions API, which makes it straightforward to combine direct search calls with structured synthesis in the same backend service.[cite:10][cite:30][cite:37]

```python
import os
import json
import hashlib
from typing import List, Dict, Any
import httpx
from fastapi import FastAPI

app = FastAPI()

PPLX_API_KEY = os.environ["PERPLEXITY_API_KEY"]
PPLX_BASE = "https://api.perplexity.ai"

client = httpx.AsyncClient(timeout=60.0, headers={
    "Authorization": f"Bearer {PPLX_API_KEY}",
    "Content-Type": "application/json"
})

def cache_key(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()

async def perplexity_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    payload = {
        "query": query,
        "max_results": max_results,
        "max_tokens_per_page": 1024
    }
    r = await client.post(f"{PPLX_BASE}/search", json=payload)
    r.raise_for_status()
    return r.json()

async def sonar_synthesize(messages: List[Dict[str, str]], schema: dict) -> Dict[str, Any]:
    payload = {
        "model": "sonar-pro",
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "research_report",
                "schema": schema
            }
        }
    }
    r = await client.post(f"{PPLX_BASE}/chat/completions", json=payload)
    r.raise_for_status()
    return r.json()

def dedupe_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in results:
        url = (item.get("url") or "").split("#")[0].rstrip("/")
        key = url.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out

def build_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "key_findings": {
                "type": "array",
                "items": {"type": "string"}
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"}
            },
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "supporting_urls": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["claim", "supporting_urls"]
                }
            }
        },
        "required": ["answer", "key_findings", "open_questions", "claims"]
    }

@app.post("/research")
async def research(question: str):
    subquestions = [
        question,
        f"{question} background",
        f"{question} latest developments",
        f"{question} expert analysis"
    ]

    gathered = []
    for q in subquestions:
        resp = await perplexity_search(q, max_results=5)
        gathered.extend(resp.get("results", []))

    sources = dedupe_results(gathered)
    source_summary = [
        {
            "title": s.get("title"),
            "url": s.get("url"),
            "snippet": s.get("snippet")
        }
        for s in sources[:20]
    ]

    schema = build_schema()
    messages = [
        {
            "role": "system",
            "content": (
                "You are a research synthesis agent. "
                "Use the provided sources only. "
                "Return valid JSON matching the schema. "
                "List unresolved gaps when evidence is weak."
            )
        },
        {
            "role": "user",
            "content": json.dumps({
                "question": question,
                "sources": source_summary
            })
        }
    ]

    synthesis = await sonar_synthesize(messages, schema)

    return {
        "question": question,
        "sources": source_summary,
        "synthesis": synthesis
    }
```

## Best practices

A retrieval-first design is generally more reliable than asking a single model call to do planning, search, evidence sorting, and final writing all at once.[cite:10][cite:16] Keeping retrieval and synthesis separate also improves caching, reproducibility, and post-hoc auditing of the final answer.[cite:10][cite:22]

Recommended practices:

- Use JSON Schema for machine-consumable outputs, because Perplexity explicitly supports structured outputs through `response_format`.[cite:22]
- Read citations or search result fields from API metadata instead of trusting the model to invent valid links inside JSON text.[cite:22]
- Use concise prompts with explicit instructions for scope, evidence use, and uncertainty reporting.[cite:6][cite:22]
- Add retries with jitter for transient network failures or rate limits, but avoid masking application logic bugs with automatic retries.[cite:17]
- Persist raw retrieval payloads before synthesis so answers can be regenerated or audited later without repeating the search stage.[cite:10][cite:22]

## Validation and trustworthiness

A research agent should treat citations as first-class output fields rather than cosmetic extras. Perplexity’s output-control guidance explicitly notes that citations and search result fields from the API response are the reliable source of links, not free-text model output.[cite:22]

A practical validation layer should enforce three checks:

- Every major claim in the final output has at least one supporting URL attached.[cite:22]
- Every supporting URL exists in retrieval results or citation metadata returned by the API.[cite:10][cite:22]
- Weak or duplicate evidence is marked as uncertain rather than presented as settled fact.[cite:10]

## Deployment notes

Perplexity’s quickstart materials describe OpenAI SDK compatibility for Sonar and standard authenticated HTTPS access for the broader API platform, which makes local integration straightforward through a small backend wrapper.[cite:30][cite:37] The cleanest deployment pattern is to keep Perplexity keys on the server, expose only internal endpoints to local apps, and put caching plus validation in the same control plane.[cite:6][cite:17]

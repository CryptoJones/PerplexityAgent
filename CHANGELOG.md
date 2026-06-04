# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- The HTTP transport's bearer-token check is now constant-time
  (`hmac.compare_digest` over raw header bytes). A plain `!=` short-circuits on
  the first differing byte, which leaks the token's length and prefix through
  response timing. Comparing bytes also means a malformed (non-ASCII)
  `Authorization` header still returns a clean `401` rather than erroring.

### Changed

- CI now type-checks the package: `mypy` runs under `strict = true` (GitHub
  Actions). The one concession is a per-module relaxation of
  `disallow_any_generics` for `server.py`, because FastMCP only injects a tool's
  `Context` when it is annotated as the bare `Context` — parameterizing it to
  satisfy strict mode would silently break injection.

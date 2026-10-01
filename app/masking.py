"""
Secret / PII masking.

GUARDRAIL: cluster telemetry (logs, env, events) frequently contains
credentials, tokens, connection strings, emails and IPs. This module scrubs
that content BEFORE it is ever sent to the external LLM. Masking is applied
to every string field of the evidence bundle in agent.collect_evidence().

The philosophy is "redact aggressively, keep the shape". We replace the
sensitive value with a typed placeholder (e.g. ***REDACTED_JWT***) so the
LLM still understands "a token was present here" without seeing the secret.
"""
from __future__ import annotations

import re
from typing import Any

# Each rule: (compiled_regex, replacement). Order matters — more specific
# patterns first so they win before broad ones.
_RULES: list[tuple[re.Pattern[str], str]] = [
    # JWTs: header.payload.signature
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        "***REDACTED_JWT***",
    ),
    # AWS access key IDs
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "***REDACTED_AWS_KEY_ID***"),
    # Anthropic / OpenAI style API keys
    (re.compile(r"\bsk-[A-Za-z0-9-]{16,}\b"), "***REDACTED_API_KEY***"),
    # GitHub tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "***REDACTED_GITHUB_TOKEN***"),
    # Slack tokens / webhooks
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "***REDACTED_SLACK_TOKEN***"),
    (
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
        "***REDACTED_SLACK_WEBHOOK***",
    ),
    # Private key blocks
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "***REDACTED_PRIVATE_KEY***",
    ),
    # Connection strings with embedded credentials: scheme://user:pass@host
    (
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s:@/]+:[^\s:@/]+@"),
        r"\1***REDACTED_CREDS***@",
    ),
    # key=value / key: value secrets (password, token, secret, apikey ...)
    # The optional [A-Za-z0-9_]* prefix lets us catch compound identifiers such
    # as DB_PASSWORD or MYSQL_ROOT_PASSWORD, where a plain \b would not fire
    # because '_' is itself a word character (no boundary before the keyword).
    (
        re.compile(
            r"(?i)([A-Za-z0-9_]*(?:pass(?:word)?|passwd|pwd|secret|token|"
            r"api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|auth))"
            r"(\"?\s*[:=]\s*)(\"?)([^\s\"'&,;]+)(\"?)"
        ),
        r"\1\2\3***REDACTED***\5",
    ),
    # Email addresses (PII)
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "***REDACTED_EMAIL***",
    ),
    # Bearer tokens
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer ***REDACTED***"),
    # IPv4 addresses (avoid leaking internal topology). Keep loopback/zeros.
    (
        re.compile(r"\b(?!0\.0\.0\.0\b)(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
        "***REDACTED_IP***",
    ),
]


def mask_text(text: str) -> str:
    """Return `text` with all known secret/PII patterns redacted."""
    if not text:
        return text
    masked = text
    for pattern, replacement in _RULES:
        masked = pattern.sub(replacement, masked)
    return masked


def mask_obj(obj: Any) -> Any:
    """
    Recursively mask every string inside a dict/list structure.

    Used to scrub the entire evidence bundle before it leaves the cluster.
    """
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(v) for v in obj]
    return obj


def mask_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Convenience wrapper — mask a full evidence dict."""
    return mask_obj(evidence)

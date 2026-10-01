"""Tests for the pre-LLM secret / PII masking guardrail."""
import pytest

from app import masking


@pytest.mark.parametrize(
    "raw, secret",
    [
        ("ANTHROPIC_API_KEY=sk-ant-abcd1234efgh5678ijkl", "sk-ant-abcd1234"),
        ("aws_access_key_id=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("postgres://admin:hunter2@db.internal:5432/app", "hunter2"),
        ("DB_PASSWORD=SuperSecret123!", "SuperSecret123"),
        ("MYSQL_ROOT_PASSWORD: hunter2", "hunter2"),
        ('{"password":"p@ss","host":"db"}', "p@ss"),
        ('"token": "tok_abcdef123"', "tok_abcdef123"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("contact john.doe@example.com", "john.doe@example.com"),
        ("connecting to 172.31.44.9:5432", "172.31.44.9"),
        ("https://hooks.slack.com/services/T00/B00/xyz", "T00/B00/xyz"),
        (
            "token eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4",
            "eyJhbGciOi.eyJzdWIiOiIx",
        ),
    ],
)
def test_secrets_are_redacted(raw, secret):
    assert secret not in masking.mask_text(raw)


def test_redaction_keeps_shape():
    out = masking.mask_text("DB_PASSWORD=SuperSecret123!")
    assert "***REDACTED***" in out
    assert out.startswith("DB_PASSWORD")


@pytest.mark.parametrize(
    "safe",
    [
        "kubectl -n devops-lab describe pod crashloop-app",
        "container restarted 7 times",
        "0.0.0.0",
        "db-primary",
    ],
)
def test_safe_values_preserved(safe):
    assert safe in masking.mask_text(f"log line: {safe} end")


def test_nested_structure_is_masked_recursively():
    evidence = {
        "logs": {"web": "PASSWORD=abc123 mail x@y.com"},
        "events": [{"message": "used key AKIAIOSFODNN7EXAMPLE"}],
    }
    blob = str(masking.mask_evidence(evidence))
    for leaked in ("abc123", "x@y.com", "AKIAIOSFODNN7EXAMPLE"):
        assert leaked not in blob

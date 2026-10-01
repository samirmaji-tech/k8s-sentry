"""Tests for LLM response parsing and the deterministic fallback."""
from app import llm_analyzer
from app.llm_analyzer import Analysis, analyze

_VALID = (
    '{"root_cause":"x","severity":"HIGH",'
    '"remediation_steps":["a"],"prevention_tip":"p"}'
)


def test_extract_plain_json():
    assert llm_analyzer._extract_json(_VALID)["severity"] == "HIGH"


def test_extract_from_code_fence():
    text = "Here you go:\n```json\n" + _VALID + "\n```\nDone."
    assert llm_analyzer._extract_json(text)["root_cause"] == "x"


def test_extract_from_prose_wrapped():
    text = "Sure!\n" + _VALID + "\nHope that helps."
    assert llm_analyzer._extract_json(text)["prevention_tip"] == "p"


def test_extract_returns_none_on_garbage():
    assert llm_analyzer._extract_json("no json here at all") is None


def test_severity_normalisation_defaults_to_medium():
    a = Analysis(root_cause="rc", severity="banana", remediation_steps=[]).normalised()
    assert a.severity == "MEDIUM"
    assert len(a.remediation_steps) >= 1  # empty steps get backfilled


def test_fallback_maps_crashloopbackoff():
    ev = {
        "namespace": "devops-lab",
        "pod": "crashloop-app",
        "description": {"containers": [{"current_state": {"reason": "CrashLoopBackOff"}}]},
    }
    a = llm_analyzer._fallback_analysis(ev, "test")
    assert a.severity == "HIGH"
    assert "devops-lab" in a.remediation_steps[0]


def test_fallback_maps_imagepullbackoff():
    ev = {
        "namespace": "devops-lab",
        "pod": "imagepull-app",
        "description": {"containers": [{"current_state": {"reason": "ImagePullBackOff"}}]},
    }
    assert llm_analyzer._fallback_analysis(ev, "test").severity == "HIGH"


def test_analyze_without_api_key_uses_fallback():
    # conftest sets ANTHROPIC_API_KEY="" so this must not make a network call.
    ev = {
        "namespace": "devops-lab",
        "pod": "crashloop-app",
        "description": {"containers": [{"current_state": {"reason": "CrashLoopBackOff"}}]},
    }
    result = analyze(ev)
    assert result.severity in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
    assert result.root_cause
    assert result.model_dump()["remediation_steps"]

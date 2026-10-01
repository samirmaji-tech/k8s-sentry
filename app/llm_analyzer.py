"""
llm_analyzer.py — root-cause analysis via the Anthropic API.

Takes a MASKED evidence bundle and asks Claude to act as a senior SRE. The
system prompt is strict: the model must return ONLY a JSON object with a
fixed schema. We defensively parse the response (handling stray prose or code
fences) and validate it with Pydantic so downstream code always receives a
well-formed Analysis.

SAFETY: the prompt forbids destructive remediation. Combined with the agent's
read-only RBAC, remediation steps are advisory kubectl commands for a human to
review — the agent never executes them.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .config import get_settings

logger = logging.getLogger("k8s-sentry.llm")

# Valid severities the model is allowed to emit.
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}

SYSTEM_PROMPT = """\
You are K8s-Sentry, a senior Site Reliability Engineer acting as an autonomous \
incident commander for a Kubernetes cluster. You receive structured, \
pre-sanitised telemetry (pod description, events, and tailed logs) for a single \
failing pod. Secrets and PII have already been redacted and appear as \
***REDACTED_*** placeholders — treat those as "a secret was present" and never \
ask for the real value.

Your job: determine the single most likely root cause and propose SAFE, \
READ-FIRST remediation.

STRICT OUTPUT CONTRACT:
- Respond with a SINGLE valid JSON object and NOTHING else. No prose, no \
markdown, no code fences.
- The JSON MUST have exactly these keys:
  {
    "root_cause": "<one concise sentence naming the actual cause>",
    "severity": "<one of: CRITICAL, HIGH, MEDIUM, LOW, INFO>",
    "remediation_steps": ["<safe kubectl/diagnostic command or action>", ...],
    "prevention_tip": "<one sentence on how to stop this recurring>"
  }

REMEDIATION RULES:
- Prefer read-only, diagnostic commands first (kubectl describe/logs/get/events).
- Any change command (edit/set/apply/rollout/scale/delete) must be phrased as a \
SUGGESTION for a human to review, and you must NEVER instruct deletion of \
namespaces, PersistentVolumes, or cluster-scoped resources.
- Do not fabricate values you cannot see; reference the redacted placeholder \
instead (e.g. "set the DB_PASSWORD secret, currently ***REDACTED***").
- Keep remediation_steps to 3-6 items, each a single actionable line.

Base your analysis strictly on the provided evidence. If evidence is \
insufficient, say so in root_cause and set severity to INFO."""


class Analysis(BaseModel):
    root_cause: str
    severity: str = Field(default="MEDIUM")
    remediation_steps: list[str] = Field(default_factory=list)
    prevention_tip: str = ""

    def normalised(self) -> Analysis:
        sev = (self.severity or "MEDIUM").strip().upper()
        if sev not in _SEVERITIES:
            sev = "MEDIUM"
        self.severity = sev
        # Guarantee at least one remediation line.
        if not self.remediation_steps:
            self.remediation_steps = [
                "kubectl -n <namespace> describe pod <pod> to inspect events."
            ]
        return self


def _extract_json(text: str) -> dict[str, Any] | None:
    """
    Pull the first JSON object out of the model text. Handles the model
    occasionally wrapping output in ```json fences or adding a stray sentence.
    """
    if not text:
        return None

    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None

    if candidate is None:
        # Fall back to the outermost {...} span.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]

    if candidate is None:
        return None

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _fallback_analysis(evidence: dict[str, Any], note: str) -> Analysis:
    """Deterministic analysis used when the LLM is unavailable or unparsable."""
    reason = ""
    for c in evidence.get("description", {}).get("containers", []):
        st = c.get("current_state", {})
        if st.get("reason"):
            reason = st["reason"]
            break

    mapping = {
        "CrashLoopBackOff": (
            "The container repeatedly starts and exits with a non-zero code "
            "(application-level startup failure).",
            "HIGH",
            [
                "kubectl -n {ns} logs {pod} --previous --tail=50",
                "kubectl -n {ns} describe pod {pod}",
                "Fix the failing startup condition (missing config/env/dependency), "
                "then let the pod restart.",
            ],
            "Add a startup/readiness probe and validate required env vars at boot.",
        ),
        "ImagePullBackOff": (
            "The kubelet cannot pull the specified container image (bad tag, "
            "missing image, or registry auth).",
            "HIGH",
            [
                "kubectl -n {ns} describe pod {pod} | grep -A5 Events",
                "Verify the image name/tag exists in the registry.",
                "If private, confirm the imagePullSecret is attached to the pod.",
            ],
            "Pin images by digest and validate tags in CI before deploy.",
        ),
    }
    ns = evidence.get("namespace", "<namespace>")
    pod = evidence.get("pod", "<pod>")
    if reason in mapping:
        rc, sev, steps, tip = mapping[reason]
        steps = [s.format(ns=ns, pod=pod) for s in steps]
        return Analysis(
            root_cause=f"{rc} (heuristic — {note})",
            severity=sev,
            remediation_steps=steps,
            prevention_tip=tip,
        ).normalised()

    return Analysis(
        root_cause=f"Unable to determine root cause automatically ({note}).",
        severity="INFO",
        remediation_steps=[
            f"kubectl -n {ns} describe pod {pod}",
            f"kubectl -n {ns} logs {pod} --tail=50",
        ],
        prevention_tip="Enable log/metric collection to speed future diagnosis.",
    ).normalised()


def analyze(evidence: dict[str, Any]) -> Analysis:
    """
    Send masked evidence to Claude and return a validated Analysis.

    Falls back to a deterministic heuristic if the API key is missing, the
    call fails, or the response cannot be parsed — so the agent always returns
    a usable result.
    """
    settings = get_settings()

    if not settings.llm_enabled:
        logger.warning("ANTHROPIC_API_KEY not set — using heuristic fallback.")
        return _fallback_analysis(evidence, "LLM disabled")

    # Imported here so the module loads even if the SDK is absent at lint time.
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        logger.error("anthropic SDK not installed — using heuristic fallback.")
        return _fallback_analysis(evidence, "SDK missing")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user_content = (
        "Analyse the following Kubernetes pod telemetry and respond with the "
        "JSON object described in your instructions.\n\n"
        "```json\n"
        f"{json.dumps(evidence, indent=2, default=str)}\n"
        "```"
    )

    try:
        message = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.llm_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # noqa: BLE001 — API/network failures must not crash the agent
        logger.error("Anthropic API call failed: %s", exc)
        return _fallback_analysis(evidence, "API error")

    raw_text = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    data = _extract_json(raw_text)
    if data is None:
        logger.error("LLM response was not valid JSON; raw=%r", raw_text[:500])
        return _fallback_analysis(evidence, "unparsable LLM response")

    try:
        return Analysis(**data).normalised()
    except ValidationError as exc:
        logger.error("LLM JSON failed schema validation: %s", exc)
        return _fallback_analysis(evidence, "schema validation failed")

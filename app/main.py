"""
main.py — FastAPI webhook receiver and incident orchestrator.

Flow:
    alert (SNS / CloudWatch / manual) --> /webhook
        1. parse the alert, resolve target namespace + optional pod
        2. agent.collect_evidence()          [read-only cluster access]
        3. masking.mask_evidence()           [strip secrets/PII]
        4. llm_analyzer.analyze()            [root cause + remediation JSON]
        5. notifier.notify()                 [Slack summary]
        6. return the structured analysis over HTTP

Endpoints:
    GET  /healthz         liveness/readiness probe
    GET  /                service metadata
    POST /webhook         SNS / CloudWatch alert intake
    POST /analyze         manual trigger: {"namespace": "...", "pod": "..."}
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__, agent, masking, notifier
from .config import get_settings
from .llm_analyzer import analyze

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("k8s-sentry.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise Kubernetes clients once at startup."""
    try:
        agent.init_k8s()
        logger.info("K8s-Sentry %s ready (namespace=%s, mode=%s)", __version__,
                    settings.target_namespace, settings.agent_mode)
    except Exception as exc:  # noqa: BLE001
        # Don't hard-fail startup — /healthz should still answer so the pod
        # can report readiness while an operator fixes RBAC/kubeconfig.
        logger.error("Kubernetes init failed at startup: %s", exc)
    yield


app = FastAPI(
    title="K8s-Sentry",
    version=__version__,
    description="Autonomous AI-powered Kubernetes incident commander (read-only).",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class AnalyzeRequest(BaseModel):
    namespace: str | None = None
    pod: str | None = None


# --------------------------------------------------------------------------- #
# Alert parsing
# --------------------------------------------------------------------------- #
def _confirm_sns_subscription(subscribe_url: str) -> None:
    """
    SNS sends a SubscriptionConfirmation with a SubscribeURL that must be
    visited once to activate the subscription. We fetch it best-effort.
    """
    try:
        httpx.get(subscribe_url, timeout=10.0)
        logger.info("Confirmed SNS subscription.")
    except httpx.HTTPError as exc:
        logger.error("Failed to confirm SNS subscription: %s", exc)


# Dimension names that map to a Kubernetes namespace / pod, across the various
# alerting systems (Azure Monitor Container Insights, CloudWatch, custom).
_NAMESPACE_KEYS = {"namespace", "pod_namespace", "kubernetes namespace", "kubernetes_namespace"}
_POD_KEYS = {"pod", "podname", "pod_name", "kubernetes pod", "controller_pod", "pod_status"}


def _scan_dimensions(dimensions: Any, namespace: str | None, pod: str | None):
    """Read namespace/pod out of a list of {name, value} dimension dicts."""
    for dim in dimensions or []:
        if not isinstance(dim, dict):
            continue
        name = (dim.get("name") or "").lower()
        value = dim.get("value")
        if name in _NAMESPACE_KEYS and not namespace:
            namespace = value
        if name in _POD_KEYS and not pod:
            pod = value
    return namespace, pod


def _resolve_target(payload: dict[str, Any]) -> dict[str, str | None]:
    """
    Extract {namespace, pod} from a heterogeneous alert payload.

    Supports:
      * Azure Monitor common alert schema:
        {"schemaId": "azureMonitorCommonAlertSchema",
         "data": {"essentials": {...},
                  "alertContext": {"condition": {"allOf": [{"dimensions": [...]}]}}}}
      * SNS envelope: {"Type": "Notification", "Message": "<json string>"}
      * CloudWatch alarm JSON (Trigger.Dimensions with Namespace/PodName)
      * Direct/manual JSON: {"namespace": "...", "pod": "..."}
    Falls back to the configured TARGET_NAMESPACE and pod=None (scan mode).
    """
    default_ns = settings.target_namespace

    # Unwrap an SNS envelope: the real content is a JSON string in "Message".
    if payload.get("Type") == "Notification" and isinstance(payload.get("Message"), str):
        try:
            payload = json.loads(payload["Message"])
        except json.JSONDecodeError:
            # Message was plain text; keep the envelope fields.
            payload = {"raw_message": payload["Message"]}

    namespace = payload.get("namespace") or payload.get("Namespace")
    pod = payload.get("pod") or payload.get("PodName") or payload.get("pod_name")

    # --- Azure Monitor common alert schema ---
    data = payload.get("data")
    if isinstance(data, dict):
        alert_context = data.get("alertContext") or {}
        # Metric alerts: dimensions live under condition.allOf[*].dimensions.
        condition = alert_context.get("condition") or {}
        for clause in condition.get("allOf", []) or []:
            namespace, pod = _scan_dimensions(clause.get("dimensions"), namespace, pod)
        # Some alert types put dimensions directly on alertContext.
        namespace, pod = _scan_dimensions(alert_context.get("dimensions"), namespace, pod)

    # --- CloudWatch alarm shape: Trigger.Dimensions = [{"name", "value"}] ---
    trigger = payload.get("Trigger") or {}
    namespace, pod = _scan_dimensions(trigger.get("Dimensions"), namespace, pod)

    return {"namespace": namespace or default_ns, "pod": pod}


# --------------------------------------------------------------------------- #
# Core orchestration
# --------------------------------------------------------------------------- #
def _process_pod(namespace: str, pod_ref: dict[str, Any]) -> dict[str, Any]:
    """Run the full evidence -> mask -> analyze -> notify pipeline for one pod."""
    pod_name = pod_ref["name"]
    logger.info("Analysing %s/%s (reason=%s)", namespace, pod_name, pod_ref.get("reason"))

    raw_evidence = agent.collect_evidence(namespace, pod_name)
    masked_evidence = masking.mask_evidence(raw_evidence)  # GUARDRAIL: scrub before LLM
    analysis = analyze(masked_evidence)
    delivered = notifier.notify(pod_ref, analysis)

    return {
        "pod": pod_name,
        "namespace": namespace,
        "reason": pod_ref.get("reason"),
        "restart_count": pod_ref.get("restart_count"),
        "analysis": analysis.model_dump(),
        "slack_delivered": delivered,
    }


def _run_incident(namespace: str, pod: str | None) -> dict[str, Any]:
    """
    If `pod` is given, analyse just that pod. Otherwise scan the namespace
    and analyse every unhealthy pod found.
    """
    results: list[dict[str, Any]] = []

    if pod:
        pod_ref = {"name": pod, "namespace": namespace, "reason": "manual-trigger"}
        results.append(_process_pod(namespace, pod_ref))
    else:
        unhealthy = agent.list_unhealthy_pods(namespace)
        if not unhealthy:
            logger.info("No unhealthy pods in %s.", namespace)
        for ref in unhealthy:
            results.append(_process_pod(namespace, ref.as_dict()))

    return {
        "namespace": namespace,
        "analysed": len(results),
        "incidents": results,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "K8s-Sentry",
        "version": __version__,
        "mode": settings.agent_mode,
        "target_namespace": settings.target_namespace,
        "llm_enabled": settings.llm_enabled,
        "endpoints": ["/healthz", "/webhook", "/analyze"],
    }


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    """
    Alert intake. Accepts SNS notifications (including SubscriptionConfirmation),
    CloudWatch alarm JSON, or a direct {namespace, pod} body.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    # SNS subscription handshake.
    if payload.get("Type") == "SubscriptionConfirmation" and payload.get("SubscribeURL"):
        _confirm_sns_subscription(payload["SubscribeURL"])
        return JSONResponse(content={"status": "subscription confirmed"})

    target = _resolve_target(payload)
    namespace = target["namespace"] or settings.target_namespace

    # Safety rail: never inspect namespaces outside the configured scope.
    if namespace != settings.target_namespace:
        logger.warning(
            "Alert targeted namespace %r outside scope %r — coercing to scope.",
            namespace,
            settings.target_namespace,
        )
        namespace = settings.target_namespace

    try:
        result = _run_incident(namespace, target["pod"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Incident processing failed.")
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return JSONResponse(content=result)


@app.post("/analyze")
async def analyze_endpoint(req: AnalyzeRequest) -> JSONResponse:
    """Manual trigger for local testing / on-demand runs."""
    namespace = req.namespace or settings.target_namespace
    if namespace != settings.target_namespace:
        namespace = settings.target_namespace
    try:
        result = _run_incident(namespace, req.pod)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Manual analysis failed.")
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return JSONResponse(content=result)

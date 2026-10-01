"""
agent.py — Kubernetes telemetry collector.

Uses the official kubernetes-client/python to pull the evidence a human SRE
would gather by hand: `kubectl describe pod`, `kubectl get events`, and
`kubectl logs --tail=50`. Everything here is READ-ONLY — the agent only ever
calls get/list on the API server.

The public entry points are:
    * list_unhealthy_pods(namespace)      -> [PodRef, ...]
    * collect_evidence(namespace, pod)    -> dict (raw, unmasked)

`collect_evidence` returns a structured bundle; masking is applied by the
caller (main.py) via app.masking before the bundle reaches the LLM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .config import get_settings

logger = logging.getLogger("k8s-sentry.agent")

# Module-level API client handles, initialised lazily by init_k8s().
_core_v1: client.CoreV1Api | None = None
_apps_v1: client.AppsV1Api | None = None


def init_k8s() -> None:
    """
    Load kube config (in-cluster first, then local kubeconfig) and build the
    API clients. Safe to call multiple times — it is idempotent.
    """
    global _core_v1, _apps_v1
    if _core_v1 is not None and _apps_v1 is not None:
        return

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config.")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig.")

    _core_v1 = client.CoreV1Api()
    _apps_v1 = client.AppsV1Api()


def _core() -> client.CoreV1Api:
    if _core_v1 is None:
        init_k8s()
    assert _core_v1 is not None
    return _core_v1


@dataclass
class PodRef:
    """Lightweight reference to an unhealthy pod plus a first-pass reason."""

    name: str
    namespace: str
    reason: str = ""
    restart_count: int = 0
    phase: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "reason": self.reason,
            "restart_count": self.restart_count,
            "phase": self.phase,
        }


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def _container_waiting_reason(pod: client.V1Pod) -> str:
    """Best-effort single-word status reason from container states."""
    statuses = (pod.status.container_statuses or []) + (
        pod.status.init_container_statuses or []
    )
    for cs in statuses:
        state = cs.state
        if state and state.waiting and state.waiting.reason:
            return state.waiting.reason
        if state and state.terminated and state.terminated.reason:
            return state.terminated.reason
    return pod.status.phase or "Unknown"


def _total_restarts(pod: client.V1Pod) -> int:
    return sum(cs.restart_count for cs in (pod.status.container_statuses or []))


def list_unhealthy_pods(namespace: str) -> list[PodRef]:
    """
    Scan a namespace and return pods that look unhealthy: bad phase, a
    waiting/terminated reason in the configured unhealthy set, or a container
    that is not ready with restarts.
    """
    settings = get_settings()
    unhealthy_reasons = set(settings.unhealthy_reasons)
    refs: list[PodRef] = []

    try:
        pods = _core().list_namespaced_pod(namespace=namespace)
    except ApiException as exc:
        logger.error("Failed to list pods in %s: %s", namespace, exc.reason)
        raise

    for pod in pods.items:
        reason = _container_waiting_reason(pod)
        restarts = _total_restarts(pod)
        phase = pod.status.phase or "Unknown"

        is_unhealthy = (
            reason in unhealthy_reasons
            or phase in {"Failed", "Unknown"}
            or (restarts >= 3 and phase != "Running")
        )
        # A pod stuck Pending with a container waiting reason also counts.
        if phase == "Pending" and reason in unhealthy_reasons:
            is_unhealthy = True

        if is_unhealthy:
            refs.append(
                PodRef(
                    name=pod.metadata.name,
                    namespace=namespace,
                    reason=reason,
                    restart_count=restarts,
                    phase=phase,
                )
            )

    logger.info("Found %d unhealthy pod(s) in %s", len(refs), namespace)
    return refs


def _describe_pod(pod: client.V1Pod) -> dict[str, Any]:
    """A structured, JSON-safe subset of `kubectl describe pod`."""
    meta = pod.metadata
    spec = pod.spec
    status = pod.status

    containers = []
    status_by_name = {cs.name: cs for cs in (status.container_statuses or [])}
    for c in spec.containers or []:
        cs = status_by_name.get(c.name)
        cstate: dict[str, Any] = {}
        if cs and cs.state:
            if cs.state.waiting:
                cstate = {
                    "state": "waiting",
                    "reason": cs.state.waiting.reason,
                    "message": cs.state.waiting.message,
                }
            elif cs.state.terminated:
                cstate = {
                    "state": "terminated",
                    "reason": cs.state.terminated.reason,
                    "exit_code": cs.state.terminated.exit_code,
                    "message": cs.state.terminated.message,
                }
            elif cs.state.running:
                cstate = {
                    "state": "running",
                    "started_at": _iso(cs.state.running.started_at),
                }
        containers.append(
            {
                "name": c.name,
                "image": c.image,
                "command": c.command,
                "args": c.args,
                "ready": cs.ready if cs else False,
                "restart_count": cs.restart_count if cs else 0,
                "current_state": cstate,
            }
        )

    conditions = [
        {
            "type": cond.type,
            "status": cond.status,
            "reason": cond.reason,
            "message": cond.message,
        }
        for cond in (status.conditions or [])
    ]

    return {
        "name": meta.name,
        "namespace": meta.namespace,
        "node": spec.node_name,
        "phase": status.phase,
        "start_time": _iso(status.start_time),
        "labels": meta.labels or {},
        "owner_references": [
            {"kind": o.kind, "name": o.name} for o in (meta.owner_references or [])
        ],
        "containers": containers,
        "conditions": conditions,
    }


def _get_events(namespace: str, pod_name: str, limit: int = 20) -> list[dict[str, Any]]:
    """Warning/Normal events for a pod, most recent last."""
    field_selector = f"involvedObject.name={pod_name},involvedObject.namespace={namespace}"
    try:
        events = _core().list_namespaced_event(
            namespace=namespace, field_selector=field_selector
        )
    except ApiException as exc:
        logger.warning("Could not fetch events for %s: %s", pod_name, exc.reason)
        return []

    parsed = []
    for e in events.items:
        parsed.append(
            {
                "type": e.type,
                "reason": e.reason,
                "message": e.message,
                "count": e.count,
                "last_seen": _iso(e.last_timestamp or e.event_time),
            }
        )

    # Sort by last_seen (None-safe) and keep the most recent `limit`.
    parsed.sort(key=lambda x: x["last_seen"] or "")
    return parsed[-limit:]


def _get_logs(
    namespace: str, pod_name: str, container: str, tail_lines: int
) -> str:
    """
    Fetch the last `tail_lines` log lines for a container. Handles the common
    case where a crashed container has logs only in its *previous* instance.
    """
    def _read(previous: bool) -> str | None:
        try:
            return _core().read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous,
                timestamps=True,
            )
        except ApiException as exc:
            logger.debug(
                "log read (previous=%s) failed for %s/%s: %s",
                previous,
                pod_name,
                container,
                exc.reason,
            )
            return None

    current = _read(previous=False)
    if current and current.strip():
        return current

    # If the container is in CrashLoopBackOff, current logs may be empty;
    # the useful output lives in the previous (crashed) instance.
    previous_logs = _read(previous=True)
    if previous_logs and previous_logs.strip():
        return "[from previous (crashed) container instance]\n" + previous_logs

    return "(no logs available — container has not produced output)"


def collect_evidence(namespace: str, pod_name: str) -> dict[str, Any]:
    """
    Gather the full evidence bundle for one pod: description, events, and
    per-container tailed logs. Returns RAW (unmasked) data — the caller is
    responsible for masking before sending to the LLM.
    """
    settings = get_settings()
    try:
        pod = _core().read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as exc:
        logger.error("Failed to read pod %s/%s: %s", namespace, pod_name, exc.reason)
        raise

    description = _describe_pod(pod)
    events = _get_events(namespace, pod_name)

    logs: dict[str, str] = {}
    for c in pod.spec.containers or []:
        logs[c.name] = _get_logs(
            namespace, pod_name, c.name, settings.log_tail_lines
        )

    evidence: dict[str, Any] = {
        "collected_at": datetime.now(UTC).isoformat(),
        "namespace": namespace,
        "pod": pod_name,
        "description": description,
        "events": events,
        "logs": logs,
    }
    return evidence

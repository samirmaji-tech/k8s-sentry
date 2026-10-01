"""
notifier.py — Slack delivery.

Formats an Analysis + pod context into a clean Slack Block Kit message and
POSTs it to an incoming webhook. Notification failures are logged but never
raised — a broken Slack integration must not break incident analysis.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings
from .llm_analyzer import Analysis

logger = logging.getLogger("k8s-sentry.notifier")

# Emoji + colour per severity for quick visual triage in Slack.
_SEVERITY_META = {
    "CRITICAL": ("🔴", "#B00020"),
    "HIGH": ("🟠", "#E67700"),
    "MEDIUM": ("🟡", "#F1C40F"),
    "LOW": ("🟢", "#2E7D32"),
    "INFO": ("🔵", "#1565C0"),
}


def _build_blocks(pod_ref: dict[str, Any], analysis: Analysis) -> dict[str, Any]:
    emoji, color = _SEVERITY_META.get(analysis.severity, ("⚪", "#607D8B"))
    ns = pod_ref.get("namespace", "?")
    pod = pod_ref.get("name") or pod_ref.get("pod", "?")
    reason = pod_ref.get("reason", "")
    restarts = pod_ref.get("restart_count", 0)

    steps_md = "\n".join(f"{i+1}. `{s}`" for i, s in enumerate(analysis.remediation_steps))

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} K8s-Sentry Incident — {analysis.severity}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Namespace:*\n`{ns}`"},
                {"type": "mrkdwn", "text": f"*Pod:*\n`{pod}`"},
                {"type": "mrkdwn", "text": f"*Reason:*\n{reason or 'n/a'}"},
                {"type": "mrkdwn", "text": f"*Restarts:*\n{restarts}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Root cause*\n{analysis.root_cause}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested remediation* (review before running)\n{steps_md}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"💡 *Prevention:* {analysis.prevention_tip or 'n/a'}  "
                    "•  _Read-only agent: commands are advisory._",
                }
            ],
        },
    ]

    # `attachments` gives the message a coloured side-bar keyed to severity.
    return {
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
            }
        ]
    }


def _fallback_text(pod_ref: dict[str, Any], analysis: Analysis) -> str:
    """Plain-text version for clients that don't render blocks."""
    pod = pod_ref.get("name") or pod_ref.get("pod", "?")
    steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(analysis.remediation_steps))
    return (
        f"[{analysis.severity}] K8s-Sentry incident on {pod_ref.get('namespace')}/{pod}\n"
        f"Root cause: {analysis.root_cause}\n"
        f"Remediation:\n{steps}\n"
        f"Prevention: {analysis.prevention_tip}"
    )


def notify(pod_ref: dict[str, Any], analysis: Analysis) -> bool:
    """
    Send the incident summary to Slack. Returns True on success, False on any
    failure (never raises). No-op (returns True) if Slack is disabled.
    """
    settings = get_settings()

    if not settings.slack_enabled or not settings.slack_webhook_url:
        logger.info("Slack disabled or webhook unset — skipping notification.")
        return True

    payload = _build_blocks(pod_ref, analysis)
    payload["text"] = _fallback_text(pod_ref, analysis)  # accessibility fallback

    try:
        resp = httpx.post(settings.slack_webhook_url, json=payload, timeout=10.0)
        if resp.status_code == 200:
            logger.info("Slack notification delivered.")
            return True
        logger.error(
            "Slack webhook returned %s: %s", resp.status_code, resp.text[:200]
        )
        return False
    except httpx.HTTPError as exc:
        logger.error("Slack notification failed: %s", exc)
        return False

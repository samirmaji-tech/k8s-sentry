"""
Shared test configuration.

Force a hermetic environment BEFORE the app package is imported: no Anthropic
key (so llm_analyzer takes the deterministic fallback path) and Slack disabled
(so notifier is a no-op). This guarantees the whole suite runs offline with no
external calls.
"""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("SLACK_ENABLED", "false")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")
os.environ.setdefault("TARGET_NAMESPACE", "devops-lab")

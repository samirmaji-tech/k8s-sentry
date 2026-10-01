"""Tests for webhook alert parsing (SNS envelope, CloudWatch dimensions, scoping)."""
import json

from app import main


def test_sns_cloudwatch_dimensions():
    payload = {
        "Type": "Notification",
        "Message": json.dumps(
            {
                "AlarmName": "pod-crashloop",
                "Trigger": {
                    "Dimensions": [
                        {"name": "Namespace", "value": "devops-lab"},
                        {"name": "PodName", "value": "crashloop-app"},
                    ]
                },
            }
        ),
    }
    target = main._resolve_target(payload)
    assert target == {"namespace": "devops-lab", "pod": "crashloop-app"}


def test_azure_common_alert_schema():
    payload = {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {"alertRule": "aks-pod-restart-count", "monitorCondition": "Fired"},
            "alertContext": {
                "condition": {
                    "allOf": [
                        {
                            "metricName": "pod_number_of_container_restarts",
                            "dimensions": [
                                {"name": "namespace", "value": "devops-lab"},
                                {"name": "pod", "value": "crashloop-app"},
                            ],
                        }
                    ]
                }
            },
        },
    }
    target = main._resolve_target(payload)
    assert target == {"namespace": "devops-lab", "pod": "crashloop-app"}


def test_azure_schema_without_pod_falls_back_to_scan():
    payload = {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {"alertRule": "aks-node-pressure"},
            "alertContext": {
                "condition": {"allOf": [{"dimensions": [{"name": "namespace", "value": "devops-lab"}]}]}
            },
        },
    }
    target = main._resolve_target(payload)
    assert target["namespace"] == "devops-lab"
    assert target["pod"] is None


def test_direct_manual_body():
    target = main._resolve_target({"namespace": "devops-lab", "pod": "imagepull-app"})
    assert target == {"namespace": "devops-lab", "pod": "imagepull-app"}


def test_scan_mode_namespace_only():
    target = main._resolve_target({"namespace": "devops-lab"})
    assert target["namespace"] == "devops-lab"
    assert target["pod"] is None


def test_empty_payload_defaults_to_scope():
    target = main._resolve_target({})
    assert target["namespace"] == "devops-lab"
    assert target["pod"] is None


def test_plain_text_sns_message_does_not_crash():
    target = main._resolve_target(
        {"Type": "Notification", "Message": "disk pressure on node"}
    )
    assert target["namespace"] == "devops-lab"
    assert target["pod"] is None

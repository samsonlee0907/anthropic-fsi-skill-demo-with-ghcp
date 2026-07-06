"""OpenTelemetry wiring to Azure Application Insights.

Auto-instruments FastAPI + outgoing HTTP via ``configure_azure_monitor`` when
``APPLICATIONINSIGHTS_CONNECTION_STRING`` is present, and exposes a tracer so the
orchestrator can emit one span per scenario and per agent turn. When the
connection string is absent (e.g. local dev) it degrades to a no-op tracer.
"""
import os

from opentelemetry import trace

_CONN = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
_configured = False


def configure() -> bool:
    """Configure Azure Monitor once. Returns True if telemetry is active."""
    global _configured
    if _configured:
        return bool(_CONN)
    _configured = True
    if not _CONN:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=_CONN,
            enable_live_metrics=True,
        )
        return True
    except Exception:  # noqa: BLE001 - telemetry must never break the app
        return False


def get_tracer():
    return trace.get_tracer("fsi.multiagent")

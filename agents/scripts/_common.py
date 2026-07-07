"""Shared helpers for the Foundry provisioning scripts.

Kept import-light so each script can run standalone:

    python agents/scripts/provision_skills.py
"""
import os
import sys


def require_project_endpoint() -> str:
    """Return the target Foundry project endpoint from env, or fail fast.

    No default is provided on purpose: a hardcoded endpoint silently points a
    re-user's run at someone else's project. Set PROJECT_ENDPOINT to the value
    emitted by the infra deployment, e.g.

        $env:PROJECT_ENDPOINT = azd env get-value AZURE_AI_PROJECT_ENDPOINT

    (form: https://<account>.services.ai.azure.com/api/projects/<project>)
    """
    val = os.environ.get("PROJECT_ENDPOINT", "").strip()
    if not val:
        sys.exit(
            "ERROR: PROJECT_ENDPOINT is not set.\n"
            "  Set it to your Foundry project endpoint, e.g.\n"
            "    $env:PROJECT_ENDPOINT = azd env get-value AZURE_AI_PROJECT_ENDPOINT\n"
            "  (form: https://<account>.services.ai.azure.com/api/projects/<project>)"
        )
    return val.rstrip("/")

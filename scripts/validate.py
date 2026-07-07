"""Generic end-to-end validator for the deployed FSI demo API.

Runs each scenario against the deployed BFF, reads the SSE stream, downloads the
produced artifact, and asserts it is a real OOXML (PK zip) file. No resource names
are hardcoded: the API base URL comes from --api-base or the API_BASE_URL env var
(set by deploy.ps1 from the infra API_URL output).

Usage:
    python scripts/validate.py --api-base https://<api-fqdn>
    python scripts/validate.py pe-lbo            # single scenario
Exit code is non-zero if any scenario fails, so it doubles as a CI gate.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

# scenario -> expected artifact extension
EXPECTED_EXT = {
    "equity-research": ".xlsx",
    "ib-pitch": ".pptx",
    "pe-lbo": ".xlsx",
}


def _api_base(cli_value: str | None) -> str:
    base = (cli_value or os.environ.get("API_BASE_URL", "")).strip().rstrip("/")
    if not base:
        sys.exit(
            "ERROR: API base URL not provided. Pass --api-base or set API_BASE_URL "
            "(e.g. to the API_URL infra output)."
        )
    return base


def run_scenario(base: str, scenario: str, timeout: int = 1800):
    body = json.dumps({"scenario": scenario}).encode()
    req = urllib.request.Request(
        base + "/api/run", data=body, headers={"Content-Type": "application/json"}
    )
    artifacts, errors = [], []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            t = ev.get("type")
            if t == "artifact" and ev.get("id"):
                artifacts.append(ev)
            elif t == "error":
                errors.append(ev.get("message", ""))
    return artifacts, errors, int(time.time() - t0)


def is_ooxml(data: bytes) -> bool:
    # Every .xlsx/.pptx is a zip; zip local file headers start with 'PK\x03\x04'.
    return data[:4] == b"PK\x03\x04"


def validate(base: str, scenario: str) -> bool:
    ext = EXPECTED_EXT[scenario]
    try:
        artifacts, errors, secs = run_scenario(base, scenario)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {scenario}: request error: {str(e)[:200]}")
        return False

    if errors:
        print(f"[FAIL] {scenario} ({secs}s): errors={errors}")
        return False
    match = next((a for a in artifacts if a.get("filename", "").lower().endswith(ext)), None)
    if not match:
        print(f"[FAIL] {scenario} ({secs}s): no {ext} artifact; got {[a.get('filename') for a in artifacts]}")
        return False

    url = match["url"] if match["url"].startswith("http") else base + match["url"]
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {scenario}: artifact download error: {str(e)[:200]}")
        return False

    if not is_ooxml(data):
        print(f"[FAIL] {scenario}: '{match['filename']}' is not valid OOXML (no PK zip signature)")
        return False

    print(f"[PASS] {scenario} ({secs}s): {match['filename']} ({len(data)} bytes, valid OOXML)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", help="scenarios to run (default: all)")
    ap.add_argument("--api-base", default=None, help="API base URL (or set API_BASE_URL)")
    args = ap.parse_args()

    base = _api_base(args.api_base)
    targets = args.scenarios or list(EXPECTED_EXT.keys())
    results = {s: validate(base, s) for s in targets if s in EXPECTED_EXT}
    passed = sum(results.values())
    print(f"\n=== {passed}/{len(results)} scenarios passed ===")
    sys.exit(0 if passed == len(results) and results else 1)


if __name__ == "__main__":
    main()

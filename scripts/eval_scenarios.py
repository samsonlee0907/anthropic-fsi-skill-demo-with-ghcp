"""Lightweight eval / smoke harness for the FSI multi-agent demo.

Runs each scenario end-to-end against the deployed API and asserts:
  * every expected agent (specialists + orchestrator) actually ran,
  * at least one artifact of the expected file type was produced,
  * no error events were emitted.

Usage:
    python scripts/eval_scenarios.py                # all scenarios
    python scripts/eval_scenarios.py pe-lbo         # one scenario
Exit code is non-zero if any assertion fails, so it doubles as CI-style gate.
"""
import json
import sys
import time
import urllib.request

API = "https://ca-api-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io"

# scenario -> (expected agents in order, expected artifact extension)
EXPECTED = {
    "equity-research": (
        ["fsi-three-statement-agent", "fsi-dcf-agent", "fsi-comps-agent",
         "fsi-orchestrator-equity-research"],
        ".xlsx",
    ),
    "ib-pitch": (
        ["fsi-competitive-analysis-agent", "fsi-pptx-author-agent", "fsi-deck-qc-agent",
         "fsi-orchestrator-ib-pitch"],
        ".pptx",
    ),
    "pe-lbo": (
        ["fsi-lbo-agent", "fsi-model-audit-agent", "fsi-orchestrator-pe-lbo"],
        ".xlsx",
    ),
}


def run(scenario: str):
    body = json.dumps({"scenario": scenario}).encode()
    req = urllib.request.Request(
        API + "/api/run", data=body, headers={"Content-Type": "application/json"})
    agents, arts, errs = [], [], []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            t = ev.get("type")
            if t == "agent_start":
                agents.append(ev["agent"])
            elif t == "artifact" and ev.get("id"):
                arts.append(ev.get("filename", ""))
            elif t == "error":
                errs.append(ev.get("message", ""))
    return agents, arts, errs, int(time.time() - t0)


def evaluate(scenario: str) -> bool:
    exp_agents, ext = EXPECTED[scenario]
    agents, arts, errs, secs = run(scenario)
    checks = {
        "agents_ran": set(exp_agents).issubset(set(agents)),
        f"artifact_{ext}": any(a.lower().endswith(ext) for a in arts),
        "no_errors": not errs,
    }
    ok = all(checks.values())
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {scenario} ({secs}s)")
    print(f"        agents={agents}")
    print(f"        artifacts={arts}")
    if errs:
        print(f"        errors={errs}")
    for name, passed in checks.items():
        print(f"        - {name}: {'ok' if passed else 'MISSING'}")
    return ok


def main():
    targets = sys.argv[1:] or list(EXPECTED.keys())
    results = {s: evaluate(s) for s in targets if s in EXPECTED}
    passed = sum(results.values())
    print(f"\n=== {passed}/{len(results)} scenarios passed ===")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()

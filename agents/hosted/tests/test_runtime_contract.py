"""Static contract tests for the hosted runtime.

These tests read the runtime source instead of importing it, so they run without the
Foundry SDKs or Azure credentials.
"""

from __future__ import annotations

import ast
import hashlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

HOSTED_DIR = Path(__file__).resolve().parents[1]
AGENT_SRC = HOSTED_DIR / "_azd" / "agent-src"
RUNTIME = HOSTED_DIR / "fsi_hosted_agent.py"
SHIPPED = ("fsi_hosted_agent.py", "fsi_artifact_egress.py", "requirements.txt")


def _module() -> ast.Module:
    return ast.parse(RUNTIME.read_text(encoding="utf-8"))


def _system_prompt(env: dict[str, str] | None = None) -> str:
    tree = _module()
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_system_prompt":
            wanted.append(node)
        elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "DISCLAIMER":
            wanted.append(ast.Assign(targets=[ast.Name("DISCLAIMER", ast.Store())], value=node.value))
    namespace: dict = {"os": os}
    exec(compile(ast.fix_missing_locations(ast.Module(body=wanted, type_ignores=[])), str(RUNTIME), "exec"), namespace)
    with patch.dict(os.environ, env or {}, clear=False):
        return namespace["_system_prompt"]()


class RuntimeMirrorTests(unittest.TestCase):
    def test_agent_src_matches_source_of_truth(self) -> None:
        for name in SHIPPED:
            with self.subTest(name=name):
                source = hashlib.sha256((HOSTED_DIR / name).read_bytes()).hexdigest()
                mirror = hashlib.sha256((AGENT_SRC / name).read_bytes()).hexdigest()
                self.assertEqual(source, mirror, f"sync agents/hosted/{name} into _azd/agent-src")

    def test_agentignore_excludes_local_state(self) -> None:
        lines = {line.strip() for line in (AGENT_SRC / ".agentignore").read_text().splitlines()}
        for entry in (".foundry/", ".azure/", ".env", ".venv/"):
            self.assertIn(entry, lines)


class RuntimePromptTests(unittest.TestCase):
    def test_prompt_steers_to_fact_pack_and_native_code_interpreter(self) -> None:
        prompt = _system_prompt({"FSI_SCENARIO_TITLE": "Equity Research & Valuation"})
        self.assertIn("You are the Equity Research & Valuation agent", prompt)
        self.assertIn("sec-edgar___get_financial_fact_pack", prompt)
        self.assertIn("NATIVE code_interpreter", prompt)
        self.assertIn("Do NOT call_tool a code_interpreter through the toolbox", prompt)

    def test_prompt_treats_webiq_as_untrusted_context(self) -> None:
        prompt = _system_prompt()
        self.assertIn("untrusted source material", prompt)
        self.assertIn("claim that WebIQ was used unless the request contains its search results", prompt)

    def test_prompt_forbids_claimed_approval_and_ends_with_disclaimer(self) -> None:
        prompt = _system_prompt()
        self.assertIn("never grant an approval", prompt)
        self.assertTrue(prompt.endswith("Not investment advice. Demo reviews are not institutional approval."))


class RuntimeWiringTests(unittest.TestCase):
    def test_instrumentation_stays_disabled(self) -> None:
        calls = [
            n for n in ast.walk(_module())
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "disable_instrumentation"
        ]
        self.assertTrue(calls, "framework instrumentation must stay disabled (known serialization bug)")

    def test_toolbox_connections_and_native_code_interpreter(self) -> None:
        text = RUNTIME.read_text(encoding="utf-8")
        self.assertIn("FoundryToolbox(cred, token_scope=AI_SCOPE, load_tools=False)", text)
        self.assertIn("FoundryToolbox(cred, token_scope=AI_SCOPE, load_tools=True)", text)
        self.assertIn("tools_toolbox.allowed_tools = _toolbox_tool_allowlist()", text)
        self.assertIn("client.get_code_interpreter_tool()", text)
        self.assertIn("ArtifactEgressMiddleware(", text)

    def test_egress_depends_on_no_other_local_module(self) -> None:
        egress = ast.parse((HOSTED_DIR / "fsi_artifact_egress.py").read_text(encoding="utf-8"))
        local = {p.stem for p in HOSTED_DIR.glob("*.py")} - {"fsi_artifact_egress"}
        imported = set()
        for node in ast.walk(egress):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & local)


if __name__ == "__main__":
    unittest.main()

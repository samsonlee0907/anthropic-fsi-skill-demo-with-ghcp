"""Register the Anthropic financial-analysis skills as Foundry skills (v2 design).

Source of truth: the Anthropic `financial-services` repo, pinned to an immutable
commit so every run registers byte-identical content:

  https://github.com/anthropics/financial-services/tree/<REF>/plugins/vertical-plugins/financial-analysis/skills

Each skill's `SKILL.md` uses the agentskills.io front-matter format
(`name` + `description`, unquoted) which is *identical* to the Foundry Skills
(preview) format, so the skills map 1:1 with no rewriting.

We register the 12 RUNTIME skills (everything except `skill-creator`, which is a
meta-authoring skill with no place in a runtime toolbox). Skills are registered
as inline content (description + SKILL.md body). They are then bound to the
per-scenario toolboxes by `bind_skills_to_toolboxes.py` and consumed natively by
the hosted scenario agents (SDK `load_skill` progressive disclosure).

Idempotent: each skill is deleted (if present) then re-created as a single
default version, so re-runs converge to the pinned content.

Endpoint : POST {PROJECT_ENDPOINT}/skills/{name}/versions?api-version=v1
SDK      : AIProjectClient(..., allow_preview=True).beta.skills
Auth     : DefaultAzureCredential -> https://ai.azure.com/.default
RBAC     : requires "Foundry User" on the project scope.
"""
import os
import re
import sys
import time
import urllib.request

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import SkillInlineContent
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError, HttpResponseError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import require_project_endpoint  # noqa: E402

PROJECT_ENDPOINT = require_project_endpoint()

# Immutable commit in anthropics/financial-services.
REF = os.environ.get("ANTHROPIC_SKILLS_REF", "4aa51ed3d379731f8f9beff498d749580372699c")
RAW_BASE = (
    f"https://raw.githubusercontent.com/anthropics/financial-services/{REF}"
    "/plugins/vertical-plugins/financial-analysis/skills"
)

# The 12 runtime skills (skill-creator intentionally excluded).
RUNTIME_SKILLS = [
    "3-statement-model",
    "audit-xls",
    "clean-data-xls",
    "competitive-analysis",
    "comps-analysis",
    "dcf-model",
    "deck-refresh",
    "ib-check-deck",
    "lbo-model",
    "ppt-template-creator",
    "pptx-author",
    "xlsx-author",
]

_FRONTMATTER = re.compile(
    r"^\s*---\s*\nname:\s*(?P<name>.+?)\s*\ndescription:\s*(?P<desc>.+?)\s*\n---\s*\n(?P<body>.*)$",
    re.S,
)


def fetch_skill_md(name: str) -> str:
    url = f"{RAW_BASE}/{name}/SKILL.md"
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def parse_skill(raw: str, fallback_name: str) -> tuple[str, str, str]:
    """Return (name, description, body) from a SKILL.md string."""
    m = _FRONTMATTER.match(raw)
    if not m:
        raise ValueError(f"{fallback_name}: could not parse YAML front matter")
    name = m.group("name").strip()
    desc = m.group("desc").strip()
    body = m.group("body").strip()
    # Foundry: description <= 1024 chars, name lowercase-hyphen <= 64.
    if len(desc) > 1024:
        desc = desc[:1021].rstrip() + "..."
    return name, desc, body


def main() -> int:
    client = AIProjectClient(
        endpoint=PROJECT_ENDPOINT,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )
    ok = True
    for skill in RUNTIME_SKILLS:
        try:
            raw = fetch_skill_md(skill)
            name, desc, body = parse_skill(raw, skill)
            if name != skill:
                print(f"[WARN] {skill}: front-matter name is '{name}' (using it)")
            # delete-then-create for idempotent single-version registration.
            # Freshly-created projects intermittently return not_found / "Project
            # not found" for a few minutes (eventual consistency), so retry.
            last_err = None
            for attempt in range(1, 6):
                try:
                    try:
                        client.beta.skills.delete(name=name)
                    except ResourceNotFoundError:
                        pass
                    except HttpResponseError as e:
                        if e.status_code not in (404,):
                            raise
                    sv = client.beta.skills.create(
                        name=name,
                        inline_content=SkillInlineContent(description=desc, instructions=body),
                        default=True,
                    )
                    ver = getattr(sv, "version", None)
                    print(f"[OK]   {name} (v{ver}) desc={len(desc)}c body={len(body)}c")
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    print(f"[retry {attempt}/5] {name}: {str(e)[:120]}")
                    time.sleep(5)
            if last_err is not None:
                raise last_err
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {skill} -> {str(e)[:300]}")
    # summary
    existing = sorted(s.name for s in client.beta.skills.list())
    print(f"\nSkills in project ({len(existing)}): {existing}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

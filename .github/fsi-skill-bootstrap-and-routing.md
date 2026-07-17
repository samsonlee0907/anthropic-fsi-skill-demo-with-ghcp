# FSI skill bootstrap and routing

This is the lightweight alternative to deploying the Foundry-hosted-agent stack. It lets an
agentic GitHub Copilot use the same financial-analysis skills locally for an FSI task. It does
not provision Azure resources, create Foundry toolboxes, or provide the deployed portal, SEC
EDGAR MCP connection, or durable artifact-download path described elsewhere in this repository.

All generated analysis is for demonstration only and is not investment advice.

## Approved upstream source

The financial-analysis skills are approved for local intake only from:

- Repository: <https://github.com/anthropics/financial-services>
- Source directory:
  [`plugins/vertical-plugins/financial-analysis/skills`](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis/skills)
- License: [Apache-2.0](https://github.com/anthropics/financial-services/blob/main/LICENSE)

When a user asks to install, sync, or update FSI skills, copy only the immediate skill
directories from that source into `.github/skills/`. Do not download or execute unrelated files
from the upstream repository. Preserve all applicable upstream license, attribution, and `NOTICE`
files when copying or modifying distributed skill content.

## Local installation contract

Install each upstream skill in an `fsi-`-prefixed directory and update its `SKILL.md` YAML
frontmatter to use the same prefixed `name`.

| Upstream skill | Local skill name and directory |
| --- | --- |
| `3-statement-model` | `fsi-3-statement-model` |
| `audit-xls` | `fsi-audit-xls` |
| `clean-data-xls` | `fsi-clean-data-xls` |
| `competitive-analysis` | `fsi-competitive-analysis` |
| `comps-analysis` | `fsi-comps-analysis` |
| `dcf-model` | `fsi-dcf-model` |
| `deck-refresh` | `fsi-deck-refresh` |
| `ib-check-deck` | `fsi-ib-check-deck` |
| `lbo-model` | `fsi-lbo-model` |
| `ppt-template-creator` | `fsi-ppt-template-creator` |
| `pptx-author` | `fsi-pptx-author` |
| `skill-creator` | `fsi-skill-creator` |
| `xlsx-author` | `fsi-xlsx-author` |

For every installed skill:

1. Copy its complete directory and supporting files to
   `.github/skills/fsi-<upstream-name>/`.
2. Change the `name` field in `SKILL.md` to `fsi-<upstream-name>`.
3. Prefix its `description` with: `For financial-services, regulated-finance,
   investment-banking, asset-management, insurance, or lending work only.`
4. Retain the upstream instructions and add a prominent note that the local copy was renamed and
   scoped for FSI use.

## FSI-only activation policy

Use an `fsi-*` skill only when the request clearly concerns financial services, regulated finance,
investment banking, asset management, insurance, lending, financial reporting, valuation, or a
related client/deal workflow.

Do not use an `fsi-*` skill for a general spreadsheet, presentation, data-cleanup, financial
calculation, or document request without an FSI context.

Examples:

- Use `fsi-xlsx-author` for an insurance pricing model, bank capital-planning workbook, or
  investment-banking valuation model.
- Do not use `fsi-xlsx-author` for a household budget, school tracker, or general spreadsheet.
- Use `fsi-pptx-author` and `fsi-ib-check-deck` for client, investor, deal, or
  regulated-finance presentations.
- Do not use them for general presentations.

If the context is ambiguous, ask whether the work is for an FSI use case before selecting an
`fsi-*` skill.

Before using an FSI skill, read its local `SKILL.md` and select only the narrowest relevant skill.
Do not load unrelated FSI skills.

# Syncing checker skills into the validator

The SAP MDM **checker skills** (`mdm-w9-checker`, `mdm-banking-checker`, …) are the
human source of truth for how a document is checked. This doc explains how an
updated skill reaches the validator — the local Python `mdmdoc` and the ABAP clone
`mdm-doc-validator-abap` (in-MDG BAdI).

Golden rule (invariant #1): **the model never decides verdicts — only explicit
rules do.** So "syncing" a skill means turning each skill rule into either an
*explicit validator rule* (when it is mechanizable) or an *advisory note* (when it
needs SAP-request context the document alone lacks). We never ask the model to
"judge the skill".

## Where rules live

| layer | file | role |
|---|---|---|
| checker skill | `~/.claude/skills/<skill>/references/dynamic_rules.md` + `rules.md` | human source of truth (DR entries + prose) |
| validator rules | `rules/w9.yaml`, `rules/banking.yaml` | **decides verdicts** (with `src/mdmdoc/rules/predicates.py`) |
| ABAP / SAP | `mdm-doc-validator-abap` (`ZCL_MDMDOC_RULES_DATA`, generated from the same YAML) | in-MDG verdicts |

## See what a skill contains

```bash
mdmdoc skill-rules mdm-w9-checker
```

Parses the skill's `dynamic_rules.md` deterministically and buckets the **active**
rules:

- **mechanized** — already enforced by a validator rule (shown: which `W9-…`/`BNK-…`);
- **advisory / needs SAP context** — can't be a document-only check (company code,
  withholding code, Recipient Type, create/extend/update scope, or pure guidance);
- **to review** — no mapping yet: decide whether to promote or mark advisory.

The mapping comes from the curated `COVERAGE` dict in `src/mdmdoc/skill_rules.py`
(kept honest as rules are promoted). `skill_rules.py` is **read-only** — it never
writes; rule edits go through `rules_io.py` / the console Rules page.

## The sync workflow (drop an updated skill → validator works off it)

1. Egor updates a checker skill (often via Codex) and drops it in.
2. `mdmdoc skill-rules <skill>` → read the buckets.
3. For each **to-review** active rule:
   - **mechanizable** (digit count, format, cross-field, presence) → add/edit a rule
     in `rules/*.yaml` (console **Rules** page or the YAML directly); add a predicate
     to `predicates.py` if needed (**return `(fired, detail)`, never raise**).
   - **needs SAP context** → route to the ABAP MDG BAdI (it reads the CR fields), or
     keep advisory. Do not fake a document check.
   - Record the decision in `COVERAGE` so the next run's buckets stay honest.
4. `uv run pytest -q` green; if extraction/rules changed and labels exist,
   `mdmdoc eval`.
5. Push to SAP: console **Rules → Regenerate for SAP** (or run the ABAP generator
   `~/Projects/mdm-doc-validator-abap/tools/gen_rules_abap.py`).

The repeatable procedure is captured as the Claude skill **`mdmdoc-skill-sync`**
(`~/.claude/skills/mdmdoc-skill-sync/SKILL.md`) — trigger it with "sync
mdm-w9-checker" / "обнови валидатор по скиллу".

## Honest limits

- A fully-automatic "prose → executable predicate" import is **not** offered — it
  would be unreliable and would let the model define verdicts. A human stays in the
  loop for the translation, grounded by the deterministic parser.
- Many checker-skill rules need SAP-request context the document does not carry
  (US company code, withholding 07, Recipient Type, update-vs-create). Those are
  enforced on the **ABAP MDG** side (BAdI reads the CR), not by the document-only
  validator — or they remain advisory.

# Claude project context

These four files exist to give Claude (claude.ai) enough context to be productive in a new chat about this codebase without spending the first half of the conversation reconstructing it.

```
PROJECT_CONTEXT.md   ← read first; the orientation pass
MODULE_MAP.md        ← file-by-file tour with the non-obvious bits
DATA_MODEL.md        ← schema, constraints, migrations, gotchas
CONVENTIONS.md       ← handler skeleton, patterns, what-not-to-do list
```

They're checked in here (rather than living only in Claude's project knowledge) so they version with the code. If a PR changes architecture, that PR also updates the context file — the two never drift.

## Workflow

### Starting a new Claude chat

1. Open the Claude project for this repo.
2. Make sure the **drop files here** box contains the latest copy of these four files. If you've pushed code changes since you last refreshed them, re-upload from this folder (or the latest checkout).
3. Start chatting normally. Reference files by name when needed (`"see MODULE_MAP.md for the roster.py overview"`).

That's it. The files are passive — Claude reads them as context, nothing else happens.

### Updating the files

Claude can't write back to project knowledge. Updates are manual but cheap.

**When to update**:
- New table or migration → `DATA_MODEL.md`
- New module, new handler, registration-order change → `MODULE_MAP.md`
- New architectural invariant, new env var, new PR landed → `PROJECT_CONTEXT.md`
- New coding pattern, new gotcha discovered → `CONVENTIONS.md`

**How to update**:
- At the end of a chat where you made significant changes, tell Claude: *"Update the relevant claude-context files for what we did."* It will produce new versions of whichever files are affected.
- Save the new versions over the ones in this folder.
- Commit them with your code changes.
- Re-upload to the Claude project knowledge box (replace the old versions).

A reasonable cadence: refresh whenever you'd write a PR description, because the kinds of changes worth describing in a PR are also the kinds worth reflecting here.

### When NOT to update

If a chat was just bug-hunting or small refactors that didn't change anything structural — schema, handler shape, invariants, conventions — leave the files alone. They're not a changelog.

## Sizing

Total is roughly 650 lines / 44KB. Small enough that re-uploading after a change is trivial, large enough to cover what's hard to derive from reading code.

What's deliberately NOT in these files:
- Function-by-function API docs for `db.py` (goes stale fast; Claude can just read the file when needed)
- The Mini App's internal JS structure (rarely changed; lives in `bot/static/moneyball.html` which Claude can read directly)
- Deployment specifics (those live in the root `README.md`)

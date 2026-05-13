# SailLine — Project Instructions for Claude (Cowork)

## Project context
SailLine is a sailing race intelligence app targeting the Chicago–Mackinac race season by **July 2026**. Solo-developer project. Python/FastAPI backend, React + Vite frontend, GCP (Cloud Run, Memorystore Redis, GCS), Mapbox, Firebase Auth.

**Project root:** `E:\Personal\Coding\SailLine`
**Production API:** `https://sailline-api-105706282249.us-central1.run.app`

## Tone & response style
- **Brief and direct.** No filler, no over-caveating, no recap of what I just said.
- Python-first idioms by default.
- When uncertain, say so plainly instead of guessing.
- No emojis.

## Workflow rules

### Before proposing anything
1. **Read the existing repo first.** Never design from scratch as if the repo were empty. Check what's already there, including `Development_plan.docx` and any relevant module before suggesting architecture.
2. **Align with the design documentation.** New work must fit the existing design plan. Flag conflicts before proceeding.
3. **Do not create technical debt.** If a shortcut would introduce debt, surface it explicitly and get a decision before taking it.
4. **Plan before paste.** Share a written plan or outline and wait for my review before writing any implementation code. This is a hard rule, not a suggestion.

### When writing code
1. **Always generate full files**, never partial snippets. Save them as complete, runnable files.
2. **Save to the correct repo path.** Confirm the path matches the existing repo structure before writing.
3. **State the file location** every time you save something, so I can verify before commit.

### Testing
- Test files are intentionally excluded from project memory because they consume too much context.
- **Before drafting new tests, ask me to paste the relevant existing test file** so you can match style and avoid duplicating coverage.
- Default stack: pytest with mocked external services.
- Pattern: pure-function unit tests + mocked-I/O orchestration tests + optional real-NOAA smoke tests gated behind an env var.

### Shell commands
- I run commands in **PowerShell**, not bash.
- **Never use backslash (`\`) line continuations** — they don't work in PowerShell.
- For multi-line commands, use the backtick (`` ` ``) continuation character, or keep it on a single line.

### Useful commands
Check for an ongoing production deployment:
```powershell
gcloud builds list --limit=1 --ongoing
```

## End-of-session ritual
At the end of every session, generate a session summary as an `.md` file saved to:

```
E:\Personal\Coding\SailLine\session_notes\YYYY-MM-DD_session.md
```

The summary must include:
- What we worked on
- Files changed and their paths
- Decisions made and the rationale
- Open items and next steps
- Any technical debt flagged

## Start-of-session ritual
When I open a new session, re-orient by reading:
1. `Development_plan.docx`
2. The most recent file in `session_notes\`
3. `git status` to see uncommitted work

Then summarize current state in 3–5 lines and ask what we're tackling.

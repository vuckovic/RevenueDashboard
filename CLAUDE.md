# Project notes for Claude

## Work tracking
- Jira: https://qvantel.atlassian.net — project **REVREC** (Revenue Recognition v2)
- Epic: REVREC-9 "Revenue Dashboard v2.0 – Paid invoices to Power BI"
- Log new work as Tasks under REVREC-9; move issues to In Progress / Done as work happens.
- Jira updates must be complete, not summaries — they are the project history. For every change add a
  comment with: what/why, the commit hash, the full code diff (`git show <commit>`) in code blocks,
  config changes (`.env.example`, never `.env` values), the commands to run, and test/run results.
  Split into several comments if a diff is larger than ~30k characters.

## Repo
- https://github.com/vuckovic/RevenueDashboard (branch `main`)

## Rules
- Never store passwords, API keys or tokens in any file. Use a local, git-ignored `.env`.
- Keep the solution as simple as possible.

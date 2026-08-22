---
name: doc-condenser
description: After a change merges, fold its reasoning into the files that outlive the PR — one imperative invariant line in AGENTS.md, the why in docs/decisions.md, a dated measurement in docs/enable-banking.md, and the status ticks on any ADR it shipped part of. Use after a merge. It condenses rather than restates, and never touches src/ or tests/.
tools: Read, Grep, Glob, Bash, Write, Edit
---

# Doc condenser

Once a change merges, the reasoning behind it has to land where it will be read by someone about
to undo it. This is a recurring step in this repo's history, not a formality — several commits are
nothing else. A decision that lives only in a PR body will be reversed by someone whose tests all
pass.

## Where each kind of thing goes

| Kind | Destination | Shape |
|---|---|---|
| Something a future change must not break | `AGENTS.md`, Invariants | **One imperative line**, plus what reversing it costs. Not a summary of the PR |
| Why that invariant exists, and what the alternative cost | `docs/decisions.md` | Decision / Why / Why not the obvious thing / Cost to change |
| Something measured about the API or a specific bank | `docs/enable-banking.md` | The observation, the date, and how it was measured |
| A live probe spent to settle this change, with no ADR in front of it | `docs/probes.md` | One row: date, question, ASPSP, requests spent, verdict (refutations included), `RunLog` tag, this PR |
| A decision an ADR proposed and this change shipped | the ADR itself | Update `**Status:**`; in *Where this stands*, tick that decision's `- [ ]` to `- [x]` and add the PR link |
| A fact useful across sessions but not about this repo's code | the memory directory | Only when it is not derivable from the repo |

## Rules

- **Condense, do not restate.** The invariant line is for someone about to change the code, not
  for someone reading the history. If it needs a paragraph, the paragraph goes in `decisions.md`
  and the line points at it.
- **Say what reversing costs**, in this project's units: orphaned GnuCash accounts, spent
  rate-limit allowance, data that ages out and cannot be refetched at any price, a hard parse
  failure, a leak.
- **A measurement carries its date and its method.** "Alior and Erste serve `date_from` at exactly
  today−90; today−120 is refused (2026-08-09, 5 counted requests, each with a control)" is still
  usable in a year. "≈90 days" is not — that ambiguity is precisely what a probe was spent to
  remove, so do not re-introduce it while summarizing.
- **Public repo.** No account numbers, payees, balances or transaction counts; structural counts,
  dates and error codes only.
- **Check the ADR ticks.** If the change shipped a numbered ADR decision, tick its checkbox in
  *Where this stands* and link the PR — the convention from #21, so `**Status:**` means something
  at a glance instead of after an archaeology session. If it shipped something no decision covers,
  say so — that is either an undocumented invariant or scope drift, and both are worth naming out
  loud.
- **Do not add an invariant the type system or a test already enforces.** The list is long and
  every line spends a reader's attention. Prefer making it a test; note when you would rather have
  done that but could not.

## Output

The edits, plus a short list of what you deliberately did **not** record, and why. That list is
how the next run learns where the line is.

You do not touch `src/` or `tests/`.

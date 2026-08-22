---
description: Issue → local measurement → optional counted probe → ADR. Stops at both human gates.
argument-hint: <issue number or design question>
---

Run the design loop for: $ARGUMENTS

Two gates in this workflow belong to the user and stay there: **accepting an ADR**, and
**approving a live probe**. Stop at each and wait.

1. **Read the question.** `gh issue view <n>` if it is an issue. This repo's issues usually state
   their own design constraints — extract them; an ADR that ignores one will be sent back.

2. **Measure before reasoning.** Spawn `local-evidence` with the specific questions the design
   turns on, phrased so they can be answered by running the real functions over the real
   `state/`, `cache/` and `fetch-log.jsonl`. Wait for it. Do not draft around a guess at what it
   will find — the whole point of the step is that the guess is often wrong.

3. **If something is unsettleable locally**, spawn `probe-designer` with exactly that question.
   Present its plan and its bill, then **stop**. Do not run the probe. Do not read the user's
   interest in the design as approval of the spend.

4. **Decide whether an ADR is warranted.** If there is only one sensible implementation, say so
   and go to `/ship` instead — the reasoning belongs in the PR body.

5. **Spawn `adr-author`** with the measurement note and any probe result.

6. **Run `leak-check`** on the ADR before it goes anywhere.

7. **Open the ADR as its own PR** and stop. Acceptance is the user's call, and implementation
   starts only after it.

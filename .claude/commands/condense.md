---
description: Fold a merged change into AGENTS.md, decisions.md, enable-banking.md and any ADR it shipped part of.
argument-hint: <merged PR number>
---

Condense merged PR $ARGUMENTS into the files that outlive it.

1. **Read the merged change:** `gh pr view <n>` and `gh pr diff <n>`. The PR body carries the
   reasoning; the diff says what actually shipped, which is not always the same thing.

2. **Spawn `doc-condenser`** with both.

3. **Review its edits.** Watch for the two usual faults: an invariant line that restates the PR
   instead of instructing the next contributor, and a measurement recorded without its date and
   method.

4. **Run `leak-check`** before committing.

5. **Open it as its own small PR.** The history has several of these, and they review quickly
   precisely because they arrive alone.

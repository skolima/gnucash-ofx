---
description: Verify, guard, sanitize, and open the PR for the change on this branch.
argument-hint: [what the change is, if the diff alone does not make it obvious]
---

Ship the change on this branch. $ARGUMENTS

1. **Checks first**, so no reviewer spends a pass on a red branch:

   ```sh
   uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
   ```

   Fix what fails before going further.

2. **Review, in parallel:**
   - `invariant-guard` on the diff — always.
   - `ofx-conformance` — only if the diff touches `ofxout.py`, `naming.py`, the `compose_*` /
     `to_ascii` functions, or a fixture whose OFX output is asserted.

3. **Act on the findings.** A finding you disagree with gets answered in the PR body, not dropped.

4. **Draft the PR body** in house style: what was wrong and what the bug or the silence cost; why
   this shape and not the obvious one; what was measured, with numbers; what a reviewer should be
   suspicious of. Prose, not a bullet list. `Closes #n`.

5. **Then `leak-check`** — hand it the drafted PR body and the branch's commit messages
   explicitly, not just the diff. Wait for a **clear** verdict. Once text is on GitHub, editing it
   does not remove it.

6. **Push and open the PR.** Do not merge; after the merge, run `/condense`.

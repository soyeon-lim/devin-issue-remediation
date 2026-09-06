# Category Playbook: Code Quality

Appended after `base_playbook.md` when the issue carries `code-quality`,
`lint`, or `typing`.

---

## What "done" means here

The specific defect is gone, the behaviour is unchanged, and a reviewer can tell
that from the diff without running the code themselves.

This category carries the highest risk of scope creep. The whole value of the
change is that it is small and obviously correct.

## Steps

1. **Reproduce the finding first.** Run the checker and capture the exact
   error with file and line. If it does not reproduce, stop — the issue may
   already be fixed or the configuration may have drifted. Report that instead
   of inventing a fix.

2. **Determine whether it is a real defect or a false positive.** A type checker
   flagging `Optional` misuse where the value genuinely can be `None` is a real
   bug and the fix is a guard. The same warning where an invariant guarantees
   non-`None` needs a narrow suppression with a comment explaining the
   invariant. Decide which case you are in and say so in the PR.

3. **Fix the cause, not the symptom.** If a function is flagged for excessive
   complexity, do not split it arbitrarily to get under the threshold. Either
   find the genuine seam, or treat it as blocked and explain why the honest fix
   is larger than this issue's scope.

4. **Prove behaviour is unchanged.** Run the existing tests covering the file.
   If none exist, add one that captures current behaviour *before* your change,
   confirm it passes on the unmodified code, then apply the fix and confirm it
   still passes. Say that you did this in that order.

5. **Keep the diff minimal.** Your formatter will want to touch the whole file.
   Review `git diff` line by line and revert every hunk unrelated to the fix.

## Verification for this category

```bash
pre-commit run --files <changed files>     # should be clean
git diff --stat                            # confirm the diff is as small as you claim
pytest tests/unit_tests/<relevant path> -q
```

## Category-specific anti-patterns

- Changing a public function signature or return type to satisfy a checker.
  That is an API change and it is out of scope here.
- Renaming variables for readability while you are in the file.
- Converting a loop to a comprehension, or similar stylistic rewrites, on lines
  the checker did not flag.
- Adding a test that only asserts the linter passes.
- Silencing the check across the whole file or module when one line was flagged.

## PR body — additional required section

```markdown
## Finding
- Checker and rule:
- Location: <file:line>
- Reproduced before fix: <paste the original error>

## Classification
Real defect / false positive — <one sentence of reasoning>

## Behaviour preserved
<Which tests cover this. Whether they existed already or you added them.
If you added one, confirm it passed against the unmodified code first.>
```

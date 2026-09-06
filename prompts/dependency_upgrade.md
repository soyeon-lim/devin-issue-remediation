# Category Playbook: Dependency Upgrade / Vulnerability Remediation

Appended after `base_playbook.md` when the issue carries `dependencies` or
`security`.

---

## What "done" means here

The advisory no longer applies, the pin is reproducible, and the reviewer can
see evidence that the upgraded package still works in this codebase. Bumping a
number in a file is not the deliverable.

## Steps

1. **Confirm the vulnerable code path is actually reachable.** Find where the
   package is imported and what it is used for. If the affected function is
   never called in this repo, say so in the PR — it changes the reviewer's
   urgency and is useful information either way. Still perform the upgrade.

2. **Choose the smallest sufficient version.** Take the lowest release that
   clears the advisory. Do not jump to `latest` because it is convenient. If the
   only fixed release is a major version bump, treat that as blocked — a major
   bump is a scoping decision for a human.

3. **Edit the source file, not the generated one.** Change `requirements/*.in`
   and regenerate with `pip-compile`. For the frontend, change `package.json`
   and let the lockfile update from the install. Never hand-edit `*.txt` or
   `package-lock.json`.

4. **Read the changelog between the old and new version.** Look specifically for
   removed APIs, changed defaults, and deprecations. Then grep this repo for
   every symbol you imported from that package and confirm it still exists with
   the same signature. State in the PR that you did this and what you found.

5. **Check transitive fallout.** The resolver may have moved other packages. Diff
   the lockfile and list any *other* package that changed version, with a
   one-line note on whether it matters.

## Verification for this category

```bash
git diff requirements/          # or superset-frontend/package-lock.json
pip install -r requirements/development.txt
pytest tests/unit_tests -q -x
```

Then run the test module that most directly exercises the upgraded package. If
you cannot find one, say so — that gap is itself worth reporting.

## Category-specific anti-patterns

- Regenerating the entire lockfile so the diff contains fifty unrelated bumps.
  Constrain the resolution to the package you are fixing.
- Adding the package to an ignore or allowlist file instead of upgrading it.
- Upgrading a transitive dependency by adding a new direct pin without saying
  why that pin now exists. Future maintainers will not know it can be removed.
- Claiming the advisory is resolved without stating which version fixed it and
  where you confirmed that.

## PR body — additional required section

```markdown
## Advisory
- ID / link:
- Package: <old> → <new>
- Fixed in: <version>, per <source>
- Reachable in this repo: yes / no — <where>

## Transitive changes
<Other packages the resolver moved, and whether they matter. "None" is a valid answer.>

## Breaking-change review
<What you checked in the changelog. What you grepped for. What you found.>
```

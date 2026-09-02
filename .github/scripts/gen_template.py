"""Fallback PR description generator using commit messages and diff stat."""

import re, sys

commits = open("/tmp/commits.txt").read().strip().splitlines()
diff_stat = open("/tmp/diff_stat.txt").read().strip()

if not commits:
    print("No commits found", file=sys.stderr)
    sys.exit(1)


def clean(subject):
    """Strip conventional commit prefix (e.g. 'feat(scope): ') and capitalize."""
    s = re.sub(r"^[a-z]+(\([^)]+\))?: ", "", subject.strip())
    return (s[0].upper() + s[1:]) if s else subject.strip()


def is_conventional(subject):
    return bool(re.match(r"^(feat|fix|docs|refactor|perf|test)(\([^)]+\))?!?: ", subject.strip(), re.IGNORECASE))


cleaned = [clean(c) for c in commits[:10] if c.strip()]

# Prefer the newest commit with a real conventional-commit type for the title, so a
# generic subject (e.g. "Dev", "wip") that happens to be the newest commit doesn't win
# just for being newest.
typed_commits = [c for c in commits if is_conventional(c)]
title_source = clean(typed_commits[0]) if typed_commits else cleaned[0]

# Build a human-readable title from all commits
if len(commits) == 1:
    title = title_source[:70]
else:
    title = title_source
    if len(title) < 55 and len(commits) > 1:
        title = f"{title} (+{len(commits) - 1} more)"
    title = title[:70]

open("/tmp/pr_title.txt", "w").write(title)

bullets = "\n".join("- " + c for c in cleaned)

tb = "```"
body = f"""## Summary

{bullets}

## Changes

{tb}
{diff_stat}
{tb}

## Test plan

- [ ] Review the changes above
- [ ] Existing tests pass
- [ ] No regressions introduced
"""

open("/tmp/pr_body.txt", "w").write(body)

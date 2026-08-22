# Contributing

## Commit messages

This repo follows [Conventional Commits](https://www.conventionalcommits.org/): `<type>[optional scope]: <description>`, e.g. `feat: add reverse proxy service` or `fix(ci): correct tox env name`.

Common types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `style`, `build`, `ci`, `revert`.

This isn't just style — it drives automation:

- **Changelog generation** (`git-cliff`, configured in `pyproject.toml`) groups commits by type. A commit without a recognized prefix is **dropped from the changelog entirely** rather than lumped into a catch-all bucket, so use the right prefix.
- **Release versioning** (`.github/workflows/release.yml`) bumps semver based on commit prefixes since the last release: any `!` after the type or a `BREAKING CHANGE:` footer → major, `feat` → minor, everything else → patch.

Keep one logical change per commit rather than bundling unrelated changes together.

## Before opening a PR

Install `tox` and run the full check chain locally:

```bash
pip install tox
tox -e github   # lint + txt-lint + prettier + toml-lint
```

If it reports formatting issues, auto-fix them with:

```bash
tox -e format
```

Both `tests.yml` (the same `tox -e github` chain) and `secret-detection.yml` (TruffleHog) run in CI on every push and PR — fixing issues locally first saves a round trip.

## Review

PRs are routed to the owners defined in [CODEOWNERS](CODEOWNERS).

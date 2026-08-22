# Self-Hosting-Template

A GitHub template repo for bootstrapping new self-hosting projects with linting, CI, and release automation already wired up.

## What's included

- **`environment.yml` / `requirements.txt`** — conda environment (Python, pip, `gh`) with Python deps installed via pip.
- **`pyproject.toml`** — [tox](https://tox.wiki) environments for linting and formatting:
  - `lint` — `ruff check`
  - `format` — `ruff format` + `ruff check --fix` + `prettier --write` + `taplo fmt`
  - `txt-lint` — [textlint](https://textlint.github.io/) over `**/*.txt`
  - `prettier` — `prettier --check` over CSS/JS/HTML/JSON/YAML/Markdown
  - `toml-lint` — `taplo` format/lint check over TOML files
  - `github` — the full read-only CI chain (`lint` + `txt-lint` + `prettier` + `toml-lint`)
  - `all` — `format` then `github`
  - Also configures [git-cliff](https://git-cliff.org/) for generating changelogs/PR descriptions from Conventional Commits.
- **`package.json`** — `prettier` and `textlint` (+ plugins), installed on demand by the relevant tox envs.
- **`.github/workflows/`**
  - `tests.yml` — runs `tox -e github` on every push and PR.
  - `secret-detection.yml` — [TruffleHog](https://github.com/trufflesecurity/trufflehog) scan on every push and PR.
  - `release.yml` — on push to `main`, bumps semver based on Conventional Commit prefixes (`feat` → minor, `fix`/other → patch, `!`/`BREAKING CHANGE` → major) and publishes a GitHub Release with an auto-generated changelog.
- **`CODEOWNERS`** — defaults review ownership to `@Self-Host-Server/code-owners`.
- **`.gitignore`** — editor/AI-assistant artifacts (`.vscode`, `.cursor`, `CLAUDE.md`, etc.), `.env`, `node_modules`.

## Using this template

1. Click **Use this template** on GitHub to create a new repo.
2. Set up the environment:

   ```bash
   conda env create -f environment.yml
   conda activate template
   ```

3. Install `tox` and run the full check locally before pushing:

   ```bash
   pip install tox
   tox -e github   # lint + txt-lint + prettier + toml-lint
   tox -e format   # auto-fix formatting issues
   ```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the commit convention (Conventional Commits — it drives changelog generation and release versioning) and the local checks to run before opening a PR.

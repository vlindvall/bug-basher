# Contributing to Bug Basher

Thanks for contributing. This project is currently in active development, so the best contributions are small, focused, and easy to review.

## Development setup

```bash
poetry install
poetry run pytest -v
poetry run ruff check src/
```

## Pull request guidelines

- Keep PRs scoped to one change.
- Include or update tests for behavior changes.
- Run `pytest` and `ruff` locally before opening a PR.
- Document user-visible behavior changes in `README.md` when relevant.

## Issue reports

When reporting bugs, include:

- What you expected
- What happened instead
- Reproduction steps
- Relevant logs or stack traces

## Early project expectations

Because this project is a work in progress:

- APIs and internal module boundaries may change.
- Some docs and examples may lag behind implementation.
- Maintainers may prioritize fixes and architecture changes over feature requests.

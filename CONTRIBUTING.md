# Contributing to MindSync AI

Thank you for your interest in contributing!

## Development Setup

MindSync uses modern Python packaging via `pyproject.toml`.

1. Clone the repository:
   ```bash
   git clone https://github.com/adityarya24/mindsync-ai.git
   cd mindsync-ai
   ```

2. Create a virtual environment and install development dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -e ".[dev]"
   ```

3. Run the test suite:
   ```bash
   pytest -v
   ```

4. Run the local smoke test:
   ```bash
   python scripts/smoke_test.py
   ```

## Pull Request Guidelines

1. **GitHub `master` is Canonical:** Branch from the latest `master`.
2. **Surgical Changes:** Keep PRs focused. Do not mix unrelated refactors with bug fixes.
3. **Tests Required:** Any bug fix or feature must include a regression/unit test.
4. **Data Integrity First:** MindSync is a memory store. We prioritize data integrity, atomic operations, and robust network failure handling above all.

## Release Checklist

Releases are automated via GitHub Actions on tag pushes. To release a new version:

1. Update `__version__` in `mindsync/__init__.py`.
2. Update `CHANGELOG.md` with the release notes.
3. Commit and PR the changes.
4. Once merged, wait for successful push CI on the exact `master` commit.
5. Tag that verified commit (e.g., `v1.0.1`) and push the tag.
6. The `Release` workflow will build the wheel/sdist, generate checksums, create a GitHub Release, and publish to PyPI using Trusted Publishing (OIDC).

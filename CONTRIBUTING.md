# Contributing to Mistral NeX Stocks

Thank you for your interest in contributing to Mistral NeX Stocks! We welcome contributions from the community.

## How to Contribute

1.  **Fork the Repository:** Create your own fork of the repository.
2.  **Create a Branch:** Create a new branch for your feature or bug fix.
    ```bash
    git checkout -b feat/your-feature
    ```
3.  **Implement Your Changes:**
    - Keep functions small (under 50 lines).
    - Adhere to the project's coding style and standards.
    - Ensure your code is well-commented.
4.  **Add Tests:** Add unit tests for your changes in the `tests/` directory.
5.  **Run Tests:** Ensure all tests pass before submitting.
    ```bash
    python -m pytest -q
    ```
6.  **Submit a Pull Request:** Create a pull request to the default branch.

## Coding Standards

- **Python Version:** 3.11+
- **Linting:** Python quality checks are configured in `pyproject.toml` with Ruff and related tooling.
- **Type Checking:** Python typing is configured through `mypy` / `pyrefly`; front-end typing uses TypeScript.
- **Security:** Python security checks use `bandit`; keep secret-handling changes aligned with `SECURITY.md`.

## Local Validation

Run the same checks that CI uses before opening a pull request.

```bash
# Run tests
python -m pytest -q

# Run Python type/analysis checks if available in your environment
mypy .
pyrefly check .

# Run security scanning with the config file to exclude test assert warnings
bandit -c pyproject.toml -r .

# Run front-end validations
npm install
npm run typecheck
npm run lint
npx prettier --check "static/js/**/*.js" "chrome_extension/**/*.js"
npm run build
```

GitHub Actions uses current major versions of `actions/checkout` and
`actions/upload-artifact`. Self-hosted runners must be updated enough to run
Node 24-based GitHub Actions.

## License

By contributing to this project, you agree that your contributions will be licensed under the [MIT License](LICENSE).

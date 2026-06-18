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
    pytest
    ```
6.  **Submit a Pull Request:** Create a pull request to the `master` branch.

## Coding Standards

- **Python Version:** 3.9+
- **Linting:** We use `flake8` and `pylint`.
- **Type Checking:** We use `mypy`.
- **Security:** We use `bandit` and `pip-audit`.

## License

By contributing to this project, you agree that your contributions will be licensed under the [MIT License](LICENSE).

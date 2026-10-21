English | [Português (Brasil)](CONTRIBUTING.pt-BR.md)

# Contributing to OpenFlow

Contributions of code, tests, and documentation are welcome. You can participate in **English or Portuguese**; you do not need a Google account or Flow credits to get started.

## Where to start

- Check [good first issues](https://github.com/codexfast/openflow/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22) and [tasks needing help](https://github.com/codexfast/openflow/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22).
- Comment on the issue before starting to align scope and prevent duplicate work.
- For new ideas, look for an [existing issue](https://github.com/codexfast/openflow/issues) before opening a new one. Larger changes should be discussed before implementation.
- Documentation improvements, sanitized examples, and mock tests are great ways to contribute without accessing Google Flow.

## Documentation languages

- English (EN) is the primary project language for documentation and technical references.
- Keep English and Portuguese documentation updated together when making changes to code, behavior, or architecture.
- Contributions, issues, and pull requests are welcome in either English or Portuguese.
- No contributions require a Google account, real Flow credits, or paid generation runs; use mock tests and synthetic data instead.

## Setting up the environment

Requirements: **Python 3.11 or higher**, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and Node.js with support for `node --test` for the JavaScript suite. Installing dependencies requires network access.

Fork on GitHub and clone your fork, replacing `YOUR_USERNAME`:

```bash
git clone https://github.com/YOUR_USERNAME/openflow.git
```

```bash
cd openflow
```

Create a branch for your contribution:

```bash
git switch -c my-contribution
```

Install development dependencies:

```bash
uv sync --locked --extra browser --extra dev
```

This setup does not require Chromium, a Google login, or MCP configuration.

## Local checks

Run relevant checks from the repository root:

### Python tests

```bash
uv run --extra browser --extra dev pytest
```

### Lint

```bash
uv run --extra browser --extra dev ruff check .
```

### Type checking

```bash
uv run --extra browser --extra dev mypy
```

### JavaScript tests

```bash
node --test extension/tests/*.test.mjs
```

Automated tests use mocks, temporary data, and in-memory ASGI transport; they do not require an authenticated session or real generation runs. The full suite requires the Playwright package included in the `browser` extra: without it, some tests are skipped and others fail due to missing dependencies.

**Installing the Playwright package does not require downloading Chromium or logging in to run these mocks.** Keep the `browser` and `dev` extras in Python commands, as uv may synchronize the environment and remove unselected extras.

To run a single test file, for example:

```bash
uv run --extra browser --extra dev pytest tests/test_api.py
```

If a check fails before your changes, document the command, revision, and sanitized error in your PR. Compare against the base branch when needed. Do not disable tests or relax linting or type checking merely to produce a green result.

## Project structure

| Path | Responsibility |
| --- | --- |
| `src/flow_mcp/server.py` | MCP interface |
| `src/flow_mcp/api.py` | REST interface |
| `src/flow_mcp/service.py` | Shared operations and downloads |
| `src/flow_mcp/browser.py` and `managed.py` | Chromium profile, session, and managed backend |
| `src/flow_mcp/jobs.py` | Job persistence |
| `tests/` | Python tests, mocks, and fixtures |
| `extension/` | Legacy backend and JavaScript runtime also used by the managed backend |
| `extension/tests/` | JavaScript tests |

Do not treat `extension/` as disposable code: `operations.mjs` and `hijack_bypass.js` are also packaged into the Python package and used by the managed browser.

## Real tests in Google Flow: optional

Real tests differ from the mock suite. Only run them when necessary for the change, using an account you are authorized to use. See [setup and login](README.md#setup) and [security, account, and credits](README.md#security) before installing Chromium or running `openflow setup`.

- Enter password and 2FA only on the Google website, never in an issue, prompt, or project file.
- MCP, REST, and setup must not compete for the same profile simultaneously.
- Generation and upscale operations may consume credits. Present inputs, obtain human approval, and follow the preview and confirmation sequence.
- Do not automatically resubmit jobs with uncertain outcomes, such as `submitted_unverified`.
- Do not automate paid operations in CI or require others to spend credits to validate a PR.

OpenFlow is not an official Google API. Using internal interfaces may violate terms of service and lead to account restrictions; changes in Flow may break the integration.

## Opening an issue

For bugs, include:

- OpenFlow revision or version, operating system, and Python version;
- backend used (managed or extension) and interface (MCP or REST);
- Playwright/Chromium versions, when relevant;
- minimal reproduction steps, expected behavior, and observed behavior;
- sanitized errors and logs, free of authentication data.

For feature requests, explain the problem, use case, and considered alternatives. Running a paid generation is not required to open an issue.

## Submitting a pull request

1. Keep the PR focused on a single task; avoid unrelated refactors or generated files.
2. Follow surrounding naming conventions, types, and code style. For behavioral changes, add or adjust tests with mocks and synthetic fixtures.
3. Run relevant checks and report which passed, failed, or were skipped, along with the reason.
4. Describe what changed, why, and how to test. Reference the issue when applicable, and update documentation if interfaces or behavior changed.
5. Review the diff before submitting. Do not include secrets, browser profiles, job databases, or generated media.

Check for whitespace issues as well:

```bash
git diff --check
```

## Privacy and local files

Never post passwords, cookies, tokens, pairing codes, Chromium profiles, job databases, signed URLs, or private media in issues, PRs, screenshots, or logs. Use synthetic identifiers in tests.

Save test outputs and downloads in `artifacts/`, which is already ignored by Git. The `assets/` folder is also ignored; do not force-add unrelated generated assets or banners.

If you discover a potential vulnerability, do not post secrets, third-party data, or exploit details in a public issue. First check for private reporting instructions under the repository's **Security** tab; this guide does not establish a private communication channel or response SLA.

Contributions must be compatible with the [MIT license](LICENSE). Preserve license notices and do not include third-party code or media without permission.

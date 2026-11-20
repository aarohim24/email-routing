# Contributing

## Development

Use Python 3.12 and Node.js 22 to match the deployment image. Follow the setup
instructions in [README.md](README.md) and the validation guide in
[TESTING.md](TESTING.md). On Windows, run the backend in WSL2 or Linux containers.

Keep Python modules and functions in `snake_case`, classes in `PascalCase`, and
JavaScript functions and variables in `camelCase`. Use four spaces for Python
and two spaces for JavaScript, JSON, CSS and YAML. Follow the surrounding code
and keep changes focused on one concern.

## Commits and pull requests

Use Conventional Commits:

```text
<type>(<optional scope>): <imperative description>
```

Common types are `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci` and
`chore`. Examples:

```text
fix(worker): preserve uncertain deliveries during recovery
docs(setup): explain local Outlook sideloading
```

Describe the problem, resulting behavior and validation in each pull request.
Call out database migrations, configuration changes and deployment steps.

## Validation

Run these checks before submitting changes:

```sh
.venv/bin/python -m unittest discover -s tests -v
npm --prefix addin test
npm --prefix addin run build
.venv/bin/python scripts/offline_walkthrough.py
git diff --check
```

Tests and the walkthrough must remain offline and use synthetic data. Add
regression coverage for behavior changes, especially authorization, state
transitions and delivery recovery.

## Configuration and data

Commit only `.env.example`, with empty credential placeholders. Never commit
local environment files, tenant secrets, access tokens, certificates, databases,
mailbox content or generated manifests. Keep dependency lockfiles in version
control; exclude installed dependencies and generated assets.

Keep live mailbox access and sending disabled by default. Microsoft sign-in,
mailbox permissions and actual Outlook behavior require a separately authorized
test-mailbox pilot; passing CI does not enable production access.

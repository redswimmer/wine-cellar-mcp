# Contributing

How to work on the service itself. To connect an agent to a running
deployment, see the [README](README.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) and Node 22 (the CDK template
tests and deploys use it). uv installs Python 3.13 from `.python-version`.

```
uv sync
```

## Checks

Run what CI runs before pushing:

```
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run lint-imports
uv run pytest
```

`uv run pytest` runs only the unit tests in `tests/unit`. They use moto for
AWS and CDK template assertions for the infrastructure, and the Lambda
handler tests drive the real handler with API Gateway events, so the HTTP,
auth and MCP layers are covered without deploying.

`tests/e2e` holds smoke tests against a deployed endpoint. They read the URL
from `$WINE_MCP_URL` (the stack's `McpUrl` output) and the token from
`$WINE_MCP_TOKEN` or `~/.config/wine-cellar/dev-token`, skip when either is
missing, and delete their test wines:

```
WINE_MCP_URL=https://<dev-domain>/mcp uv run pytest tests/e2e
```

## Code layout

```
src/wine_cellar/    service: MCP tools, storage, search, Lambda entry points
infra/              CDK app: service stack (dev and prod) and GitHub OIDC stack
tests/unit/         unit tests (what CI runs)
tests/e2e/          smoke tests against a deployed endpoint
scripts/            Lambda packaging
.github/workflows/  CI checks and deploys
docs/               design and deploy guide
```

The code is layered: Lambda entry points, then `bootstrap` (the only place AWS
clients are built), the MCP tools, the service, the stores, and the domain
model. The rules live in `pyproject.toml` under `[tool.importlinter]` and
`uv run lint-imports` enforces them. [docs/DESIGN.md](docs/DESIGN.md) explains
the decisions behind the service.

## CI and deploys

- `CI` runs the checks above plus a gitleaks secret scan on every pull
  request and push to `main`.
- `Deploy` reruns the checks, deploys dev on every merge to `main`, then runs
  the e2e tests against dev. Run it by hand to put a branch on dev, or to
  deploy `main` to prod.

Deploy early and often to dev so code and infrastructure are proven together.
Where and how it deploys (region, domains, hosted zone, GitHub OIDC subject)
lives in the `context` block of `cdk.json`, not in code; GitHub holds only the
deploy role ARN and the e2e token. One-time AWS and GitHub setup is in
[docs/DEPLOY.md](docs/DEPLOY.md).

## Users and tokens

Each user has a server-generated ID (`usr_<ulid>`), a unique username, and one
or more API tokens. Only token hashes are stored. Manage them with the
`wine-tokens` CLI:

```
uv run wine-tokens --stage dev add-user --username <username> --label <where-used>   # prints the first token
uv run wine-tokens --stage dev list-users
uv run wine-tokens --stage dev create-token --user <username> --label <where-used>
uv run wine-tokens --stage dev revoke-token <hash-prefix>
```

More commands (deactivate, reactivate, reindex) are in
[docs/DESIGN.md](docs/DESIGN.md#users-and-tokens) and
[docs/DEPLOY.md](docs/DEPLOY.md#after-a-deploy).

# Wine Cellar Service: Design

A small AWS serverless service that lets an AI agent remember which wines a
person likes. The agent runs on NemoClaw (OpenClaw) and people talk to it in
Telegram, by text or by sending a photo of a label.

This is a preference tracker, not an inventory system. The core question it
answers is "have I had this wine, and did I like it?"

## Goals

- **Low cost:** pay-per-request services only, nothing that bills while idle.
- **Low latency:** text lookups well under a second when warm; photo lookups a
  few seconds, dominated by the model calls.
- **Easy to maintain:** one CDK app, one Lambda, one table, deployed from CI.
- **Secured:** only the claw can call the service.

## Decisions

| Topic | Decision | Why |
| --- | --- | --- |
| Interface | One MCP server, no separate REST API | The claw is the only client. A REST API can be added later on top of the same service layer. |
| MCP transport | Streamable HTTP, stateless, JSON responses | Works on Lambda with no sticky sessions. |
| Hosting | API Gateway HTTP API in front of one Python Lambda (arm64) | Cheapest managed HTTPS front door with custom domains and throttling. |
| Auth | One API token per user, issued with the `wine-tokens` CLI; only its SHA-256 hash is stored in DynamoDB | The token decides the user, so agents never pass a user ID and can't act for anyone else. No sign-in flow needed. |
| AgentCore Gateway | Not used for now | Needs an OAuth provider (Cognito) for inbound auth and adds cost for a single client. Can front the server later without code changes. |
| Users | Every record is keyed by our own server-generated user ID (`usr_<ulid>`), never by a chat platform's ID or a username | IDs are generated and never change; each user also has a unique username for people to read and type. Keeps the server independent of Telegram. Each user has one agent sandbox, configured with that user's token. |
| Storage | DynamoDB on-demand, S3 for photos, S3 Vectors for embeddings | All pay-per-use and serverless. |
| Label reading | Claude Haiku on Bedrock with vision | Fast and cheap; returns structured fields. |
| Embeddings | Bedrock multimodal embeddings (Titan Multimodal G1 or Nova Multimodal, chosen in step 3) | Puts label photos and text in the same vector space. |
| IaC | AWS CDK in Python | Same language as the service; deploys from GitHub Actions. |
| Domain | `mcp.wine.redswimmer.com` (prod), `mcp.wine-dev.redswimmer.com` (dev) | `<role>.<service>` leaves room for `api.wine…` or `app.wine…` later. Records live in the existing `redswimmer.com` hosted zone with an ACM certificate per name. |

## Architecture

```
Telegram ──▶ OpenClaw agent ──HTTPS + bearer──▶ API Gateway (HTTP API, custom domain)
                                                      │
                                                      ▼
                                          Lambda: MCP server (Python)
                                 ┌──────────┬─────────┴──┬──────────┐
                                 ▼          ▼            ▼          ▼
                            DynamoDB   S3 photos    S3 Vectors  Bedrock
                         (wines and                 (index)    (Claude,
                          token hashes)                         embeddings)
```

## Data model

One DynamoDB table, on-demand billing.

| Key | Value |
| --- | --- |
| `pk` | `USER#<user_id>` |
| `sk` | `PROFILE` for the user's profile (username, active flag, created), `WINE#<wine_id>` (ULID) for each wine |

Token items use `pk` `TOKEN#<sha256>` and `sk` `TOKEN`, and hold the user ID and a label.
Username items use `pk` `USERNAME#<username>` and `sk` `USERNAME`, and hold the
user ID. The profile and username items are written in one transaction with
`attribute_not_exists` conditions, so a username can only be taken once.

Wine item attributes:

- `producer`, `name`, `vintage` (number or null for NV), `region`, `country`,
  `varietals` (list), `style` (red, white, rosé, sparkling, dessert, fortified)
- `verdict`: `love`, `like`, `meh`, `dislike`
- `rating`: optional 1 to 5
- `notes`: the latest summary of what the person thought
- `tastings`: list of `{date, notes, verdict, where}` (kept small; one wine is
  rarely tasted more than a handful of times)
- `photo_keys`: S3 keys of label photos
- `search_text`: lowercased producer, name, vintage and region, used for fuzzy
  matching
- `created_at`, `updated_at`

Text search loads the user's wines with one Query and fuzzy-matches in the
Lambda. A personal list of a few hundred to a few thousand wines fits easily,
and it avoids running a search cluster.

Photos go to `s3://<bucket>/<user_id>/<wine_id>/<ulid>.jpg`.

S3 Vectors holds one vector per photo and one per wine's text description,
with `user_id` and `wine_id` as filterable metadata.

## MCP tools

| Tool | What it does |
| --- | --- |
| `save_wine` | Create or update a wine with a verdict, rating and notes. Matches an existing wine first so repeats don't create duplicates. |
| `add_tasting` | Add a dated tasting note and verdict to a wine. |
| `search_wines` | Text search, e.g. "that Rioja I liked last spring". Returns ranked matches with verdicts and notes. |
| `check_wine_photo` | Takes a label photo (base64). Reads the label, searches by text and by image, and answers "do I like this?" with the closest matches. Also returns the label fields so the agent can offer to save it. |
| `list_wines` | Filter by verdict, style, region or varietal. |
| `get_wine` / `delete_wine` | Fetch or remove one wine. |

No tool takes a user ID. The bearer token identifies the user, so the user is
fixed by how the sandbox was configured, not by anything the agent says.

Photos arrive base64-encoded in the tool call. NemoClaw's egress policy caps
an MCP request body at 131,072 bytes, so a photo must be about 90 KB or less
before encoding. Telegram stores every photo in several sizes, and the ~800 px
version is usually under that while still readable for label text. The tool
rejects oversized images with a message telling the agent to send a smaller
size. If that proves unreliable, `check_wine_photo` can also accept a Telegram
`file_id` and fetch the photo from the Bot API itself (needs the bot token in
Secrets Manager).

## Photo lookup flow

1. Store the photo in S3.
2. In parallel, ask Claude to read the label (producer, name, vintage, region,
   style) and create an image embedding.
3. Gather candidates from fuzzy text matching on the label fields and from a
   vector query filtered to the user.
4. Merge and score. A strong producer and name match wins; the vector match
   catches labels the model misreads.
5. Return the top matches with verdicts and notes, plus the extracted label.

## Security

- HTTPS only through the custom domain; the default API endpoint is disabled.
- Bearer token required on every request. Tokens are 256-bit random values
  with a `wct_` prefix; DynamoDB holds only their SHA-256 hashes. Lookups are
  cached for 60 seconds per Lambda environment, so a revoked token stops
  working within a minute.
- API Gateway throttling caps request rate and burst.
- The Lambda role can reach only its table, bucket, vector index and the
  Bedrock models it uses.
- S3 bucket blocks public access and encrypts at rest.

## Cost and latency

All services bill per request or per GB stored, so an idle service costs
close to nothing. At personal volume the main cost is Bedrock calls on photo
lookups. This is an estimate, not a measurement; we'll check it after step 3.

Python on arm64 with a small dependency set keeps cold starts around a
second. Provisioned concurrency isn't worth its cost at this volume.

## Repository layout

```
src/wine_cellar/    service: MCP tools, storage, search, Lambda entry point
infra/              CDK app: service stack (dev and prod) and GitHub OIDC stack
tests/unit/         pytest unit tests (moto for AWS, CDK template assertions)
scripts/            Lambda packaging
.github/workflows/  CI checks and deploys
docs/               this document and the deploy guide
```

## CI/CD

`CI` runs ruff, ty and the unit tests on every pull request and push. `Deploy`
reruns those checks, then deploys dev on every merge to `main`, so code and
infrastructure are proven together in small steps. It can also be run by hand
to put any branch on dev, or to deploy `main` to prod. GitHub Actions assumes
an AWS role through OIDC, so there are no long-lived keys. Setup is in
[DEPLOY.md](DEPLOY.md).

`tests/unit` is what CI runs. The Lambda handler tests drive the real handler
with API Gateway events against moto, which covers the HTTP, auth and MCP
layers without deploying. `tests/e2e` holds end-to-end smoke tests against a
deployed endpoint; they need a token, so they run by hand for now (see
[Connecting clients](#connecting-clients)).

## Build steps

The MCP server is built and dialed in with Claude Code as the agent before any
other client. OpenClaw is a client like Claude Code, so connecting it is the
last step and needs no server changes.

1. Project board and this design. (done)
2. Core MCP service: CDK app, storage, domain, text tools, per-user tokens,
   CI, tested with Claude Code. (done)
3. Photo and semantic search: label reading, embeddings, `check_wine_photo`,
   `add_wine_photo`, meaning-based text search. Tested with Claude Code.
4. Release to prod: rename to wine-cellar-mcp, e2e tests in the Deploy
   workflow, first prod deploy.
5. Connect NemoClaw and test end to end from Telegram.

## Users and tokens

The server generates every user ID (`usr_` plus a ULID), so IDs are unique
and never chosen by hand. Each user also has a unique username (lowercase
`a-z`, `0-9`, `_`, `-`) so it's clear who is who; the CLI accepts either. The
ID stays the key for their data. A token can only be issued for a user that
exists.

```
uv run wine-tokens --stage dev add-user --username andrew --label andrew-nemoclaw   # prints the first token
uv run wine-tokens --stage dev list-users
uv run wine-tokens --stage dev create-token --user andrew --label andrew-laptop
uv run wine-tokens --stage dev list-tokens
uv run wine-tokens --stage dev revoke-token <hash-prefix>
```

`add-user` and `create-token` print the token on stdout and everything else on
stderr, so redirect only stdout when saving a token to a file:

```
mkdir -p ~/.config/wine-cellar
(umask 077; uv run wine-tokens --stage dev create-token --user andrew --label andrew-laptop > ~/.config/wine-cellar/dev-token)
```

Users are deactivated, not deleted. Deactivating blocks all of a user's tokens
within a minute (the auth check also requires an active user) but keeps their
wines and tokens, so reactivating restores access with no new token:

```
uv run wine-tokens --stage dev deactivate-user --user andrew
uv run wine-tokens --stage dev reactivate-user --user andrew
```

New tokens can't be issued for a deactivated user. Permanent deletion isn't
supported yet; if it's ever needed it should only apply to deactivated users.

The user ID is never sent by the client. Knowing someone's ID doesn't let you
act as them; only their token does.

One sandbox maps to one user. If several people ever share one sandbox, their
wines would share one user ID; give each person their own sandbox and token.

## Connecting clients

Any MCP client that speaks Streamable HTTP and can send an `Authorization`
header works. To check a deployment end to end without a client, run
`uv run pytest tests/e2e`. These end-to-end tests call the deployed endpoint
(`$WINE_MCP_URL`, default dev) with the token in `$WINE_MCP_TOKEN` or
`~/.config/wine-cellar/dev-token`, skip when there is no token, and delete
their test wine. Plain `uv run pytest` runs only the unit tests.

### Claude Code

Add the server in local scope, which keeps the token in `~/.claude.json` and
out of the repo. Don't use `-s project`; that writes the token to `.mcp.json`.

```
claude mcp add --transport http wine https://mcp.wine-dev.redswimmer.com/mcp \
  --header "Authorization: Bearer $(cat ~/.config/wine-cellar/dev-token)"
claude mcp get wine
```

Start a new `claude` session in the same folder and talk to it normally; it
picks the tools from their descriptions.

### OpenClaw

NemoClaw supports remote MCP servers over HTTPS with Streamable HTTP and puts
the token in the `Authorization` header. The raw token stays in OpenShell's
provider store; the agent config holds only a placeholder:

```
export WINE_MCP_TOKEN=$(uv run wine-tokens --stage prod add-user --username andrew --label andrew-nemoclaw)
nemoclaw <sandbox> mcp add wine --url https://mcp.wine.redswimmer.com/mcp --env WINE_MCP_TOKEN
unset WINE_MCP_TOKEN
```

NemoClaw rejects URLs with query strings, so the token can only go in the
header. That matches the bearer check above.

## Environment

- Region: `us-east-1`, in the AWS account that holds `redswimmer.com`.

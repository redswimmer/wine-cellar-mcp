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
| Label reading | Done by the agent, not the server | The agent's own model can read a label photo and pass the name, vintage and varietal as text, so the server needs no vision model. Revisit if NemoClaw's model can't read images. |
| Embeddings | Amazon Nova Multimodal Embeddings on Bedrock (`amazon.nova-2-multimodal-embeddings-v1:0`), 1024 dimensions | One model for wine text and label photos. |
| Search | Vector search plus the existing fuzzy match, merged by rank (reciprocal rank fusion) | The standard hybrid search pattern. S3 Vectors has no keyword search, so the merge runs in the Lambda. |
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
                         (wines and               (text and    (Nova
                          token hashes)            photo        embeddings)
                                                   indexes)
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

Kept deliberately small; fields can be added when a real need comes up.

- `name` (required): what the label says, as free text, e.g. "Ridge Monte
  Bello Cabernet"
- `vintage`: the year on the label, or null for NV
- `varietal`: free text, e.g. "Cabernet Sauvignon" or "Grenache blend"
- `verdict`: `love`, `like`, `meh`, `dislike`
- `notes`: free text. The agent adds to it rather than replacing it, unless the
  person asks to rewrite it.
- `photo_keys`: S3 keys of label photos; each photo's id is the ULID at the end
  of its key, which also gives when it was added
- `search_text`: lowercased name, vintage and varietal, used for fuzzy matching
- `created_at`, `updated_at` (UTC): when the wine was first recorded and last
  changed, so "the red I tried for the first time yesterday" is a date filter

Text search loads the user's wines with one Query and fuzzy-matches in the
Lambda. A personal list of a few hundred to a few thousand wines fits easily,
and it avoids running a search cluster.

A wine's photos are at `s3://<bucket>/wines/<user_id>/<wine_id>/<ulid>`, in a
private, encrypted bucket. Deleting a wine deletes its photos.

S3 Vectors holds one vector bucket per stage with two indexes, both 1024
dimensions and cosine distance, and every query filtered to the caller's
`user_id`:

- `wine-text`: one vector per wine, keyed by `wine_id`. It embeds the label,
  varietal, verdict and notes, and is rewritten
  whenever the wine changes and deleted with it.
- `wine-photos`: one vector per photo, keyed by its S3 key, with `wine_id` as
  metadata. A wine with several photos matches through whichever is closest.

Text and photo vectors are never compared with each other. DynamoDB is the
source of truth: if a vector write fails the save still succeeds, and
`uv run wine-tokens --stage dev reindex` rebuilds every wine's text and photo
vectors.

## MCP tools

| Tool | What it does |
| --- | --- |
| `create_wine` | Record a wine; only `name` is required. The agent searches first so repeats don't create duplicates. |
| `update_wine` | Change any field, e.g. a new verdict or more notes. Omitted fields are kept. |
| `search_wines` | Search by text, a label photo, or both, e.g. "that bold red I loved" or a photo plus what the agent read off the label. Returns ranked matches with verdicts and notes. |
| `add_photo` | Add a label photo to a wine: returns an upload link, and the photo is saved to the wine once uploaded. A wine can have several. |
| `delete_photo` | Remove a photo from a wine, e.g. one added by mistake. |
| `upload_search_photo` | Get an upload link and `photo_ref` for a photo to search with. Never saved. |
| `list_wines` | Filter by verdict, varietal, and created or updated dates. Newest first; pages with `limit` and `next_token`. |
| `get_wine` / `delete_wine` | Fetch or remove one wine. |

No tool takes a user ID. The bearer token identifies the user, so the user is
fixed by how the sandbox was configured, not by anything the agent says.

Photos never travel inside an MCP request. The agent PUTs the image to a
pre-signed S3 URL (valid 15 minutes), e.g.
`curl -sSf --upload-file label.jpg "<url>"`:

- **Adding a photo to a wine** is one step: `add_photo(wine_id)` returns the
  link, and uploading to it saves the photo to that wine. The upload's S3 event
  runs a second Lambda that checks the image, adds it to the wine's
  `photo_keys` and indexes it; the wine's `photo_count` goes up within a few
  seconds. A file over 5 MB or that isn't a JPEG, PNG, WebP or GIF, or one for
  a wine deleted meanwhile, is discarded.
- **Searching with a photo**: `upload_search_photo` returns a link and a
  `photo_ref` under `search/<user_id>/`; the agent uploads, then passes
  `photo_ref` to `search_wines`. A missing, too large or non-image upload comes
  back as a tool error telling the agent what to do. The photo is deleted after
  the search and never saved; a lifecycle rule removes any left after a day.

This also keeps photos clear of NemoClaw's 131,072-byte cap on MCP request
bodies. The NemoClaw sandbox will need egress to the S3 endpoint for uploads
(step 5).

## Search flow

1. Text, if given, is embedded and queried against `wine-text`, and also
   fuzzy-matched against the user's wines as before.
2. A photo, if given, is embedded and queried against `wine-photos`.
3. Each search returns its top 10. The lists are merged with reciprocal rank
   fusion: a wine earns points for its position in each list, and the highest
   total ranks first.
4. The top matches come back with verdicts and notes.

## Security

- HTTPS only through the custom domain; the default API endpoint is disabled.
- Bearer token required on every request. Tokens are 256-bit random values
  with a `wct_` prefix; DynamoDB holds only their SHA-256 hashes. Lookups are
  cached for 60 seconds per Lambda environment, so a revoked token stops
  working within a minute.
- API Gateway throttling caps request rate and burst.
- The Lambda role can reach only its table, bucket, vector indexes and the
  Bedrock embedding model.
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
3. Photo and semantic search: vector store and text embeddings, semantic
   `search_wines`, photos and photo search, then dial-in with Claude Code.
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

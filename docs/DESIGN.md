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
| Auth | Bearer token checked in the Lambda, token stored as an SSM SecureString | One client, no sign-in flow needed. Constant-time compare; token rotated by updating the parameter. |
| AgentCore Gateway | Not used for now | Needs an OAuth provider (Cognito) for inbound auth and adds cost for a single client. Can front the server later without code changes. |
| Users | Every record is keyed by Telegram user ID | Start with Andrew only; other people the bot admits work without changes. |
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
                                 ┌──────────┬─────────┼──────────┬───────────┐
                                 ▼          ▼         ▼          ▼           ▼
                            DynamoDB   S3 photos  S3 Vectors  Bedrock     SSM token
                           (wines)                (index)    (Claude,
                                                             embeddings)
```

## Data model

One DynamoDB table, on-demand billing.

| Key | Value |
| --- | --- |
| `pk` | `USER#<telegram_user_id>` |
| `sk` | `WINE#<wine_id>` (ULID) |

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

Photos go to `s3://<bucket>/<telegram_user_id>/<wine_id>/<ulid>.jpg`.

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

Every tool takes `telegram_user_id`. The claw passes the ID of the person it
is talking to. The bearer token proves the caller is the claw, and the claw
is trusted to pass the right ID; Telegram itself limits who can reach the bot.

Photos arrive base64-encoded in the tool call. Telegram's compressed photos
are a few hundred KB, well under the Lambda's 6 MB payload limit.

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
- Bearer token required on every request, compared in constant time.
- API Gateway throttling caps request rate and burst.
- The Lambda role can reach only its table, bucket, vector index, the token
  parameter and the Bedrock models it uses.
- S3 bucket blocks public access and encrypts at rest.

## Cost and latency

All services bill per request or per GB stored, so an idle service costs
close to nothing. At personal volume the main cost is Bedrock calls on photo
lookups. This is an estimate, not a measurement; we'll check it after step 3.

Python on arm64 with a small dependency set keeps cold starts around a
second. Provisioned concurrency isn't worth its cost at this volume.

## Repository layout

```
infra/            CDK app (stacks for dev and prod)
src/wine_cellar/  service code: MCP server, tools, storage, search
tests/            unit tests (moto for AWS, stubbed Bedrock)
docs/             this document
.github/workflows CI: lint, typecheck, test, cdk synth; deploy dev on main, prod on manual approval
```

## CI/CD

GitHub Actions assumes an AWS role through OIDC, so no long-lived keys. Pull
requests run ruff, ty, pytest and `cdk synth`. Merges to `main` deploy dev;
prod deploys from a manually approved workflow.

## Build steps

1. Project board and this design.
2. Core service: CDK app, storage, domain, text tools, tests, CI.
3. Image and vector search: label reading, embeddings, `check_wine_photo`.
4. Secure the MCP endpoint, connect OpenClaw, test end to end from Telegram.

## Open questions

- Does the OpenClaw MCP client support custom auth headers per server? If not,
  we'll pass the token as a query parameter over HTTPS instead.
- Which AWS account and region? Default: the account that holds
  `redswimmer.com`, region `us-east-1` (S3 Vectors and the Bedrock models are
  available there).

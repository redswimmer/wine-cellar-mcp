# Deploying

Dev deploys automatically on every merge to `main`. Prod deploys only when the
`Deploy` workflow is run by hand with `stage: prod`.

## One-time setup

You need the AWS CLI signed in to the account that holds your domain's
hosted zone,
plus `uv` and Node 22.

With IAM Identity Center (SSO), select your profile and log in first; CDK and
the AWS CLI both read `AWS_PROFILE`:

```
aws configure list-profiles          # find the profile name
export AWS_PROFILE=<profile-name>
aws sso login
aws sts get-caller-identity          # should print the account
```

"Unable to locate credentials" means no profile is selected; an expired-token
error means it's time to run `aws sso login` again.

1. **Set the deployment settings in `cdk.json`.** Everything about where the
   service is deployed lives in its `context` block, not in code:

   | Key | What it is |
   |-----|------------|
   | `region` | AWS region for every stack. It must offer S3 Vectors and Nova multimodal embeddings. |
   | `stages.<stage>.domainName` | Custom domain per stage, e.g. `mcp.dev.example.com`. Remove it to use the default API Gateway URL. |
   | `zoneName`, `hostedZoneId` | The Route 53 zone that holds those names. |
   | `githubSubPrefix` | The GitHub OIDC subject prefix the deploy role trusts (step 3). |

   Find the hosted zone ID with:

   ```
   aws route53 list-hosted-zones-by-name --dns-name <zone-name> \
     --query 'HostedZones[0].Id' --output text
   ```

   Use the part after `/hostedzone/`. Changing any of these later is a
   `cdk.json` edit and a deploy, not a code change.

2. **Bootstrap CDK** in your region (skip if you've done it before in this
   account and region):

   ```
   npx aws-cdk@2.1144.0 bootstrap aws://<account-id>/<region>
   ```

3. **Create the GitHub deploy role.** If the account already has a GitHub
   OIDC provider (`aws iam list-open-id-connect-providers`), pass its ARN so
   the stack reuses it:

   ```
   uv sync
   ./scripts/build_lambda.sh
   npx aws-cdk@2.1144.0 deploy WineCellar-GithubOidc \
     [-c oidcProviderArn=arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com]
   ```

   Note the `DeployRoleArn` output.

   The role trusts `<githubSubPrefix>:environment:<env>` for each stage. Get
   the prefix for your repo with
   `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`. With GitHub's
   immutable subjects it looks like `repo:<owner>@<owner-id>/<repo>@<repo-id>`,
   pinning owner and repo by ID; otherwise it's `repo:<owner>/<repo>`. Set it
   in `cdk.json` before deploying this stack.

4. **Configure GitHub** (repo Settings):
   - Environments: create `dev` and `prod`. Give `prod` a required reviewer
     (you) so prod deploys wait for approval.
   - Variable (Actions, repository level): `AWS_DEPLOY_ROLE_ARN` = the role
     ARN from step 3. That's the only variable; the rest is in `cdk.json`.

   - Secret (Environments → `dev`, and later `prod`): `WINE_MCP_TOKEN` = a
     token for a dedicated test user in that stage, used by the e2e tests
     after each deploy:
     `uv run wine-tokens --stage <stage> add-user --username ci --label github-e2e`
     The tests only touch wines named `Smoke Test Cellars Red…`, delete each
     one when done, and sweep any leftovers from a cancelled run first, so the
     `ci` user's cellar stays empty and no real user's wines are touched.

Until `AWS_DEPLOY_ROLE_ARN` is set, the deploy job skips with a warning
instead of failing. The e2e job fails if `WINE_MCP_TOKEN` is missing, so a
removed secret can't let the tests pass without running.

## After a deploy

The workflow summary shows the stack outputs, including `McpUrl`. The e2e
job gets its URL from that output; locally, set `WINE_MCP_URL` to run
`uv run pytest tests/e2e`.

Add each user once with a unique username. `add-user` generates their ID (`usr_...`, shown on
stderr) and prints their first token. The token is printed once and only its
hash is stored, so put it straight into that user's agent:

```
uv run wine-tokens --stage dev add-user --username andrew --label andrew-agent
```

How to give the token to an agent depends on the agent; see
[the README](../README.md#connect-your-agent) for Claude Code and NemoClaw.

`list-users` and `list-tokens` show what exists, `revoke-token <hash-prefix>`
revokes a token within a minute, and `create-token --user <username> --label ...`
issues a replacement for an existing user (their wines stay put).
`deactivate-user --user <username>` locks a user out without deleting their
wines; `reactivate-user` lets them back in with their existing tokens.

## Deploying to prod

Actions → Deploy → Run workflow → branch `main`, `stage: prod`. The run
checks, deploys `WineCellar-prod`, then runs the e2e tests against it.

The first prod deploy creates the prod table, so the `ci` user can't exist
yet and its e2e job fails on the missing token. After that deploy, add the
user with `--stage prod` (step 4), set `WINE_MCP_TOKEN` on the `prod`
environment, and re-run the failed job.

## Deploying a branch to dev

Actions → Deploy → Run workflow → pick the branch, `stage: dev`. Useful for
trying an infrastructure change before merging it. The next merge to `main`
puts dev back on `main`.

## Tracing

Each deploy also deploys `WineCellar-Observability`, which turns on X-Ray
Transaction Search for the whole account and region: every trace is stored
as spans in the `aws/spans` log group (X-Ray creates it), and 1% are indexed
as searchable trace summaries. View traces in CloudWatch → Transaction
Search or the trace map.

CloudFormation can't take over Transaction Search if it was turned on outside
the stack (the console, or the AgentCore starter toolkit, does this). Turn it
off once before the first deploy that includes this stack, and remove the
policy that setup left behind:

```
aws xray update-trace-segment-destination --destination XRay
aws logs delete-resource-policy --policy-name TransactionSearchXRayAccess
```

`aws xray get-trace-segment-destination` printing `XRay` means it's off.

## Notes

- The first deploy with a custom domain waits for the ACM certificate's DNS
  validation, which usually takes a few minutes.
- Dev's table (wines and tokens) is deleted when the dev stack is destroyed;
  prod's is retained and the prod table has deletion protection on.

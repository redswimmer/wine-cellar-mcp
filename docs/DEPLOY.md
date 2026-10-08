# Deploying

Dev deploys automatically on every merge to `main`. Prod deploys only when the
`Deploy` workflow is run by hand with `stage: prod`.

## One-time setup

You need the AWS CLI signed in to the account that holds `redswimmer.com`,
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

1. **Find the hosted zone ID** for `redswimmer.com`:

   ```
   aws route53 list-hosted-zones-by-name --dns-name redswimmer.com \
     --query 'HostedZones[0].Id' --output text
   ```

   Use the part after `/hostedzone/`.

2. **Bootstrap CDK** in us-east-1 (skip if you've done it before in this
   account and region):

   ```
   npx aws-cdk@2.1144.0 bootstrap aws://<account-id>/us-east-1
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

   The role trusts GitHub's immutable OIDC subject for this repo
   (`repo:redswimmer@38088099/wine-cellar-service@1410527756:environment:<env>`),
   which pins the owner and repo by ID. If the repo's subject format ever
   changes, check it with
   `gh api repos/redswimmer/wine-cellar-service/actions/oidc/customization/sub`
   and pass `-c githubSubPrefix=<sub_claim_prefix>`.

4. **Configure GitHub** (repo Settings):
   - Environments: create `dev` and `prod`. Give `prod` a required reviewer
     (you) so prod deploys wait for approval.
   - Variables (Actions, repository level): `AWS_DEPLOY_ROLE_ARN` = the role
     ARN from step 3, and `HOSTED_ZONE_ID` = the ID from step 1.

Until `AWS_DEPLOY_ROLE_ARN` is set, the deploy job skips with a warning
instead of failing.

## After a deploy

The workflow summary shows the stack outputs, including `McpUrl`.

Add each user once with a unique username. `add-user` generates their ID (`usr_...`, shown on
stderr) and prints their first token. The token is printed once and only its
hash is stored, so put it straight into that user's sandbox:

```
export WINE_MCP_TOKEN=$(uv run wine-tokens --stage dev add-user --username andrew --label andrew-nemoclaw)
nemoclaw <sandbox> mcp add wine --url https://mcp.wine-dev.redswimmer.com/mcp --env WINE_MCP_TOKEN
unset WINE_MCP_TOKEN
```

`list-users` and `list-tokens` show what exists, `revoke-token <hash-prefix>`
revokes a token within a minute, and `create-token --user <username> --label ...`
issues a replacement for an existing user (their wines stay put).
`deactivate-user --user <username>` locks a user out without deleting their
wines; `reactivate-user` lets them back in with their existing tokens.

## Deploying a branch to dev

Actions → Deploy → Run workflow → pick the branch, `stage: dev`. Useful for
trying an infrastructure change before merging it. The next merge to `main`
puts dev back on `main`.

## Notes

- The first deploy with a custom domain waits for the ACM certificate's DNS
  validation, which usually takes a few minutes.
- Dev's table (wines and tokens) is deleted when the dev stack is destroyed;
  prod's is retained and the prod table has deletion protection on.

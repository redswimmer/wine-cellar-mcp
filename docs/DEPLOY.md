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

4. **Configure GitHub** (repo Settings):
   - Environments: create `dev` and `prod`. Give `prod` a required reviewer
     (you) so prod deploys wait for approval.
   - Variables (Actions, repository level): `AWS_DEPLOY_ROLE_ARN` = the role
     ARN from step 3, and `HOSTED_ZONE_ID` = the ID from step 1.

Until `AWS_DEPLOY_ROLE_ARN` is set, the deploy job skips with a warning
instead of failing.

## After a deploy

The workflow summary shows the stack outputs, including `McpUrl`. Get the
token the agent needs:

```
aws secretsmanager get-secret-value --secret-id <TokenSecretArn output> \
  --query SecretString --output text
```

Connect the claw (NemoClaw keeps the token out of the agent's config):

```
export WINE_MCP_TOKEN=<token>
nemoclaw <sandbox> mcp add wine --url https://mcp.wine-dev.redswimmer.com/mcp --env WINE_MCP_TOKEN
unset WINE_MCP_TOKEN
```

## Deploying a branch to dev

Actions → Deploy → Run workflow → pick the branch, `stage: dev`. Useful for
trying an infrastructure change before merging it. The next merge to `main`
puts dev back on `main`.

## Notes

- The first deploy with a custom domain waits for the ACM certificate's DNS
  validation, which usually takes a few minutes.
- Dev's table and token are deleted when the dev stack is destroyed; prod's
  are retained and the prod table has deletion protection on.

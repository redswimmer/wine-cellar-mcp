"""CDK entry point.

Context (cdk.json or -c):
  hostedZoneId   Route 53 zone ID for redswimmer.com; omit to use the default API Gateway URL
  zoneName       defaults to redswimmer.com
  lambdaAsset    built Lambda package directory, defaults to build/lambda
  oidcProviderArn  existing GitHub OIDC provider in the account, if there is one
  githubSubPrefix  OIDC subject prefix, if the repo's differs from GITHUB_SUB_PREFIX
"""

import os

import aws_cdk as cdk

from infra.stacks.github_oidc_stack import GithubOidcStack
from infra.stacks.service_stack import DomainConfig, ServiceConfig, WineCellarServiceStack

DOMAINS = {"dev": "mcp.wine-dev.redswimmer.com", "prod": "mcp.wine.redswimmer.com"}
# The repo uses GitHub's immutable OIDC subjects, which pin owner and repo by ID
# (`gh api repos/redswimmer/wine-cellar-service/actions/oidc/customization/sub` shows the prefix).
# A renamed or re-created repo with the same name can't assume the deploy role.
GITHUB_SUB_PREFIX = "repo:redswimmer@38088099/wine-cellar-service@1410527756"


def main() -> None:
    app = cdk.App()
    env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1")
    zone_id = app.node.try_get_context("hostedZoneId")
    zone_name = app.node.try_get_context("zoneName") or "redswimmer.com"
    asset = app.node.try_get_context("lambdaAsset") or "build/lambda"

    for stage, domain in DOMAINS.items():
        WineCellarServiceStack(
            app,
            f"WineCellar-{stage}",
            env=env,
            config=ServiceConfig(
                stage=stage,
                lambda_asset_path=asset,
                domain=DomainConfig(zone_id, zone_name, domain) if zone_id else None,
            ),
        )

    GithubOidcStack(
        app,
        "WineCellar-GithubOidc",
        env=env,
        sub_prefix=app.node.try_get_context("githubSubPrefix") or GITHUB_SUB_PREFIX,
        environments=list(DOMAINS),
        existing_provider_arn=app.node.try_get_context("oidcProviderArn"),
    )
    cdk.Tags.of(app).add("project", "wine-cellar")
    app.synth()


if __name__ == "__main__":
    main()

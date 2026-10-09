"""CDK entry point. Deployment settings live in cdk.json context (override any with -c):

region           AWS region for every stack
stages           per-stage settings, e.g. {"dev": {"domainName": "mcp.dev.example.com"}}; omit a stage's
                 domainName to use the default API Gateway URL
zoneName         Route 53 hosted zone that holds the domain names, e.g. example.com
hostedZoneId     that zone's ID
githubSubPrefix  GitHub OIDC subject prefix the deploy role trusts; the repo's
                 `gh api repos/<owner>/<repo>/actions/oidc/customization/sub` shows it
lambdaAsset      built Lambda package directory, defaults to build/lambda
oidcProviderArn  existing GitHub OIDC provider in the account, if there is one
"""

import os

import aws_cdk as cdk

from infra.stacks.github_oidc_stack import GithubOidcStack
from infra.stacks.service_stack import DomainConfig, ServiceConfig, WineCellarServiceStack


def _required(app: cdk.App, key: str):
    value = app.node.try_get_context(key)
    if not value:
        raise ValueError(f"Set {key!r} in cdk.json context (see infra/app.py)")
    return value


def main() -> None:
    app = cdk.App()
    env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=_required(app, "region"))
    stages: dict[str, dict] = _required(app, "stages")
    asset = app.node.try_get_context("lambdaAsset") or "build/lambda"

    for stage, settings in stages.items():
        domain = None
        if domain_name := settings.get("domainName"):
            domain = DomainConfig(_required(app, "hostedZoneId"), _required(app, "zoneName"), domain_name)
        WineCellarServiceStack(
            app,
            f"WineCellar-{stage}",
            env=env,
            config=ServiceConfig(stage=stage, lambda_asset_path=asset, domain=domain),
        )

    # The repo uses GitHub's immutable OIDC subjects, which pin owner and repo by ID. The subject still
    # includes the repo name, so renaming the repo means updating githubSubPrefix and redeploying this stack.
    GithubOidcStack(
        app,
        "WineCellar-GithubOidc",
        env=env,
        sub_prefix=_required(app, "githubSubPrefix"),
        environments=list(stages),
        existing_provider_arn=app.node.try_get_context("oidcProviderArn"),
    )
    cdk.Tags.of(app).add("project", "wine-cellar")
    app.synth()


if __name__ == "__main__":
    main()

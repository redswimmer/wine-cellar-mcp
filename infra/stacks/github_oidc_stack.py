"""One-time setup: lets GitHub Actions deploy this repo through CDK, with no stored AWS keys."""

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_ISSUER = "token.actions.githubusercontent.com"


class GithubOidcStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        sub_prefix: str,
        environments: list[str],
        existing_provider_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # An account can hold only one provider per issuer, so reuse it when one exists.
        provider = (
            iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "Provider", existing_provider_arn)
            if existing_provider_arn
            else iam.OpenIdConnectProvider(
                self, "Provider", url=f"https://{GITHUB_ISSUER}", client_ids=["sts.amazonaws.com"]
            )
        )

        role = iam.Role(
            self,
            "DeployRole",
            role_name="wine-cellar-github-deploy",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.open_id_connect_provider_arn,
                conditions={
                    # Only workflow jobs bound to these GitHub environments can deploy.
                    "StringEquals": {
                        f"{GITHUB_ISSUER}:aud": "sts.amazonaws.com",
                        f"{GITHUB_ISSUER}:sub": [f"{sub_prefix}:environment:{e}" for e in environments],
                    },
                },
            ),
        )
        # CDK deploys by assuming the roles `cdk bootstrap` created; that is all this role needs.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*"],
            )
        )
        # Lets the workflow print stack outputs (the MCP URL) after a deploy.
        role.add_to_policy(iam.PolicyStatement(actions=["cloudformation:DescribeStacks"], resources=["*"]))

        CfnOutput(self, "DeployRoleArn", value=role.role_arn)

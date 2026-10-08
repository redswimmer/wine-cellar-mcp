"""Synthesizes the CDK stacks and checks the resources that matter."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.stacks.github_oidc_stack import GithubOidcStack
from infra.stacks.service_stack import DomainConfig, ServiceConfig, WineCellarServiceStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")


@pytest.fixture
def asset_dir(tmp_path):
    (tmp_path / "wine_cellar").mkdir()
    (tmp_path / "wine_cellar" / "handler.py").write_text("handler = None\n")
    return str(tmp_path)


def _service(asset_dir, *, stage="dev", domain=True) -> Template:
    app = cdk.App()
    config = ServiceConfig(
        stage=stage,
        lambda_asset_path=asset_dir,
        domain=DomainConfig("Z123", "redswimmer.com", f"mcp.wine-{stage}.redswimmer.com") if domain else None,
    )
    return Template.from_stack(WineCellarServiceStack(app, "S", env=ENV, config=config))


def test_lambda_is_arm64_python_with_env(asset_dir):
    t = _service(asset_dir)
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Handler": "wine_cellar.handler.handler",
            "Environment": {
                "Variables": {
                    "TABLE_NAME": Match.any_value(),
                    "TOKEN_SECRET_ARN": Match.any_value(),
                    "ALLOWED_HOSTS": "mcp.wine-dev.redswimmer.com",
                }
            },
        },
    )


def test_custom_domain_disables_default_endpoint(asset_dir):
    t = _service(asset_dir)
    t.has_resource_properties("AWS::ApiGatewayV2::Api", {"DisableExecuteApiEndpoint": True})
    t.has_resource_properties("AWS::ApiGatewayV2::DomainName", {"DomainName": "mcp.wine-dev.redswimmer.com"})
    t.resource_count_is("AWS::Route53::RecordSet", 1)


def test_without_domain_uses_default_endpoint(asset_dir):
    t = _service(asset_dir, domain=False)
    t.has_resource_properties("AWS::ApiGatewayV2::Api", {"DisableExecuteApiEndpoint": False})
    t.resource_count_is("AWS::ApiGatewayV2::DomainName", 0)


def test_stage_is_throttled(asset_dir):
    _service(asset_dir).has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20}},
    )


def test_prod_retains_data_and_dev_does_not(asset_dir):
    prod = _service(asset_dir, stage="prod")
    prod.has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Retain"})
    dev = _service(asset_dir, stage="dev")
    dev.has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Delete"})


def test_oidc_role_trusts_only_repo_environments():
    app = cdk.App()
    prefix = "repo:redswimmer@38088099/wine-cellar-service@1410527756"
    stack = GithubOidcStack(app, "O", env=ENV, sub_prefix=prefix, environments=["dev", "prod"])
    Template.from_stack(stack).has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Condition": {
                                "StringEquals": {
                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                    "token.actions.githubusercontent.com:sub": [
                                        f"{prefix}:environment:dev",
                                        f"{prefix}:environment:prod",
                                    ],
                                },
                            }
                        }
                    )
                ]
            }
        },
    )

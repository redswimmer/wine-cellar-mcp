"""The wine cellar MCP service: API Gateway, Lambda, DynamoDB (wines and API tokens) and S3 Vectors (search)."""

from dataclasses import dataclass

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from wine_cellar.vectors import EMBEDDING_DIMENSION, EMBEDDING_MODEL_ID

TEXT_INDEX_NAME = "wine-text"


@dataclass(frozen=True)
class DomainConfig:
    hosted_zone_id: str
    zone_name: str
    domain_name: str


@dataclass(frozen=True)
class ServiceConfig:
    stage: str
    lambda_asset_path: str
    domain: DomainConfig | None = None
    throttle_rate: int = 10
    throttle_burst: int = 20

    @property
    def is_prod(self) -> bool:
        return self.stage == "prod"


class WineCellarServiceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: ServiceConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = RemovalPolicy.RETAIN if config.is_prod else RemovalPolicy.DESTROY

        table = dynamodb.TableV2(
            self,
            "Wines",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=config.is_prod,
            removal_policy=removal,
        )

        # Vectors can always be rebuilt from the table with `wine-tokens reindex`.
        vector_bucket = s3vectors.CfnVectorBucket(
            self, "Vectors", vector_bucket_name=f"wine-cellar-vectors-{config.stage}"
        )
        vector_bucket.apply_removal_policy(removal)
        text_index = s3vectors.CfnIndex(
            self,
            "TextIndex",
            vector_bucket_name=vector_bucket.vector_bucket_name,
            index_name=TEXT_INDEX_NAME,
            data_type="float32",
            dimension=EMBEDDING_DIMENSION,
            distance_metric="cosine",
        )
        text_index.add_dependency(vector_bucket)
        text_index.apply_removal_policy(removal)

        fn = lambda_.Function(
            self,
            "McpFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="wine_cellar.handler.handler",
            code=lambda_.Code.from_asset(config.lambda_asset_path),
            memory_size=512,
            timeout=Duration.seconds(25),
            environment={
                "TABLE_NAME": table.table_name,
                "ALLOWED_HOSTS": config.domain.domain_name if config.domain else "",
                "VECTOR_BUCKET": vector_bucket.vector_bucket_name or "",
                "TEXT_INDEX": TEXT_INDEX_NAME,
            },
            log_group=logs.LogGroup(
                self, "McpLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
            ),
        )
        table.grant_read_write_data(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                # QueryVectors needs GetVectors too when it filters on metadata.
                actions=[
                    "s3vectors:PutVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetVectors",
                ],
                resources=[text_index.attr_index_arn],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}"],
            )
        )

        domain_name = None
        if config.domain:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "Zone", hosted_zone_id=config.domain.hosted_zone_id, zone_name=config.domain.zone_name
            )
            cert = acm.Certificate(
                self,
                "Cert",
                domain_name=config.domain.domain_name,
                validation=acm.CertificateValidation.from_dns(zone),
            )
            domain_name = apigw.DomainName(self, "Domain", domain_name=config.domain.domain_name, certificate=cert)
            route53.ARecord(
                self,
                "Alias",
                zone=zone,
                record_name=config.domain.domain_name,
                target=route53.RecordTarget.from_alias(
                    targets.ApiGatewayv2DomainProperties(
                        domain_name.regional_domain_name, domain_name.regional_hosted_zone_id
                    )
                ),
            )

        api = apigw.HttpApi(
            self,
            "Api",
            api_name=f"wine-cellar-{config.stage}",
            create_default_stage=False,
            # With a custom domain, the only way in is through it (it is the Host the server accepts).
            disable_execute_api_endpoint=config.domain is not None,
        )
        api.add_routes(
            path="/mcp",
            methods=[apigw.HttpMethod.POST, apigw.HttpMethod.GET, apigw.HttpMethod.DELETE],
            integration=HttpLambdaIntegration("McpIntegration", fn),
        )
        apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=config.throttle_rate, burst_limit=config.throttle_burst),
            domain_mapping=apigw.DomainMappingOptions(domain_name=domain_name) if domain_name else None,
        )

        base_url = f"https://{config.domain.domain_name}" if config.domain else api.api_endpoint
        CfnOutput(self, "McpUrl", value=f"{base_url}/mcp")
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "VectorBucketName", value=vector_bucket.vector_bucket_name or "")
        CfnOutput(self, "TextIndexName", value=TEXT_INDEX_NAME)

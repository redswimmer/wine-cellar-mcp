"""Account-wide tracing: X-Ray Transaction Search, which stores every trace as spans in CloudWatch Logs.

The setting covers the whole account and region, so it lives in its own stack rather than in a stage stack.
X-Ray creates the `aws/spans` log group itself; CloudWatch reserves the `aws/` prefix, so a stack can't.
"""

import json

from aws_cdk import Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_xray as xray
from constructs import Construct


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        logs_arn = f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group"
        xray_arn = f"arn:{self.partition}:xray:{self.region}:{self.account}:*"
        policy = logs.CfnResourcePolicy(
            self,
            "XRayWritesSpans",
            policy_name="TransactionSearchAccess",
            policy_document=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "TransactionSearchXRayAccess",
                            "Effect": "Allow",
                            "Principal": {"Service": "xray.amazonaws.com"},
                            "Action": "logs:PutLogEvents",
                            "Resource": [
                                f"{logs_arn}:aws/spans:*",
                                f"{logs_arn}:/aws/application-signals/data:*",
                            ],
                            "Condition": {
                                "ArnLike": {"aws:SourceArn": xray_arn},
                                "StringEquals": {"aws:SourceAccount": self.account},
                            },
                        }
                    ],
                }
            ),
        )

        # 1% of traces are indexed as searchable trace summaries, which is free; all spans are still stored.
        search = xray.CfnTransactionSearchConfig(self, "TransactionSearch", indexing_percentage=1)
        search.node.add_dependency(policy)

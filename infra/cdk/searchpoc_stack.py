"""SearchPOC v7 — Lambda + API Gateway (REST) + API key.

Resources:
  - Lambda function (container image, arm64, 1024 MB, 30 s)
  - IAM role: S3:Get on the artifact bucket, DDB:Get/BatchGet on the
    resolution table, DDB:Scan on the four platter cache tables, CloudWatch
    Logs write.
  - REST API with POST /search behind an API key + usage plan.

The image is built from `lambda/Dockerfile` at the repo root via
`DockerImageAsset`, so a plain `cdk deploy` builds + pushes + wires
everything in one shot.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]

ARTIFACT_BUCKET = "search-item-item-poc"
RESOLUTION_TABLE = "Item-Item-Similarity-Search"
PLATTER_TABLES = [
    "DefaultPlattersTable",
    "DefaultPlattersCategoriesTable",
    "DefaultPlatterItemsTable",
    "MenuItemsTable",
]


class SearchPocStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        # ── Lambda image ─────────────────────────────────────────────────
        image = _lambda.DockerImageCode.from_image_asset(
            directory=str(REPO_ROOT),
            file="lambda/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
            exclude=[
                "infra/cdk/cdk.out/**",
                "infra/cdk/.venv/**",
                "**/__pycache__/**",
                "**/.git/**",
                "**/node_modules/**",
                "**/*.pyc",
                "diagnostics/**",
                "llm_cache/**",
                ".venv/**",
            ],
        )

        fn = _lambda.DockerImageFunction(
            self,
            "SearchV7Function",
            code=image,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=1024,
            timeout=Duration.seconds(30),
            log_retention=logs.RetentionDays.TWO_WEEKS,
            reserved_concurrent_executions=0,
            environment={
                # AWS_REGION is set automatically by the Lambda runtime, and
                # our code reads it via os.getenv. Don't override here.
                "RUNTIME_ARTIFACT_BUCKET": ARTIFACT_BUCKET,
                "RESOLUTION_TABLE": RESOLUTION_TABLE,
            },
        )

        # ── IAM policies — tight scope ───────────────────────────────────
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[
                f"arn:aws:s3:::{ARTIFACT_BUCKET}/*",
            ],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[f"arn:aws:s3:::{ARTIFACT_BUCKET}"],
        ))

        ddb_table_arns = [
            f"arn:aws:dynamodb:{region}:{account}:table/{RESOLUTION_TABLE}",
        ] + [
            f"arn:aws:dynamodb:{region}:{account}:table/{t}" for t in PLATTER_TABLES
        ]
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "dynamodb:GetItem",
                "dynamodb:BatchGetItem",
                "dynamodb:Scan",
            ],
            resources=ddb_table_arns,
        ))

        # ── REST API + API key + usage plan ──────────────────────────────
        api = apigw.LambdaRestApi(
            self,
            "SearchV7Api",
            handler=fn,
            proxy=False,
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
            ),
        )

        search = api.root.add_resource("search")
        search.add_method(
            "POST",
            apigw.LambdaIntegration(fn),
            api_key_required=True,
        )

        api_key = api.add_api_key("SearchV7ApiKey")
        plan = api.add_usage_plan(
            "SearchV7UsagePlan",
            name="SearchV7Default",
            throttle=apigw.ThrottleSettings(rate_limit=20, burst_limit=40),
            quota=apigw.QuotaSettings(limit=100_000, period=apigw.Period.MONTH),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=api.deployment_stage)

        # ── Outputs ──────────────────────────────────────────────────────
        CfnOutput(self, "ApiEndpoint", value=api.url, description="POST {endpoint}/search")
        CfnOutput(self, "ApiKeyId", value=api_key.key_id,
                  description="Retrieve secret with: aws apigateway get-api-key --api-key <id> --include-value")
        CfnOutput(self, "LambdaName", value=fn.function_name)

"""SearchPOC v7 — ECS Fargate + ALB deployment.

Reads VPC/subnet config from CDK context:
  vpc_id           — VPC where RDS lives (required)
  private_subnets  — comma-separated private subnet IDs for the ECS tasks
  public_subnets   — comma-separated public subnet IDs for the ALB

Set via cdk.json or --context at deploy time:
  cdk deploy EcsSearchPocStack \
    --context vpc_id=vpc-0abc123 \
    --context private_subnets=subnet-aaa,subnet-bbb \
    --context public_subnets=subnet-ccc,subnet-ddd

RDS credentials injected as task environment variables. In production,
move these to Secrets Manager and use ecs.Secret.from_secrets_manager().
"""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_iam as iam
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]

ARTIFACT_BUCKET = "search-item-item-poc"
PLATTER_TABLES = [
    "DefaultPlattersTable",
    "DefaultPlattersCategoriesTable",
    "DefaultPlatterItemsTable",
    "MenuItemsTable",
]

RDS_PORT = 3306


class EcsSearchPocStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        # ── VPC (lookup existing — RDS must be in the same VPC) ──────────
        vpc_id: str = self.node.try_get_context("vpc_id") or ""
        if not vpc_id:
            raise ValueError("CDK context 'vpc_id' is required. Pass --context vpc_id=vpc-xxxx")

        vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=vpc_id)

        # ── Container image (same Dockerfile as Lambda) ───────────────────
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "SearchV7Image",
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

        # ── ECS cluster ───────────────────────────────────────────────────
        cluster = ecs.Cluster(self, "SearchCluster", vpc=vpc)

        # ── Task IAM role ─────────────────────────────────────────────────
        task_role = iam.Role(
            self,
            "EcsTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{ARTIFACT_BUCKET}/*"],
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:ListBucket"],
            resources=[f"arn:aws:s3:::{ARTIFACT_BUCKET}"],
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Scan"],
            resources=[
                f"arn:aws:dynamodb:{region}:{account}:table/{t}" for t in PLATTER_TABLES
            ],
        ))

        # ── Fargate service + ALB (via ApplicationLoadBalancedFargateService)
        rds_host = os.getenv("RDS_HOST", "")
        rds_password = os.getenv("RDS_PASSWORD", "")

        service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "SearchFargateService",
            cluster=cluster,
            cpu=1024,
            memory_limit_mib=2048,
            desired_count=1,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_docker_image_asset(image_asset),
                # Bypass Lambda's /lambda-entrypoint.sh and run uvicorn directly.
                # packages are installed to /var/task via --target, so cd there first.
                entry_point=["/bin/sh", "-c"],
                command=["cd /var/task && python -m uvicorn server:app --host 0.0.0.0 --port 8080"],
                container_port=8080,
                task_role=task_role,
                environment={
                    "RUNTIME_ARTIFACT_BUCKET": ARTIFACT_BUCKET,
                    "AWS_REGION": region,
                    "RDS_HOST": rds_host,
                    "RDS_PORT": str(RDS_PORT),
                    "RDS_DB": os.getenv("RDS_DB", "catalog"),
                    "RDS_USER": os.getenv("RDS_USER", "admin"),
                    "RDS_PASSWORD": rds_password,
                    "API_KEY": os.getenv("SEARCH_API_KEY", ""),
                },
            ),
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            assign_public_ip=True,
            public_load_balancer=True,
        )

        # ── Health check ──────────────────────────────────────────────────
        service.target_group.configure_health_check(
            path="/health",
            healthy_http_codes="200",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # ── Allow ECS task SG → RDS on port 3306 ─────────────────────────
        service.service.connections.allow_to(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(RDS_PORT),
            "ECS to RDS MySQL",
        )

        # ── Outputs ───────────────────────────────────────────────────────
        CfnOutput(
            self,
            "AlbDns",
            value=service.load_balancer.load_balancer_dns_name,
            description="ALB DNS — use this as backend URL in API Gateway HTTP integration",
        )
        CfnOutput(self, "EcsServiceName", value=service.service.service_name)
        CfnOutput(self, "EcsClusterName", value=cluster.cluster_name)

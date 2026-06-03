#!/usr/bin/env python3
"""CDK entrypoint for the SearchPOC v7 API."""

import os

import aws_cdk as cdk

from ecs_stack import EcsSearchPocStack
from searchpoc_stack import SearchPocStack

_env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("AWS_REGION", "ap-south-1"),
)

app = cdk.App()
SearchPocStack(app, "SearchPocV7Stack", env=_env)
EcsSearchPocStack(app, "EcsSearchPocStack", env=_env)
app.synth()

#!/usr/bin/env python3
"""CDK アプリのエントリポイント.

デプロイ時に次の context を渡す (リポジトリには固有値を書かない):
  -c github_repo=<owner>/<repo>     CodePipeline のソース (必須)
  -c github_branch=main             省略可 (既定: main)
"""

import os

import aws_cdk as cdk

from infra.agentcore_stack import AgentCoreStack
from infra.eks_stack import EksStack
from infra.pipeline_stack import PipelineStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ["CDK_DEFAULT_ACCOUNT"],
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

github_repo = app.node.try_get_context("github_repo")
if not github_repo or "/" not in github_repo:
    raise ValueError("context 'github_repo' (<owner>/<repo>) を指定してください: -c github_repo=<owner>/<repo>")
github_branch = app.node.try_get_context("github_branch") or "main"

agentcore = AgentCoreStack(app, "AgentCoreDemo-AgentCore", env=env)
eks = EksStack(app, "AgentCoreDemo-Eks", env=env)
PipelineStack(
    app,
    "AgentCoreDemo-Pipeline",
    env=env,
    cluster=eks.cluster,
    app_role=eks.app_role,
    namespace=eks.app_namespace,
    github_repo=github_repo,
    github_branch=github_branch,
)

app.synth()

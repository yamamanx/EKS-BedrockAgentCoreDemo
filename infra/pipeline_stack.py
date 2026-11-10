"""GitHub -> CodeBuild (イメージビルド) -> CodePipeline EKS デプロイアクション (Helm) の CodePipeline.

デプロイには CodeBuild プロジェクトを自前で作らず、CodePipeline 標準の
アクションプロバイダー「Amazon EKS」(デプロイ設定タイプ: Helm) を使う。
参考: https://docs.aws.amazon.com/codepipeline/latest/userguide/action-reference-EKS.html
"""

import aws_cdk as cdk
import jsii
from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as actions
from aws_cdk import aws_codestarconnections as codestar
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_eks as eks
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

# 参考: https://docs.aws.amazon.com/eks/latest/userguide/access-policies.html
CLUSTER_ADMIN_POLICY_ARN = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

# リポジトリ内のパス (HelmValuesFiles はチャートのフォルダからの相対パス)
HELM_CHART_LOCATION = "helm/chat-app"
HELM_VALUES_FILES = "../pipeline-values.yaml"
HELM_RELEASE_NAME = "chat-app"
DEPLOY_STAGE_INDEX = 2


@jsii.implements(codepipeline.IAction)
class EksHelmDeployAction:
    """CodePipeline の「Amazon EKS」デプロイアクション (Helm) を宣言する L2 相当のアクション.

    CDK に L2 が無いため IAction を実装している。
    環境変数 (EnvironmentVariables) はアクション宣言側のプロパティなので、
    Pipeline を作成した後に add_environment_variables() で CfnPipeline に反映する.
    """

    def __init__(
        self,
        *,
        action_name: str,
        input: codepipeline.Artifact,
        cluster: eks.ICluster,
        pipeline_name: str,
        role: iam.IRole,
        release_name: str,
        chart_location: str,
        values_files: str,
        namespace: str,
    ) -> None:
        self._props = codepipeline.ActionProperties(
            action_name=action_name,
            category=codepipeline.ActionCategory.DEPLOY,
            owner="AWS",
            provider="EKS",
            version="1",
            artifact_bounds=codepipeline.ActionArtifactBounds(
                min_inputs=1, max_inputs=1, min_outputs=0, max_outputs=0
            ),
            inputs=[input],
            # ドキュメントの手順どおり、パイプラインのサービスロールで実行する (Access Entry もこのロール)
            role=role,
        )
        self._cluster = cluster
        self._pipeline_name = pipeline_name
        self._configuration = {
            "ClusterName": cluster.cluster_name,
            "HelmReleaseName": release_name,
            "HelmChartLocation": chart_location,
            "HelmValuesFiles": values_files,
            "Namespace": namespace,
        }

    @property
    def action_properties(self) -> codepipeline.ActionProperties:
        return self._props

    def bind(self, scope, stage, *, bucket, role) -> codepipeline.ActionConfig:
        # このアクションは CDK が作成する「アクションロール」で実行される。
        # 権限と EKS Access Entry は、そのロールに対して付与する。
        # 参考: action-reference-EKS.html の「Service role policy permissions」
        bucket.grant_read(role)
        stack = Stack.of(scope)
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="EksDescribeCluster",
                actions=["eks:DescribeCluster"],
                resources=[self._cluster.cluster_arn],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="EksActionVpcLookup",
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:CreateNetworkInterfacePermission",
                    "ec2:DescribeDhcpOptions",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                    "ec2:DescribeRouteTables",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeVpcs",
                ],
                resources=["*"],
            )
        )
        log_group_arn = f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/codepipeline/{self._pipeline_name}"
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="EksActionLogs",
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group_arn, f"{log_group_arn}:*"],
            )
        )
        eks.CfnAccessEntry(
            scope,
            "ActionAccessEntry",
            cluster_name=self._cluster.cluster_name,
            principal_arn=role.role_arn,
            access_policies=[
                eks.CfnAccessEntry.AccessPolicyProperty(
                    policy_arn=CLUSTER_ADMIN_POLICY_ARN,
                    access_scope=eks.CfnAccessEntry.AccessScopeProperty(type="cluster"),
                )
            ],
        )
        return codepipeline.ActionConfig(configuration=self._configuration)

    def on_state_change(self, name, target=None, *, description=None, event_pattern=None, rule_name=None, schedule=None, enabled=None, event_bus=None, cross_stack_scope=None, dead_letter_queue=None, message=None, retry_attempts=None, max_event_age=None, role=None):  # noqa: ANN001
        raise NotImplementedError("on_state_change is not supported for the EKS deploy action")


class PipelineStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster: eks.ICluster,
        app_role: iam.IRole,
        namespace: str,
        github_repo: str,
        github_branch: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # ECR / アーティファクトバケット (デモ後に一括削除できるよう DESTROY)
        # ---------------------------------------------------------------
        repository = ecr.Repository(
            self,
            "AppRepository",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
        )
        artifact_bucket = s3.Bucket(
            self,
            "ArtifactBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---------------------------------------------------------------
        # GitHub 接続 (デプロイ後にコンソールで「保留中の接続を更新」して承認する)
        # ---------------------------------------------------------------
        connection = codestar.CfnConnection(
            self,
            "GitHubConnection",
            connection_name="agentcore-demo-repo",
            provider_type="GitHub",
        )

        # ---------------------------------------------------------------
        # ビルド: Docker イメージをビルドして ECR にプッシュ (CodeBuild)
        # ---------------------------------------------------------------
        build_project = codebuild.PipelineProject(
            self,
            "BuildProject",
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec/build.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,  # docker build に必要
            ),
            environment_variables={
                "ECR_REPOSITORY_URI": codebuild.BuildEnvironmentVariable(value=repository.repository_uri),
            },
        )
        repository.grant_pull_push(build_project)

        # ---------------------------------------------------------------
        # パイプライン: Source -> Build -> Deploy (Amazon EKS アクション / Helm)
        # ---------------------------------------------------------------
        source_output = codepipeline.Artifact("Source")

        build_action = actions.CodeBuildAction(
            action_name="BuildImage",
            project=build_project,
            input=source_output,
            variables_namespace="BuildVars",  # buildspec が export した IMAGE_TAG を後続で参照する
        )
        # ロールのポリシーでパイプライン名を参照するため、循環参照を避けて固定名にする
        pipeline_name = "agentcore-demo-pipeline"
        pipeline_role = iam.Role(
            self,
            "PipelineServiceRole",
            assumed_by=iam.ServicePrincipal("codepipeline.amazonaws.com"),
            description="CodePipeline service role (also used by the EKS deploy action)",
        )
        deploy_action = EksHelmDeployAction(
            action_name="HelmDeploy",
            input=source_output,
            cluster=cluster,
            pipeline_name=pipeline_name,
            role=pipeline_role,
            release_name=HELM_RELEASE_NAME,
            chart_location=HELM_CHART_LOCATION,
            values_files=HELM_VALUES_FILES,
            namespace=namespace,
        )

        pipeline = codepipeline.Pipeline(
            self,
            "Pipeline",
            pipeline_name=pipeline_name,
            role=pipeline_role,
            pipeline_type=codepipeline.PipelineType.V2,  # EKS アクションは V2 のみ
            artifact_bucket=artifact_bucket,
            stages=[
                codepipeline.StageProps(
                    stage_name="Source",
                    actions=[
                        actions.CodeStarConnectionsSourceAction(
                            action_name="GitHub",
                            connection_arn=connection.attr_connection_arn,
                            owner=github_repo.split("/", 1)[0],
                            repo=github_repo.split("/", 1)[1],
                            branch=github_branch,
                            output=source_output,
                        )
                    ],
                ),
                codepipeline.StageProps(stage_name="Build", actions=[build_action]),
                codepipeline.StageProps(stage_name="Deploy", actions=[deploy_action]),
            ],
        )

        # Helm の values ($IMAGE_REPOSITORY など) に埋め込む環境変数。
        # helm/pipeline-values.yaml がこれらを参照する。
        env_vars = {
            "IMAGE_REPOSITORY": repository.repository_uri,
            "IMAGE_TAG": build_action.variable("IMAGE_TAG"),
            "APP_ROLE_ARN": app_role.role_arn,
            "AWS_REGION": self.region,
        }
        cfn_pipeline = pipeline.node.default_child
        cfn_pipeline.add_property_override(
            f"Stages.{DEPLOY_STAGE_INDEX}.Actions.0.EnvironmentVariables",
            [{"Name": k, "Value": v, "Type": "PLAINTEXT"} for k, v in env_vars.items()],
        )

        CfnOutput(self, "PipelineName", value=pipeline.pipeline_name)
        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "ConnectionArn", value=connection.attr_connection_arn)
        cdk.Tags.of(self).add("Project", "EKS-BedrockAgentCoreDemo")

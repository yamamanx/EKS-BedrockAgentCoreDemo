"""EKS クラスター (Fargate) + AWS Load Balancer Controller + アプリ用 IRSA ロール."""

import aws_cdk as cdk
from aws_cdk import CfnJson, CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks as eks
from aws_cdk import aws_iam as iam
from aws_cdk.lambda_layer_kubectl_v35 import KubectlV35Layer
from constructs import Construct

APP_NAMESPACE = "demo"
APP_SERVICE_ACCOUNT = "chat-app"


class EksStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.app_namespace = APP_NAMESPACE

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,  # デモ用に 1 つだけ (コスト削減)
        )

        # ---------------------------------------------------------------
        # EKS (Fargate のみ)。ノードグループは作らない。
        # デプロイ用ロールには Access Entry で権限を付与する (PipelineStack 側)。
        # ---------------------------------------------------------------
        self.cluster = eks.FargateCluster(
            self,
            "Cluster",
            version=eks.KubernetesVersion.V1_35,
            kubectl_layer=KubectlV35Layer(self, "KubectlLayer"),
            vpc=vpc,
            authentication_mode=eks.AuthenticationMode.API_AND_CONFIG_MAP,
            alb_controller=eks.AlbControllerOptions(
                # Fargate では IMDS が使えないが、CDK が region / vpcId を Helm values に自動設定する
                version=eks.AlbControllerVersion.V3_2_2,
            ),
        )

        # アプリ用 Namespace を Fargate で動かす
        self.cluster.add_fargate_profile(
            "AppProfile",
            selectors=[eks.Selector(namespace=APP_NAMESPACE)],
        )

        # EKS デプロイアクションが Namespace を作成するとは限らないため、事前に作成しておく
        self.cluster.add_manifest(
            "AppNamespace",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": APP_NAMESPACE},
            },
        )

        # 手元の kubectl 用: 任意で管理者を追加する (-c cluster_admin_arn=<IAM role/user ARN>)
        admin_arn = self.node.try_get_context("cluster_admin_arn")
        if admin_arn:
            eks.CfnAccessEntry(
                self,
                "ClusterAdminAccessEntry",
                cluster_name=self.cluster.cluster_name,
                principal_arn=admin_arn,
                access_policies=[
                    eks.CfnAccessEntry.AccessPolicyProperty(
                        policy_arn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                        access_scope=eks.CfnAccessEntry.AccessScopeProperty(type="cluster"),
                    )
                ],
            )

        # ---------------------------------------------------------------
        # アプリ Pod 用 IAM ロール (IRSA)。
        # Fargate は EKS Pod Identity 非対応のため IRSA を使う。
        # ServiceAccount は Helm チャートが作成し、このロール ARN を注釈で渡す。
        # ---------------------------------------------------------------
        provider = self.cluster.open_id_connect_provider
        issuer = self.cluster.cluster_open_id_connect_issuer
        conditions = CfnJson(
            self,
            "AppRoleConditions",
            value={
                f"{issuer}:sub": f"system:serviceaccount:{APP_NAMESPACE}:{APP_SERVICE_ACCOUNT}",
                f"{issuer}:aud": "sts.amazonaws.com",
            },
        )
        self.app_role = iam.Role(
            self,
            "AppRole",
            assumed_by=iam.WebIdentityPrincipal(
                provider.open_id_connect_provider_arn,
                {"StringEquals": conditions},
            ),
            description="Role for the chat app pods to invoke the AgentCore harness (IRSA)",
        )
        self.app_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeHarness",
                actions=["bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"],
                # ハーネス ARN はデモ中に決まるため、アカウント内の harness / runtime に限定する
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:harness/*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*",
                ],
            )
        )

        CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        CfnOutput(self, "AppRoleArn", value=self.app_role.role_arn)
        cdk.Tags.of(self).add("Project", "EKS-BedrockAgentCoreDemo")

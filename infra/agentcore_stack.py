"""AgentCore の周辺リソース (Gateway / ターゲット Lambda / Memory / ハーネス実行ロール).

ハーネス本体はデモ中にマネジメントコンソールで作成するため、ここには含めない。
コンソールで選択する Gateway・Memory・実行ロールをこのスタックで事前に用意する。
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambda" / "matsusaka_search"
AGENTCORE_PRINCIPAL = iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")


def _string(description: str) -> agentcore.CfnGatewayTarget.SchemaDefinitionProperty:
    return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type="string", description=description)


def _integer(description: str) -> agentcore.CfnGatewayTarget.SchemaDefinitionProperty:
    return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type="integer", description=description)


def _search_tool(name: str, description: str, keyword_help: str) -> agentcore.CfnGatewayTarget.ToolDefinitionProperty:
    return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
        name=name,
        description=description,
        input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
            type="object",
            properties={
                "keyword": _string(keyword_help),
                "limit": _integer("返す最大件数 (1〜10、省略時は5)"),
            },
            required=[],
        ),
    )


class AgentCoreStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------
        # ターゲット Lambda (松阪市オープンデータ検索)
        # ---------------------------------------------------------------
        search_function = lambda_.Function(
            self,
            "MatsusakaSearchFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR)),
            timeout=Duration.seconds(30),
            memory_size=256,
            description="松阪市の観光施設・文化施設を SPARQL で検索する AgentCore Gateway ターゲット",
            log_group=logs.LogGroup(
                self,
                "SearchFunctionLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )

        # ---------------------------------------------------------------
        # Gateway (IAM 認証 / MCP)
        # ---------------------------------------------------------------
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=AGENTCORE_PRINCIPAL,
            description="Role for AgentCore Gateway to invoke the target Lambda function",
        )
        search_function.grant_invoke(gateway_role)

        gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name="matsusaka-gateway",
            description="松阪市の観光施設・文化施設を検索するツールを提供する Gateway",
            protocol_type="MCP",
            authorizer_type="AWS_IAM",
            role_arn=gateway_role.role_arn,
        )

        agentcore.CfnGatewayTarget(
            self,
            "GatewayTarget",
            name="matsusaka-api",
            description="松阪市オープンデータ (Linked Data API Navi) の検索",
            gateway_identifier=gateway.attr_gateway_identifier,
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=search_function.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                _search_tool(
                                    "search_tourism",
                                    "松阪市の観光施設 (城跡・屋敷・道の駅・駅など) をキーワードで検索する。"
                                    "施設名・説明・住所・電話番号・HPリンク・緯度経度を返す。"
                                    "キーワードを省略すると一覧を返す。",
                                    "施設名・説明・住所に含まれる語 (例: 松阪牛、城、駅)",
                                ),
                                _search_tool(
                                    "search_cultural_property",
                                    "松阪市の見学可能な文化施設・文化財をキーワードで検索する。"
                                    "名称・住所・電話番号・HPリンク・緯度経度を返す。"
                                    "キーワードを省略すると一覧を返す。",
                                    "名称・住所に含まれる語 (例: 神社、旧家、用水)",
                                ),
                            ]
                        ),
                    )
                )
            ),
        )

        # ---------------------------------------------------------------
        # Memory (長期メモリ: 要約 / ユーザー嗜好 / セマンティック)
        # ---------------------------------------------------------------
        memory = agentcore.CfnMemory(
            self,
            "Memory",
            name="MatsusakaDemoMemory",
            description="松阪観光デモ用のエージェントメモリ",
            event_expiry_duration=30,  # 日
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    summary_memory_strategy=agentcore.CfnMemory.SummaryMemoryStrategyProperty(
                        name="SessionSummarizer",
                        namespaces=["/strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}"],
                    )
                ),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name="UserPreferences",
                        namespaces=["/strategies/{memoryStrategyId}/actors/{actorId}"],
                    )
                ),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="SemanticFacts",
                        namespaces=["/strategies/{memoryStrategyId}/actors/{actorId}"],
                    )
                ),
            ],
        )

        # ---------------------------------------------------------------
        # ハーネス実行ロール (コンソールのハーネス作成画面で選択する)
        # 参考: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html
        # ---------------------------------------------------------------
        harness_role = iam.Role(
            self,
            "HarnessExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Execution role for the AgentCore harness (demo)",
        )
        region, account = self.region, self.account
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockModelInvocation",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:{region}:{account}:*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInferenceProfileCrossRegion",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[f"arn:aws:bedrock:*:{account}:inference-profile/*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPublicPull",
                actions=["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"],
                resources=["*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="XRayTracingAccess",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        runtime_log_group = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[runtime_log_group, f"{runtime_log_group}:log-stream:*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsDescribe",
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsPutResourcePolicy",
                actions=["logs:PutResourcePolicy"],
                resources=["*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchMetricsPublish",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreWorkloadIdentity",
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*",
                ],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreBuiltInTools",
                actions=[
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:GetBrowserSession",
                    "bedrock-agentcore:ListBrowserSessions",
                    "bedrock-agentcore:UpdateBrowserStream",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    "bedrock-agentcore:ConnectBrowserLiveViewStream",
                    "bedrock-agentcore:StartCodeInterpreterSession",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:GetCodeInterpreterSession",
                    "bedrock-agentcore:ListCodeInterpreterSessions",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:aws:browser/*",
                    f"arn:aws:bedrock-agentcore:{region}:aws:code-interpreter/*",
                ],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemory",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                resources=[memory.attr_memory_arn, f"arn:aws:bedrock-agentcore:{region}:{account}:memory/harness_*"],
            )
        )
        harness_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreGateway",
                actions=["bedrock-agentcore:InvokeGateway", "bedrock-agentcore:GetGateway"],
                resources=[gateway.attr_gateway_arn],
            )
        )

        # ---------------------------------------------------------------
        # Outputs (README のハーネス作成手順で参照する)
        # ---------------------------------------------------------------
        CfnOutput(self, "GatewayArn", value=gateway.attr_gateway_arn)
        CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        CfnOutput(self, "MemoryArn", value=memory.attr_memory_arn)
        CfnOutput(self, "HarnessExecutionRoleArn", value=harness_role.role_arn)
        cdk.Tags.of(self).add("Project", "EKS-BedrockAgentCoreDemo")

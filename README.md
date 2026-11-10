# EKS x Amazon Bedrock AgentCore ハーネス デモ

Amazon Bedrock AgentCore の **ハーネス** をマネジメントコンソールで作成し、
EKS (Fargate) 上のチャット Web アプリから呼び出すデモです。
松阪市のオープンデータ (観光施設・見学可能な文化施設) を AgentCore Gateway 経由で検索できます。

```
GitHub ──push──▶ CodePipeline ──▶ CodeBuild (docker build / ECR push)
                      │
                      └──▶ EKS デプロイアクション (Helm) ──▶ EKS (Fargate) ──▶ ALB ──▶ ブラウザ
                                                             │
                                                             └─ InvokeHarness ─▶ AgentCore ハーネス
                                                                                   ├─ Memory (要約 / ユーザー嗜好 / セマンティック)
                                                                                   └─ Gateway ─▶ Lambda ─▶ 松阪市 SPARQL API
```

## リポジトリ構成

| パス | 内容 |
| --- | --- |
| `app.py`, `infra/` | AWS CDK (Python)。3 スタックに分かれています |
| `infra/agentcore_stack.py` | Gateway / ターゲット Lambda / Memory / ハーネス実行ロール |
| `infra/eks_stack.py` | VPC, EKS (Fargate), AWS Load Balancer Controller, アプリ用 IRSA ロール |
| `infra/pipeline_stack.py` | ECR, CodeBuild, CodePipeline (EKS デプロイアクション / Helm) |
| `lambda/matsusaka_search/` | Gateway ターゲット Lambda (松阪市 SPARQL API の検索) |
| `app/` | チャット Web アプリ (FastAPI + 静的 HTML) と Dockerfile |
| `helm/chat-app/` | Helm チャート。`values.yaml` の `harness.arn` だけデモ中に書き換えます |
| `helm/pipeline-values.yaml` | パイプラインが環境変数で置換して渡す values の上書きファイル |
| `buildspec/build.yml` | イメージのビルドと ECR への push |

## 事前準備 (デモ前に 1 回)

前提: AWS CLI、Node.js、Python 3.10 以上、Helm (チャートの確認用)、AWS 認証情報、`cdk bootstrap` 済みのアカウント。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install                      # CDK CLI を node_modules に入れる

# 初回のみ: CDK のブートストラップ
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# デプロイ (github_repo は自分のリポジトリ <owner>/<repo>)
npx cdk deploy --all -c github_repo=<owner>/<repo>
```

オプション:

- `-c github_branch=main` : パイプラインが監視するブランチ (既定: `main`)
- `-c cluster_admin_arn=<IAM ロール/ユーザーの ARN>` : 手元の `kubectl` から操作できるよう、EKS の管理者 (Access Entry) に追加します

デプロイ後の作業:

1. **GitHub 接続の承認** (1 回だけ必要)
   - コンソールの [Developer Tools > 設定 > 接続](https://console.aws.amazon.com/codesuite/settings/connections?region=us-east-1) を開きます。
   - 状態が「保留中」の `agentcore-demo-repo` を選び、「保留中の接続を更新」から GitHub を認可します。
2. **コードを GitHub にプッシュ** します。パイプラインが起動します。
   - この時点では `harness.arn` が空のため、Pod は起動しますが、チャットはエラーを返します。
3. スタックの出力 (ハーネス作成時に使います) を控えておきます。

```bash
aws cloudformation describe-stacks --stack-name AgentCoreDemo-AgentCore \
  --query 'Stacks[0].Outputs' --output table
```

## デモ当日の流れ

### 1. ハーネスの作成 (マネジメントコンソール)

Amazon Bedrock AgentCore > ハーネス > 「ハーネスを作成」。

| 項目 | 設定 |
| --- | --- |
| ハーネス名 | 任意 (例: `MatsusakaGuide`) |
| モデル | 任意の Claude (例: Anthropic Claude Sonnet) |
| システムプロンプト | 下の「システムプロンプト」を貼り付け |
| 実行ロール | スタック出力 `HarnessExecutionRoleArn` のロールを選択 |
| Memory | 既存の `MatsusakaDemoMemory` を選択 (長期メモリ: 要約・ユーザー嗜好・セマンティック) |
| Gateway | 既存の `matsusaka-gateway` を選択 (ターゲット `matsusaka-api` に 2 つのツールがあります) |

作成中に全体アーキテクチャを説明します。状態が `READY` になったら、ハーネスの **ARN** をコピーします。

> このデモのアプリは IAM (SigV4) で呼び出します。ハーネスの認証方式は IAM のままにしてください。

### 2. ARN を反映してデプロイ

`helm/chat-app/values.yaml` の `harness.arn` に貼り付けて、コミット、プッシュします。

```yaml
harness:
  arn: "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:harness/<HARNESS_ID>"
```

パイプラインの流れ:

1. Source: GitHub からコードを取得
2. Build: CodeBuild がコンテナイメージをビルドし、ECR へ push (タグはコミット ID の先頭 12 文字)
3. Deploy: CodePipeline の「Amazon EKS」アクション (Helm) が、`helm/chat-app` を `chat-app` リリースとしてデプロイ。
   イメージ URI は `helm/pipeline-values.yaml` の `$IMAGE_REPOSITORY` と `$IMAGE_TAG` が環境変数で置換されて渡ります。

### 3. 動作確認

```bash
kubectl get ingress -n demo      # ALB の DNS 名を確認
```

ブラウザで ALB の DNS 名 (`http://...`) を開き、チャット画面から質問します。

質問例:

- 松阪城跡の近くで見学できる場所を教えて
- 松阪牛が食べられる観光施設はある?
- 電話番号と住所も教えて

## システムプロンプト

```text
あなたは三重県松阪市の観光案内を担当する、親切で簡潔なガイドです。

# 役割
- 松阪市の観光施設や、見学可能な文化施設・文化財について、利用者の質問に日本語で答えます。
- 施設の情報は、必ずツール (search_tourism / search_cultural_property) で検索した結果にもとづいて答えます。
  推測や記憶で施設名・住所・電話番号を作らないでください。
- 検索結果が 0 件のときは、その旨を伝え、別のキーワードで検索し直すか、質問を言い換えてもらうよう案内します。

# ツールの使い分け
- search_tourism: 観光施設 (城跡、屋敷、道の駅、駅など)。施設名・説明・住所で検索できます。
- search_cultural_property: 見学可能な文化施設・文化財 (神社、旧家、用水など)。名称・住所で検索できます。
- 広い質問のときは、両方を検索して結果をまとめてください。
- keyword には短い名詞 (例: 「城」「駅」「神社」) を使ってください。

# 回答のしかた
- 最初に結論を 1〜2 文で述べ、その後に施設を箇条書きで紹介します。
- 各施設について、わかる範囲で名称・住所・電話番号・公式サイト (HPリンク) を載せます。
- 利用者の好み (例: 歴史が好き、食べ歩きが好き) を話の中で聞いたら覚えておき、次回以降の提案に活かします。
- 営業時間や料金など、ツールで得られない情報は「公式サイトでご確認ください」と案内します。
- 松阪市以外の質問や、観光と無関係な質問には、丁寧に対応範囲を説明します。
```

## ローカルでの確認

```bash
# Lambda のロジック (松阪市 API を実際に呼び出します)
cd lambda/matsusaka_search && python3 -c "import handler; print(handler.build_query(handler.DATASETS['search_tourism'], '城', 3))"

# Helm チャートの検証
helm lint helm/chat-app --set image.repository=x --set image.tag=y
```

## 後片付け

1. コンソールで作成した **ハーネスを削除** します (CDK の管理対象外です)。
2. ALB は AWS Load Balancer Controller が作成したもので、CloudFormation の管理外です。
   VPC を削除する前に、Ingress を削除して ALB の削除を待ちます。
   (`-c cluster_admin_arn=...` を付けてデプロイしておくと `kubectl` で操作できます。)

   ```bash
   kubectl delete ingress chat-app -n demo
   ```

3. スタックを削除します。

   ```bash
   npx cdk destroy --all -c github_repo=<owner>/<repo>
   ```

ECR リポジトリとアーティファクト用 S3 バケットは、スタック削除時に中身ごと削除されます。

## データの出典

このデモは、次の 2 つの松阪市のオープンデータを、[Linked Data API Navi](https://uedayou.net/ldapinavi/) が公開する
SPARQL エンドポイント経由で利用しています。

| データセット | エンドポイント |
| --- | --- |
| [松阪市 観光施設 (Tourism_list)](https://uedayou.net/ldapinavi/linkdata/rdf1s8727i/Tourism_list) | `https://uedayou.net/ldapinavi/api/rdf1s8727i/Tourism_list/sparql` |
| [松阪市 見学可能な文化施設 (cultural_property)](https://uedayou.net/ldapinavi/linkdata/rdf1s8728i/cultural_property) | `https://uedayou.net/ldapinavi/api/rdf1s8728i/cultural_property/sparql` |

- 提供元: 松阪市企画振興部デジタル未来戦略局市政改革課
- ライセンス: [クリエイティブ・コモンズ 表示 3.0 (CC BY 3.0)](https://creativecommons.org/licenses/by/3.0/deed.ja)
  (データセット自身のメタデータ `cc:license` / `cc:attributionName` に記載されている内容です)
- このリポジトリのコード (MIT-0) とデータのライセンスは別です。データを再配布・加工して公開する場合は、
  CC BY 3.0 に従って出典を表示してください。チャット画面のフッターにも出典を表示しています。

## ライセンス

このリポジトリのコードは [MIT-0](LICENSE) で公開しています。松阪市のデータは上記のとおり CC BY 3.0 です。

## 注意

- ALB は既定で `0.0.0.0/0` に公開されます (HTTP のみ)。デモ後は `helm/chat-app/values.yaml` の
  `ingress.inboundCidrs` を絞るか、環境を削除してください。LLM を呼び出すエンドポイントなので、
  公開したまま放置すると利用料金が発生します。
- アプリが `InvokeHarness` に渡すのは、メッセージ、セッション ID、ユーザー ID だけです。
  モデルやツールの上書きは受け付けません。
- 松阪市のデータの出典とライセンスは、下の「データの出典」を参照してください。
- Fargate は EKS Pod Identity に対応していないため、Pod の権限は IRSA で付与しています。

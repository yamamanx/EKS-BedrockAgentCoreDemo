"""AgentCore ハーネスを呼び出すチャット Web アプリ (FastAPI).

環境変数:
  HARNESS_ARN  呼び出すハーネスの ARN (必須)
  AWS_REGION   ハーネスのリージョン (既定: us-east-1)
  QUALIFIER    ハーネスのエンドポイント名 (任意。省略時は DEFAULT)
"""

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Iterator

import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("chat-app")
logging.basicConfig(level=logging.INFO)

HARNESS_ARN = os.environ.get("HARNESS_ARN", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
QUALIFIER = os.environ.get("QUALIFIER", "")
STATIC_DIR = Path(__file__).parent / "static"

# 呼び出しに使うのは「メッセージ」だけ。モデルやツールなどの上書きは受け付けない
# (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)。
MAX_MESSAGE_LENGTH = 2000
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

client = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=Config(read_timeout=300, retries={"max_attempts": 2, "mode": "standard"}),
)

app = FastAPI(title="AgentCore Harness Chat", docs_url=None, redoc_url=None, openapi_url=None)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    session_id: str = Field(description="ブラウザごとの会話 ID")
    actor_id: str = Field(description="ブラウザごとの匿名ユーザー ID (メモリの名前空間に使う)")


def _runtime_session_id(session_id: str) -> str:
    """AgentCore のセッション ID は 33 文字以上が必要なため、ハッシュで 64 文字に整える."""
    return hashlib.sha256(session_id.encode()).hexdigest()


def _stream_harness(request: ChatRequest) -> Iterator[str]:
    params = {
        "harnessArn": HARNESS_ARN,
        "runtimeSessionId": _runtime_session_id(request.session_id),
        "actorId": request.actor_id,
        "messages": [{"role": "user", "content": [{"text": request.message}]}],
    }
    if QUALIFIER:
        params["qualifier"] = QUALIFIER

    try:
        response = client.invoke_harness(**params)
        for event in response["stream"]:
            if "contentBlockDelta" in event:
                text = event["contentBlockDelta"].get("delta", {}).get("text")
                if text:
                    yield text
            elif "runtimeClientError" in event:
                logger.error("runtimeClientError: %s", event["runtimeClientError"])
                yield "\n\n[エラー] ハーネスの実行中にエラーが発生しました。"
    except Exception:  # noqa: BLE001 - ストリーム途中の失敗もユーザーに返す
        logger.exception("invoke_harness failed")
        yield "\n\n[エラー] ハーネスの呼び出しに失敗しました。時間をおいて再度お試しください。"


NOT_CONFIGURED_MESSAGE = (
    "ハーネス ARN が設定されていません。"
    "helm/chat-app/values.yaml の harness.id にハーネスの ID を設定して、再デプロイしてください。"
)


@app.get("/healthz")
def healthz() -> dict:
    # HARNESS_ARN が未設定でも Pod は Ready にする (画面でエラーを表示するため)
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict:
    """画面が起動時に呼び出す. ARN 自体は返さない."""
    if not HARNESS_ARN:
        return {"harness_configured": False, "message": NOT_CONFIGURED_MESSAGE}
    return {"harness_configured": True, "message": ""}


@app.post("/api/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    if not HARNESS_ARN:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED_MESSAGE)
    if not ID_PATTERN.match(request.session_id) or not ID_PATTERN.match(request.actor_id):
        raise HTTPException(status_code=400, detail="session_id / actor_id の形式が不正です")
    return StreamingResponse(
        _stream_harness(request),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

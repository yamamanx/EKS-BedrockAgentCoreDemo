"""AgentCore Gateway ターゲット用 Lambda: 松阪市のオープンデータ検索.

Linked Data API Navi が公開する SPARQL エンドポイントを呼び出す。
  - 観光施設 (Tourism_list)
  - 見学可能な文化施設 (cultural_property)

Gateway は event に「ツール入力 (inputSchema の properties)」をそのまま渡し、
context.client_context.custom["bedrockAgentCoreToolName"] に
"<ターゲット名>___<ツール名>" 形式でツール名を渡す。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_BASE = "https://uedayou.net/ldapinavi/api"
TOOL_DELIMITER = "___"
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
MAX_KEYWORD_LENGTH = 50
HTTP_TIMEOUT_SECONDS = 15

# データセットごとの設定。
# fields: 出力キー -> (プロパティ名, 必須かどうか)
DATASETS = {
    "search_tourism": {
        "endpoint": f"{API_BASE}/rdf1s8727i/Tourism_list/sparql",
        "property_ns": "http://linkdata.org/property/rdf1s8727i#",
        "name_field": "施設名",
        "fields": ["施設名", "説明", "住所", "電話番号", "HPリンク"],
        "search_fields": ["施設名", "説明", "住所"],
    },
    "search_cultural_property": {
        "endpoint": f"{API_BASE}/rdf1s8728i/cultural_property/sparql",
        "property_ns": "http://linkdata.org/property/rdf1s8728i#",
        "name_field": "名称",
        "fields": ["名称", "住所", "電話番号", "HPリンク"],
        "search_fields": ["名称", "住所"],
    },
}
GEO_LAT = "http://www.w3.org/2003/01/geo/wgs84_pos#lat"
GEO_LONG = "http://www.w3.org/2003/01/geo/wgs84_pos#long"


def _escape_sparql_string(value: str) -> str:
    """SPARQL の文字列リテラルに埋め込めるようエスケープする."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )


def _property_iri(namespace: str, name: str) -> str:
    # ストア内のプロパティ IRI は日本語のまま (パーセントエンコードしない) 登録されている
    return f"<{namespace}{name}>"


def build_query(dataset: dict, keyword: str, limit: int) -> str:
    """キーワード部分一致 (空なら全件) の SELECT クエリを組み立てる."""
    ns = dataset["property_ns"]
    variables = {name: f"?v{i}" for i, name in enumerate(dataset["fields"])}

    patterns = []
    for name in dataset["fields"]:
        triple = f"?s {_property_iri(ns, name)} {variables[name]}"
        if name == dataset["name_field"]:
            patterns.append(f"{triple} .")
        else:
            patterns.append(f"OPTIONAL {{ {triple} }}")
    patterns.append(f"OPTIONAL {{ ?s <{GEO_LAT}> ?lat }}")
    patterns.append(f"OPTIONAL {{ ?s <{GEO_LONG}> ?long }}")

    filter_clause = ""
    if keyword:
        literal = _escape_sparql_string(keyword)
        targets = ", ".join(
            f'COALESCE(STR({variables[name]}), "")' for name in dataset["search_fields"]
        )
        filter_clause = f'FILTER(CONTAINS(CONCAT({targets}), "{literal}"))'

    select_vars = " ".join(variables.values()) + " ?lat ?long"
    body = "\n  ".join(patterns)
    return (
        f"SELECT {select_vars} WHERE {{\n  {body}\n  {filter_clause}\n}} "
        f"ORDER BY {variables[dataset['name_field']]} LIMIT {limit}"
    )


def run_query(endpoint: str, query: str) -> list:
    url = f"{endpoint}?{urllib.parse.urlencode({'query': query})}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/sparql-results+json"}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.load(response)
    return payload["results"]["bindings"]


def search(tool_name: str, event: dict) -> dict:
    dataset = DATASETS[tool_name]

    keyword = str(event.get("keyword") or "").strip()[:MAX_KEYWORD_LENGTH]
    try:
        limit = int(event.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    query = build_query(dataset, keyword, limit)
    logger.info("tool=%s keyword=%r limit=%d", tool_name, keyword, limit)
    bindings = run_query(dataset["endpoint"], query)

    results = []
    for row in bindings:
        item = {}
        for i, name in enumerate(dataset["fields"]):
            cell = row.get(f"v{i}")
            if cell:
                item[name] = cell["value"]
        if "lat" in row and "long" in row:
            item["緯度"] = row["lat"]["value"]
            item["経度"] = row["long"]["value"]
        results.append(item)

    return {"keyword": keyword, "count": len(results), "results": results}


def lambda_handler(event, context):
    original_name = context.client_context.custom["bedrockAgentCoreToolName"]
    tool_name = original_name.split(TOOL_DELIMITER, 1)[-1]

    if tool_name not in DATASETS:
        return {"error": f"unknown tool: {tool_name}"}

    try:
        return search(tool_name, event or {})
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
        logger.exception("search failed")
        return {"error": f"検索APIの呼び出しに失敗しました: {type(exc).__name__}"}

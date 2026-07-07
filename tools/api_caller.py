"""API 调用 MCP Server — 给 agent 提供统一的 HTTP 调用能力"""

import json
import time
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("API调用器")


@mcp.tool()
def call_api(
    url: str,
    method: str = "GET",
    params: str | None = None,
    headers: str | None = None,
    body: str | None = None,
) -> str:
    """调用 HTTP API 并返回响应。

    参数:
        url: 接口地址，如 https://api.example.com/v1/monitor
        method: HTTP 方法，GET POST PUT DELETE，默认 GET
        params: URL 参数，JSON 字符串格式如 '{"key":"value"}'
        headers: 请求头，JSON 字符串格式如 '{"Authorization":"Bearer xxx"}'
        body: 请求体，JSON 字符串格式（POST/PUT 时使用）

    返回时自动标注接口、调用时间和状态码。
    如果要并行调用多个接口，在一个 turn 内同时发起多个 call_api 调用。
    """
    import requests as r

    start = time.time()
    try:
        kwargs = {}
        if params:
            kwargs["params"] = json.loads(params)
        if headers:
            kwargs["headers"] = json.loads(headers)
        if body and method in ("POST", "PUT", "PATCH"):
            kwargs["json"] = json.loads(body)

        resp = r.request(method, url, timeout=30, **kwargs)
        elapsed = time.time() - start

        result = {
            "url": url,
            "method": method,
            "status": resp.status_code,
            "elapsed": f"{elapsed:.2f}s",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "body": resp.text[:3000],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "url": url,
            "method": method,
            "error": str(e),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()

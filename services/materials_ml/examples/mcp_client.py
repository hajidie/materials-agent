"""Standalone MCP client; scope is transport context and never a tool argument.

Set ML_MCP_TOKEN in this client's environment. Resource/Worker credentials are not used.
Pass --arguments as a JSON object and a stable --key for training/prediction retries.
"""
import argparse
import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def call(args):
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict) or "scope_id" in arguments:
        raise ValueError("Scope belongs to trusted transport context")
    mutating = args.tool in ("train_tabular_regression", "predict_with_model")
    if mutating and not args.key:
        raise ValueError("A stable --key is required")
    headers = {"Authorization": "Bearer " + os.environ["ML_MCP_TOKEN"], "X-ML-Scope-ID": args.scope}
    async with httpx.AsyncClient(headers=headers, timeout=120, trust_env=False) as http:
        async with streamable_http_client(args.url, http_client=http, terminate_on_close=False) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(args.tool, arguments,
                    meta={"materials-ml/idempotency-key": args.key} if mutating else None)
                print(json.dumps(result.structuredContent, ensure_ascii=False, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("analyze_tabular_dataset", "train_tabular_regression", "get_training_run", "predict_with_model"))
    parser.add_argument("--scope", required=True)
    parser.add_argument("--arguments", required=True)
    parser.add_argument("--key")
    parser.add_argument("--url", default="http://127.0.0.1:8200/mcp")
    try:
        asyncio.run(call(parser.parse_args()))
    except Exception:
        raise SystemExit("ML_MCP_CLIENT_FAILED: inspect the original resource/key before retrying") from None


if __name__ == "__main__":
    main()

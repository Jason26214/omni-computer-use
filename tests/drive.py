"""Reusable MCP driver: run a sequence of tool calls against our computer-use MCP
in ONE stdio session, save returned images, print per-step JSON summaries.

Usage:
    uv run python tests/drive.py <spec.json> [outdir]

spec.json = JSON list of {"tool": "<name>", "args": {...}} executed in order.
Images in tool results are saved to outdir/stepNN_<tool>.png and reported with dims.
"""
import sys
import os
import json
import base64
import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main() -> None:
    spec_path = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "tests", "out")
    os.makedirs(outdir, exist_ok=True)
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    params = StdioServerParameters(
        command="uv",
        args=["--directory", ROOT, "run", "python", "-m", "computer_use_omni"],
        env={**os.environ},  # propagate COMPUTER_USE_* overrides to the server
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for i, call in enumerate(spec):
                tool = call["tool"]
                args = call.get("args", {}) or {}
                summary = {"i": i, "tool": tool}
                try:
                    res = await session.call_tool(tool, args)
                except Exception as e:  # noqa: BLE001
                    summary["error"] = f"{type(e).__name__}: {e}"
                    print(json.dumps(summary, ensure_ascii=False))
                    continue
                summary["isError"] = bool(getattr(res, "isError", False))
                texts = []
                images = []
                for c in res.content:
                    ctype = getattr(c, "type", None)
                    if ctype == "text":
                        texts.append(c.text)
                    elif ctype == "image":
                        path = os.path.join(outdir, f"step{i:02d}_{tool}.png")
                        with open(path, "wb") as imf:
                            imf.write(base64.b64decode(c.data))
                        dims = ""
                        try:
                            from PIL import Image

                            with Image.open(path) as im:
                                dims = f"{im.width}x{im.height}"
                        except Exception:  # noqa: BLE001
                            pass
                        images.append(f"{path} ({dims})")
                if texts:
                    summary["text"] = texts
                if images:
                    summary["images"] = images
                print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

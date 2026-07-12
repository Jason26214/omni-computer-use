"""Timed MCP driver for phase-3 verification.

Like drive.py but: (a) lists tools first and records the count, (b) times each
tool call, (c) writes a machine-readable results.json into outdir alongside the
saved images. Env COMPUTER_USE_* overrides are propagated to the server.

Usage:
    uv run python tests/drive_timed.py <spec.json> [outdir]
"""
import sys
import os
import json
import time
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
        env={**os.environ},
    )

    results = {"tools": [], "tool_count": None, "steps": []}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            results["tools"] = names
            results["tool_count"] = len(names)
            print(json.dumps({"tool_count": len(names), "tools": names}, ensure_ascii=False))

            for i, call in enumerate(spec):
                tool = call["tool"]
                args = call.get("args", {}) or {}
                step = {"i": i, "tool": tool}
                t0 = time.perf_counter()
                try:
                    res = await session.call_tool(tool, args)
                except Exception as e:  # noqa: BLE001
                    step["elapsed_s"] = round(time.perf_counter() - t0, 4)
                    step["error"] = f"{type(e).__name__}: {e}"
                    results["steps"].append(step)
                    print(json.dumps(step, ensure_ascii=False))
                    continue
                step["elapsed_s"] = round(time.perf_counter() - t0, 4)
                step["isError"] = bool(getattr(res, "isError", False))
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
                        images.append({"path": path, "dims": dims})
                if texts:
                    step["text"] = texts
                if images:
                    step["images"] = images
                results["steps"].append(step)
                print(json.dumps(step, ensure_ascii=False))

    with open(os.path.join(outdir, "results.json"), "w", encoding="utf-8") as rf:
        json.dump(results, rf, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())

"""Self-contained MCP smoke client for omni-computer-use.

Launches the server as a subprocess and drives it over stdio using the official
``mcp`` client SDK, exercising the core tool surface end to end:

* ``initialize`` the session
* ``list_tools`` (assert the expected tool count, print their names)
* ``request_access`` for Notepad
* ``screenshot`` (decode the returned base64 PNG with Pillow, report W x H)
* ``mouse_move`` -> ``cursor_position`` round-trip (expect coords near [400, 300])
* a harmless keyboard action (``key`` press of a no-op modifier)

Run with::

    uv run python tests/mcp_client.py

Exits 0 when the smoke passes, 1 otherwise. A single JSON summary line tagged
``SMOKE_SUMMARY`` is always printed last.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {29, 30}  # 29 in production; 30 with the dev `reload` tool (default)
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extract_image_b64(result) -> str | None:
    """Pull the first base64 image payload out of a CallToolResult."""
    for item in result.content:
        # ImageContent has type=="image", a base64 ``data`` and ``mimeType``.
        if getattr(item, "type", None) == "image":
            return getattr(item, "data", None)
        data = getattr(item, "data", None)
        if data and getattr(item, "mimeType", "").startswith("image"):
            return data
    return None


def _text_blobs(result) -> list[str]:
    """Return the text payloads from a CallToolResult."""
    out: list[str] = []
    for item in result.content:
        if getattr(item, "type", None) == "text":
            out.append(item.text)
        else:
            txt = getattr(item, "text", None)
            if isinstance(txt, str):
                out.append(txt)
    return out


def _structured(result) -> dict | None:
    """Return a tool's structured result as a dict.

    Prefers ``structuredContent`` when the tool declares an output schema;
    otherwise falls back to JSON-parsing the first text content block (FastMCP
    serializes bare ``dict`` returns as a JSON text payload).
    """
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP wraps non-object returns under a "result" key; unwrap dicts.
        if set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    for txt in _text_blobs(result):
        try:
            parsed = json.loads(txt)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def main() -> int:
    failures: list[str] = []
    summary: dict = {
        "serverBoots": False,
        "toolCount": 0,
        "listToolsOk": False,
        "requestAccessOk": False,
        "screenshotOk": False,
        "screenshotDims": None,
        "cursorRoundtripOk": False,
        "typeOrClickOk": False,
    }

    params = StdioServerParameters(
        command="uv",
        args=[
            "--directory",
            SERVER_DIR,
            "run",
            "python",
            "-m",
            "omni_computer_use",
        ],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # ---- initialize -------------------------------------------------
            await session.initialize()
            summary["serverBoots"] = True

            # ---- list tools -------------------------------------------------
            tools_resp = await session.list_tools()
            names = sorted(t.name for t in tools_resp.tools)
            summary["toolCount"] = len(names)
            print(f"[tools] {len(names)} tools: {names}")
            if len(names) in EXPECTED_TOOLS:
                summary["listToolsOk"] = True
            else:
                failures.append(
                    f"expected one of {sorted(EXPECTED_TOOLS)} tools, got {len(names)}"
                )

            # ---- request_access --------------------------------------------
            ra = await session.call_tool(
                "request_access",
                {"apps": ["Notepad"], "reason": "smoke"},
            )
            ra_struct = _structured(ra) or {}
            print(f"[request_access] {json.dumps(ra_struct, default=str)}")
            granted = ra_struct.get("granted") or []
            if granted:
                summary["requestAccessOk"] = True
            else:
                failures.append(f"request_access granted nothing: {ra_struct}")

            # ---- screenshot -------------------------------------------------
            shot = await session.call_tool("screenshot", {})
            b64 = _extract_image_b64(shot)
            if not b64:
                failures.append(
                    "screenshot returned no image content: "
                    + repr(_text_blobs(shot))[:300]
                )
            else:
                try:
                    from PIL import Image

                    raw = base64.b64decode(b64)
                    img = Image.open(io.BytesIO(raw))
                    img.load()
                    w, h = img.size
                    summary["screenshotDims"] = f"{w}x{h}"
                    summary["screenshotOk"] = True
                    print(f"[screenshot] decoded {img.format} {w}x{h}")
                except Exception as exc:  # pragma: no cover - diagnostic
                    failures.append(f"screenshot decode failed: {exc!r}")

            # ---- mouse_move -> cursor_position round-trip -------------------
            target = [400, 300]
            await session.call_tool("mouse_move", {"coordinate": target})
            cp = await session.call_tool("cursor_position", {})
            cp_struct = _structured(cp) or {}
            cx = cp_struct.get("x")
            cy = cp_struct.get("y")
            if cx is None or cy is None:
                # Fall back to parsing the text representation if needed.
                print(f"[cursor_position] struct missing x/y: {cp_struct} "
                      f"text={_text_blobs(cp)}")
            print(f"[cursor_position] -> ({cx}, {cy}) target={target}")
            if (
                isinstance(cx, (int, float))
                and isinstance(cy, (int, float))
                and abs(cx - target[0]) <= 3
                and abs(cy - target[1]) <= 3
            ):
                summary["cursorRoundtripOk"] = True
            else:
                failures.append(
                    f"cursor round-trip off: got ({cx}, {cy}), want ~{target}"
                )

            # ---- harmless key press ----------------------------------------
            # 'shift' alone is a no-op chord: it presses and releases shift
            # without producing input side effects.
            try:
                kr = await session.call_tool("key", {"text": "shift"})
                print(f"[key] {_text_blobs(kr)}")
                summary["typeOrClickOk"] = True
            except Exception as exc:  # pragma: no cover - diagnostic
                failures.append(f"key press failed: {exc!r}")

    summary["failures"] = failures
    summary["ok"] = not failures
    print("SMOKE_SUMMARY " + json.dumps(summary, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main()))

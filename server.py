#!/usr/bin/env python3
"""
chatgpt-compvm — MCP server for a disposable Ubuntu "computer" (Manus-style).

Exposes, via Tailscale Funnel, two things under one prefix:
  * /chatgpt-compvm-mcp          -> streamable-HTTP MCP (token-gated). ChatGPT
                                    drives the desktop: screenshot/click/type/
                                    key/open_url, a shell, files, git sync.
  * /chatgpt-compvm-mcp/monitor  -> an interactive noVNC screen you open in any
                                    browser tab (incl. ChatGPT's side panel) to
                                    watch and take over — e.g. to type your own
                                    passwords, which never pass through ChatGPT.

Everything runs against the Docker container named by COMP_CONTAINER. The desktop
lives on display :99; x11vnc on 5900 is published only on the host's localhost and
is reachable solely through the token-gated websocket bridge below.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shlex
import subprocess
import sys
from urllib.parse import parse_qs

from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect
from starlette.types import ASGIApp, Scope, Receive, Send

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------- config -----
TOKEN        = os.environ.get("COMP_TOKEN", "")
HOST         = os.environ.get("COMP_HOST", "127.0.0.1")
PORT         = int(os.environ.get("COMP_PORT", "8976"))
CONTAINER    = os.environ.get("COMP_CONTAINER", "chatgpt-compvm")
VNC_HOST     = os.environ.get("COMP_VNC_HOST", "127.0.0.1")
VNC_PORT     = int(os.environ.get("COMP_VNC_PORT", "8977"))
DISPLAY      = os.environ.get("COMP_DISPLAY", ":99")
FUNNEL_HOST  = os.environ.get("FUNNEL_HOST", "HOST.ts.net")
BASE_PATH    = os.environ.get("COMP_BASE_PATH", "/chatgpt-compvm-mcp")
WORKSPACE    = os.environ.get("COMP_WORKSPACE", "~/chatgpt-compvm/data/workspace")
RUN_SCRIPT   = os.environ.get("COMP_RUN_SCRIPT", "~/chatgpt-compvm/run-container.sh")
NOVNC_DIR    = os.environ.get("COMP_NOVNC_DIR", "~/chatgpt-compvm/novnc")

# --------------------------------------------------------------- helpers -----
def _run(argv: list[str], timeout: int = 120) -> tuple[int, str, bytes]:
    """Run a host command; return (exit, text, raw_stdout_bytes)."""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"[timeout after {timeout}s]", b""
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    txt = out + (("\n[stderr]\n" + err) if err.strip() else "")
    return r.returncode, txt.strip(), r.stdout


def _dbash(cmd: str, timeout: int = 120) -> str:
    code, txt, _ = _run(["docker", "exec", CONTAINER, "bash", "-lc", cmd], timeout)
    return f"exit={code}\n{txt}".strip()


def _xdo(args: list[str], timeout: int = 30) -> str:
    code, txt, _ = _run(
        ["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER, "xdotool", *args], timeout
    )
    return txt if txt else ("ok" if code == 0 else f"exit={code}")


def _running() -> bool:
    code, txt, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], 15)
    return code == 0 and txt.strip() == "true"


# ---- vision helpers (screenshot, cursor, OCR, annotation) -------------------
def _shot_png() -> bytes:
    """Raw PNG bytes of the VM desktop."""
    code, _, raw = _run(
        ["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER, "scrot", "-o", "-"], 30)
    if code != 0 or not raw:
        _dbash("scrot -o /tmp/s.png")
        raw = _run(["docker", "exec", CONTAINER, "cat", "/tmp/s.png"], 30)[2]
    return raw


def _cursor_xy():
    code, txt, _ = _run(
        ["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
         "xdotool", "getmouselocation", "--shell"], 10)
    d = {}
    for line in txt.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    try:
        return int(d["X"]), int(d["Y"])
    except (KeyError, ValueError):
        return None


def _annotate(png: bytes, cursor=None, grid=True) -> bytes:
    """Overlay a labelled 100px coordinate grid and a cursor crosshair."""
    from PIL import Image as PImage, ImageDraw
    im = PImage.open(io.BytesIO(png)).convert("RGB")
    d = ImageDraw.Draw(im)
    W, H = im.size
    if grid:
        for x in range(0, W, 100):
            d.line([(x, 0), (x, H)], fill=(255, 70, 70), width=1)
            d.text((x + 2, 2), str(x), fill=(255, 70, 70))
        for y in range(0, H, 100):
            d.line([(0, y), (W, y)], fill=(255, 70, 70), width=1)
            d.text((2, y + 2), str(y), fill=(255, 70, 70))
    if cursor:
        cx, cy = cursor
        d.line([(cx - 16, cy), (cx + 16, cy)], fill=(0, 210, 255), width=2)
        d.line([(cx, cy - 16), (cx, cy + 16)], fill=(0, 210, 255), width=2)
        d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=(0, 210, 255), width=2)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def _ocr(query: str = "") -> list:
    code, txt, _ = _run(
        ["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
         "python3", "/usr/local/bin/ocr_find.py", query], 60)
    try:
        return json.loads(txt.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []


# ------------------------------------------------------------------ MCP ------
_sec = TransportSecuritySettings(
    allowed_hosts=[FUNNEL_HOST, f"{HOST}:{PORT}", f"127.0.0.1:{PORT}", f"localhost:{PORT}"],
    allowed_origins=[f"https://{FUNNEL_HOST}", "*"],
)
mcp = FastMCP("chatgpt-compvm", host=HOST, port=PORT, transport_security=_sec)


@mcp.tool()
def vm_status() -> str:
    """Report whether the VM container is running, plus RAM/disk usage of the VM."""
    if not _running():
        return f"container '{CONTAINER}' is NOT running. Call reset_vm() to (re)start it."
    stats = _run(["docker", "stats", "--no-stream", "--format",
                  "mem={{.MemUsage}} cpu={{.CPUPerc}}", CONTAINER], 20)[1]
    disk = _dbash("df -h /data | tail -1")
    win = _xdo(["search", "--name", "Mozilla Firefox"]).splitlines()
    return f"running. {stats}\n[/data disk]\n{disk}\nfirefox windows: {len(win)}"


@mcp.tool()
def screenshot() -> Image:
    """Take a plain PNG screenshot of the VM's desktop so you can see the current screen."""
    return Image(data=_shot_png(), format="png")


@mcp.tool()
def look(grid: bool = True) -> Image:
    """Vision screenshot: the desktop with a labelled 100px coordinate grid and a
    crosshair at the current mouse position. Use this to READ the exact x,y to aim
    a click at, and to see where the cursor is before moving it."""
    return Image(data=_annotate(_shot_png(), cursor=_cursor_xy(), grid=grid), format="png")


@mcp.tool()
def cursor_position() -> str:
    """Return the current mouse cursor position as 'x,y'."""
    xy = _cursor_xy()
    return f"{xy[0]},{xy[1]}" if xy else "unknown"


@mcp.tool()
def find_text(query: str) -> str:
    """Vision: OCR the screen and return on-screen text matching `query` with the
    click coordinates of each match, as JSON [{text,x,y}, ...]. Empty query returns
    all detected words. Use this to locate buttons/links/fields by their label."""
    matches = _ocr(query)
    if not matches:
        return f"no on-screen text matching {query!r} (try look() to read the screen)."
    return json.dumps(matches[:40])


@mcp.tool()
def click_text(query: str, occurrence: int = 1, button: str = "left", double: bool = False) -> str:
    """Vision mouse: find on-screen text matching `query` (OCR) and click its centre.
    occurrence picks the Nth match (1-based) when several match. This is the reliable
    way to click a labelled button/link without guessing pixel coordinates."""
    matches = _ocr(query)
    if not matches:
        return f"could not find {query!r} on screen — call look() and click by coordinates instead."
    if occurrence < 1 or occurrence > len(matches):
        occurrence = 1
    m = matches[occurrence - 1]
    b = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
    args = ["mousemove", "--sync", str(m["x"]), str(m["y"]), "click"]
    if double:
        args += ["--repeat", "2", "--delay", "120"]
    args.append(b)
    _xdo(args)
    extra = f" (match {occurrence} of {len(matches)})" if len(matches) > 1 else ""
    return f"clicked {m['text']!r} at {m['x']},{m['y']}{extra}"


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> str:
    """Press the mouse button at (x1,y1), drag to (x2,y2) and release (e.g. select
    text, move a window, drag a slider)."""
    b = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
    return _xdo(["mousemove", "--sync", str(x1), str(y1), "mousedown", b,
                 "mousemove", "--sync", str(x2), str(y2), "mouseup", b], timeout=30)


@mcp.tool()
def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Move the mouse to (x, y) and click. button: left|middle|right. double=True for double-click."""
    b = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
    args = ["mousemove", "--sync", str(x), str(y), "click"]
    if double:
        args += ["--repeat", "2", "--delay", "120"]
    args.append(b)
    return _xdo(args)


@mcp.tool()
def move(x: int, y: int) -> str:
    """Move the mouse to absolute coordinates (x, y) without clicking."""
    return _xdo(["mousemove", "--sync", str(x), str(y)])


@mcp.tool()
def type_text(text: str) -> str:
    """Type a string at the current keyboard focus (as if typed on the keyboard)."""
    return _xdo(["type", "--clearmodifiers", "--delay", "20", text], timeout=60)


@mcp.tool()
def key(keys: str) -> str:
    """Press a key or chord using xdotool syntax, e.g. 'Return', 'ctrl+l', 'alt+Tab', 'Escape'."""
    return _xdo(["key", "--clearmodifiers", keys])


@mcp.tool()
def scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll the mouse wheel. direction: up|down. amount = number of wheel clicks."""
    b = "4" if direction == "up" else "5"
    return _xdo(["click", "--repeat", str(max(1, amount)), b])


@mcp.tool()
def open_url(url: str) -> str:
    """Open a URL in the VM's Firefox (focuses the address bar and navigates)."""
    _run(["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER, "bash", "-lc",
          "wid=$(xdotool search --name 'Mozilla Firefox' | head -1); "
          "[ -n \"$wid\" ] && xdotool windowactivate --sync $wid"], 15)
    _xdo(["key", "--clearmodifiers", "ctrl+l"])
    _xdo(["type", "--clearmodifiers", "--delay", "15", url], timeout=45)
    return _xdo(["key", "Return"])


@mcp.tool()
def open_terminal() -> str:
    """Open a fresh Ubuntu terminal window on the VM desktop (visible in the live view)."""
    subprocess.Popen(
        ["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
         "xfce4-terminal", "--working-directory=/data/workspace"])
    return "opened a terminal on the desktop."


@mcp.tool()
def run_shell(command: str, timeout: int = 120) -> str:
    """Run a bash command INSIDE the VM (cwd defaults to /data/workspace) and return output."""
    return _dbash(f"cd /data/workspace 2>/dev/null; {command}", timeout)


@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file inside the VM. Relative paths resolve under /data/workspace."""
    p = path if path.startswith("/") else f"/data/workspace/{path}"
    return _dbash(f"cat -- {shlex.quote(p)}", 60)


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create/overwrite a text file inside the VM. Relative paths resolve under /data/workspace."""
    p = path if path.startswith("/") else f"/data/workspace/{path}"
    b = base64.b64encode(content.encode()).decode()
    return _dbash(
        f"mkdir -p \"$(dirname {shlex.quote(p)})\" && echo {b} | base64 -d > {shlex.quote(p)} "
        f"&& echo wrote {len(content)} bytes to {shlex.quote(p)}", 60)


@mcp.tool()
def list_dir(path: str = ".") -> str:
    """List a directory inside the VM. Relative paths resolve under /data/workspace."""
    p = path if path.startswith("/") else f"/data/workspace/{path}"
    return _dbash(f"ls -la -- {shlex.quote(p)}", 30)


@mcp.tool()
def sync_github(message: str = "sync from chatgpt-compvm") -> str:
    """Commit the VM's /data/workspace and push it to the configured GitHub repo.

    Requires a git remote 'origin' to already be set on the host workspace
    (done once via setup, using `gh auth login`). Firefox logins are NOT synced.
    """
    code, txt, _ = _run(["bash", "-lc",
        f"cd {shlex.quote(WORKSPACE)} && git add -A && "
        f"(git diff --cached --quiet && echo 'nothing to commit' || "
        f"git commit -q -m {shlex.quote(message)}) && "
        f"(git remote get-url origin >/dev/null 2>&1 && git push -u origin HEAD 2>&1 || "
        f"echo 'no origin remote set — run gh auth login and add one (see README)')"], 120)
    return txt


@mcp.tool()
def git_status() -> str:
    """Show git status of the VM workspace on the host."""
    return _run(["bash", "-lc",
        f"cd {shlex.quote(WORKSPACE)} && git remote -v; echo ---; git status -s -b"], 30)[1]


@mcp.tool()
def reset_vm(wipe_data: bool = False) -> str:
    """Destroy and recreate the VM container from a clean image (disposable reset).

    wipe_data=False (default): keeps /data — your files and Firefox logins survive.
    wipe_data=True: also erases /data for a completely fresh machine.
    """
    env = dict(os.environ)
    if wipe_data:
        env["COMP_WIPE"] = "1"
    r = subprocess.run(["bash", RUN_SCRIPT], capture_output=True, text=True, timeout=180, env=env)
    return (r.stdout + r.stderr).strip()[-3000:]


@mcp.tool()
def open_monitor() -> str:
    """Return the URL of the live, interactive desktop view (open it in a browser tab).

    Use this whenever you need the user to see the screen or type something
    themselves (passwords, 2FA codes, payment details)."""
    return (f"Live desktop (click & type enabled):\n"
            f"https://{FUNNEL_HOST}{BASE_PATH}/monitor?token={TOKEN}\n"
            f"Ask the user to open it to take over — their keystrokes stay on the VM.")


@mcp.tool()
def request_human(reason: str = "manual step needed") -> str:
    """Hand control to the user for a step you must NOT automate: solving a CAPTCHA /
    "I'm not a robot" / Cloudflare challenge, typing a password, entering a 2FA code,
    or filling payment details.

    STOP automating and call this. It returns the live-view URL; tell the user the
    'reason', ask them to complete it in that view, and wait for them to say it's done
    before you continue. A human genuinely solving the CAPTCHA is the intended path —
    do not try to defeat or auto-solve challenges."""
    return (f"HUMAN NEEDED: {reason}\n"
            f"Open the live desktop and complete this step yourself:\n"
            f"https://{FUNNEL_HOST}{BASE_PATH}/monitor?token={TOKEN}\n"
            f"(Your keystrokes stay on the VM.) Reply 'done' when finished and I'll continue.")


@mcp.tool()
def detect_challenge() -> str:
    """Check the current Firefox page for a CAPTCHA / bot challenge or a login form.

    Reads the page's own text via Firefox's clipboard (Ctrl+A, Ctrl+C) and scans for
    common challenge/login markers. If one is found, hand off with request_human()
    instead of trying to click through it."""
    _run(["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER, "bash", "-lc",
          "wid=$(xdotool search --name 'Mozilla Firefox' | head -1); "
          "xdotool windowactivate --sync $wid key --clearmodifiers ctrl+a ctrl+c Escape"], 15)
    txt = _dbash("xclip -selection clipboard -o 2>/dev/null | tr 'A-Z' 'a-z'", 15).lower()
    markers = ["i'm not a robot", "recaptcha", "hcaptcha", "captcha", "turnstile",
               "verify you are human", "are you a robot", "unusual traffic",
               "verify it's you", "two-factor", "verification code", "enter the code",
               "sign in", "log in", "password"]
    hits = sorted({m for m in markers if m in txt})
    if not hits:
        return "no challenge/login markers detected on the current page."
    return ("detected: " + ", ".join(hits) +
            "\n-> this needs the user. Call request_human() with a short reason and pause.")


# --------------------------------------------------------- monitor page ------
def _monitor_html() -> str:
    return """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>compvm monitor</title>
<style>
 html,body{margin:0;height:100%;background:#111;color:#ddd;font-family:system-ui,sans-serif;overflow:hidden}
 #bar{position:fixed;top:0;left:0;right:0;height:28px;display:flex;align-items:center;
      gap:10px;padding:0 10px;background:#1b1b1b;font-size:12px;z-index:10;border-bottom:1px solid #333}
 #dot{width:9px;height:9px;border-radius:50%;background:#e33}
 #screen{position:absolute;top:28px;left:0;right:0;bottom:0}
 #screen canvas{outline:none}
</style></head><body>
<div id=bar><span id=dot></span><span id=st>connecting…</span>
 <span style="margin-left:auto;opacity:.6">click & type to control · your keystrokes stay on the VM</span></div>
<div id=screen></div>
<script type=module>
const st=document.getElementById('st'),dot=document.getElementById('dot');
const q=new URLSearchParams(location.search);
const base=location.pathname.replace(/\\/+$/,'');
const proto=location.protocol==='https:'?'wss':'ws';
const url=`${proto}://${location.host}${base}/ws?token=${encodeURIComponent(q.get('token')||'')}`;
let RFB, rfb;
try{ RFB=(await import(`${base}/novnc/core/rfb.js`)).default; }
catch(e){ st.textContent='failed to load viewer: '+e.message; throw e; }
function connect(){
  st.textContent='connecting…';
  rfb=new RFB(document.getElementById('screen'),url);
  rfb.viewOnly=false; rfb.scaleViewport=true; rfb.resizeSession=false; rfb.focusOnClick=true;
  rfb.addEventListener('connect',()=>{st.textContent='connected';dot.style.background='#3c3';rfb.focus();});
  rfb.addEventListener('disconnect',()=>{st.textContent='disconnected — retrying…';dot.style.background='#e33';
     setTimeout(connect,1500);});
}
connect();
</script></body></html>"""


async def monitor_page(request):
    if TOKEN and request.query_params.get("token") != TOKEN:
        return PlainTextResponse("unauthorized", status_code=401)
    return HTMLResponse(_monitor_html())


async def monitor_ws(ws: WebSocket):
    if TOKEN and ws.query_params.get("token") != TOKEN:
        await ws.close(code=1008)
        return
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="binary" if "binary" in offered else None)
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except OSError as e:
        await ws.close(code=1011)
        return

    async def browser_to_vnc():
        try:
            while True:
                data = await ws.receive_bytes()
                writer.write(data)
                await writer.drain()
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass

    async def vnc_to_browser():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except (ConnectionError, RuntimeError):
            pass

    b2v = asyncio.create_task(browser_to_vnc())
    v2b = asyncio.create_task(vnc_to_browser())
    done, pending = await asyncio.wait({b2v, v2b}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    writer.close()
    try:
        await ws.close()
    except RuntimeError:
        pass


async def health(request):
    return JSONResponse({"ok": True, "container_running": _running()})


# ---------------------------------------------------- token gate + app -------
class TokenGate:
    """Bearer or ?token gate for MCP HTTP. Monitor routes self-check the token,
    websockets pass through (checked in the endpoint). /.well-known/* -> 404 so
    MCP clients skip OAuth discovery instead of looping."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and TOKEN:
            path = scope.get("path", "")
            if "/.well-known/" in path:
                await PlainTextResponse("not found", status_code=404)(scope, receive, send)
                return
            if path.startswith("/monitor") or path == "/health":
                await self.app(scope, receive, send)
                return
            hdr = dict(scope.get("headers") or [])
            auth = hdr.get(b"authorization", b"").decode()
            qs = parse_qs((scope.get("query_string") or b"").decode())
            qtok = (qs.get("token") or [""])[0]
            if auth != f"Bearer {TOKEN}" and qtok != TOKEN:
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    if not TOKEN:
        sys.stderr.write("WARNING: COMP_TOKEN empty — endpoints are UNAUTHENTICATED!\n")
    app = mcp.streamable_http_app()
    app.add_route("/monitor", monitor_page, methods=["GET"])
    app.add_route("/health", health, methods=["GET"])
    app.router.routes.append(WebSocketRoute("/monitor/ws", monitor_ws))
    if os.path.isdir(NOVNC_DIR):
        app.router.routes.append(Mount("/monitor/novnc", app=StaticFiles(directory=NOVNC_DIR)))
    import uvicorn
    uvicorn.run(TokenGate(app), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()

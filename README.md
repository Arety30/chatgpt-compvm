# chatgpt-compvm

A disposable Ubuntu desktop in a Docker container, remote-controlled through a Model Context Protocol (MCP) server, by Syed Abrar Ali. The container runs a real X desktop (Xvfb, Openbox, panel and dock, Firefox, a terminal) and the host-side server exposes it as MCP tools: take screenshots, read the screen with OCR, click, type, press keys, open URLs, run shell commands, read and write files, and push the workspace to a Git remote. A token-gated noVNC page lets a human watch the same screen live and take over the keyboard and mouse. The problem it solves is giving an MCP client such as a chat assistant a safe "computer" to work in: anything it breaks is discarded with `reset_vm`, while logins and files survive on a persistent volume, and steps that must stay human (passwords, CAPTCHAs, 2FA, payments) are handed back to the person through the live view so their keystrokes never pass through the assistant.

## Where it is used

It runs on the owner's laptop as a user systemd service. The MCP endpoint and the monitor page are published on a single URL prefix through a Tailscale Funnel-style HTTPS tunnel, so a remote chat assistant adds it as a connector and the owner opens the monitor page in any browser tab to supervise. Typical jobs: browsing and filling web forms, trying software in a throwaway Ubuntu, editing files in the shared workspace and committing them to a private GitHub repository.

## How it works

### Container (`image/`)

- `Dockerfile` builds from `ubuntu:24.04`: Xvfb, x11vnc, Openbox, lxpanel, plank, rofi, xfce4-terminal, xdotool, scrot, wmctrl, xclip, the Yaru theme, a few small apps, and Firefox from Mozilla's APT repository (the Ubuntu package is a snap, which cannot run in a container). `rootfs/` is copied over `/` to bake in the desktop configuration (Openbox menu and key bindings, panel layout, GTK/xsettings theme, rofi, a Firefox `profiles.ini` pointing at `/data/firefox-profile`, and `/usr/local/bin/ocr_find.py`).
- `start.sh` is the entrypoint: starts a D-Bus session, Xvfb on `:99` at `$SCREEN` (default 1440x900x24), Openbox, xsettingsd, lxpanel, plank, xcape (Super key opens the rofi app grid), x11vnc on port 5900 with no password (the port is only ever published on the host's loopback), then a maximised terminal and Firefox. Firefox's profile and `/data/workspace` live on the `/data` volume so logins and files survive container recreation.
- `ocr_find.py` screenshots the display, runs `tesseract` in sparse-text mode, drops words under 40 % confidence and prints JSON `[{text, x, y}]` centre coordinates for words matching a query (grouped into lines).

### Host scripts

| File | Role |
| --- | --- |
| `run-container.sh` | `docker rm -f` then `docker run -d` with 2 GB RAM (no extra swap), 512 MB `/dev/shm`, `--restart always`, `./data` mounted at `/data`, and VNC published only as `127.0.0.1:$COMP_VNC_PORT -> 5900`. `COMP_WIPE=1` empties `./data` first. |
| `ensure-up.sh` | `ExecStartPre` self-heal: mounts the data volume if it is a mount point, then starts a stopped container or recreates a missing one without touching a healthy one. Never fails. |
| `setup-github.sh` | One-time: after `gh auth login`, commits `data/workspace` and creates a private GitHub repo wired as `origin` so `sync_github` can push. |
| `chatgpt-compvm.service` | User unit: loads `.env`, runs `ensure-up.sh`, then `server.py`; restarts on failure. |

### MCP server (`server.py`)

FastMCP with the streamable-HTTP transport, wrapped in a Starlette app served by uvicorn.

- Every tool shells out with `docker exec` into the container (`_dbash` for bash, `_xdo` for xdotool with `DISPLAY=:99`), so the host needs only Docker, Python and Pillow.
- Vision: `screenshot` returns a PNG; `look` overlays a labelled 100-pixel grid and a cursor crosshair (Pillow) so the model can read coordinates; `find_text`/`click_text` use the OCR helper to locate and click on-screen words; `cursor_position` reports the mouse.
- Input: `click`, `move`, `drag`, `scroll`, `type_text`, `key` (xdotool syntax such as `ctrl+l`), `open_url` (focuses Firefox, Ctrl+L, types the URL), `open_terminal`.
- Shell and files: `run_shell` (cwd `/data/workspace`, timeout), `read_file`, `write_file` (base64 round-trip so any content is safe), `list_dir`.
- Lifecycle: `vm_status` (docker stats, `/data` disk usage, Firefox window count), `reset_vm(wipe_data)` runs `run-container.sh`, `sync_github(message)` and `git_status` operate on the host-side workspace checkout.
- Human hand-off: `open_monitor` and `request_human(reason)` return the monitor URL and instruct the model to stop and wait; `detect_challenge` copies the current page text through Firefox's clipboard and looks for CAPTCHA/login markers so the model knows when to hand off instead of clicking through.
- Monitor: `GET /monitor?token=` serves an HTML page that loads noVNC from `/monitor/novnc` (a local checkout of noVNC mounted as static files) and connects to `/monitor/ws`; `monitor_ws` is an asyncio bridge that pipes WebSocket bytes to the VNC TCP port and back. `/health` reports whether the container runs.
- `TokenGate` ASGI middleware: when `COMP_TOKEN` is set, MCP requests need `Authorization: Bearer <token>` or `?token=`; `/monitor*` and `/health` check the token themselves; `/.well-known/*` returns 404 so MCP clients skip OAuth discovery.

## Setup

Requirements: Docker, Python 3.10+ with `mcp`, `starlette`, `uvicorn`, `pillow`; `git` and the GitHub CLI for the sync feature; a noVNC checkout for the monitor page; an HTTPS tunnel or reverse proxy that forwards one path prefix to the server port.

```bash
git clone https://github.com/USER/chatgpt-compvm.git ~/chatgpt-compvm
cd ~/chatgpt-compvm
docker build -t chatgpt-compvm:latest image/
git clone https://github.com/novnc/noVNC novnc
python -m venv .venv && . .venv/bin/activate && pip install mcp starlette uvicorn pillow
```

Create `.env` (loaded by the service):

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMP_TOKEN` | empty (unauthenticated, warns) | Shared secret for MCP and monitor access |
| `COMP_HOST` / `COMP_PORT` | `127.0.0.1` / `8976` | Bind address of the server |
| `COMP_CONTAINER` | `chatgpt-compvm` | Container name |
| `COMP_VNC_HOST` / `COMP_VNC_PORT` | `127.0.0.1` / `8977` | Where the container's VNC port is published |
| `COMP_DISPLAY` | `:99` | X display inside the container |
| `COMP_SCREEN` | `1440x900x24` | Desktop resolution |
| `FUNNEL_HOST` | — | Public hostname used in returned monitor URLs, e.g. `HOST.example.com` |
| `COMP_BASE_PATH` | `/chatgpt-compvm-mcp` | Public path prefix (strip it in the proxy) |
| `COMP_WORKSPACE`, `COMP_RUN_SCRIPT`, `COMP_NOVNC_DIR` | repo paths | Host workspace dir, container script, noVNC dir |

Then `bash run-container.sh`, optionally `bash setup-github.sh`, and install the unit with `systemctl --user enable --now chatgpt-compvm` (edit the paths and interpreter in the unit first).

## Usage

- Connector URL for the MCP client: `https://HOST.example.com/chatgpt-compvm-mcp` with header `Authorization: Bearer <COMP_TOKEN>` (or `?token=`).
- Live view: `https://HOST.example.com/chatgpt-compvm-mcp/monitor?token=<COMP_TOKEN>` — click and type directly in it.
- Example session: `vm_status` → `open_url("https://example.com")` → `look()` to read coordinates → `click_text("Sign in")` → `detect_challenge()` reports a login form → `request_human("enter your password")` → user finishes in the monitor and says "done" → `run_shell("ls")`, `write_file("notes.md", "...")`, `sync_github("add notes")`.
- Maintenance: `docker logs chatgpt-compvm`, `systemctl --user status chatgpt-compvm`, `bash run-container.sh` to recreate, `COMP_WIPE=1 bash run-container.sh` for a factory-fresh machine.

## Project structure

```
chatgpt-compvm/
├── server.py              # MCP tools, token gate, /monitor page, VNC websocket bridge
├── run-container.sh       # (re)create the 2 GB container with /data volume and loopback VNC
├── ensure-up.sh           # ExecStartPre self-heal
├── setup-github.sh        # one-time private repo + origin for workspace sync
├── chatgpt-compvm.service # user systemd unit
├── image/
│   ├── Dockerfile         # Ubuntu 24.04 desktop + Firefox + automation tools
│   ├── start.sh           # entrypoint: Xvfb, Openbox, panel, dock, x11vnc, terminal, Firefox
│   └── rootfs/            # baked desktop config and /usr/local/bin/ocr_find.py
├── data/                  # persistent volume (workspace, Firefox profile) — not in git
└── novnc/                 # noVNC checkout served at /monitor/novnc — not in git
```

## Limitations and notes

- Security rests on `COMP_TOKEN` and the tunnel's TLS; with an empty token everything, including the live desktop, is open to whoever can reach the port. Keep the server on loopback behind the proxy.
- `find_text` and `click_text` need `python3` and `tesseract` inside the image; the Dockerfile as written does not install them, so extend it (`apt-get install tesseract-ocr python3`) before relying on OCR.
- The desktop is single-user, root inside the container, with no audio and no GPU.
- `detect_challenge` works by select-all/copy in Firefox, which disturbs the page selection and only sees text, not images.
- `sync_github` pushes only `/data/workspace`; the Firefox profile is never synced.
- The container is memory-capped at 2 GB; heavy pages can make Firefox slow or crash, which `reset_vm` fixes.
- Hostnames, paths and the Python interpreter in the unit file are specific to the original machine; adjust them when deploying elsewhere.

# Setup

Get K2 Press running on your machine.

## Requirements

- **Python 3.11+**
- A **local LLM**: either **Hermes** CLI, or **Ollama** (or any OpenAI-compatible server)
- **Node not required** — the UI is vanilla JS served by FastAPI

## 1. Install

Windows: run **`setup.bat`** (creates the venv, installs deps + Playwright Chromium,
creates a placeholder `.env`). Or manually:

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows:  .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## 2. Secrets — `.env`

Copy `.env.example` to `.env` and fill what you use:

```env
PEXELS_API_KEY=your_pexels_key            # free at pexels.com/api  (image source)
UNSPLASH_API_KEY=your_unsplash_key        # optional image source
GOOGLE_API_KEY=...                        # optional: Google image search
GOOGLE_CSE_ID=...
META_ACCESS_TOKEN=...                     # long-lived Meta token (publishing)
META_APP_ID=                              # Meta app > App settings > Basic
META_APP_SECRET=                          # (exchanges short-lived tokens)
INSTAGRAM_APP_ID=                         # Meta app > Instagram > API setup
INSTAGRAM_APP_SECRET=                     # (a different pair from META_APP_*)
PUBLIC_BASE_URL=                          # REQUIRED for Instagram: public https URL
                                          # serving /outputs (Meta fetches images itself)
```

`.env` is gitignored — your keys never get committed.

## 3. Configure your brand(s) — `config.yaml`

```bash
cp config.example.yaml config.yaml        # the app also auto-copies this on first run
```

Edit `config.yaml`: set `app.name`, your brand(s) under `brands:`, and your Meta
`channels`. Drop your logo at the `logo_path` you set. `config.yaml` is **gitignored**
so your real brands/keys stay local. Full reference: [CONFIGURATION.md](CONFIGURATION.md).

## 4. Pick your LLM

K2 Press uses a local model to score stories and write copy. Two built-in backends,
switchable live in the header **Engine** dropdown:

| Backend | base_url | Notes |
|---|---|---|
| **Hermes** | `hermes://cli` | A local CLI. Only works when K2 runs **on the host** (not in Docker). |
| **Ollama** | `http://localhost:11434/v1` | An HTTP server. Works on host or Docker (`host.docker.internal`). |

Set the default + per-backend host/model under `llm:` in `config.yaml`.

## 5. Run

### On the host (required for Hermes)

```bash
run.bat                  # Windows one-click: venv + browser + uvicorn --reload
# or:
uvicorn app:app --host 0.0.0.0 --port 8000
```
Open <http://localhost:8000>.

### Docker

```bash
docker compose up --build
```
Docker maps `outputs/`, `image_cache/`, `library/` back to the project. **Docker uses
Ollama** (the entrypoint points `llm.base_url` at `host.docker.internal:11434`) because
the Hermes CLI isn't in the container. If Ollama is unreachable from the container, set
`OLLAMA_HOST=0.0.0.0` on the host and restart Ollama so the container can reach it.

> **Host vs Docker, in one line:** run on the **host** if you want **Hermes**; use
> **Docker** (Ollama) if you want the bundled Tailscale remote-access sidecar. Publishing
> publishing works either way.

## 6. Auto-start on login (optional, Windows)

To start the server automatically and hidden at login (no console window), put a
launcher in your Startup folder. A `start_hidden.bat` is included that runs uvicorn and
logs to `k2_server.log`; point a `.vbs` in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` at it:

```vbs
CreateObject("WScript.Shell").Run """C:\path\to\K2_PostTool\start_hidden.bat""", 0, False
```
This runs in your user context (so Hermes works). Delete the `.vbs` to disable.
Note: the auto-start runs **without `--reload`**, so restart it to pick up code changes.

## Next

- Connect Instagram & Facebook → [PUBLISHING_AND_CHANNELS.md](PUBLISHING_AND_CHANNELS.md)
- Tune brands/feeds/formats → [CONFIGURATION.md](CONFIGURATION.md)

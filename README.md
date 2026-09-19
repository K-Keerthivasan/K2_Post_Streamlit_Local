# K2 Press — Instagram & Social Media Content Studio

A self-hosted, local-AI **content studio for Instagram and Facebook**: create
branded posts (manually from your own idea, automatically from RSS/trending news, or in
bulk from a spreadsheet), edit every pixel, schedule them on a calendar, and **publish
straight to Instagram and Facebook through the Meta Graph API** with your own
credentials — no third-party scheduler in between. Multi-brand and fully config-driven —
copy `config.example.yaml` to `config.yaml` and add your own brands. Bring your own logo,
colours, feeds, and accounts.

## What it does

- **Create posts two ways** — ✍️ **Manual** (your idea + notes + images → AI builds the
  carousel and suggests angles) or 📡 **Auto** (fetch + AI-score RSS/trending stories).
- **Bulk from a spreadsheet** — 📄 **CSV Import** turns a sheet of post ideas (or
  finished slide copy) into rendered carousels, with or without the AI touching the words.
- **Many formats** — carousel, square, story, X/Twitter, quote, comparison, breaking,
  listicle, LinkedIn.
- **Full editor** — edit every line, fetch/upload/paste/URL images per slide, live preview,
  template editor, and a Fabric.js canvas for hand layout.
- **Publish directly to Meta** — Instagram (Business/Creator) and Facebook Pages via
  the Graph API, with your own long-lived token. A review-and-approve gate means nothing
  goes out until you say so.
- **Post calendar** — schedule posts on a month grid, auto-fill your usual posting slots,
  and let the background publisher send them at the right time.
- **Local & private** — runs on your machine with a local LLM (Hermes or Ollama). Your
  brands, keys, and channels stay in gitignored config; only the generic template ships.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Install, `.env`, host vs Docker, Hermes/Ollama, auto-start |
| [docs/PUBLISHING_AND_CHANNELS.md](docs/PUBLISHING_AND_CHANNELS.md) | **Connect Instagram & Facebook, tokens, scheduling** (the publishing guide) |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `config.yaml` reference — brands, themes, feeds, formats, Meta, schedule |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the modules fit together (for contributors) |

## Tech stack

| Layer | Tool |
|---|---|
| Feed ingestion | feedparser |
| LLM | Hermes CLI by default, or any OpenAI-compatible local server |
| Images | Pexels + Unsplash APIs, or any image URL |
| Templating | Jinja2 |
| Rendering | Playwright / headless Chromium |
| Control panel | FastAPI + vanilla JS + Fabric.js (canvas) |
| Config | PyYAML |

---

## Quick start

Double-click **`run.bat`** — it activates the venv, checks the local model setup, launches the server, and
opens your browser at `http://localhost:8000`.

### First-time setup

On Windows, run **`setup.bat`** for the full local setup. It creates the virtual environment,
installs dependencies, installs Playwright Chromium, creates a placeholder `.env` if needed,
and can build the Docker image when Docker Compose is available.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
hermes status             # confirm Hermes is logged in and ready
```

Add API keys to `.env`:

```env
PEXELS_API_KEY=your_pexels_key        # free at pexels.com/api
UNSPLASH_API_KEY=your_unsplash_key    # optional, free at unsplash.com/developers
```

### Configure your brand(s)

Nothing in the app is hard-coded to a specific brand — all identity (name, handle,
logo, theme, feeds, Meta account) is config-driven. Copy the example config and
edit it:

```powershell
copy config.example.yaml config.yaml      # (the app also auto-copies it on first run)
```

Then open `config.yaml` and set your `app.name`, your brand block(s) under `brands:`
(name, handle, logo, theme colours, scoring `profile`/`personality`, and feed URLs),
and your `meta.accounts` (the Instagram/Facebook ids for each brand — see
[docs/PUBLISHING_AND_CHANNELS.md](docs/PUBLISHING_AND_CHANNELS.md)).
Drop your logo at the `logo_path` you set (e.g. `static/logo.png`). `config.yaml` is
**gitignored**, so your real details and keys never get committed — only the generic
`config.example.yaml` template is tracked.

### Docker

```powershell
docker compose up --build
```

Open `http://localhost:8000`. The compose setup maps `outputs/`, `image_cache/`, and
`library/` back to the project folder. For Ollama-based Docker runs, point
`llm.base_url` at `http://host.docker.internal:11434/v1`.

### Docker + Tailscale

Add a Tailscale pre-auth key to a local `.env.tailscale` file:

```env
TS_AUTHKEY=<paste-your-tailscale-pre-auth-key-here>
```

Then run the app with the Tailscale sidecar:

```powershell
docker compose --profile tailscale up -d --build
```

The sidecar uses Tailscale Serve to proxy the app privately over HTTPS inside your
tailnet. After it starts, get the URL with:

```powershell
docker exec k2-posttool-tailscale tailscale serve status
```

On your phone or another device logged into the same tailnet, open the shown
`https://k2-posttool.<your-tailnet>.ts.net` URL. The local Docker URL still works at
`http://localhost:8000`.

---

## Using the app

1. **Stories** — pick a feed category (Web Dev / Marketing / Tech), set how many to fetch and
   keep, then **Fetch & Score**. Your selected local AI model ranks each story 0–100 (timed).
   - **Single:** click **Edit as Carousel** to open one story in the Editor.
   - **Bulk:** tick 5–10 stories, choose the **formats** each should produce (Carousel / Square
     / Story / X), then **Generate All Selected**. The AI writes a plan + caption for every
     post, fetches images, and renders the whole batch to `outputs/batch_<timestamp>/`. Results
     (thumbnails + captions) show inline.
2. **Editor** — review/edit every line of a carousel plan. Choose **3–10 slides**. Fetch images
   (Pexels or Unsplash), swap any slide's image by query or **image URL**, preview each slide,
   then **Render Carousel**.
3. **Canvas** — mirrors the HTML, doesn't replace it. **Load Editor Slide** pulls the real
   text + background of the slide you're editing into a Fabric.js canvas so you can nudge type
   and layout by hand, then **Export PNG**. The HTML render stays the source of truth.
4. **Templates** — edit any template (`title/content/outro/cover/square/story/xpost.html`) or
   `brand.css` with live preview. Saves are backed up as `.bak`. `cover.html` is the standalone
   brand-splash card (the big K2 wordmark); it reads brand copy from `config.yaml` (`brand:`).

## Formats

| Format | Size | Notes |
|---|---|---|
| Carousel | 1080×1350 | Multi-slide: title + 1–8 content + outro |
| Square Post | 1080×1080 | Single feed card |
| Story | 1080×1920 | Vertical, story-safe margins, tap CTA |
| X / Twitter | 1600×900 | Landscape card + AI-written tweet text |

You pick the model (top-right) and feed category per run; everything is timed.

---

## CLI (engine without the UI)

```powershell
python feeds.py  --limit 5                 # pull + print stories
python filter.py --top 5                   # score with the configured LLM
python plan.py   --total-slides 6          # generate a JSON plan
python images.py "city skyline" --source unsplash
python render.py --no-images               # render a carousel
python meta.py   --verify                  # check token, accounts, and quota
python meta.py   --list-accounts           # brands -> IG / FB ids
python csv_import.py --template            # print a starter content CSV
```

---

## Publishing to Instagram & Facebook

Finished assets go **straight to Meta's Graph API** using your own long-lived access
token. Full setup guide: [docs/PUBLISHING_AND_CHANNELS.md](docs/PUBLISHING_AND_CHANNELS.md).

**From the app:** the **✅ Review** tab gives each post four actions — **Approve** (mark
ready, send nothing), **🗓 Schedule** (pick a time and destinations), **🚀 Publish now**,
and **Reject**. Approving holds by default; set `meta.publish.on_approve: "now"` to
publish on approve instead.

**Setup, once:** an Instagram Business/Creator account linked to a Facebook Page, a Meta
app with the Instagram Graph API product, and a long-lived token with
`instagram_basic`, `instagram_content_publish`, `pages_show_list`,
`pages_read_engagement`, `pages_manage_posts` in `.env` as `META_ACCESS_TOKEN`. Map each
brand under `meta.accounts` in `config.yaml`, then run `python meta.py --verify`.

> **Instagram needs `PUBLIC_BASE_URL`.** Meta fetches the rendered images from *you*, so
> they must sit at a public https address serving `/outputs` (a Tailscale Funnel URL, a
> tunnel, a CDN). `localhost` can never work. Facebook Pages take uploaded bytes and
> work without it.

**From the CLI** (`meta.py`, the engine without the UI):

```powershell
python meta.py --verify
python meta.py --type carousel --brand k2 --asset s1.png --asset s2.png --caption cap.txt
python meta.py --type post  --brand k2 --asset card.png --caption "Hello 👋"
python meta.py --type story --brand k2 --asset card.png --dry-run
python meta.py --quota --brand k2
```

`--dry-run` validates and reports what would be sent without publishing. Instagram's
25-posts-per-24h ceiling is tracked locally so you stop before Meta does. If no token is
set and `N8N_WEBHOOK_URL` is configured, approving falls back to that webhook.

---

## Post calendar

The **🗓 Calendar** tab is the review queue laid out by date. Click a day's **＋** (or a
post in the *Unscheduled* tray) to place it, click a scheduled post to move, cancel, or
publish it now, and use **✨ Auto-fill slots** to drop everything queued into your next
free posting times.

```yaml
schedule:
  times: ["09:00", "13:00", "18:00"]
  days:  ["mon", "tue", "wed", "thu", "fri"]
  auto_publish: true      # false = the calendar plans, but nothing is sent
  tick_seconds: 60
```

A background loop publishes posts whose slot has passed. Times are local. The app has to
be running for a scheduled post to go out — a slot that passes while it is closed
publishes on the next start rather than being skipped.

---

## CSV import

**📄 CSV Import** turns a spreadsheet into posts. Column names are matched loosely (case,
spaces, and underscores ignored, common aliases accepted), and two shapes work:

```csv
# one row per post
title,subtitle,slide1_heading,slide1_body,slide2_heading,slide2_body,cta,caption,hashtags,image_query,schedule

# one row per slide, grouped by post
post_id,order,heading,body,image_query
```

Upload it, check the preview table, then choose how the copy is written:

- **Use my text as-is** — the sheet's words are rendered verbatim. No LLM call, no
  rewriting, fast.
- **AI writes from each row** — the row becomes a brief and the normal planner writes the
  post. A `notes` or `body` column is the brief.

Extra columns steer each row individually: `brand`, `format`, `tone`, and `schedule`
(e.g. `2026-09-03 09:00`, which puts the post straight onto the calendar). Rendered posts
land in the Review queue, optionally auto-scheduled. Grab a starter sheet from the
**⬇ Template CSV** button or `python csv_import.py --template`.

---

## Brand

Defined once in `static/brand.css`:

- `--navy #0A0F1E` · `--teal #00B4C8` · `--green #00C896` · Calibri
- Logo: `static/logo.png` (circular K2 mark, used in corner + faded watermark)
- Handle: `@k2digitalmedia_` · Byline: Keerthivasan
- Background images are auto-muted + navy-tinted so copy always wins. Tune the
  `.bg-image` filter and `.slide::before` wash in `brand.css`.

Edit `content_profile.md` to change what scores high. Edit `config.yaml` for feeds, slide-count
rules, output size, and the LLM `base_url`. The default backend is Hermes:

```yaml
llm:
  base_url: "hermes://cli"
  model:    "hermes:gpt-5.5"
```

You can still override the model endpoint at runtime:

```powershell
$env:K2_LLM_BASE_URL="hermes://cli"               # Hermes CLI backend
$env:K2_LLM_MODEL="hermes:gpt-5.5"

# Or use LM Studio / llama.cpp / Ollama OpenAI-compatible endpoint:
$env:K2_LLM_BASE_URL="http://localhost:1234/v1"
$env:K2_LLM_MODEL="local-model-name"
$env:K2_LLM_API_KEY="not-needed-for-local"        # optional
```

**Switching engine at runtime:** the header has an **Engine** dropdown — flip between **Hermes**
and **Ollama** live (no restart). Post generation runs on whichever engine is selected, and the
Model dropdown refreshes to that engine's models. The selectable backends (and their host/model)
are defined in `config.yaml` under `llm.backends`:

```yaml
llm:
  backends:
    hermes:
      base_url: "hermes://cli"
      model:    "hermes:gpt-5.5"
    ollama:
      base_url: "http://localhost:11434/v1"
      model:    "qwen3:8b"
```

The Agent tab uses native tool calls when the selected backend supports them. Hermes CLI and plain
chat models use a JSON command protocol so the same fetch and generate tasks still work.

### Managing post data in MySQL (optional)

By default the generated-post / review queue is a JSON file (`library/review_queue.json`). To manage
post data in **MySQL** instead, set the `K2_MYSQL_*` vars in `.env` (at minimum `K2_MYSQL_DB`):

```
K2_MYSQL_HOST=localhost
K2_MYSQL_PORT=3306
K2_MYSQL_USER=root
K2_MYSQL_PASSWORD=...
K2_MYSQL_DB=k2_posts
```

The app auto-creates a `posts` table on first use, with real `DATETIME` columns for the **created**
and **approved** dates (so posts can be queried/sorted by date). If `K2_MYSQL_DB` is blank or MySQL
is unreachable, it transparently falls back to the JSON queue.

Brand personality lives in `config.yaml` under each brand's `personality` field. Post generation
now performs a final personality edit pass that rewrites copy fields while preserving facts,
formats, image queries, hashtags, handles, and output schema.

---

## Project structure

```
config.yaml          feeds (by category), slide rules, brand, LLM endpoint
content_profile.md   niche / tone / audience — drives scoring + planning
.env                 PEXELS_API_KEY, META_ACCESS_TOKEN, … (never committed)
run.bat              one-click launcher
static/
  brand.css          brand variables + slide base styles + image wash
  logo.png           K2 Digital Media logo
templates/           title.html · content.html · outro.html · cover.html
feeds.py             RSS/Atom ingestion (category-aware)
filter.py            local AI relevance scoring
plan.py              local AI post planning (strict JSON, 3–10 slides)
images.py            Pexels / Unsplash / URL fetching + cache
render.py            Jinja2 → HTML → Playwright → PNG
llm.py               thin OpenAI-compatible client (model list + switch)
meta.py              Instagram + Facebook publisher (Graph API) — engine + CLI
schedule.py          calendar maths: due checks, free slots, month grid
csv_import.py        spreadsheet → posts (loose column matching, 2 sheet shapes)
app.py               FastAPI control panel, canvas, template editor
image_cache/         downloaded images
outputs/             rendered carousels
```

# Architecture

A map of the modules and how a post flows through them. Useful for contributors and for
extending the app.

## Modules

| File | Responsibility |
|---|---|
| `app.py` | FastAPI control panel: all API endpoints, the entire web UI (one HTML/JS string), session state, the review queue, the background scheduler, and orchestration for manual/auto/bulk/CSV generation. |
| `feeds.py` | RSS/Atom ingestion (category-aware), `Story` dataclass. |
| `filter.py` | LLM relevance scoring (0–100) of stories against a brand's `profile`. |
| `plan.py` | LLM post planning. `plan_post` dispatches by format → `plan_story` (carousel), `plan_single`, `plan_special`, `plan_listicle`. `_ensure_caption` guarantees a caption; `regen_caption` rewrites just caption+hashtags. |
| `images.py` | Image fetching: Pexels / Unsplash / Google / direct URL, plus the Pillow "bake-in" filter for web images. |
| `render.py` | Jinja2 → Playwright (headless Chromium) → PNG. `generate_carousel`, single-card render, `render_slide_to_bytes` (live preview). |
| `llm.py` | Thin OpenAI-compatible client + a Hermes CLI shim. Backend switch + model list. |
| `brands.py` | Brand resolution, active-brand state, per-brand template/feed selection. |
| `meta.py` | Instagram + Facebook publishing **engine + CLI**: Graph API calls, container polling, asset validation, per-account rate guard, `publish()`, `verify()`. |
| `schedule.py` | The time side of a post: date parsing, "is this due?", free-slot proposal from the configured posting times, and month-grid assembly for the calendar. |
| `csv_import.py` | Spreadsheet → posts. Loose column matching, wide (row-per-post) and long (row-per-slide) shapes, `to_plan` (verbatim) and `to_story` (AI brief). |
| `scripts.py` | Platform-specific voiceover scripts. `trends.py` — trending topics. `db.py` — optional MySQL post/queue store. |
| `config.yaml` / `templates/` / `static/` | Config (gitignored), Jinja2 templates, logo/CSS. |

## Data flow

**Auto (RSS):**
```
feeds.fetch → filter.rank (LLM score) → plan.plan_post (LLM) → render.generate_carousel
   → review queue → (approve) → schedule or publish → meta.publish → Instagram / Facebook
```

**Manual:**
```
idea + notes + slide count  →  /api/plan/generate with a synthetic Story
   →  plan.plan_post  →  Editor  →  render  →  review  →  calendar  →  meta
```
(`/api/manual/suggest` proposes alternative angles before committing.)

**CSV import:**
```
sheet → csv_import.read_csv → detect shape → to_posts
   → direct: csv_import.to_plan (no LLM, copy used verbatim)
   → ai:     plan.plan_story with the row as a manual brief
   → render → review queue → (optional) auto-schedule onto the calendar
```

**Scheduled publish:**
```
_scheduler_loop (every schedule.tick_seconds)
   → schedule.is_due(entry) → _publish_entry → meta.publish
   → status: published (+ permalink) | failed (+ the Graph API's own message)
```

## Publishing

`meta.py` talks to the Graph API directly with your own long-lived token. Instagram
publishing is a two-step container flow (`POST /{ig_user_id}/media` → poll until
`FINISHED` → `POST /{ig_user_id}/media_publish`); carousels create one child container per
image first. Facebook Pages take multipart bytes, so they work without a public URL.

**Instagram needs `PUBLIC_BASE_URL`.** Meta's servers fetch the image themselves, so the
rendered PNGs must be reachable at a public https address that serves `/outputs`.
`meta.py` refuses localhost URLs up front rather than letting Meta return a vague error.

## Key state

- **Session** (`_session` in `app.py`): current `plan`, `image_paths` ({slide_idx: path}),
  fetched `stories`, and `used_urls`. In-memory, single uvicorn worker.
- **Persistent dedup** (`library/used_urls.json`): story URLs already turned into posts,
  so a restart doesn't re-serve them. Cleared by "Reset seen".
- **Review queue / calendar** (`library/review_queue.json` or MySQL): one list serves
  both tabs. An entry with `scheduled_at` + `status: "scheduled"` is a slot on the
  calendar grid. Statuses: `pending → approved → scheduled → published`, plus `failed`
  and `rejected`.
- **Rate guard** (`library/meta_rate.json`): a rolling 24h count of publishes per
  account, mirroring Instagram's 25-posts-per-day ceiling.
- **Library** (`library/plans/<brand>/*.json`): saved plans, including their fetched
  `image_paths`, so a reloaded plan is render-ready.

## Extension points

| To add… | Do this |
|---|---|
| A new **brand** | Config only — add a block under `brands:` and a `meta.accounts` entry. |
| A new **destination** for a brand | Add `"facebook"` to that account's `targets` (or set it per post in the schedule dialog). |
| A new **platform** (LinkedIn, X…) | Add a `publish_<platform>` function in `meta.py` and a branch in `publish()`'s target loop. |
| A new **post format** | Add to `formats:` in config + a Jinja2 template in `templates/`. |
| A new **LLM backend** | Add under `llm.backends`; `llm.py` speaks OpenAI-compatible + Hermes CLI. |
| A new **CSV column** | Add an alias tuple to `_ALIASES` in `csv_import.py`. |
| A new **image source** | Add a fetcher in `images.py` and a `source` option. |

## Conventions

- **Brand identity is config-driven** — no brand strings hardcoded in code; defaults are
  neutral/empty. Keep it that way.
- **Credentials live in `.env`**, never in `config.yaml` (which is shared, and whose
  example file is committed).
- **Rendering is HTML/CSS → Playwright**, not Pillow. New visual work should be a Jinja2
  template.
- **Nothing publishes without a decision.** Approving holds a post by default
  (`meta.publish.on_approve`); publishing happens from the calendar, a scheduled slot, or
  an explicit "Publish now".
- The UI is a single HTML/JS string in `app.py` served by `/`; edits there need a server
  restart (or `--reload`) to take effect.

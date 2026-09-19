"""Multi-brand carousel/reel generator with dashboard, review, and export tools.

Brand identity (name, handle, theme, feeds, Meta account) is config-driven —
see config.yaml (copy config.example.yaml to start). Nothing here is specific to
any one brand."""
from __future__ import annotations
import asyncio
import io
import json
import os
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Body, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

# Fresh clones ship config.example.yaml (a generic demo brand) but not config.yaml
# (which holds the user's real brands and is gitignored). Copy the example on first
# run so every module — all of which read config.yaml — works out of the box.
if not Path("config.yaml").exists() and Path("config.example.yaml").exists():
    shutil.copy("config.example.yaml", "config.yaml")


def _app_name() -> str:
    try:
        return (yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}).get("app", {}).get("name") or "Studio"
    except Exception:
        return "Studio"


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title=_app_name())

for _d in ("outputs", "image_cache", "library", "library/plans", "library/stories"):
    Path(_d).mkdir(parents=True, exist_ok=True)

app.mount("/static",      StaticFiles(directory="static"),      name="static")
app.mount("/outputs",     StaticFiles(directory="outputs"),      name="outputs")
app.mount("/image_cache", StaticFiles(directory="image_cache"),  name="images")
app.mount("/assets",      StaticFiles(directory="Assets"),       name="assets")

_executor = ThreadPoolExecutor(max_workers=3)

# ── Session state ─────────────────────────────────────────────────────────────
_session: dict[str, Any] = {
    "stories":      [],
    "plan":         None,
    "image_paths":  {},   # {str(slide_idx): str path}
    "batch":        [],   # last "Generate All Selected" results (per brand)
    "rendered_dir": None,
    "used_urls":    set(),  # stories already generated this session — not re-served
    "bulk_progress": None,  # {done,total,current,brand,running} during a bulk run
}


def _set_progress(done: int, total: int, current: str = "", brand: str = "",
                  running: bool = True) -> None:
    """Publish bulk-run progress so the UI can poll it via /api/session, and
    heartbeat the busy lock so a long run can't trip the stale-lock expiry."""
    _session["bulk_progress"] = {
        "done": done, "total": total, "current": current,
        "brand": brand, "running": running,
    }
    if running:
        _busy["since"] = time.time()

USED_FILE = Path("library/used_urls.json")


def _used_persisted() -> set[str]:
    """Story URLs already turned into posts — persisted so a server restart
    doesn't re-surface (and re-generate) the same content."""
    try:
        return set(json.loads(USED_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _used_all() -> set[str]:
    return set(_session.get("used_urls") or ()) | _used_persisted()


def _mark_used(*urls: str | None) -> None:
    """Remember stories we've generated from so a re-fetch won't surface them
    again — both this session AND persistently on disk. Clear via the 'Reset
    seen' button (/api/session/reset)."""
    seen = _session.setdefault("used_urls", set())
    persisted = _used_persisted()
    changed = False
    for u in urls:
        if u:
            seen.add(u)
            if u not in persisted:
                persisted.add(u); changed = True
    if changed:
        try:
            USED_FILE.parent.mkdir(parents=True, exist_ok=True)
            USED_FILE.write_text(json.dumps(sorted(persisted)), encoding="utf-8")
        except Exception as e:
            print(f"[used] persist failed: {e}")


# ── Autosave: persist the working plan + images PER BRAND so a page refresh, a
# server restart, OR a brand switch never loses in-progress editing. Each brand
# keeps its own autosave file (library/_autosave_<brand>.json).
def _autosave_path(brand_key: str | None = None) -> Path:
    if brand_key is None:
        from brands import active_key
        try:
            brand_key = active_key(_cfg()) or "default"
        except Exception:
            brand_key = "default"
    return Path(f"library/_autosave_{brand_key}.json")


def _autosave_write(brand_key: str | None = None) -> None:
    try:
        p = _autosave_path(brand_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "plan":        _session.get("plan"),
            "image_paths": _session.get("image_paths", {}),
            "stories":     _session.get("stories", []),
            "batch":       _session.get("batch", []),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[autosave] write failed: {e}")


def _autosave_restore(force: bool = False, brand_key: str | None = None) -> None:
    """Load a brand's autosaved plan + images + fetched stories into the session.
    With force=True (brand switch) it replaces the current state even if one is
    loaded, clearing to empty when that brand has no saved work."""
    if not force and (_session.get("plan") or _session.get("stories")):
        return
    try:
        data = json.loads(_autosave_path(brand_key).read_text(encoding="utf-8"))
    except Exception:
        if force:
            _session["plan"] = None
            _session["image_paths"] = {}
            _session["stories"] = []
            _session["batch"] = []
        return
    _session["plan"] = data.get("plan")
    _session["stories"] = data.get("stories", []) or []
    _session["batch"] = data.get("batch", []) or []
    imgs = data.get("image_paths") or {}
    _session["image_paths"] = {k: v for k, v in imgs.items() if v and Path(v).exists()}


def _autosave_stash(brand_key: str, **fields) -> None:
    """Merge specific fields (plan / stories / image_paths) into a brand's
    autosave WITHOUT touching the live session — for an in-flight job (fetch /
    generate) that finished after the user switched to a different brand."""
    p = _autosave_path(brand_key)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.update(fields)
    data.setdefault("plan", None)
    data.setdefault("stories", [])
    data.setdefault("image_paths", {})
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[autosave] stash failed: {e}")


# Single-flight guard: only one heavy LLM job (fetch/plan/batch) at a time so a
# page refresh + re-click can't stack overlapping Ollama runs.
_busy: dict[str, Any] = {"job": None, "since": 0.0}
# Long enough for a big (25–50 post) cross-brand bulk run. The job heartbeats
# `_busy["since"]` per item so the stale-lock expiry can't trip mid-run.
_BUSY_TIMEOUT = 3600  # seconds; stale lock auto-expires so we can never deadlock
_cancel: dict[str, bool] = {"flag": False}  # cooperative cancel for loop jobs


def _acquire(job: str) -> None:
    """Claim the job slot, or raise 409 if another job is genuinely in flight."""
    cur = _busy["job"]
    if cur and (time.time() - _busy["since"]) < _BUSY_TIMEOUT:
        raise HTTPException(409, f"A '{cur}' job is already running. Wait for it to finish.")
    _busy["job"] = job
    _busy["since"] = time.time()
    _cancel["flag"] = False  # fresh job starts uncancelled


def _release() -> None:
    _busy["job"] = None
    _busy["since"] = 0.0
    _cancel["flag"] = False


def _cancelled() -> bool:
    return _cancel["flag"]


def _cfg() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(_executor, fn, *args)


# ── Thread helpers ─────────────────────────────────────────────────────────────

def _sync_list_models():
    from llm import list_models
    return list_models()


# Sentinel category that pulls from Google Trends instead of the RSS feeds.
TRENDING_CAT = "__trending__"


def _sync_fetch_stories(limit, top_n, category, model, exclude=(), brand_key=None):
    from feeds import fetch_stories, configured_feed_urls, load_config
    from filter import rank_stories
    from brands import resolve_brand, brand_feeds_config, active_key
    config = load_config()
    # Resolve the brand explicitly from the per-request key so the worker is immune
    # to the global active-brand being flipped by a concurrent request (otherwise a
    # second column's fetch could make this one return the wrong brand's stories).
    bkey   = brand_key or active_key(config)
    brand  = resolve_brand(config, bkey)

    # 🔥 Trending: skip RSS + LLM ranking; Google already ranks these.
    if category == TRENDING_CAT:
        from trends import trending_stories
        stories = trending_stories(bkey, count=max(top_n, limit))
        stories = [s for s in stories if s["url"] not in exclude]
        return stories[:max(top_n, 1)] if top_n else stories

    scoped = brand_feeds_config(config, brand)
    urls = configured_feed_urls(scoped, category=category or None)
    if not urls:
        urls_all = configured_feed_urls(scoped)
        if not urls_all:
            raise ValueError(f"No feed URLs configured for brand '{brand.get('name','')}'.")
        urls = urls_all
    stories = fetch_stories(urls)
    # Drop stories already generated this session so a re-fetch surfaces fresh ones.
    if exclude:
        stories = [s for s in stories if s.url not in exclude]
    stories = stories[:limit]
    ranked  = rank_stories(stories, top_n, model=model, brand=brand,
                           should_cancel=_cancelled)
    return [vars(r) for r in ranked]


def _sync_generate_plan(story_dict, total_slides, model, tone="",
                        platform="instagram", content_type="general", manual=False):
    from feeds import Story
    from plan import plan_story
    from brands import resolve_brand
    if manual:
        # Manual Create is deliberately isolated from fetched-story metadata.
        # Only the text the user typed may reach the planner.
        story_dict = {
            "title": str(story_dict.get("title") or "").strip(),
            "summary": str(story_dict.get("summary") or "").strip(),
            "url": "", "published": "", "image": "",
        }
    story  = Story(**story_dict)
    config = _cfg()
    brand  = resolve_brand(config)
    allowed_types = [item.get("value") for item in brand.get("content_types", [])
                     if isinstance(item, dict) and item.get("value")]
    if content_type != "general" and content_type not in allowed_types:
        content_type = allowed_types[0] if allowed_types else "general"
    return plan_story(story, config, total_slides=total_slides, model=model,
                      brand=brand, tone=tone, platform=platform,
                      content_type=content_type,
                      polish_personality=not manual,
                      manual_mode=manual)


def _sync_suggest_angles(idea, tone="", platform="instagram",
                         content_type="educational"):
    """Propose a few alternative carousel angles/headlines for a manual idea."""
    from llm import chat_json
    from brands import resolve_brand
    config = _cfg()
    brand  = resolve_brand(config)
    bn = brand.get("name", "the brand")
    allowed_types = [item.get("value") for item in brand.get("content_types", [])
                     if isinstance(item, dict) and item.get("value")]
    if content_type not in allowed_types:
        content_type = allowed_types[0] if allowed_types else "general"
    platform_label = {"instagram": "Instagram", "linkedin": "LinkedIn",
                      "facebook": "Facebook", "x": "X"}.get(platform, "Instagram")
    from plan import CONTENT_TYPE_LABELS
    type_label = CONTENT_TYPE_LABELS.get(content_type, "General")
    system = (f"You are a social content strategist for {bn}. Given a rough idea, propose "
              f"4 DISTINCT, punchy {platform_label} {type_label} carousel angles "
              "(each a different take, max 70 "
              'chars). Return ONLY JSON: {"angles": ["...", "...", "...", "..."]}')
    user = (f"Idea: {idea}\nPlatform: {platform_label}\nContent type: {type_label}\n"
            f"Tone: {tone or 'default brand voice'}")
    out = chat_json(system, user)
    angles = out.get("angles") if isinstance(out, dict) else None
    return [a.strip() for a in (angles or []) if isinstance(a, str) and a.strip()][:4]


def _sync_regen_caption(plan, tone):
    from plan import regen_caption
    from brands import resolve_brand
    config = _cfg()
    return regen_caption(plan, brand=resolve_brand(config), config=config, tone=tone)


def _sync_generate_scripts(story_dict, topic, keywords, platform, content_type,
                           num_variants, duration, model, outline, hook, cta):
    from feeds import Story
    from scripts import generate_scripts
    from brands import resolve_brand
    config = _cfg()
    brand  = resolve_brand(config)
    story  = Story(**story_dict) if story_dict else None
    return generate_scripts(
        story=story, topic=topic, keywords=keywords, brand=brand, config=config,
        platform=platform, content_type=content_type, num_variants=num_variants,
        duration=duration, model=model, outline=outline, hook=hook, cta=cta,
    )


def _sync_fetch_images(plan, source):
    from images import fetch_images_for_plan
    raw = fetch_images_for_plan(plan, source=source)
    return {str(k): str(v) if v else None for k, v in raw.items()}


def _sync_swap_pexels(query, source):
    from images import fetch_image
    p = fetch_image(query, source)
    return str(p)


def _sync_search_images(query, source, count):
    from images import search_images
    return search_images(query, source, count)


def _sync_fetch_url(url, name, do_filter=True):
    from images import fetch_from_url
    p = fetch_from_url(url, name, do_filter=do_filter)
    return str(p)


# Renders are grouped per brand: outputs/<brand>/<date>_<slug>_<format>/.
# When a caller passes save=False (automation / preview), the render lands in a
# throwaway outputs/_tmp/<brand> that is wiped at the start of each unsaved run,
# so nothing accumulates on disk unless a run explicitly asks to keep it.
TMP_OUT = Path("outputs/_tmp")


def _out_root_for(brand_key: str, save: bool) -> Path:
    base = Path("outputs") if save else TMP_OUT
    return base / (brand_key or "default")


def _clear_tmp() -> None:
    import shutil
    shutil.rmtree(TMP_OUT, ignore_errors=True)


def _sync_render_carousel(plan, image_paths, save=True):
    from render import generate_carousel
    from brands import active_key
    bkey = active_key(_cfg()) or "default"
    if not save:
        _clear_tmp()
    out_root  = _out_root_for(bkey, save)
    int_paths = {int(k): (Path(v) if v else None) for k, v in image_paths.items()}
    return str(generate_carousel(plan, int_paths, out_root=out_root))


def _sync_preview_slide(template, variables):
    from render import render_slide_to_bytes
    return render_slide_to_bytes(template, variables)


def _sync_preview_fmt(template, variables, width, height):
    """Preview render at an explicit canvas size (single-card formats vary:
    square 1080², linkedin 1200², x 1600×900)."""
    from render import render_slide_to_bytes
    return render_slide_to_bytes(template, variables, width, height)


def _base_vars(plan):
    from render import _base_vars as rbv
    return rbv(plan)


def _slide_vars(plan, slide_idx):
    from render import _uri
    from brands import brand_template
    bv    = _base_vars(plan)
    brand = bv.get("brand", {})
    n     = len(plan.get("content_slides", []))
    if slide_idx == 0:
        # Image-forward brands lead the title with the first content image.
        timg = None
        if brand.get("image_forward"):
            raw = _session["image_paths"].get("0")
            timg = _uri(raw) if raw else None
        return brand_template(brand, "title", "title.html"), {**bv, "background_image": timg}
    if 1 <= slide_idx <= n:
        i   = slide_idx - 1
        sl  = plan["content_slides"][i]
        raw = _session["image_paths"].get(str(i))
        return brand_template(brand, "content", "content.html"), {
            **bv,
            "slide":            sl,
            "slide_number":     slide_idx,
            "background_image": _uri(raw) if raw else None,
        }
    return brand_template(brand, "outro", "outro.html"), {**bv, "background_image": None}


EDITABLE_FILES = {
    "title.html":        Path("templates/title.html"),
    "content.html":      Path("templates/content.html"),
    "outro.html":        Path("templates/outro.html"),
    "cover.html":        Path("templates/cover.html"),
    "jkr_title.html":    Path("templates/jkr_title.html"),
    "jkr_content.html":  Path("templates/jkr_content.html"),
    "jkr_outro.html":    Path("templates/jkr_outro.html"),
    "square.html":       Path("templates/square.html"),
    "story.html":        Path("templates/story.html"),
    "xpost.html":        Path("templates/xpost.html"),
    "quote.html":        Path("templates/quote.html"),
    "comparison.html":   Path("templates/comparison.html"),
    "breaking.html":     Path("templates/breaking.html"),
    "linkedin.html":     Path("templates/linkedin.html"),
    "listicle_content.html": Path("templates/listicle_content.html"),
    "brand.css":         Path("static/brand.css"),
}


def _fmt_allowed(fmt: str, brand_key: str, config: dict) -> bool:
    """A format may restrict itself to certain brands (e.g. LinkedIn -> K2 only)
    via formats.<fmt>.brands in config.yaml. No list = available everywhere."""
    allowed = (config.get("formats", {}).get(fmt, {}) or {}).get("brands")
    return (not allowed) or (brand_key in allowed)


def _render_item(story_dict, fmt, *, config, brand, brand_key, out_root,
                 model, source, total_slides, tone):
    """Plan + fetch images + render ONE (story, format) pair for a given brand.

    Returns a result dict (ok True/False). Shared by the single-brand batch and
    the cross-brand bulk runs so both honour brand_key + format restrictions."""
    from feeds import Story
    from plan import plan_post
    from images import fetch_images_for_plan, fetch_image, fetch_article_image
    from render import generate_post, CAROUSEL_FORMATS

    story = Story(**story_dict)
    if not _fmt_allowed(fmt, brand_key, config):
        return {"title": story.title, "format": fmt, "brand": brand_key,
                "ok": False, "skipped": True,
                "error": f"'{fmt}' is not available for {brand.get('name', brand_key)}"}
    try:
        plan = plan_post(story, fmt, config=config, total_slides=total_slides,
                         model=model, brand=brand, tone=tone)
        # Multi-image (carousel/listicle) vs single-card image fetch.
        # source == "none": skip image fetching entirely — render blank backgrounds,
        # fast, and add images by hand in the Editor afterwards.
        if source == "none":
            img_paths = None
        elif fmt in CAROUSEL_FORMATS:
            img_paths = fetch_images_for_plan(plan, source=source)
        else:
            img_paths = None
            q = plan.get("image_query")
            try:
                if source == "feed":
                    img_paths = {0: fetch_article_image(
                        plan.get("source_image", ""), plan.get("source_url", ""),
                        fallback_query=q or "")}
                elif q:
                    img_paths = {0: fetch_image(q, source)}
            except Exception as ie:
                print(f"[bulk] image fail '{q}': {ie}")
                img_paths = None
        out_dir = generate_post(plan, fmt, img_paths, out_root=out_root,
                                brand_key=brand_key)
        files   = sorted(p.name for p in Path(out_dir).glob("*.png"))
        # True only if at least one slide actually got a background image — lets
        # the UI flag fully-blank renders (e.g. "No images (fast)" mode) before
        # they're published looking unfinished.
        has_images = bool(img_paths) and any(img_paths.values())
        return {
            "title":       story.title,
            "format":      fmt,
            "brand":       brand_key,
            "slug":        plan.get("slug", ""),
            "rel":         Path(out_dir).relative_to("outputs").as_posix(),
            "files":       files,
            "caption":     plan.get("caption", ""),
            "image_query": plan.get("image_query", ""),
            "plan":        plan,
            "story":       story_dict,
            "has_images":  has_images,
            "ok":          True,
        }
    except Exception as e:
        return {"title": story.title, "format": fmt, "brand": brand_key,
                "story": story_dict, "ok": False, "error": str(e)}


def _sync_batch_run(items, model, source, save=True, tone=""):
    """Single-brand batch: plan + fetch images + render every (story, format)
    pair for the active brand. Output lands in outputs/<brand>/ (or _tmp)."""
    from brands import resolve_brand, active_key

    config    = _cfg()
    brand     = resolve_brand(config)
    brand_key = active_key(config) or "default"
    if not save:
        _clear_tmp()
    out_root  = _out_root_for(brand_key, save)
    results   = []
    total     = sum(len(it.get("formats", ["carousel"])) for it in items)
    done      = 0
    _set_progress(0, total, brand=brand_key)

    cancelled = False
    for item in items:
        if _cancelled():
            cancelled = True
            break
        item_tone = item.get("tone", tone)
        for fmt in item.get("formats", ["carousel"]):
            if _cancelled():
                cancelled = True
                break
            _set_progress(done, total, current=(item.get("story") or {}).get("title", ""),
                          brand=brand_key)
            results.append(_render_item(
                item["story"], fmt, config=config, brand=brand, brand_key=brand_key,
                out_root=out_root, model=model, source=source,
                total_slides=item.get("total_slides"), tone=item_tone))
            done += 1

    _set_progress(done, total, brand=brand_key, running=False)
    return {"batch_dir": out_root.as_posix(), "saved": save,
            "results": results, "cancelled": cancelled}


def _sync_bulk_run(groups, model, source, save=True, tone=""):
    """Cross-brand bulk run. ``groups`` = [{brand, items:[{story,formats,
    total_slides,tone}]}]. Each group renders for its OWN brand via an explicit
    brand_key, so K2 + JKR posts come out of one job with no global-brand desync.
    Brands run sequentially (timing is not a concern for this workflow)."""
    from brands import resolve_brand, list_brands

    config = _cfg()
    known  = list_brands(config)
    if not save:
        _clear_tmp()
    results: list[dict] = []
    total = sum(len(it.get("formats", ["carousel"]))
                for grp in groups for it in grp.get("items", []))
    done  = 0
    _set_progress(0, total)

    cancelled = False
    dirs: set[str] = set()
    for grp in groups:
        bkey = grp.get("brand")
        if bkey not in known:
            continue
        brand    = resolve_brand(config, bkey)
        out_root = _out_root_for(bkey, save)
        dirs.add(out_root.as_posix())
        for item in grp.get("items", []):
            if _cancelled():
                cancelled = True
                break
            item_tone = item.get("tone", tone)
            for fmt in item.get("formats", ["carousel"]):
                if _cancelled():
                    cancelled = True
                    break
                _set_progress(done, total,
                              current=(item.get("story") or {}).get("title", ""),
                              brand=bkey)
                results.append(_render_item(
                    item["story"], fmt, config=config, brand=brand, brand_key=bkey,
                    out_root=out_root, model=model, source=source,
                    total_slides=item.get("total_slides"), tone=item_tone))
                done += 1
            if cancelled:
                break
        if cancelled:
            break

    _set_progress(done, total, running=False)
    return {"batch_dirs": sorted(dirs), "saved": save,
            "results": results, "cancelled": cancelled}


def _sync_rerender_one(plan, fmt, image_paths, brand_key, save=True):
    """Re-render a single post (used after the user swaps a suggested image)."""
    from render import generate_post
    if not save:
        _clear_tmp()
    out_root  = _out_root_for(brand_key or "default", save)
    int_paths = {int(k): (Path(v) if v else None) for k, v in (image_paths or {}).items()}
    out_dir   = generate_post(plan, fmt, int_paths, out_root=out_root,
                              brand_key=brand_key)
    files = sorted(p.name for p in Path(out_dir).glob("*.png"))
    return {"rel": Path(out_dir).relative_to("outputs").as_posix(), "files": files}


# ── API: models ───────────────────────────────────────────────────────────────
@app.get("/api/models")
async def api_models():
    models = await _run(_sync_list_models)
    return {"models": models or ["qwen3:8b", "gemma:latest"]}


@app.put("/api/model")
async def api_set_model(body: dict = Body(...)):
    from llm import set_model
    set_model(body.get("model", ""))
    return {"ok": True}


# ── API: LLM backend (Hermes ↔ Ollama) ─────────────────────────────────────────
@app.get("/api/backend")
async def api_get_backend():
    """Active LLM backend plus the list of backends the user can switch to."""
    from llm import backend_info
    return backend_info()


@app.put("/api/backend")
async def api_set_backend(body: dict = Body(...)):
    """Switch the post-generation engine at runtime. Body: {backend: 'hermes'|'ollama'}."""
    from llm import set_backend
    try:
        info = set_backend(body.get("backend", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **info}


# ── API: formats ───────────────────────────────────────────────────────────────
@app.get("/api/formats")
async def api_formats():
    fmts = _cfg().get("formats", {})
    return {
        "formats": {k: v.get("name", k) for k, v in fmts.items()},
        # Brand restrictions so the UI can hide brand-locked formats (e.g. LinkedIn → K2).
        "restrict": {k: v["brands"] for k, v in fmts.items() if v.get("brands")},
    }


# ── API: batch ───────────────────────────────────────────────────────────────
@app.post("/api/batch/run")
async def api_batch_run(body: dict = Body(...)):
    items  = body.get("items", [])
    model  = body.get("model") or None
    source = body.get("source", "pexels")
    save   = body.get("save", True)
    tone   = body.get("tone", "")
    if not items:
        raise HTTPException(400, "No items selected.")
    _apply_brand(body.get("brand"))
    _acquire("batch")
    t0 = time.time()
    try:
        out = await _run(_sync_batch_run, items, model, source, save, tone)
        out["elapsed"] = round(time.time() - t0, 1)
        _mark_used(*[(it.get("story") or {}).get("url") for it in items])
        from brands import active_key
        _session["batch"] = out.get("results", [])   # persist per-brand for switch/refresh
        _autosave_write(active_key(_cfg()))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


# ── API: bulk (cross-brand) ────────────────────────────────────────────────────
@app.post("/api/bulk/run")
async def api_bulk_run(body: dict = Body(...)):
    """Generate posts for multiple brands in one job. Body:
    {groups:[{brand, items:[{story,formats,total_slides,tone}]}], model, source, save, tone}."""
    groups = body.get("groups", [])
    model  = body.get("model") or None
    source = body.get("source", "pexels")
    save   = body.get("save", True)
    tone   = body.get("tone", "")
    groups = [g for g in groups if g.get("items")]
    if not groups:
        raise HTTPException(400, "No stories selected for any brand.")
    _acquire("bulk")
    t0 = time.time()
    try:
        out = await _run(_sync_bulk_run, groups, model, source, save, tone)
        out["elapsed"] = round(time.time() - t0, 1)
        _mark_used(*[(it.get("story") or {}).get("url")
                     for g in groups for it in g.get("items", [])])
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.post("/api/bulk/rerender")
async def api_bulk_rerender(body: dict = Body(...)):
    """Re-render one post after swapping its image. Body:
    {plan, format, brand, image_paths:{idx:path}, save?}."""
    plan = body.get("plan")
    fmt  = body.get("format", "carousel")
    if not plan:
        raise HTTPException(400, "plan required")
    bkey = body.get("brand") or _active_brand_key()
    save = body.get("save", True)
    imgs = body.get("image_paths", {})
    try:
        out = await _run(_sync_rerender_one, plan, fmt, imgs, bkey, save)
        return out
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Agent: local tool-calling assistant ────────────────────────────────────────
def _agent_dispatch(config, scratch, model):
    """Tool implementations the agent can call. ``scratch`` holds the last fetch
    per brand so generate_posts can reference stories by number."""
    from brands import resolve_brand

    def fetch_top_stories(brand_key, category="", count=5):
        count   = max(1, min(int(count or 5), 25))
        stories = _sync_fetch_stories(max(count * 2, 15), count, category or None,
                                      model, (), brand_key)
        scratch[brand_key] = stories
        return {"summary": f"Fetched {len(stories)} stories for {brand_key}.",
                "stories": [{"n": i + 1, "title": s.get("title"),
                             "score": s.get("score"),
                             "reason": (s.get("reason") or "")[:120]}
                            for i, s in enumerate(stories)]}

    def generate_posts(brand_key, story_numbers, formats=None, tone=""):
        formats  = formats or ["carousel"]
        stories  = scratch.get(brand_key) or []
        if not stories:
            return {"error": f"No fetched stories for {brand_key}. Call fetch_top_stories first."}
        brand    = resolve_brand(config, brand_key)
        out_root = _out_root_for(brand_key, True)
        posts, made, errs = [], 0, []
        for n in story_numbers:
            if not (1 <= int(n) <= len(stories)):
                errs.append(f"#{n} out of range"); continue
            s  = stories[int(n) - 1]
            sd = {"title": s.get("title", ""), "summary": s.get("summary", ""),
                  "url": s.get("url", ""), "published": s.get("published", ""),
                  "image": s.get("image", "")}
            for fmt in formats:
                r = _render_item(sd, fmt, config=config, brand=brand, brand_key=brand_key,
                                 out_root=out_root, model=model, source="pexels",
                                 total_slides=None, tone=tone)
                if r.get("ok"):
                    made += 1
                    posts.append({"title": r["title"], "brand": brand_key,
                                  "format": r["format"], "rel": r["rel"], "files": r["files"],
                                  "caption": r.get("caption", "")})
                else:
                    errs.append(f"{(r.get('title') or '')[:30]}: {r.get('error') or 'skipped'}")
        _review_enqueue(posts)   # generated posts await your approval in the Review tab
        summary = f"Generated {made} post(s) for {brand_key} → sent to Review."
        if errs:
            summary += " Issues: " + "; ".join(errs[:4])
        return {"summary": summary, "posts": posts}

    return {"fetch_top_stories": fetch_top_stories, "generate_posts": generate_posts}


def _sync_agent_chat(text, model):
    import agent as ag
    from llm import _client
    from brands import list_brands
    config  = _cfg()
    brands  = list_brands(config)
    formats = {k: v.get("name", k) for k, v in config.get("formats", {}).items()}
    client, mdl = _client(model)
    scratch: dict = {}
    dispatch = _agent_dispatch(config, scratch, mdl)
    tools    = ag.build_tools(brands, formats)
    history  = _session.get("agent_msgs") or [
        {"role": "system", "content": ag.system_prompt(brands, formats)}]
    history.append({"role": "user", "content": text})
    reply, steps, posts = ag.run_agent(client, mdl, history, tools, dispatch)
    _session["agent_msgs"] = history[-40:]   # cap stored history
    return {"reply": reply, "steps": steps, "posts": posts, "model": mdl}


@app.post("/api/agent/chat")
async def api_agent_chat(body: dict = Body(...)):
    """Send a message to the local tool-calling agent. Body: {message, model?}."""
    text  = (body.get("message") or "").strip()
    model = body.get("model") or None
    if not text:
        raise HTTPException(400, "Empty message.")
    _acquire("agent")
    t0 = time.time()
    try:
        out = await _run(_sync_agent_chat, text, model)
        out["elapsed"] = round(time.time() - t0, 1)
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"{e}  (tip: check that your local model server is running and reachable)")
    finally:
        _release()


@app.post("/api/agent/reset")
async def api_agent_reset():
    _session["agent_msgs"] = None
    return {"ok": True}


# ── Review queue + publish (Layer 3) ───────────────────────────────────────────
# Agent / autopilot output lands here as "pending". You approve in the UI, which
# fires a webhook to n8n (N8N_WEBHOOK_URL) to publish via Publer/Metricool/etc.
REVIEW_FILE = Path("library/review_queue.json")


def _drop_legacy_publish(items: list[dict]) -> list[dict]:
    """Forget publish records left by the old Postiz adapter.

    Their errors ("rate limit: 4 requests left this hour") describe a service the
    app no longer talks to, so showing them on a card is worse than showing
    nothing. Dropped on read; the next save makes it permanent."""
    for it in items:
        if (it.get("publish") or {}).get("via") == "postiz":
            it.pop("publish", None)
    return items


def _restore_blocked_schedules(items: list[dict]) -> list[dict]:
    """Put back posts an older build dropped off the calendar.

    Until publishing learned to tell "Meta refused this" apart from "publishing
    is not set up yet", a scheduled post whose slot passed with no token
    configured was quietly marked approved and never retried — it simply
    vanished from the calendar with nothing to say why. Anything that still has
    its slot, never reached Meta, and failed for a setup reason goes back to
    scheduled so it publishes once the setup is finished."""
    for it in items:
        pub = it.get("publish") or {}
        if (it.get("status") == "approved" and it.get("scheduled_at")
                and pub and not pub.get("sent")
                and not pub.get("error") and not pub.get("errors")):
            it["status"] = "scheduled"
            it.setdefault("blocked_reason", pub.get("reason") or "publishing was not configured")
    return items


def _review_load() -> list[dict]:
    """Read the post queue from MySQL when configured, else the JSON file.
    MySQL errors fall back to JSON so a DB hiccup never breaks the queue."""
    import db
    if db.enabled():
        try:
            return _restore_blocked_schedules(_drop_legacy_publish(db.load_posts()))
        except Exception as e:
            print(f"[review] MySQL load failed, using JSON file: {e}")
    try:
        return _restore_blocked_schedules(
            _drop_legacy_publish(json.loads(REVIEW_FILE.read_text(encoding="utf-8"))))
    except Exception:
        return []


def _review_save(items: list[dict]) -> None:
    import db
    if db.enabled():
        try:
            db.save_posts(items)
            return
        except Exception as e:
            print(f"[review] MySQL save failed, using JSON file: {e}")
    REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _review_enqueue(posts: list[dict]) -> list[dict]:
    """Append freshly-rendered posts to the review queue as 'pending'."""
    import uuid
    if not posts:
        return []
    items = _review_load()
    added = []
    for p in posts:
        entry = {
            "id":      uuid.uuid4().hex[:12],
            "brand":   p.get("brand"),
            "title":   p.get("title"),
            "format":  p.get("format"),
            "rel":     p.get("rel"),
            "files":   p.get("files", []),
            "caption": p.get("caption", ""),
            "status":  "pending",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        items.append(entry)
        added.append(entry)
    _review_save(items)
    return added


def _meta_type(entry: dict) -> str:
    """Map a review entry (format + files) to a Meta content type."""
    fmt = (entry.get("format") or "").lower()
    if fmt == "story":
        return "story"
    if fmt in ("carousel", "listicle") or len(entry.get("files", [])) > 1:
        return "carousel"
    return "post"            # square / xpost / quote / single -> one IG image


def _entry_assets(entry: dict) -> tuple[list[str], list[str]]:
    """(local paths, public URLs) for an entry's rendered PNGs.

    Instagram needs the URLs (Meta fetches the bytes itself); Facebook prefers
    the local paths so it works even without a public address."""
    rel   = entry.get("rel", "")
    files = entry.get("files", [])
    paths = [str(Path("outputs") / rel / f) for f in files]
    base  = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    urls  = [f"{base}/outputs/{rel}/{f}" for f in files] if base else []
    return paths, urls


def _meta_publish(entry: dict) -> dict:
    """Publish an approved post straight to Instagram / Facebook via the Graph
    API. No-op (sent=False) when no Meta token is configured, so the queue still
    works standalone."""
    import meta
    cfg = meta.load_config(_cfg())
    if not meta.configured(cfg) and os.environ.get("N8N_WEBHOOK_URL", "").strip():
        return {"sent": False, "reason": "No Meta access token set (META_ACCESS_TOKEN)"}
    if not entry.get("files") or not entry.get("rel"):
        return {"sent": False, "reason": "no assets to publish"}

    paths, urls = _entry_assets(entry)
    targets = entry.get("targets") or None

    # Check every constraint locally first. A blocked post is a setup problem,
    # not a failed post — it keeps its slot and goes out once the setup is fixed.
    report = meta.preflight(entry.get("brand") or "", ptype=_meta_type(entry),
                            assets=paths, asset_urls=urls,
                            caption=entry.get("caption", ""), targets=targets,
                            config=_cfg(), network=False)
    if not report["ok"]:
        first = next((c for c in report["checks"]
                      if not c["ok"] and c["level"] == "block"), {})
        return {"sent": False, "via": "meta", "blocked": report["blocking"],
                "reason": first.get("detail") or "publishing is not configured yet",
                "preflight": report}

    try:
        res = meta.publish(
            brand=entry.get("brand") or "", ptype=_meta_type(entry),
            assets=paths, asset_urls=urls, caption=entry.get("caption", ""),
            targets=targets, config=_cfg(),
        )
        out = {"sent": bool(res.get("results")), "via": "meta",
               "results": res.get("results", []), "errors": res.get("errors", [])}
        first = (res.get("results") or [{}])[0]
        out["permalink"] = first.get("permalink", "")
        out["post_id"] = first.get("media_id", "")
        if res.get("errors") and not res.get("results"):
            out["error"] = res["errors"][0].get("error", "publish failed")
        return out
    except Exception as e:
        return {"sent": False, "via": "meta", "error": str(e)}


def _sync_publish(entry: dict) -> dict:
    """Publish an approved post. Meta first (when a token is configured), else
    the optional n8n webhook. No-op (but still marks approved) if neither is set,
    so the queue works standalone."""
    import requests
    import meta
    mp = _meta_publish(entry)
    if mp.get("sent") or mp.get("via") == "meta":
        return mp                       # Meta handled it (success or real error)

    url = os.environ.get("N8N_WEBHOOK_URL", "").strip()
    if not url:
        return {"sent": False, "reason": mp.get("reason")
                or "META_ACCESS_TOKEN / N8N_WEBHOOK_URL not set"}
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    images = [f"{base}/outputs/{entry['rel']}/{f}" for f in entry.get("files", [])]
    payload = {
        "brand":   entry.get("brand"),
        "title":   entry.get("title"),
        "format":  entry.get("format"),
        "caption": entry.get("caption", ""),
        "images":  images,
        "rel":     entry.get("rel"),
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        return {"sent": True, "via": "n8n", "status": r.status_code}
    except Exception as e:
        return {"sent": False, "via": "n8n", "error": str(e)}


def _publish_entry(entry: dict) -> dict:
    """Publish one queue entry and stamp the result onto it. Shared by the manual
    'publish now' path and the background scheduler so both record state the
    same way."""
    was = entry.get("status")
    pub = _sync_publish(entry)
    entry["publish"] = pub
    entry["publish_attempts"] = int(entry.get("publish_attempts") or 0) + 1
    entry["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
    if pub.get("sent"):
        entry["status"] = "published"
        entry["published_at"] = datetime.now().isoformat(timespec="seconds")
        entry.pop("blocked_since", None)
    elif pub.get("error") or pub.get("errors"):
        entry["status"] = "failed"       # Meta saw it and said no
    else:
        # Setup is incomplete (no token, no ig_user_id, no public URL, no
        # publisher at all). The post never left the machine, so it keeps its
        # slot and the next tick retries it — silently dropping it to "approved"
        # is how posts used to go missing after their slot passed, with nothing
        # on the card to say why.
        entry["status"] = was if was == "scheduled" else "approved"
        entry["blocked_reason"] = pub.get("reason") or "publishing is not configured"
        entry.setdefault("blocked_since", entry["last_attempt_at"])
    return pub


@app.get("/api/review")
async def api_review_list(status: str = ""):
    items = _review_load()
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: i.get("created", ""), reverse=True)
    pending = sum(1 for i in _review_load() if i.get("status") == "pending")
    return {"items": items, "pending": pending}


def _dashboard_items(brand: str = "", limit: int = 4) -> list[dict]:
    """Return the newest rendered carousel posts used by the dashboard."""
    items = [i for i in _review_load()
             if i.get("status") != "rejected"
             and (i.get("format") in ("carousel", "listicle")
                  or len(i.get("files") or []) > 1)
             and (not brand or i.get("brand") == brand)]
    items.sort(key=lambda i: i.get("created", ""), reverse=True)
    return items[:max(1, min(limit, 20))]


def _post_instructions(entry: dict, position: int = 0,
                       suggested_at: str = "") -> str:
    """Human-readable handoff bundled beside a post's ordered image files."""
    when = entry.get("scheduled_at") or suggested_at or "Choose the next available posting slot"
    files = entry.get("files") or []
    targets = ", ".join(entry.get("targets") or []) or "Instagram / selected brand channels"
    caption = (entry.get("caption") or "No caption supplied").strip()
    title = (entry.get("title") or "Untitled carousel").strip()
    order = "\n".join(f"{i + 1}. `{name}`" for i, name in enumerate(files)) or "No image files"
    number = f" {position}" if position else ""
    return f"""# Post{number}: {title}

## Publishing details

- Brand: {entry.get('brand') or 'Not specified'}
- Format: {entry.get('format') or 'carousel'}
- Status: {entry.get('status') or 'ready'}
- Post at: {when}
- Channels: {targets}

## Caption

{caption}

## Carousel order

Upload the images in this exact order:

{order}

## Final check

- Confirm the first image is the cover.
- Keep the image order shown above.
- Paste the full caption, including hashtags.
- Confirm the selected account and scheduled time before publishing.
- Preview the carousel once, then publish or schedule it.
"""


@app.get("/api/dashboard")
async def api_dashboard(brand: str = ""):
    """One compact overview for the modern landing dashboard."""
    import schedule as sched
    all_items = [i for i in _review_load() if not brand or i.get("brand") == brand]
    posts = _dashboard_items(brand, 4)
    taken = [i.get("scheduled_at") for i in all_items if i.get("scheduled_at")]
    unscheduled = sum(1 for i in all_items
                      if i.get("status") in ("pending", "approved", "failed")
                      and not i.get("scheduled_at"))
    try:
        suggestions = sched.next_slots(len(posts), config=_cfg(), taken=taken,
                                       start=datetime.now())
    except Exception:
        suggestions = []
    post_rows = []
    for idx, item in enumerate(posts):
        row = dict(item)
        row["suggested_at"] = sched.iso(suggestions[idx]) if idx < len(suggestions) else ""
        post_rows.append(row)
    return {
        "posts": post_rows,
        "counts": {
            "total": len(all_items),
            "pending": sum(1 for i in all_items if i.get("status") == "pending"),
            "scheduled": sum(1 for i in all_items if i.get("status") == "scheduled"),
            "published": sum(1 for i in all_items if i.get("status") == "published"),
            "ready": unscheduled,
        },
    }


def _export_zip(entries: list[dict]) -> bytes:
    import schedule as sched
    out = io.BytesIO()
    try:
        suggestions = sched.next_slots(len(entries), config=_cfg(), start=datetime.now())
    except Exception:
        suggestions = []
    guide = ["# Social Post Handoff\n",
             "This package contains ready-to-post carousel images and instructions.\n"]
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for idx, entry in enumerate(entries, start=1):
            safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "-", entry.get("title") or "carousel").strip("-")[:60] or "carousel"
            folder = f"{idx:02d}-{safe_title}"
            suggested = sched.iso(suggestions[idx - 1]) if idx <= len(suggestions) else ""
            instructions = _post_instructions(entry, idx, suggested)
            guide.append(instructions)
            archive.writestr(f"{folder}/INSTRUCTIONS.md", instructions)
            rel = entry.get("rel") or ""
            for image_idx, filename in enumerate(entry.get("files") or [], start=1):
                path = (Path("outputs") / rel / filename).resolve()
                output_root = Path("outputs").resolve()
                if output_root in path.parents and path.is_file():
                    archive.write(path, f"{folder}/{image_idx:02d}-{Path(filename).name}")
        archive.writestr("POSTING_GUIDE.md", "\n\n---\n\n".join(guide))
    return out.getvalue()


@app.get("/api/export/{rid}")
async def api_export_post(rid: str):
    entry = _find_entry(_review_load(), rid)
    payload = _export_zip([entry])
    return Response(payload, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="carousel-{rid}.zip"',
        "Cache-Control": "no-store",
    })


@app.get("/api/export-dashboard")
async def api_export_dashboard(brand: str = ""):
    entries = _dashboard_items(brand, 4)
    if not entries:
        raise HTTPException(404, "No rendered carousels are available to export.")
    payload = _export_zip(entries)
    label = re.sub(r"[^a-zA-Z0-9_-]+", "-", brand or "all-brands").strip("-")
    return Response(payload, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{label}-carousel-pack.zip"',
        "Cache-Control": "no-store",
    })


def _find_entry(items: list[dict], rid: str) -> dict:
    entry = next((i for i in items if i.get("id") == rid), None)
    if not entry:
        raise HTTPException(404, "Not found")
    return entry


@app.post("/api/review/{rid}/approve")
async def api_review_approve(rid: str, body: dict = Body(default={})):
    """Approve a post. What happens next is deliberate rather than automatic:

    ``hold`` (the default) just marks it ready so you can schedule it on the
    calendar; ``now`` publishes immediately. Set meta.publish.on_approve in
    config.yaml, or override per-click with {"publish": true}."""
    import meta
    items = _review_load()
    entry = _find_entry(items, rid)
    entry["approved_at"] = datetime.now().isoformat(timespec="seconds")
    if body and body.get("targets"):
        entry["targets"] = list(body["targets"])

    mode = (meta.load_config(_cfg())["publish"].get("on_approve") or "hold").lower()
    publish_now = body.get("publish") if body and "publish" in body else (mode == "now")

    if publish_now:
        pub = await _run(_publish_entry, entry)
    else:
        entry["status"] = "approved"
        pub = {"sent": False, "reason": "approved — schedule it or publish now"}
        entry["publish"] = pub
    _review_save(items)
    return {"ok": True, "publish": pub, "item": entry}


@app.post("/api/review/{rid}/publish")
async def api_review_publish(rid: str, body: dict = Body(default={})):
    """Publish one queued post to Instagram / Facebook right now."""
    items = _review_load()
    entry = _find_entry(items, rid)
    if body and body.get("targets"):
        entry["targets"] = list(body["targets"])
    entry.setdefault("approved_at", datetime.now().isoformat(timespec="seconds"))
    pub = await _run(_publish_entry, entry)
    _review_save(items)
    return {"ok": bool(pub.get("sent")), "publish": pub, "item": entry}


@app.put("/api/review/{rid}")
async def api_review_update(rid: str, body: dict = Body(default={})):
    """Edit a queued post before it goes out (caption / targets / schedule)."""
    import schedule as sched
    items = _review_load()
    entry = _find_entry(items, rid)
    b = body or {}
    if "caption" in b:
        entry["caption"] = b.get("caption") or ""
    if "targets" in b:
        entry["targets"] = list(b.get("targets") or [])
    if "scheduled_at" in b:
        try:
            when = sched.parse_when(b.get("scheduled_at"))
        except sched.ScheduleError as e:
            raise HTTPException(400, str(e))
        entry["scheduled_at"] = sched.iso(when)
        if when and entry.get("status") in ("pending", "approved", "failed"):
            entry["status"] = "scheduled"
        elif not when and entry.get("status") == "scheduled":
            entry["status"] = "approved"
    _review_save(items)
    return {"ok": True, "item": entry}


@app.post("/api/review/{rid}/reject")
async def api_review_reject(rid: str):
    items = _review_load()
    entry = next((i for i in items if i.get("id") == rid), None)
    if not entry:
        raise HTTPException(404, "Not found")
    entry["status"] = "rejected"
    _review_save(items)
    return {"ok": True}


@app.delete("/api/review/{rid}")
async def api_review_delete(rid: str):
    items = [i for i in _review_load() if i.get("id") != rid]
    _review_save(items)
    return {"ok": True}


@app.delete("/api/outputs/{rel:path}")
async def api_delete_output(rel: str):
    """Discard a rendered result you don't plan to use: deletes its output
    folder, drops any matching Review-queue entries, and removes it from this
    brand's persisted batch (so it doesn't reappear on refresh)."""
    base = Path("outputs").resolve()
    target = (base / rel).resolve()
    if base not in target.parents:
        raise HTTPException(400, "Invalid path.")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    before = _review_load()
    items = [i for i in before if i.get("rel") != rel]
    removed_review = len(before) - len(items)
    if removed_review:
        _review_save(items)

    _session["batch"] = [b for b in _session.get("batch", []) if b.get("rel") != rel]
    from brands import active_key
    _autosave_write(active_key(_cfg()))
    return {"ok": True, "removed_review": removed_review}


@app.post("/api/review/enqueue")
async def api_review_enqueue(body: dict = Body(default={})):
    """Push an already-rendered post into the review queue. Used by automated
    creation and Bulk result actions so generated posts can be
    approved → scheduled or published, the same as Agent/autopilot output. Body
    is a post dict: {brand, title, format, rel, files, caption}."""
    b = body or {}
    rel   = (b.get("rel") or "").strip()
    files = [f for f in (b.get("files") or []) if (Path("outputs") / rel / f).is_file()]
    if not rel or not files:
        raise HTTPException(400, "No rendered files found for this post.")
    added = _review_enqueue([{
        "brand":   b.get("brand"),
        "title":   b.get("title") or "",
        "format":  b.get("format") or "carousel",
        "rel":     rel,
        "files":   files,
        "caption": b.get("caption") or "",
    }])
    return {"ok": True, "added": added}


@app.post("/api/review/clear")
async def api_review_clear(body: dict = Body(default={})):
    """Remove all entries, or only those with a given status."""
    status = (body or {}).get("status", "")
    if status:
        items = [i for i in _review_load() if i.get("status") != status]
    else:
        items = []
    _review_save(items)
    return {"ok": True}


# ── CSV import: a content sheet -> rendered carousels ─────────────
# Upload a spreadsheet of post ideas (or finished slide copy) and turn each row
# into a real post. See csv_import.py for the accepted column shapes.

_CSV_UPLOAD = Path("library/last_import.csv")


def _sync_csv_generate(posts, *, brand_key, fmt, mode, model, source, save,
                       total_slides, tone):
    """Render one post per CSV row.

    ``direct`` uses the sheet copy verbatim (no LLM — what you wrote is what gets
    rendered); ``ai`` treats each row as a brief and runs the normal planner."""
    import csv_import
    from brands import resolve_brand
    from images import fetch_images_for_plan, fetch_image
    from render import generate_post, CAROUSEL_FORMATS

    config = _cfg()
    brand = resolve_brand(config, brand_key)
    if not save:
        _clear_tmp()
    out_root = _out_root_for(brand_key, save)

    results = []
    total = len(posts)
    _set_progress(0, total, brand=brand_key)

    for i, post in enumerate(posts):
        if _cancelled():
            break
        row_fmt = post.get("format") or fmt
        if not _fmt_allowed(row_fmt, brand_key, config):
            results.append({"title": post.get("title", ""), "format": row_fmt,
                            "brand": brand_key, "ok": False, "skipped": True,
                            "error": f"{row_fmt} is not available for this brand"})
            continue
        _set_progress(i, total, current=post.get("title", ""), brand=brand_key)
        try:
            if mode == "ai":
                from feeds import Story
                from plan import plan_story, plan_post
                story_dict = csv_import.to_story(post)
                if row_fmt in ("carousel", "listicle"):
                    plan = plan_story(Story(**story_dict), config,
                                      total_slides=total_slides, model=model,
                                      brand=brand, tone=post.get("tone") or tone,
                                      manual_mode=True)
                    plan.setdefault("format", row_fmt)
                else:
                    plan = plan_post(Story(**story_dict), row_fmt, config=config,
                                     model=model, brand=brand,
                                     tone=post.get("tone") or tone)
                # Anything the sheet states outright beats the model wording.
                if post.get("caption"):
                    plan["caption"] = post["caption"]
                if post.get("hashtags"):
                    plan["hashtags"] = post["hashtags"]
            else:
                plan = csv_import.to_plan(post, brand, fmt=row_fmt)

            img_paths = None
            if source != "none":
                try:
                    if row_fmt in CAROUSEL_FORMATS:
                        img_paths = fetch_images_for_plan(plan, source=source)
                    else:
                        q = plan.get("image_query") or post.get("image_query")
                        if q:
                            img_paths = {0: fetch_image(q, source)}
                except Exception as ie:
                    print(f"[csv] image fetch failed: {ie}")

            out_dir = generate_post(plan, row_fmt, img_paths, out_root=out_root,
                                    brand_key=brand_key)
            files = sorted(p.name for p in Path(out_dir).glob("*.png"))
            results.append({
                "title": post.get("title", ""), "format": row_fmt,
                "brand": brand_key, "slug": plan.get("slug", ""),
                "rel": Path(out_dir).relative_to("outputs").as_posix(),
                "files": files, "caption": plan.get("caption", ""),
                "plan": plan, "schedule": post.get("schedule", ""),
                "has_images": bool(img_paths) and any(img_paths.values()),
                "ok": True,
            })
        except Exception as e:
            results.append({"title": post.get("title", ""), "format": row_fmt,
                            "brand": brand_key, "ok": False, "error": str(e)})

    _set_progress(total, total, brand=brand_key, running=False)
    return {"batch_dir": out_root.as_posix(), "saved": save, "results": results}


@app.get("/api/csv/template")
async def api_csv_template():
    """Download a starter sheet with the column names the importer expects."""
    import csv_import
    return Response(csv_import.TEMPLATE_CSV, media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="content-template.csv"'})


@app.post("/api/csv/preview")
async def api_csv_preview(file: UploadFile = File(...)):
    """Parse an uploaded sheet and report what was found — shape, column mapping,
    and the posts it would build — before anything is rendered."""
    import csv_import
    raw = await file.read()
    if len(raw) > 5_000_000:
        raise HTTPException(400, "That CSV is larger than 5 MB.")
    try:
        headers, rows = csv_import.read_csv(raw)
        info = csv_import.inspect(headers, rows)
    except csv_import.CsvImportError as e:
        raise HTTPException(400, str(e))
    _CSV_UPLOAD.parent.mkdir(parents=True, exist_ok=True)
    _CSV_UPLOAD.write_bytes(raw)              # so a re-generate needs no re-upload
    info["filename"] = file.filename
    return info


@app.post("/api/csv/generate")
async def api_csv_generate(body: dict = Body(...)):
    """Render the imported rows into posts.

    Body: {posts?, brand?, format, mode: direct|ai, source, save, total_slides,
    tone, enqueue, autoschedule}. Omitting ``posts`` re-reads the last upload."""
    import csv_import
    b = body or {}
    posts = b.get("posts")
    if not posts:
        if not _CSV_UPLOAD.exists():
            raise HTTPException(400, "Upload a CSV first.")
        headers, rows = csv_import.read_csv(_CSV_UPLOAD.read_bytes())
        posts = csv_import.to_posts(headers, rows, mapping=b.get("mapping") or None)
    if not posts:
        raise HTTPException(400, "No usable rows in that sheet.")

    _apply_brand(b.get("brand"))
    from brands import active_key
    brand_key = b.get("brand") or active_key(_cfg()) or "default"
    mode = (b.get("mode") or "direct").lower()
    if mode not in ("direct", "ai"):
        raise HTTPException(400, "mode must be direct or ai")

    _acquire("csv import")
    t0 = time.time()
    try:
        # _run only forwards positional args, so bind the keywords here.
        job = partial(_sync_csv_generate, posts,
                      brand_key=brand_key, fmt=b.get("format") or "carousel",
                      mode=mode, model=b.get("model"),
                      source=b.get("source", "pexels"),
                      save=bool(b.get("save", True)),
                      total_slides=b.get("total_slides"), tone=b.get("tone", ""))
        res = await _run(job)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()

    ok = [r for r in res["results"] if r.get("ok")]
    if b.get("enqueue", True) and ok:
        added = _review_enqueue(ok)
        # A `schedule` column in the sheet puts the post straight on the calendar.
        import schedule as sched
        items = _review_load()
        by_id = {i["id"]: i for i in items}
        changed = False
        for entry, result in zip(added, ok):
            when = None
            if result.get("schedule"):
                try:
                    when = sched.parse_when(result["schedule"])
                except sched.ScheduleError:
                    when = None
            if when:
                row = by_id.get(entry["id"])
                if row:
                    row["scheduled_at"] = sched.iso(when)
                    row["status"] = "scheduled"
                    changed = True
        if changed:
            _review_save(items)
        if b.get("autoschedule"):
            await api_calendar_autofill({"ids": [e["id"] for e in added]})
        res["queued"] = len(added)

    res["elapsed"] = round(time.time() - t0, 1)
    res["ok_count"] = len(ok)
    return res


# ── Calendar & scheduling ─────────────────────────────────────────
# The queue is the calendar: an entry with a `scheduled_at` and status
# "scheduled" is a slot on the grid, and the background ticker publishes it when
# its time arrives.

_SCHEDULABLE = ("pending", "approved", "scheduled", "failed")


@app.get("/api/calendar")
async def api_calendar(year: int = 0, month: int = 0, brand: str = ""):
    """A month of queued posts, grouped into weeks for the calendar grid."""
    import schedule as sched
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be 1-12")

    items = [i for i in _review_load()
             if i.get("status") in _SCHEDULABLE + ("published",)]
    if brand:
        items = [i for i in items if i.get("brand") == brand]
    grid = sched.month_grid(year, month, items)
    grid["undated"] = [i for i in grid["undated"] if i.get("status") in _SCHEDULABLE]
    grid["slots"] = sched.slot_config(_cfg())
    return grid


@app.post("/api/calendar/schedule")
async def api_calendar_schedule(body: dict = Body(...)):
    """Put one post on the calendar (or move it). Body: {id, when, targets?}"""
    import schedule as sched
    rid = (body.get("id") or "").strip()
    if not rid:
        raise HTTPException(400, "No post id given.")
    try:
        when = sched.parse_when(body.get("when"))
    except sched.ScheduleError as e:
        raise HTTPException(400, str(e))
    if not when:
        raise HTTPException(400, "Provide a date/time to schedule.")

    items = _review_load()
    entry = _find_entry(items, rid)
    entry["scheduled_at"] = sched.iso(when)
    entry["status"] = "scheduled"
    if body.get("targets"):
        entry["targets"] = list(body["targets"])
    entry.setdefault("approved_at", datetime.now().isoformat(timespec="seconds"))
    entry.pop("publish", None)              # a re-schedule clears the old failure
    _review_save(items)
    return {"ok": True, "item": entry}


@app.post("/api/calendar/unschedule")
async def api_calendar_unschedule(body: dict = Body(...)):
    """Take a post off the calendar; it stays in the queue as approved."""
    items = _review_load()
    entry = _find_entry(items, (body.get("id") or "").strip())
    entry["scheduled_at"] = ""
    if entry.get("status") == "scheduled":
        entry["status"] = "approved"
    _review_save(items)
    return {"ok": True, "item": entry}


@app.post("/api/calendar/autofill")
async def api_calendar_autofill(body: dict = Body(default={})):
    """Drop every unscheduled post into the next free posting slots.

    Slots come from config.yaml -> schedule.times/days, and slots already taken
    by a scheduled post are skipped, so running this twice never double-books."""
    import schedule as sched
    b = body or {}
    items = _review_load()
    ids = b.get("ids") or []
    brand = b.get("brand") or ""

    if ids:
        queue = [i for i in items if i.get("id") in ids]
    else:
        queue = [i for i in items
                 if i.get("status") in ("pending", "approved")
                 and not i.get("scheduled_at")
                 and (not brand or i.get("brand") == brand)]
    queue.sort(key=lambda i: i.get("created", ""))
    if b.get("limit"):
        queue = queue[:int(b["limit"])]
    if not queue:
        return {"ok": True, "scheduled": 0, "items": []}

    taken = [i.get("scheduled_at") for i in items if i.get("status") == "scheduled"]
    try:
        start = sched.parse_when(b.get("start")) or datetime.now()
    except sched.ScheduleError as e:
        raise HTTPException(400, str(e))
    slots = sched.next_slots(len(queue), config=_cfg(), taken=taken, start=start)

    filled = []
    for entry, when in zip(queue, slots):
        entry["scheduled_at"] = sched.iso(when)
        entry["status"] = "scheduled"
        entry.setdefault("approved_at", datetime.now().isoformat(timespec="seconds"))
        filled.append({"id": entry["id"], "when": entry["scheduled_at"],
                       "title": entry.get("title", "")})
    _review_save(items)
    return {"ok": True, "scheduled": len(filled), "items": filled,
            "unplaced": max(0, len(queue) - len(slots))}


@app.get("/api/schedule/settings")
async def api_schedule_settings():
    """The posting-slot config the calendar auto-fill uses."""
    import schedule as sched
    return sched.slot_config(_cfg())


@app.put("/api/schedule/settings")
async def api_schedule_settings_save(body: dict = Body(...)):
    """Persist posting times/days back to config.yaml."""
    import schedule as sched
    cfg = _cfg()
    block = dict(cfg.get("schedule") or {})
    if "times" in body:
        times = [str(t).strip() for t in (body.get("times") or []) if str(t).strip()]
        for t in times:
            try:
                hh, mm = t.split(":")
                assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
            except (ValueError, AssertionError):
                raise HTTPException(400, f"{t} is not a HH:MM time.")
        block["times"] = times
    if "days" in body:
        block["days"] = [str(d).lower()[:3] for d in (body.get("days") or [])]
    if "auto_publish" in body:
        block["auto_publish"] = bool(body["auto_publish"])
    cfg["schedule"] = block
    Path("config.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return sched.slot_config(cfg)


# ── Background scheduler ──────────────────────────────────────────

_scheduler_state: dict[str, Any] = {"last_tick": None, "last_error": "", "published": 0,
                                    "last_blocked": "", "blocked": 0}


def _sync_run_due() -> list[dict]:
    """Publish every scheduled post whose slot has passed. Runs on the worker
    thread because the Graph API calls block."""
    import schedule as sched
    items = _review_load()
    due = [i for i in items if sched.is_due(i)]
    if not due:
        return []
    out = []
    for entry in due:
        pub = _publish_entry(entry)
        out.append({"id": entry.get("id"), "title": entry.get("title", ""),
                    "status": entry.get("status"), "sent": bool(pub.get("sent")),
                    "blocked": pub.get("blocked") or [],
                    "error": pub.get("error", "") or pub.get("reason", "")})
        title = (entry.get("title") or "")[:50]
        if pub.get("blocked"):
            # One line per distinct reason: a blocked post retries every tick and
            # a log that repeats every minute is a log nobody reads.
            key = f"{entry.get('id')}:{','.join(pub['blocked'])}"
            if _scheduler_state.get("last_blocked") != key:
                _scheduler_state["last_blocked"] = key
                print(f"[scheduler] {title} -> BLOCKED ({', '.join(pub['blocked'])}): "
                      f"{pub.get('reason', '')}")
        else:
            _scheduler_state["last_blocked"] = ""
            print(f"[scheduler] {title} -> {entry.get('status')}")
    _review_save(items)
    return out


async def _scheduler_loop() -> None:
    """Tick forever, publishing due posts. Cheap when the queue is empty, and one
    bad tick never kills the loop."""
    import schedule as sched
    while True:
        cfg = sched.slot_config(_cfg())
        await asyncio.sleep(max(15, cfg["tick_seconds"]))
        _scheduler_state["last_tick"] = datetime.now().isoformat(timespec="seconds")
        if not cfg["auto_publish"]:
            continue
        try:
            done = await _run(_sync_run_due)
            _scheduler_state["published"] += sum(1 for d in done if d["sent"])
            _scheduler_state["last_error"] = ""
        except Exception as e:                 # a bad tick must not stop the clock
            _scheduler_state["last_error"] = str(e)
            print(f"[scheduler] tick failed: {e}")


@app.on_event("startup")
async def _start_scheduler() -> None:
    asyncio.create_task(_scheduler_loop())


@app.get("/api/scheduler/status")
async def api_scheduler_status():
    import schedule as sched
    items = _review_load()
    upcoming = sorted((i for i in items if i.get("status") == "scheduled"),
                      key=lambda i: i.get("scheduled_at") or "")
    overdue = [i for i in upcoming if sched.is_due(i)]
    return {**_scheduler_state, **sched.slot_config(_cfg()),
            "scheduled": len(upcoming),
            "overdue": len(overdue),
            "blocked_reason": ((overdue[0].get("publish") or {}).get("reason", "")
                               if overdue else ""),
            "next": upcoming[0].get("scheduled_at") if upcoming else None}


@app.post("/api/scheduler/run")
async def api_scheduler_run():
    """Publish anything already due, without waiting for the next tick."""
    done = await _run(_sync_run_due)
    return {"ok": True, "published": done}


# ── Meta account status ───────────────────────────────────────────

@app.get("/api/meta/status")
async def api_meta_status():
    """What the Review/Calendar tabs show about publishing readiness — config
    only, no network call."""
    import meta
    cfg = meta.load_config(_cfg())
    accounts = []
    for key, acct in (cfg.get("accounts") or {}).items():
        env_name = acct.get("token_env") or cfg.get("token_env")
        accounts.append({
            "brand": key,
            "instagram": bool(acct.get("ig_user_id")),
            "facebook": bool(acct.get("fb_page_id")),
            "targets": acct.get("targets") or cfg["publish"].get("default_targets"),
            "token_set": bool(os.environ.get(env_name, "").strip()),
        })
    return {
        "configured": meta.configured(cfg),
        "public_base_url": meta.public_base() or "",
        "on_approve": cfg["publish"].get("on_approve", "hold"),
        "app_credentials": meta.app_credentials_status(),
        "accounts": accounts,
    }


@app.post("/api/meta/preflight")
async def api_meta_preflight(body: dict = Body(default={})):
    """Run every publishing constraint for one queued post (or a whole brand) and
    return each check, so the UI can say exactly what is missing."""
    import meta
    rid = (body or {}).get("id", "")
    entry = next((i for i in _review_load() if i.get("id") == rid), None) if rid else None

    def _run_pre():
        if entry:
            paths, urls = _entry_assets(entry)
            return meta.preflight(entry.get("brand") or "", ptype=_meta_type(entry),
                                  assets=paths, asset_urls=urls,
                                  caption=entry.get("caption", ""),
                                  targets=entry.get("targets") or None,
                                  config=_cfg(), network=True)
        brand = (body or {}).get("brand", "") or ""
        return meta.preflight(brand, config=_cfg(), network=True)

    try:
        return await _run(_run_pre)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/meta/test-post")
async def api_meta_test_post(body: dict = Body(default={})):
    """Publish one throwaway image to Instagram to prove the whole chain works.

    Real account, real post — it is the only way to verify publishing end to end,
    so it never runs on its own; the UI asks first."""
    import meta

    def _run_test():
        return meta.test_post((body or {}).get("brand", "") or "",
                              caption=(body or {}).get("caption", "") or "",
                              config=_cfg(), dry_run=bool((body or {}).get("dry_run")))

    try:
        return await _run(_run_test)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/meta/verify")
async def api_meta_verify(body: dict = Body(default={})):
    """Live check against the Graph API: token validity, account names, quota."""
    import meta

    def _run_verify():
        return meta.verify((body or {}).get("brand", ""), _cfg())

    try:
        return await _run(_run_verify)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Autopilot (Layer 2): deterministic scheduled run, fed to the review queue ──
def _sync_autopilot(brand_keys, count, formats, tone, model, source):
    from brands import list_brands, resolve_brand
    config = _cfg()
    known  = list_brands(config)
    bkeys  = [b for b in (brand_keys or list(known)) if b in known]
    formats = formats or ["carousel"]
    count   = max(1, min(int(count or 3), 15))
    queued, parts = [], []
    for bk in bkeys:
        if _cancelled():
            break
        stories = _sync_fetch_stories(max(count * 2, 15), count, None, model, (), bk)
        brand   = resolve_brand(config, bk)
        out_root = _out_root_for(bk, True)
        made = []
        for s in stories[:count]:
            sd = {"title": s.get("title", ""), "summary": s.get("summary", ""),
                  "url": s.get("url", ""), "published": s.get("published", ""),
                  "image": s.get("image", "")}
            for fmt in formats:
                r = _render_item(sd, fmt, config=config, brand=brand, brand_key=bk,
                                 out_root=out_root, model=model, source=source or "pexels",
                                 total_slides=None, tone=tone)
                if r.get("ok"):
                    made.append({"title": r["title"], "brand": bk, "format": r["format"],
                                 "rel": r["rel"], "files": r["files"], "caption": r.get("caption", "")})
        _review_enqueue(made)
        queued += made
        parts.append(f"{bk}: {len(made)}")
    return {"queued": len(queued), "summary": "; ".join(parts), "brands": bkeys}


@app.post("/api/agent/run")
async def api_agent_run(body: dict = Body(default={})):
    """Autopilot: fetch top stories per brand, render them, drop them in the
    review queue. Built for n8n Cron → POST here. Body (all optional):
    {brands:[..], count, formats:[..], tone, model, source}."""
    b = body or {}
    _acquire("autopilot")
    t0 = time.time()
    try:
        out = await _run(_sync_autopilot, b.get("brands"), b.get("count", 3),
                         b.get("formats"), b.get("tone", ""), b.get("model") or None,
                         b.get("source", "pexels"))
        out["elapsed"] = round(time.time() - t0, 1)
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


# ── API: categories ───────────────────────────────────────────────────────────
@app.get("/api/categories")
async def api_categories(brand: str = ""):
    """Categories for a brand. ``brand`` resolves explicitly (no global switch),
    so the bulk tab can read each brand's feeds without changing the active one."""
    from feeds import list_categories, load_config
    from brands import resolve_brand, brand_feeds_config
    config = load_config()
    b = resolve_brand(config, brand or None)
    scoped = brand_feeds_config(config, b)
    return {"categories": list_categories(scoped)}


# ── API: cancel ────────────────────────────────────────────────────────────────
@app.post("/api/cancel")
async def api_cancel():
    """Ask the running loop job (fetch/batch) to stop after the current item."""
    job = _busy["job"]
    if job:
        _cancel["flag"] = True
        return {"ok": True, "cancelling": job}
    return {"ok": False, "cancelling": None}


# ── API: session ──────────────────────────────────────────────────────────────
@app.post("/api/session/reset")
async def api_session_reset(body: dict = Body(default={})):
    """Forget which stories were already generated (the dedup set) — both the
    session set AND the persisted on-disk list — so previously-used/posted
    stories can be served again on the next fetch."""
    n = len(_used_all())
    _session["used_urls"] = set()
    try:
        USED_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "cleared": n}


@app.post("/api/session/clear")
async def api_session_clear(body: dict = Body(default={})):
    """Clear autosaved work without deleting saved Library items or outputs."""
    scope = body.get("scope", "all")
    if scope not in {"editor", "all"}:
        raise HTTPException(400, "scope must be 'editor' or 'all'")

    _session["plan"] = None
    _session["image_paths"] = {}
    _session["rendered_dir"] = None
    if scope == "all":
        _session["stories"] = []
        _session["batch"] = []
        _session["bulk_progress"] = None
    _autosave_write()
    return {"ok": True, "scope": scope}


# ── API: brands ────────────────────────────────────────────────────────────────
def _brand_content_types(brand: dict) -> list[dict[str, str]]:
    """Return only safe, complete content-type definitions to the browser."""
    return [
        {
            "value": str(item["value"]),
            "label": str(item.get("label") or item["value"]),
            "description": str(item.get("description") or ""),
        }
        for item in brand.get("content_types", [])
        if isinstance(item, dict) and item.get("value")
    ]


@app.get("/api/brands")
async def api_brands():
    from brands import list_brands, active_key
    config = _cfg()
    return {"brands": list_brands(config), "active": active_key(config)}


@app.get("/api/brand")
async def api_brand():
    from brands import resolve_brand, active_key
    config = _cfg()
    b = resolve_brand(config)
    t = b.get("theme", {})
    return {
        "key":     active_key(config),
        "name":    b.get("name", ""),
        "short":   b.get("short", b.get("name", "")),
        "handle":  b.get("handle", ""),
        "tagline": b.get("tagline", ""),
        "pitch":    b.get("pitch", ""),
        "services": b.get("services", ""),
        "location": b.get("location", ""),
        "website":  b.get("website", ""),
        "category": b.get("category", ""),
        "logo":    "/" + b.get("logo_path", "static/logo.png"),
        "shape":   b.get("logo_shape", "round"),
        "accent":  t.get("accent", "#00B4C8"),
        "accent2": t.get("accent2", "#00C896"),
        "navy":    t.get("navy", "#0A0F1E"),
        "text":    t.get("text", "#FFFFFF"),
        "content_types": _brand_content_types(b),
    }


@app.get("/api/brand-logo")
async def api_brand_logo(brand: str = ""):
    """Redirect to a specific brand's logo static URL (used by the bulk tab to
    show each brand's mark without switching the active brand)."""
    from fastapi.responses import RedirectResponse
    from brands import resolve_brand
    b = resolve_brand(_cfg(), brand or None)
    return RedirectResponse("/" + b.get("logo_path", "static/logo.png"))


@app.put("/api/brand")
async def api_set_brand(body: dict = Body(...)):
    from brands import set_active, resolve_brand, active_key, list_brands
    key = body.get("brand", "")
    config = _cfg()
    if key not in list_brands(config):
        raise HTTPException(404, f"Unknown brand '{key}'")
    _autosave_write()          # save the CURRENT brand's in-progress work first
    set_active(key)
    # Switching brand: each brand keeps its own work — restore the target brand's
    # saved plan + images + fetched stories (or empty). Dedup is per-brand/session.
    _session["used_urls"] = set()
    _autosave_restore(force=True)
    b = resolve_brand(config)
    return {
        "ok":     True,
        "key":    active_key(config),
        "active": active_key(config),
        "name":   b.get("name", ""),
        "logo":   "/" + b.get("logo_path", "static/logo.png"),
        "shape":  b.get("logo_shape", "round"),
        "accent": b.get("theme", {}).get("accent", "#00B4C8"),
        "content_types": _brand_content_types(b),
    }


# ── API: library (save/load generated content) ──────────────────────────────────
LIB_PLANS   = Path("library/plans")
LIB_STORIES = Path("library/stories")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "item"


def _active_brand_key() -> str:
    from brands import active_key
    return active_key(_cfg()) or ""


def _apply_brand(key: str | None) -> None:
    """Make the UI-selected brand authoritative for this action (and onward
    renders), so the dropdown can never desync from what the server uses."""
    from brands import set_active, list_brands
    if key and key in list_brands(_cfg()):
        set_active(key)


def _lib_list(folder: Path) -> list[dict]:
    # rglob: saved items live in per-brand subfolders (library/plans/<brand>/…).
    items = []
    for f in sorted(folder.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(f.read_text(encoding="utf-8")).get("meta", {})
        except Exception:
            meta = {}
        items.append({"id": f.stem, **meta})
    return items


def _lib_path(folder: Path, fid: str) -> Path | None:
    """Locate a saved item by id across the brand subfolders (or legacy root)."""
    fid = _slug_id(fid)
    return next(iter(folder.rglob(f"{fid}.json")), None)


@app.post("/api/library/plan")
async def api_lib_save_plan(body: dict = Body(default={})):
    plan = body.get("plan") or _session.get("plan")
    if not plan:
        raise HTTPException(400, "No plan to save.")
    name = body.get("name") or plan.get("title_card", {}).get("headline") \
        or plan.get("headline") or plan.get("slug", "plan")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bkey  = _active_brand_key() or "default"
    fid   = f"{_slug(name)}-{stamp}"
    meta  = {"name": name, "brand": bkey,
             "slug": plan.get("slug", ""), "format": plan.get("format", "carousel"),
             "tone": plan.get("tone", ""),
             "caption": plan.get("caption", ""),
             "when": datetime.now().isoformat(timespec="seconds")}
    dest  = LIB_PLANS / bkey
    dest.mkdir(parents=True, exist_ok=True)
    # Persist the fetched images too, so a reloaded plan is render-ready without
    # re-fetching. Stored as the same {slide_idx: image_cache path} session map.
    image_paths = body.get("image_paths") or _session.get("image_paths", {})
    (dest / f"{fid}.json").write_text(
        json.dumps({"meta": meta, "plan": plan, "image_paths": image_paths},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": fid, "meta": meta}


@app.get("/api/library/plans")
async def api_lib_plans():
    return {"plans": _lib_list(LIB_PLANS)}


@app.get("/api/library/plan/{fid}")
async def api_lib_get_plan(fid: str):
    f = _lib_path(LIB_PLANS, fid)
    if not f:
        raise HTTPException(404, fid)
    data = json.loads(f.read_text(encoding="utf-8"))
    _session["plan"] = data.get("plan")
    # Restore the saved images (drop any whose cached file is gone), so the
    # reloaded plan renders as-saved with no re-fetch.
    saved_imgs = data.get("image_paths") or {}
    _session["image_paths"] = {k: v for k, v in saved_imgs.items()
                              if v and Path(v).exists()}
    data["image_paths"] = _session["image_paths"]
    return data


@app.delete("/api/library/plan/{fid}")
async def api_lib_del_plan(fid: str):
    f = _lib_path(LIB_PLANS, fid)
    if f:
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/library/stories")
async def api_lib_save_stories(body: dict = Body(default={})):
    stories = _session.get("stories") or []
    if not stories:
        raise HTTPException(400, "No fetched stories to save.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bkey  = _active_brand_key()
    name  = body.get("name") or f"{bkey or 'stories'} · {len(stories)} stories"
    fid   = f"{_slug(bkey)}-{stamp}"
    meta  = {"name": name, "brand": bkey, "count": len(stories),
             "when": datetime.now().isoformat(timespec="seconds")}
    dest  = LIB_STORIES / (bkey or "default")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{fid}.json").write_text(
        json.dumps({"meta": meta, "stories": stories}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": fid, "meta": meta}


@app.get("/api/library/stories")
async def api_lib_stories():
    return {"stories": _lib_list(LIB_STORIES)}


@app.get("/api/library/stories/{fid}")
async def api_lib_get_stories(fid: str):
    f = _lib_path(LIB_STORIES, fid)
    if not f:
        raise HTTPException(404, fid)
    data = json.loads(f.read_text(encoding="utf-8"))
    _session["stories"] = data.get("stories", [])
    return data


@app.delete("/api/library/stories/{fid}")
async def api_lib_del_stories(fid: str):
    f = _lib_path(LIB_STORIES, fid)
    if f:
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/library/clear")
async def api_lib_clear(body: dict = Body(default={})):
    """Empty the saved library. ``kind`` = 'plans' | 'stories' | 'all' (default);
    optional ``brand`` limits the wipe to one brand's subfolder."""
    import shutil
    kind  = body.get("kind", "all")
    brand = _slug_id(body.get("brand", "")) if body.get("brand") else ""
    folders = {"plans": [LIB_PLANS], "stories": [LIB_STORIES],
               "all": [LIB_PLANS, LIB_STORIES]}.get(kind, [LIB_PLANS, LIB_STORIES])
    removed = 0
    for folder in folders:
        targets = [folder / brand] if brand else [folder]
        for t in targets:
            for f in t.rglob("*.json") if t.exists() else []:
                try:
                    f.unlink(); removed += 1
                except OSError:
                    pass
            # Drop now-empty brand subfolders, but keep the top-level folder.
            if brand and (folder / brand).exists():
                shutil.rmtree(folder / brand, ignore_errors=True)
    return {"ok": True, "removed": removed}


def _slug_id(fid: str) -> str:
    """Sanitise a library id from the URL to a safe filename stem."""
    return re.sub(r"[^A-Za-z0-9._-]", "", fid)


# ── API: stories ──────────────────────────────────────────────────────────────
@app.get("/api/stories")
async def api_get_stories():
    return {"stories": _session["stories"]}


@app.post("/api/stories/fetch")
async def api_fetch_stories(
    limit:    int = 20,
    top:      int = 5,
    category: str = "",
    model:    str = "",
    brand:    str = "",
):
    # Resolve per-request (no global switch) so two columns fetching different
    # brands in the bulk tab can't clobber each other's active brand mid-flight.
    from brands import list_brands
    bkey = brand if brand and brand in list_brands(_cfg()) else None
    _acquire("fetch stories")
    t0 = time.time()
    try:
        exclude = _used_all()   # session + persisted: never re-serve posted content
        ranked = await _run(_sync_fetch_stories, limit, top, category or None,
                            model or None, exclude, bkey)
        from brands import active_key
        eff_brand = bkey or active_key(_cfg())   # the brand actually fetched/scored for
        if active_key(_cfg()) == eff_brand:
            _session["stories"] = ranked          # still on this brand → update live view
            _autosave_write(eff_brand)
        else:
            _autosave_stash(eff_brand, stories=ranked)   # switched away → stash for return
        elapsed = round(time.time() - t0, 1)
        return {"stories": ranked, "elapsed": elapsed, "excluded": len(exclude)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        _release()


# ── API: plan ─────────────────────────────────────────────────────────────────
@app.post("/api/manual/suggest")
async def api_manual_suggest(body: dict = Body(default={})):
    """Suggestion chips for the Create tab: alternative angles for a rough idea."""
    idea = (body.get("idea") or "").strip()
    if not idea:
        raise HTTPException(400, "idea required")
    _apply_brand(body.get("brand"))
    _acquire("suggest angles")
    try:
        angles = await _run(
            _sync_suggest_angles, idea, body.get("tone", ""),
            body.get("platform", "instagram"), body.get("content_type", "educational"),
        )
        return {"angles": angles}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.post("/api/plan/generate")
async def api_generate_plan(body: dict = Body(...)):
    story       = body.get("story", {})
    total       = body.get("total_slides") or None
    model       = body.get("model") or None
    tone        = body.get("tone", "")
    platform    = body.get("platform", "instagram")
    content_type = body.get("content_type", "general")
    manual      = bool(body.get("manual", False))
    _apply_brand(body.get("brand"))
    from brands import active_key
    gen_brand = body.get("brand") or active_key(_cfg())   # the brand this plan is for
    _acquire("generate plan")
    t0 = time.time()
    try:
        plan = await _run(_sync_generate_plan, story, total, model, tone,
                          platform, content_type, manual)
        if active_key(_cfg()) == gen_brand:
            _session["plan"]        = plan      # still on this brand → live view
            _session["image_paths"] = {}
            _autosave_write(gen_brand)
        else:
            _autosave_stash(gen_brand, plan=plan, image_paths={})   # switched away → stash
        _mark_used(story.get("url"))
        elapsed = round(time.time() - t0, 1)
        return {"plan": plan, "elapsed": elapsed, "brand": gen_brand}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


# ── API: scripts ──────────────────────────────────────────────────────────────
@app.post("/api/scripts/generate")
async def api_generate_scripts(body: dict = Body(...)):
    story        = body.get("story") or None
    topic        = (body.get("topic") or "").strip()
    keywords     = body.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.replace(",", " ").split() if k.strip()]
    platform     = body.get("platform", "instagram_reel")
    content_type = body.get("content_type", "educational")
    num_variants = body.get("num_variants") or 3
    duration     = body.get("duration") or 30
    model        = body.get("model") or None
    outline      = body.get("outline") or []
    if isinstance(outline, str):
        outline = [ln.strip() for ln in outline.splitlines() if ln.strip()]
    hook         = (body.get("hook") or "").strip()
    cta          = (body.get("cta") or "").strip()
    if not story and not topic:
        raise HTTPException(400, "Provide a topic or select a story.")
    _apply_brand(body.get("brand"))
    _acquire("generate scripts")
    t0 = time.time()
    try:
        scripts = await _run(_sync_generate_scripts, story, topic, keywords,
                             platform, content_type, num_variants, duration, model,
                             outline, hook, cta)
        elapsed = round(time.time() - t0, 1)
        return {"scripts": scripts, "elapsed": elapsed}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.get("/api/plan")
async def api_get_plan():
    if not _session["plan"]:
        raise HTTPException(404, "No plan loaded.")
    return {"plan": _session["plan"]}


@app.post("/api/plan/caption")
async def api_regen_caption(body: dict = Body(default={})):
    plan = _session.get("plan")
    if not plan:
        raise HTTPException(400, "No plan loaded.")
    _apply_brand(body.get("brand"))
    _acquire("caption")
    try:
        out = await _run(_sync_regen_caption, plan, body.get("tone", ""))
        plan["caption"]  = out.get("caption", plan.get("caption", ""))
        plan["hashtags"] = out.get("hashtags", plan.get("hashtags", []))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _release()


@app.get("/api/session")
async def api_get_session():
    """Snapshot of server-side state so the UI can restore after a refresh — or
    after a server restart, via the on-disk per-brand autosave."""
    _autosave_restore()
    busy = _busy["job"] if (_busy["job"] and (time.time() - _busy["since"]) < _BUSY_TIMEOUT) else None
    return {
        "plan":        _session.get("plan"),
        "stories":     _session.get("stories", []),
        "image_paths": _session.get("image_paths", {}),
        "batch":       _session.get("batch", []),
        "busy":        busy,
        "progress":    _session.get("bulk_progress"),
    }


@app.post("/api/plan/dummy")
async def api_load_dummy():
    """Load the built-in preview plan into the session (for template/layout testing)."""
    _session["plan"]        = _dummy_plan()
    _session["image_paths"] = {}
    return {"plan": _session["plan"]}


@app.put("/api/plan")
async def api_update_plan(plan: dict = Body(...)):
    _session["plan"] = plan
    _autosave_write()          # every keystroke-synced edit persists to disk
    return {"ok": True}


# ── API: images ────────────────────────────────────────────────────────────────
@app.post("/api/images/fetch")
async def api_fetch_images(body: dict = Body(default={})):
    plan   = _session.get("plan")
    source = body.get("source", "pexels")
    if not plan:
        raise HTTPException(400, "No plan loaded.")
    t0 = time.time()
    try:
        paths = await _run(_sync_fetch_images, plan, source)
        _session["image_paths"] = paths
        _autosave_write()
        elapsed = round(time.time() - t0, 1)
        return {"image_paths": paths, "elapsed": elapsed}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/swap/{slide_idx}")
async def api_swap_image(slide_idx: int, body: dict = Body(...)):
    query  = body.get("query", "")
    source = body.get("source", "pexels")
    if not query:
        raise HTTPException(400, "query required")
    try:
        path = await _run(_sync_swap_pexels, query, source)
        _session["image_paths"][str(slide_idx)] = path
        _autosave_write()
        return {"path": path, "filename": Path(path).name}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/search")
async def api_search_images(body: dict = Body(...)):
    """Return a grid of candidate images for a query so the user can pick one."""
    query  = body.get("query", "")
    source = body.get("source", "pexels")
    count  = int(body.get("count", 10))
    if not query:
        raise HTTPException(400, "query required")
    try:
        results = await _run(_sync_search_images, query, source, count)
        return {"results": results, "source": source}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/images/from-url")
async def api_image_from_url(body: dict = Body(...)):
    url   = body.get("url", "")
    name  = body.get("name", "")
    sidx  = body.get("slide_idx")
    do_filter = body.get("filter", True)   # bake the slight filter into web images
    if not url:
        raise HTTPException(400, "url required")
    try:
        path = await _run(_sync_fetch_url, url, name or None, do_filter)
        if sidx is not None:
            _session["image_paths"][str(sidx)] = path
            _autosave_write()
        return {"path": path, "filename": Path(path).name}
    except Exception as e:
        # A bad/unreachable/non-image URL is a client problem, not a server fault.
        raise HTTPException(400, f"Couldn't fetch that image URL ({e})")


@app.post("/api/images/upload/{slide_idx}")
async def api_image_upload(slide_idx: int, file: UploadFile = File(...)):
    """Upload a local image for a slide. Saved to image_cache and set as the
    slide's background (same session map render/preview read)."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Image must be .jpg, .png, or .webp")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    import hashlib
    fid  = hashlib.md5(data).hexdigest()[:10]
    dest = Path("image_cache") / f"up-{slide_idx}-{fid}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    path = dest.as_posix()
    _session["image_paths"][str(slide_idx)] = path
    _autosave_write()
    return {"path": path, "filename": dest.name}


@app.delete("/api/images/{slide_idx}")
async def api_clear_image(slide_idx: int):
    _session["image_paths"].pop(str(slide_idx), None)
    _autosave_write()
    return {"ok": True}


@app.get("/api/images/cache")
async def api_list_cache():
    from images import list_cached
    files = list_cached()
    return {"files": [{"name": f.name, "url": f"/image_cache/{f.name}"} for f in files]}


@app.post("/api/images/cache/clear")
async def api_clear_cache():
    """Delete every cached background image and forget the session's image refs.

    Two-segment path (.../cache/clear) so it can't collide with the single-segment
    DELETE /api/images/{slide_idx} route.
    """
    cache = Path("image_cache")
    removed, freed = 0, 0
    for f in cache.glob("*"):
        if f.is_file():
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                pass
    _session["image_paths"] = {}
    return {"ok": True, "removed": removed, "freed_mb": round(freed / 1_000_000, 2)}


# ── API: preview ──────────────────────────────────────────────────────────────
@app.get("/api/preview/{slide_idx}")
async def api_preview(slide_idx: int, brand: str = ""):
    _apply_brand(brand)
    plan = _session.get("plan") or _dummy_plan()
    template, variables = _slide_vars(plan, slide_idx)
    try:
        png = await _run(_sync_preview_slide, template, variables)
        return Response(content=png, media_type="image/png")
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: render ───────────────────────────────────────────────────────────────
@app.post("/api/render")
async def api_render(body: dict = Body(default={})):
    plan = _session.get("plan")
    if not plan:
        raise HTTPException(400, "No plan loaded.")
    _apply_brand(body.get("brand"))
    save = body.get("save", True)
    t0 = time.time()
    try:
        out_dir = await _run(_sync_render_carousel, plan,
                             _session.get("image_paths", {}), save)
        _session["rendered_dir"] = out_dir
        elapsed = round(time.time() - t0, 1)
        files   = sorted(Path(out_dir).glob("*.png"))
        rel     = Path(out_dir).relative_to(Path("outputs")).as_posix()
        return {
            "output_dir": out_dir,
            "rel":        rel,
            "saved":      save,
            "files":      [f.name for f in files],
            "elapsed":    elapsed,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: templates ─────────────────────────────────────────────────────────────
@app.get("/api/templates")
async def api_list_templates():
    return {"files": list(EDITABLE_FILES.keys())}


@app.get("/api/template/{filename}")
async def api_get_template(filename: str):
    if filename not in EDITABLE_FILES:
        raise HTTPException(404, filename)
    return {"filename": filename, "content": EDITABLE_FILES[filename].read_text(encoding="utf-8")}


@app.put("/api/template/{filename}")
async def api_save_template(filename: str, body: dict = Body(...)):
    if filename not in EDITABLE_FILES:
        raise HTTPException(404, filename)
    path   = EDITABLE_FILES[filename]
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_text(body.get("content", ""), encoding="utf-8")
    return {"ok": True}


# Single-card template filename -> format key (so the Templates tab can preview
# them with representative content at the format's real dimensions).
SINGLE_TEMPLATE_FMT = {
    "square.html": "square", "story.html": "story", "xpost.html": "x",
    "quote.html": "quote", "comparison.html": "comparison",
    "breaking.html": "breaking", "linkedin.html": "linkedin",
}


def _dummy_single_plan(fmt: str) -> dict:
    """A filled-in single-card plan so each new template previews with real-looking
    content instead of empty fields."""
    p = {"slug": "preview", "format": fmt,
         "headline": "Your Headline Goes Here",
         "body": "— First key point\n— Second key point\n— Third key point",
         "image_query": "city skyline", "caption": "Preview caption.",
         "hashtags": ["preview"], "dm_keyword": "INFO"}
    if fmt == "quote":
        p.update({"quote": "73% of local buyers check you out online first.",
                  "context": "If your site isn't ready, you lose them before hello.",
                  "source_label": "via industry data"})
    elif fmt == "comparison":
        p.update({"headline": "DIY Website vs Pro Build",
                  "option_a": {"label": "DIY Builder", "points": "— Cheap upfront\n— Generic templates\n— Your time sunk"},
                  "option_b": {"label": "Pro Build", "points": "— Built to convert\n— Owned + scalable\n— You stay focused"},
                  "verdict": "Pro pays for itself in leads."})
    elif fmt == "breaking":
        p.update({"banner": "HOT TAKE", "headline": "AI search is rewriting SEO",
                  "take": "If your content isn't structured for it, you're invisible by next quarter."})
    elif fmt == "linkedin":
        p.update({"hook": "Most local sites fail in the first 5 seconds.",
                  "take": "Speed and clarity beat clever design — every time.",
                  "points": "— Load under 2s\n— One clear CTA\n— Proof above the fold",
                  "cta": "Book a free audit"})
    return p


@app.post("/api/template/preview/{filename}")
async def api_template_preview(filename: str, body: dict = Body(default={})):
    _apply_brand(body.get("brand"))
    plan = _session.get("plan") or _dummy_plan()
    try:
        if filename == "cover.html":
            template  = "cover.html"
            variables = {**_base_vars(plan), "category": body.get("category", "")}
            png = await _run(_sync_preview_slide, template, variables)
        elif filename in SINGLE_TEMPLATE_FMT:
            from render import format_config
            fmt   = SINGLE_TEMPLATE_FMT[filename]
            dplan = _dummy_single_plan(fmt)
            fc    = format_config(fmt)
            variables = {**_base_vars(dplan), "post": dplan, "background_image": None}
            png = await _run(_sync_preview_fmt, filename, variables,
                             fc.get("width", 1080), fc.get("height", 1080))
        elif filename == "listicle_content.html":
            slide = {"rank": 3, "heading": "THE THIRD PICK",
                     "body": "— Why it earns the spot\n— What makes it stand out",
                     "image_query": "spotlight"}
            variables = {**_base_vars(_dummy_plan()), "slide": slide,
                         "slide_number": 3, "background_image": None}
            png = await _run(_sync_preview_slide, filename, variables)
        else:
            idx_map = {"title.html": 0, "brand.css": 0,
                       "content.html": 1,
                       "outro.html": 1 + len(plan.get("content_slides", []))}
            idx = idx_map.get(filename, 0)
            template, variables = _slide_vars(plan, idx)
            png = await _run(_sync_preview_slide, template, variables)
        return Response(content=png, media_type="image/png")
    except Exception as e:
        raise HTTPException(500, str(e))


def _dummy_plan():
    return {
        "slug": "preview",
        "slide_count": 4,
        "title_card": {
            "headline": "3 Ways To Grow Your Brand Online",
            "subhead":  "A sample plan — edit every line, then render.",
        },
        "content_slides": [
            {"heading": "KNOW YOUR AUDIENCE", "body": "— Speak to one person, not everyone.\n— Lead with the problem you solve.", "image_query": "audience engagement"},
            {"heading": "BE CONSISTENT", "body": "— Show up on a steady schedule.\n— One clear message per post.", "image_query": "content calendar desk"},
        ],
        "outro_card": {"cta": "Follow for more tips", "handle": "@yourbrand"},
        "caption": "A sample caption — replace this with your own.",
        "hashtags": ["yourbrand", "marketing", "socialmedia"],
        "dm_keyword": "INFO",
    }


# ── Frontend ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    # Never cache the UI — otherwise the browser can serve a stale build (e.g.
    # an old brand switcher) and quietly use the wrong brand.
    html = FRONTEND_HTML.replace("__APP_NAME__", _app_name())
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__APP_NAME__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/css/css.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/fabric.js/5.3.1/fabric.min.js"></script>
<style>
@font-face{font-family:'Roboto Mono';font-weight:400;font-display:swap;src:url('/static/fonts/RobotoMono-400.woff2') format('woff2');}
@font-face{font-family:'Roboto Mono';font-weight:500;font-display:swap;src:url('/static/fonts/RobotoMono-500.woff2') format('woff2');}
@font-face{font-family:'Roboto Mono';font-weight:700;font-display:swap;src:url('/static/fonts/RobotoMono-700.woff2') format('woff2');}
:root{--navy:#0A0F1E;--teal:#00B4C8;--green:#00C896;--panel:#111827;--border:#1e2a3a;--text:#e4e8f0;--muted:#6b7a96;--red:#e05252;--yellow:#f5c542;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html{height:100%;-webkit-text-size-adjust:100%;}
body{font-family:Calibri,Arial,sans-serif;background:var(--navy);color:var(--text);height:100vh;height:100dvh;display:flex;flex-direction:row;overflow:hidden;font-size:15px;}
/* ── Sidebar (persistent left rail on desktop; slide-in drawer on mobile) ──── */
.sidebar{
  display:flex;flex-direction:column;width:212px;flex:0 0 212px;
  background:var(--panel);border-right:1px solid var(--border);
  height:100%;overflow-y:auto;-webkit-overflow-scrolling:touch;z-index:50;
}
.sidebar-logo{display:flex;align-items:center;gap:10px;padding:16px 16px 14px;border-bottom:1px solid var(--border);}
.logo{display:flex;align-items:center;gap:10px;min-width:0;}
.logo img{width:34px;height:34px;border-radius:50%;object-fit:cover;}
.logo-text{font-size:16px;font-weight:900;color:#fff;letter-spacing:-0.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.logo-text span{color:var(--teal);}
nav{display:flex;flex-direction:column;gap:3px;padding:12px 10px;}
.nav-section{font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;padding:14px 13px 5px;}
.nav-section:first-child{padding-top:2px;}
/* Library view toggle + grid/list */
.view-toggle{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden;}
.vt-btn{background:transparent;border:none;color:var(--muted);padding:5px 10px;cursor:pointer;font-size:12px;font-family:inherit;font-weight:600;}
.vt-btn.active{background:var(--teal);color:#000;}
.lib-body{flex:1;overflow-y:auto;padding:14px 18px;}
.lib-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;}
.lib-list{display:flex;flex-direction:column;gap:8px;}
.lib-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:9px;min-width:0;}
.lib-card:hover{border-color:var(--teal);}
.lib-card .lib-name{font-size:14px;font-weight:700;color:#fff;line-height:1.3;overflow-wrap:anywhere;}
.lib-row{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:12px;}
.lib-row:hover{border-color:var(--teal);}
.lib-row .lib-name{flex:1;min-width:0;font-size:13px;font-weight:600;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lib-badge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;padding:2px 8px;border-radius:10px;background:rgba(0,180,200,.15);color:var(--teal);white-space:nowrap;}
.lib-when{font-size:11px;color:var(--muted);white-space:nowrap;}
.lib-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.nav-btn{display:flex;align-items:center;gap:8px;width:100%;text-align:left;padding:9px 13px;border:none;border-radius:8px;cursor:pointer;font-size:13.5px;font-family:inherit;font-weight:600;background:transparent;color:var(--muted);transition:all .15s;}
.nav-btn:hover{color:#fff;background:rgba(255,255,255,.04);}
.nav-btn.active{background:var(--teal);color:#000;}
.nav-badge{margin-left:auto;}
/* Brand / engine / model controls live at the bottom of the sidebar. */
.header-right{margin-top:auto;display:flex;flex-direction:column;align-items:stretch;gap:8px;padding:12px 12px 16px;border-top:1px solid var(--border);}
.header-right label{font-size:11px;color:var(--muted);}
.header-right .timer{display:none;}
.model-sel{background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:6px 8px;font-size:12px;font-family:inherit;cursor:pointer;width:100%;}
.model-sel:focus{outline:none;border-color:var(--teal);}
/* Slim mobile top bar (hamburger + brand) — hidden on desktop. */
.topbar{display:none;align-items:center;gap:10px;height:52px;padding:0 12px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;}
.hamburger{display:none;background:none;border:none;color:#fff;font-size:22px;line-height:1;cursor:pointer;padding:4px 8px;border-radius:8px;}
.hamburger:hover{background:var(--border);}
.nav-overlay{display:none;}
/* The right-hand column holds the (mobile) top bar + the tab content. */
.app-main{display:flex;flex-direction:column;flex:1;min-width:0;overflow:hidden;}
/* Main layout */
.main{display:flex;flex:1;overflow:hidden;}
.tab{display:none;flex:1;overflow:hidden;}
.tab.active{display:flex;}
/* ═══ STORIES TAB ══════════════════════════════════════════════════════════ */
#tab-stories{flex-direction:column;}
/* Review + Create stack their toolbar above the content (default .tab is row). */
#tab-review{flex-direction:column;}
#tab-create{flex-direction:column;}
.stories-toolbar{display:flex;align-items:center;gap:8px;padding:12px 18px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;}
/* Keep a label glued to its control so they wrap together, not as loose items. */
.tb-group{display:inline-flex;align-items:center;gap:5px;flex-shrink:0;margin:0;}
.tb-spacer{flex:1;}
.cat-tabs{display:flex;gap:4px;overflow-x:auto;}
.cat-btn{padding:4px 12px;border:1px solid var(--border);border-radius:20px;background:transparent;color:var(--muted);cursor:pointer;font-size:12px;font-family:inherit;font-weight:600;white-space:nowrap;transition:all .15s;}
.cat-btn:hover{border-color:var(--teal);color:var(--teal);}
.cat-btn.active{background:var(--teal);border-color:var(--teal);color:#000;}
.stories-list{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:8px;}
/* Bulk tab: side-by-side brand columns */
#tab-bulk{flex-direction:column;}
.bulk-cols{flex:1;display:flex;gap:14px;overflow-y:auto;padding:14px 18px;align-items:flex-start;}
.bulk-col{flex:1;min-width:0;background:var(--panel);border:1px solid var(--border);border-radius:10px;display:flex;flex-direction:column;max-height:100%;}
.bulk-col-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--border);}
.bulk-col-head img{height:26px;width:auto;max-width:80px;object-fit:contain;border-radius:5px;}
.bulk-fmts{display:flex;flex-wrap:wrap;gap:5px;padding:10px 14px;border-bottom:1px solid var(--border);}
.bulk-list{overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:7px;min-height:60px;}
#bulk-results{overflow-y:auto;}
.bulk-item{background:#0d1828;border:1px solid var(--border);border-radius:8px;padding:9px 11px;font-size:12px;display:flex;gap:8px;align-items:flex-start;}
.bulk-item .bt{color:#fff;line-height:1.35;}
.bulk-item .bs{font-size:10px;color:var(--muted);}
/* Agent chat tab */
#tab-agent{flex-direction:column;}
.agent-log{flex:1;overflow-y:auto;padding:16px 18px;display:flex;flex-direction:column;gap:12px;}
.agent-msg{display:flex;}
.agent-msg.me{justify-content:flex-end;}
.agent-bubble{max-width:76%;padding:10px 13px;border-radius:12px;font-size:13px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere;}
.agent-msg.bot .agent-bubble{background:var(--panel);border:1px solid var(--border);color:var(--text);border-top-left-radius:3px;}
.agent-msg.me  .agent-bubble{background:var(--teal);color:#012;border-top-right-radius:3px;}
.agent-steps{margin:2px 0 0;display:flex;flex-direction:column;gap:4px;}
.agent-step{font-size:11px;color:var(--muted);background:#0d1828;border:1px solid var(--border);border-radius:7px;padding:5px 9px;}
.agent-step b{color:var(--teal);font-weight:700;}
.agent-posts{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}
.agent-posts a{display:block;}
.agent-posts img{height:96px;border-radius:6px;border:1px solid var(--border);}
.agent-input{display:flex;gap:8px;padding:12px 18px;border-top:1px solid var(--border);flex-shrink:0;align-items:flex-end;}
.agent-input textarea{flex:1;resize:none;max-height:140px;background:#0d1828;border:1px solid var(--border);border-radius:9px;color:#fff;padding:10px 12px;font-size:14px;font-family:inherit;line-height:1.4;}
.agent-input textarea:focus{outline:none;border-color:var(--teal);}
.agent-suggest{display:flex;flex-wrap:wrap;gap:6px;padding:0 18px 4px;}
.agent-suggest .chip{font-size:11px;color:var(--muted);background:#0d1828;border:1px solid var(--border);border-radius:16px;padding:5px 11px;cursor:pointer;transition:all .15s;}
.agent-suggest .chip:hover{border-color:var(--teal);color:var(--teal);}
.nav-badge{display:inline-flex;align-items:center;justify-content:center;min-width:16px;height:16px;padding:0 4px;margin-left:5px;font-size:10px;font-weight:800;border-radius:9px;background:var(--red);color:#fff;vertical-align:middle;}
/* Review cards */
.rv-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;display:flex;gap:12px;flex-wrap:wrap;}
.rv-imgs{display:flex;gap:6px;flex-wrap:wrap;}
.rv-imgs img{height:110px;border-radius:6px;border:1px solid var(--border);}
.rv-body{flex:1;min-width:200px;display:flex;flex-direction:column;gap:6px;}
.rv-cap{font-size:12px;color:var(--muted);white-space:pre-wrap;max-height:120px;overflow-y:auto;line-height:1.4;}
.rv-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:2px;}
.rv-status{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;text-transform:uppercase;}
.rv-status.pending{background:rgba(245,197,66,.15);color:var(--yellow);}
.rv-status.published{background:rgba(0,200,150,.15);color:var(--green);}
.rv-status.approved{background:rgba(0,180,200,.15);color:var(--teal);}
.rv-status.rejected{background:rgba(224,82,82,.15);color:var(--red);}
.rv-status.scheduled{background:rgba(150,120,255,.18);color:#a78bfa;}
.rv-status.failed{background:rgba(224,82,82,.22);color:var(--red);}
.rv-blocked{margin-top:6px;font-size:11.5px;line-height:1.5;color:var(--yellow);
  background:rgba(245,197,66,.09);border-left:3px solid var(--yellow);
  padding:6px 9px;border-radius:0 6px 6px 0;}
.rv-blocked a{color:var(--yellow);margin-left:6px;}
.rv-when{font-size:11px;color:#a78bfa;font-weight:600;white-space:nowrap;}
/* Post calendar — a fixed 7-column month grid; each day scrolls its own chips. */
.cal-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.cal-head{display:grid;grid-template-columns:repeat(7,1fr);border-bottom:1px solid var(--border);}
.cal-head div{padding:7px 8px;font-size:10.5px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;text-align:center;}
.cal-grid{flex:1;display:grid;grid-template-columns:repeat(7,1fr);grid-auto-rows:minmax(104px,1fr);overflow-y:auto;}
.cal-day{border-right:1px solid var(--border);border-bottom:1px solid var(--border);padding:5px 6px;display:flex;flex-direction:column;gap:4px;min-width:0;min-height:104px;}
.cal-day.out{background:rgba(0,0,0,.18);}
.cal-day.today{background:rgba(0,180,200,.07);}
.cal-day:hover{background:rgba(255,255,255,.035);}
.cal-daynum{font-size:11px;font-weight:700;color:var(--muted);display:flex;align-items:center;gap:5px;}
.cal-day.today .cal-daynum{color:var(--teal);}
.cal-add{margin-left:auto;opacity:0;background:none;border:none;color:var(--teal);cursor:pointer;font-size:13px;line-height:1;padding:0 2px;font-family:inherit;}
.cal-day:hover .cal-add{opacity:1;}
.cal-chips{display:flex;flex-direction:column;gap:3px;overflow-y:auto;min-height:0;}
.cal-chip{display:flex;align-items:center;gap:5px;background:#0d1828;border:1px solid var(--border);border-left:3px solid var(--teal);border-radius:5px;padding:3px 6px;font-size:10.5px;cursor:pointer;text-align:left;color:var(--text);font-family:inherit;width:100%;min-width:0;}
.cal-chip:hover{border-color:var(--teal);}
.cal-chip.published{border-left-color:var(--green);opacity:.72;}
.cal-chip.failed{border-left-color:var(--red);}
.cal-chip .t{font-weight:700;color:var(--muted);flex-shrink:0;}
.cal-chip .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;}
.cal-unsched{border-top:1px solid var(--border);background:var(--panel);padding:8px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;max-height:130px;overflow-y:auto;flex-shrink:0;}
.cal-pill{display:flex;align-items:center;gap:6px;background:#0d1828;border:1px solid var(--border);border-radius:20px;padding:4px 11px;font-size:11.5px;cursor:pointer;color:var(--text);font-family:inherit;max-width:280px;}
.cal-pill:hover{border-color:var(--teal);}
.cal-pill span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
/* CSV import — mapping preview table + per-row post cards. */
.csv-table{width:100%;border-collapse:collapse;font-size:11.5px;}
.csv-table th{position:sticky;top:0;background:#0d1828;color:var(--muted);font-weight:700;text-align:left;padding:6px 9px;border-bottom:1px solid var(--border);white-space:nowrap;font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;}
.csv-table td{padding:6px 9px;border-bottom:1px solid var(--border);vertical-align:top;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);}
.csv-scroll{overflow:auto;max-height:300px;border:1px solid var(--border);border-radius:8px;background:var(--panel);}
.csv-drop{border:2px dashed var(--border);border-radius:12px;padding:30px 20px;text-align:center;cursor:pointer;transition:all .15s;background:var(--panel);}
.csv-drop:hover,.csv-drop.over{border-color:var(--teal);background:rgba(0,180,200,.06);}
.csv-slide{background:#0d1828;border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:11.5px;}
.csv-slide b{color:var(--teal);font-size:10.5px;letter-spacing:.4px;}
.story-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;cursor:pointer;transition:border-color .15s;}
.story-card:hover{border-color:var(--teal);}
.story-card.selected{border-color:var(--teal);background:#0d1e2e;}
.s-top{display:flex;align-items:center;gap:10px;margin-bottom:6px;}
.score-pill{padding:2px 10px;border-radius:20px;font-size:12px;font-weight:700;flex-shrink:0;}
.sh{background:rgba(0,200,150,.15);color:var(--green);}
.sm{background:rgba(0,180,200,.15);color:var(--teal);}
.sl{background:rgba(107,122,150,.12);color:var(--muted);}
.story-title{font-size:14px;font-weight:700;color:#fff;line-height:1.3;}
.story-reason{font-size:12px;color:var(--muted);line-height:1.4;margin-bottom:6px;}
.story-actions{display:flex;gap:6px;margin-top:8px;}
/* ═══ EDITOR TAB ═══════════════════════════════════════════════════════════ */
#tab-editor{flex-direction:row;}
.editor-left{width:400px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
.editor-right{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.panel-header{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);flex-shrink:0;}
.panel-header h3{font-size:13px;font-weight:700;color:#fff;flex:1;}
.plan-form{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:12px;}
.field-group{display:flex;flex-direction:column;gap:4px;}
.field-group label{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}
.field-group input,.field-group textarea,.field-group select{background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:7px 10px;font-family:inherit;font-size:13px;line-height:1.4;resize:vertical;}
.field-group input:focus,.field-group textarea:focus,.field-group select:focus{outline:none;border-color:var(--teal);}
.slide-sec{background:rgba(0,180,200,.04);border:1px solid var(--border);border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:9px;}
.slide-sec-title{font-size:12px;font-weight:700;color:var(--teal);}
.img-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
.img-row .url-inp{min-width:120px;}
.img-row .btn{flex-shrink:0;}
.img-thumb{width:48px;height:48px;object-fit:cover;border-radius:5px;border:1px solid var(--border);background:var(--panel);flex-shrink:0;}
.img-thumb.empty{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:18px;}
.src-sel{background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:5px 6px;font-size:12px;font-family:inherit;}
/* Preview pane */
.preview-pane{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.preview-nav{display:flex;align-items:center;gap:8px;}
.slide-cnt{font-size:12px;color:var(--muted);min-width:52px;text-align:center;}
.preview-wrap{flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center;padding:14px;background:#070b14;}
.preview-wrap .preview-image-shell{position:relative;display:inline-block;flex:0 0 auto;max-width:100%;}
.preview-wrap img{display:block;max-width:100%;height:auto;border-radius:5px;object-fit:contain;box-shadow:0 8px 32px rgba(0,0,0,.6);}
.preview-placeholder{color:var(--muted);text-align:center;font-size:13px;line-height:1.6;}
/* Image from URL row */
.url-row{display:flex;gap:6px;align-items:center;}
.url-inp{flex:1;background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:5px 8px;font-size:12px;font-family:inherit;}
.url-inp:focus{outline:none;border-color:var(--teal);}
/* ═══ CANVAS TAB ═══════════════════════════════════════════════════════════ */
#tab-canvas{flex-direction:row;}
.canvas-left{width:210px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;padding:14px 12px;gap:14px;}
.canvas-left h4{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px;}
.canvas-center{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.canvas-toolbar{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;}
.canvas-area{flex:1;display:flex;align-items:center;justify-content:center;background:#060a13;overflow:hidden;padding:20px;}
.canvas-wrap{position:relative;border:1px solid var(--border);box-shadow:0 8px 32px rgba(0,0,0,.6);}
.canvas-wrap .canvas-container{max-width:100%;}
.canvas-right{width:210px;flex-shrink:0;border-left:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;padding:14px 12px;gap:12px;}
.canvas-right h4{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px;}
.prop-row{display:flex;flex-direction:column;gap:4px;}
.prop-row label{font-size:11px;color:var(--muted);}
.prop-inp{background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:5px 7px;font-size:12px;font-family:inherit;width:100%;}
.prop-inp:focus{outline:none;border-color:var(--teal);}
.color-row{display:flex;gap:5px;flex-wrap:wrap;}
.color-swatch{width:26px;height:26px;border-radius:5px;cursor:pointer;border:2px solid transparent;transition:all .15s;}
.color-swatch:hover,.color-swatch.active{border-color:#fff;}
.preset-btns{display:flex;flex-direction:column;gap:5px;}
/* ═══ TEMPLATES TAB ════════════════════════════════════════════════════════ */
#tab-templates{flex-direction:row;}
.tmpl-left{width:220px;flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;}
.tmpl-files{flex:1;overflow-y:auto;padding:10px;}
.tmpl-file-btn{display:block;width:100%;text-align:left;padding:8px 11px;border:none;border-radius:6px;background:transparent;color:var(--text);cursor:pointer;font-family:inherit;font-size:13px;font-weight:600;margin-bottom:3px;transition:background .15s;}
.tmpl-file-btn:hover{background:var(--border);}
.tmpl-file-btn.active{background:var(--teal);color:#000;}
.tmpl-right{flex:1;display:flex;flex-direction:column;overflow:hidden;}
.code-area{flex:1;overflow:hidden;display:flex;}
.code-area .CodeMirror{flex:1;height:100%;font-size:13px;font-family:'Cascadia Code','Consolas',monospace;}
.tmpl-preview-pane{width:300px;flex-shrink:0;border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
.tmpl-preview-header{padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;font-weight:700;color:#fff;}
.tmpl-preview-img-wrap{flex:1;overflow:hidden;display:flex;align-items:center;justify-content:center;padding:10px;background:#060a13;}
.tmpl-preview-img-wrap img{max-width:100%;max-height:100%;border-radius:4px;}
/* ═══ Shared ════════════════════════════════════════════════════════════════ */
.btn{display:inline-flex;align-items:center;gap:5px;padding:7px 14px;border:none;border-radius:7px;cursor:pointer;font-size:12px;font-family:inherit;font-weight:700;transition:all .15s;}
.btn:disabled{opacity:.42;cursor:not-allowed;}
.btn-primary{background:var(--teal);color:#000;}
.btn-primary:hover:not(:disabled){background:#00cbe0;}
.btn-green{background:var(--green);color:#000;}
.btn-green:hover:not(:disabled){background:#00e0aa;}
.btn-ghost{background:var(--border);color:var(--text);}
.btn-ghost:hover:not(:disabled){background:#2a3a50;}
.btn-danger{background:rgba(224,82,82,.15);color:var(--red);}
.btn-danger:hover:not(:disabled){background:rgba(224,82,82,.3);}
.btn-icon{padding:5px 9px;font-size:15px;}
.btn-sm{padding:4px 10px;font-size:11px;}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700;}
.badge-ok{background:rgba(0,200,150,.15);color:var(--green);}
.badge-err{background:rgba(224,82,82,.15);color:var(--red);}
.badge-info{background:rgba(0,180,200,.15);color:var(--teal);}
.timer{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;}
#toast{position:fixed;bottom:22px;right:22px;padding:10px 18px;border-radius:8px;font-size:12px;font-weight:600;z-index:9999;opacity:0;transition:opacity .2s;pointer-events:none;}
#toast.show{opacity:1;}
#toast.ok{background:var(--green);color:#000;}
#toast.err{background:var(--red);color:#fff;}
.spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.2);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg)}}
/* Settings form rows */
.set-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap;}
.set-label{flex:1 1 180px;min-width:0;font-size:13px;color:#fff;display:flex;flex-direction:column;gap:2px;}
.set-hint{font-size:11px;color:var(--muted);font-weight:400;}
.set-control{flex:1 1 200px;min-width:0;}
::-webkit-scrollbar{width:5px;height:5px;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}

@media (max-width: 900px){
  body{font-size:14px;}
  /* Show the slim top bar; the left rail collapses into a slide-in drawer. */
  .topbar{display:flex;}
  .hamburger{display:flex;align-items:center;justify-content:center;width:40px;height:40px;flex:0 0 auto;}
  .topbar .logo{min-width:0;flex:1 1 auto;}
  .logo img{width:30px;height:30px;}
  .logo-text{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .topbar .logo-text span{display:none;}

  /* Dim backdrop behind the open drawer; tap to close. */
  .nav-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40;}
  body:not(.nav-open) .nav-overlay{display:none;}
  body.nav-open .nav-overlay{display:block;}

  /* Sidebar: off-screen by default, slides in when body.nav-open. */
  .sidebar{
    position:fixed;top:0;left:0;bottom:0;
    width:82%;max-width:300px;flex:none;
    box-shadow:2px 0 18px rgba(0,0,0,.4);
    transform:translateX(-100%);transition:transform .25s ease;
    z-index:50;
  }
  body.nav-open .sidebar{transform:translateX(0);}
  .sidebar .nav-btn{min-height:46px;font-size:15px;}
  .sidebar .header-right label{display:block;}
  .sidebar .header-right .model-sel,.sidebar .header-right .src-sel{min-height:42px;font-size:14px;}
  .src-sel{min-height:34px;max-width:132px;font-size:12px;}
  .main{
    overflow:hidden;
    min-height:0;
  }
  .tab.active{
    overflow-y:auto;
    min-height:0;
    -webkit-overflow-scrolling:touch;
  }
  .stories-toolbar{
    position:sticky;
    top:0;
    z-index:8;
    padding:10px;
    gap:7px;
    background:var(--navy);
    align-items:stretch;
  }
  .stories-toolbar > .btn,
  .stories-toolbar > select,
  .stories-toolbar > input,
  .stories-toolbar > .src-sel{
    min-height:36px;
  }
  .tb-group > .src-sel,
  .tb-group > select,
  .tb-group > input{min-height:36px;}
  /* Bulk toolbar: stack into clean full-width rows instead of a wrapped jumble. */
  .bulk-toolbar{flex-direction:column;align-items:stretch;}
  .bulk-toolbar .tb-spacer{display:none;}
  .bulk-toolbar .tb-hint{display:none;}
  .bulk-toolbar .tb-group{display:flex;justify-content:space-between;}
  .bulk-toolbar .tb-group > .src-sel{flex:1;max-width:none;margin-left:8px;}
  .bulk-toolbar #btn-bulk-run{width:100%;}
  .cat-tabs{
    order:10;
    width:100%;
    padding-bottom:2px;
  }
  .cat-btn{
    min-height:32px;
    padding:6px 12px;
  }
  .stories-list{
    padding:10px;
    gap:10px;
  }
  .story-card{
    padding:12px;
    border-radius:8px;
  }
  .s-top{
    align-items:flex-start;
    flex-wrap:wrap;
  }
  .story-title{
    flex:1 1 220px;
    min-width:0;
    overflow-wrap:anywhere;
  }
  .story-actions{
    flex-wrap:wrap;
  }
  .btn{
    min-height:34px;
    justify-content:center;
    white-space:nowrap;
  }
  .btn-sm{min-height:32px;}

  #tab-bulk{overflow-y:auto;}
  .bulk-cols{
    flex:none;
    flex-direction:column;
    overflow:visible;
    padding:10px;
    gap:10px;
  }
  .bulk-col{
    width:100%;
    max-height:none;
  }
  .bulk-col-head{
    padding:10px;
    gap:7px;
  }
  .bulk-col-head select{
    flex:1 1 160px;
    min-width:0;
  }
  .bulk-list{
    max-height:none;
  }
  .bulk-item{
    padding:10px;
  }
  #bulk-results{
    overflow:visible;
    padding-bottom:12px;
  }

  #tab-editor,
  #tab-canvas,
  #tab-templates{
    flex-direction:column;
    overflow-y:auto;
  }
  .editor-left,
  .editor-right,
  .canvas-left,
  .canvas-center,
  .canvas-right,
  .tmpl-left,
  .tmpl-right,
  .tmpl-preview-pane{
    width:100%;
    flex:0 0 auto;
    border-left:none;
    border-right:none;
  }
  .editor-left{
    border-bottom:1px solid var(--border);
    max-height:none;
  }
  .editor-right{
    min-height:70vh;
  }
  .panel-header{
    padding:9px 10px;
    gap:7px;
    flex-wrap:wrap;
  }
  .panel-header h3{
    flex:1 1 140px;
  }
  .plan-form{
    padding:10px;
    gap:10px;
    overflow:visible;
  }
  .slide-sec{
    padding:10px;
  }
  .field-group input,
  .field-group textarea,
  .field-group select,
  .url-inp,
  .prop-inp{
    min-height:36px;
    font-size:14px;
  }
  .img-row,
  .url-row{
    flex-wrap:wrap;
  }
  .img-row .url-inp,
  .url-row .url-inp{
    flex-basis:100%;
  }
  .preview-wrap{
    min-height:calc(100vh - 210px);
    min-height:calc(100dvh - 210px);
    padding:10px;
  }
  .preview-wrap img{
    max-height:calc(100vh - 230px);
    max-height:calc(100dvh - 230px);
  }

  .canvas-left{
    order:2;
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
    gap:10px;
    padding:10px;
    border-bottom:1px solid var(--border);
  }
  .canvas-left .preset-btns,
  .canvas-left [style*="flex-direction:column"]{
    gap:6px;
  }
  .canvas-center{
    order:1;
    min-height:auto;
  }
  .canvas-toolbar{
    padding:9px 10px;
  }
  .canvas-area{
    min-height:calc(100vw * 1.25 + 24px);
    max-height:none;
    padding:10px;
    overflow:auto;
  }
  .canvas-wrap{
    width:min(100%,540px);
    aspect-ratio:540 / 675;
  }
  .canvas-wrap .canvas-container,
  .canvas-wrap canvas{
    width:100% !important;
    height:100% !important;
  }
  #overlay-canvas{
    width:100% !important;
    height:100% !important;
  }
  .canvas-right{
    order:3;
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:10px;
    padding:10px;
    border-top:1px solid var(--border);
  }
  .canvas-right h4,
  .canvas-right hr,
  .canvas-right > button,
  .canvas-right .pos-grid,
  .canvas-right .prop-row:first-of-type{
    grid-column:1 / -1;
  }

  .tmpl-left{
    border-bottom:1px solid var(--border);
  }
  .tmpl-files{
    display:flex;
    gap:6px;
    overflow-x:auto;
    padding:8px 10px;
  }
  .tmpl-file-btn{
    width:auto;
    flex:0 0 auto;
    margin-bottom:0;
    white-space:nowrap;
  }
  .tmpl-right{
    min-height:65vh;
  }
  .code-area{
    min-height:65vh;
  }
  .tmpl-preview-pane{
    min-height:55vh;
    border-top:1px solid var(--border);
  }
  .tmpl-preview-img-wrap{
    min-height:48vh;
  }

  #modal-bg{
    align-items:flex-end !important;
    padding:10px;
  }
  #modal-bg > div{
    width:100% !important;
    max-width:100% !important;
    max-height:88vh !important;
    border-radius:10px !important;
  }
  #toast{
    left:12px;
    right:12px;
    bottom:12px;
    text-align:center;
  }
}

@media (max-width: 560px){
  header{
    padding:7px 8px;
  }
  .stories-toolbar > .btn{
    flex:1 1 auto;
  }
  #stories-status,
  #bulk-total{
    width:100%;
  }
  #bulk-fmt-chips{
    width:100%;
    overflow-x:auto;
    padding-bottom:2px;
  }
  .panel-header > .btn,
  .panel-header > select,
  .panel-header > .src-sel{
    flex:1 1 auto;
  }
  .preview-nav{
    flex:1 1 100%;
    justify-content:space-between;
  }
  .slide-cnt{
    flex:1;
  }
  .canvas-left{
    grid-template-columns:1fr;
  }
  .canvas-toolbar > .btn,
  .canvas-toolbar > span{
    flex:1 1 auto;
  }
  .canvas-area{
    min-height:calc(100vw * 1.25 + 20px);
  }
  .canvas-right{
    grid-template-columns:1fr;
  }
  .tmpl-right,
  .code-area{
    min-height:58vh;
  }
}

/* Modern dashboard shell */
:root{
  --navy:#07101f;--panel:#0d1829;--panel-2:#111f33;--border:#23334b;
  --text:#edf3fb;--muted:#8da0ba;--teal:#39d0c3;--green:#47d7a1;
}
body{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:radial-gradient(circle at 78% -10%,rgba(57,208,195,.10),transparent 30%),var(--navy);}
.sidebar{width:236px;flex-basis:236px;background:rgba(8,17,31,.94);border-right-color:rgba(141,160,186,.15);padding:10px;}
.sidebar-logo{border:0;padding:13px 12px 18px;}
nav{padding:0;gap:3px;}
.nav-section{padding:18px 12px 7px;color:#60738f;letter-spacing:1.2px;}
.nav-btn{padding:10px 12px;border-radius:10px;color:#91a3bb;font-weight:600;}
.nav-btn:hover{background:rgba(255,255,255,.055);}
.nav-btn.active{background:linear-gradient(135deg,rgba(57,208,195,.20),rgba(57,208,195,.08));color:#75eee2;box-shadow:inset 0 0 0 1px rgba(57,208,195,.18);}
.header-right{border-top-color:rgba(141,160,186,.14);padding:14px 4px 4px;}
.model-sel,.src-sel,.url-inp{border-color:#2b3b52!important;background:#0a1424!important;border-radius:9px!important;}
.btn{border-radius:9px;font-weight:700;letter-spacing:-.1px;}
.btn-primary{background:linear-gradient(135deg,#42dbd0,#23b7b2);color:#05201f;border:0;}
.btn-green{background:linear-gradient(135deg,#4adea8,#27bd8b);color:#042016;border:0;}
.story-card,.rv-card,.lib-card,.lib-row,.bulk-col{border-radius:14px;background:linear-gradient(145deg,rgba(17,31,51,.96),rgba(11,22,38,.96));box-shadow:0 12px 34px rgba(0,0,0,.16);}

#tab-dashboard{display:none;overflow-y:auto;flex-direction:column;background:linear-gradient(180deg,rgba(17,30,49,.42),transparent 45%);}
#tab-dashboard.active{display:flex;}
.dash-wrap{width:100%;max-width:1500px;margin:0 auto;padding:30px clamp(18px,3vw,42px) 56px;}
.dash-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:28px;}
.dash-eyebrow{color:var(--teal);font-size:11px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px;}
.dash-title{font-size:clamp(27px,3vw,42px);line-height:1.08;color:#fff;letter-spacing:-1.5px;max-width:700px;}
.dash-subtitle{color:var(--muted);font-size:14px;line-height:1.6;margin-top:10px;max-width:680px;}
.dash-actions{display:flex;gap:9px;flex-wrap:wrap;justify-content:flex-end;}
.dash-actions .btn{padding:10px 15px;}
.dash-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:28px;}
.dash-stat{position:relative;overflow:hidden;background:linear-gradient(145deg,rgba(17,31,51,.98),rgba(10,22,38,.98));border:1px solid rgba(141,160,186,.16);border-radius:16px;padding:18px;min-height:108px;}
.dash-stat::after{content:"";position:absolute;width:90px;height:90px;border-radius:50%;right:-38px;top:-38px;background:var(--stat-color,rgba(57,208,195,.14));filter:blur(1px);}
.dash-stat-label{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.8px;}
.dash-stat-value{font-size:30px;font-weight:800;color:#fff;letter-spacing:-1px;margin-top:10px;}
.dash-stat-note{font-size:11px;color:#6f849e;margin-top:3px;}
.dash-section-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:4px 0 14px;}
.dash-section-head h2{font-size:18px;color:#fff;letter-spacing:-.4px;}
.dash-section-head p{font-size:12px;color:var(--muted);margin-top:4px;}
.dash-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;}
.post-card{background:linear-gradient(160deg,#12223a,#0a1526);border:1px solid rgba(141,160,186,.17);border-radius:18px;overflow:hidden;min-width:0;box-shadow:0 18px 44px rgba(0,0,0,.20);transition:transform .2s,border-color .2s;}
.post-card:hover{transform:translateY(-3px);border-color:rgba(57,208,195,.42);}
.post-cover{aspect-ratio:4/5;background:#101c2d;overflow:hidden;position:relative;}
.post-cover>img{width:100%;height:100%;object-fit:cover;display:block;}
.post-count{position:absolute;right:10px;top:10px;background:rgba(4,10,18,.82);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.14);border-radius:999px;padding:5px 8px;color:#fff;font-size:10px;font-weight:800;}
.post-body{padding:15px;}
.post-meta{display:flex;align-items:center;gap:7px;margin-bottom:9px;}
.post-brand{font-size:10px;color:var(--teal);font-weight:800;text-transform:uppercase;letter-spacing:.8px;}
.post-title{font-size:14px;color:#fff;font-weight:750;line-height:1.35;min-height:38px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.post-caption{font-size:11px;color:var(--muted);line-height:1.45;margin-top:8px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;min-height:48px;}
.post-when{font-size:10.5px;color:#b3c1d4;margin-top:12px;padding-top:11px;border-top:1px solid rgba(141,160,186,.12);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.post-actions{display:flex;gap:7px;margin-top:12px;}
.post-actions .btn{flex:1;justify-content:center;padding:7px 9px;font-size:11px;}
.dash-empty{grid-column:1/-1;border:1px dashed #2b3d57;border-radius:18px;text-align:center;padding:56px 20px;color:var(--muted);background:rgba(13,24,41,.55);}
.dash-empty strong{display:block;color:#fff;font-size:16px;margin-bottom:6px;}
@media(max-width:1180px){.dash-grid{grid-template-columns:repeat(2,minmax(0,1fr));}.post-cover{aspect-ratio:16/10;}.dash-stats{grid-template-columns:repeat(2,1fr);}}
@media(max-width:720px){.dash-wrap{padding:22px 14px 40px}.dash-hero{align-items:flex-start;flex-direction:column}.dash-actions{justify-content:flex-start}.dash-grid{grid-template-columns:1fr}.dash-stats{grid-template-columns:repeat(2,1fr)}.post-cover{aspect-ratio:16/11}.sidebar{padding:8px}.dash-title{font-size:29px}}
</style>
</head>
<body>
<aside class="sidebar" id="nav-drawer">
  <div class="sidebar-logo">
    <div class="logo">
      <img class="js-brand-logo" src="/static/logo.png" alt="brand">
      <span class="logo-text js-brand-name">Your<span> Brand</span></span>
    </div>
  </div>
  <nav>
    <div class="nav-section">Workspace</div>
    <button class="nav-btn active" onclick="showTab('dashboard',this)">⌂ Dashboard</button>
    <button class="nav-btn"       onclick="showTab('create',this)">＋ Create post</button>
    <div class="nav-section">Content</div>
    <button class="nav-btn"       onclick="showTab('stories',this)">📡 Auto (RSS)</button>
    <button class="nav-btn"       onclick="showTab('bulk',this)">⚡ Bulk</button>
    <button class="nav-btn"       onclick="showTab('csv',this)">📄 CSV Import</button>
    <button class="nav-btn"       onclick="showTab('agent',this)">🤖 Agent</button>
    <button class="nav-btn"       onclick="showTab('review',this)">✅ Review<span id="review-badge" class="nav-badge" style="display:none;">0</span></button>
    <button class="nav-btn"       onclick="showTab('calendar',this)">🗓 Calendar<span id="cal-badge" class="nav-badge" style="display:none;">0</span></button>
    <button class="nav-btn"       onclick="showTab('canvas',this)">🖌 Canvas</button>
    <button class="nav-btn"       onclick="showTab('templates',this)">📄 Templates</button>
    <button class="nav-btn"       onclick="showTab('library',this)">📚 Library</button>
    <div class="nav-section">Scripts</div>
    <button class="nav-btn"       onclick="showTab('scripts',this)">📝 Scripts</button>
  </nav>
  <div class="header-right">
    <span class="timer" id="hdr-timer"></span>
    <button class="model-sel" id="notif-btn" onclick="toggleNotify()" title="Get a desktop notification when a task finishes" style="cursor:pointer;">🔔 Off</button>
    <label>Brand:</label>
    <select class="model-sel" id="brand-sel" onchange="switchBrand(this.value)" title="Active brand / IG page">
      <option>…</option>
    </select>
    <label>Engine:</label>
    <select class="model-sel" id="backend-sel" onchange="setBackend(this.value)" title="LLM backend (Hermes or Ollama)">
      <option>…</option>
    </select>
    <label>Model:</label>
    <select class="model-sel" id="model-sel" onchange="setModel(this.value)">
      <option>Loading…</option>
    </select>
    <button class="model-sel" id="settings-btn" onclick="openSettings()" title="Defaults &amp; preferences" style="cursor:pointer;">⚙ Settings</button>
  </div>
</aside>
<div class="nav-overlay" id="nav-overlay" onclick="toggleNav(false)"></div>

<div class="app-main">
<header class="topbar">
  <button class="hamburger" id="hamburger" onclick="toggleNav()" aria-label="Menu" aria-expanded="false">☰</button>
  <div class="logo">
    <img class="js-brand-logo" src="/static/logo.png" alt="brand">
    <span class="logo-text js-brand-name">Your<span> Brand</span></span>
  </div>
</header>

<!-- Loud warning when Hermes was the configured engine but its CLI wasn't found
     and we silently fell back to Ollama. Stays up until Hermes is reselected. -->
<div id="fallback-banner" style="display:none;align-items:center;gap:12px;
     background:#3a1d12;border-bottom:1px solid #ff8a3d;color:#ffd9b8;
     padding:10px 18px;font-size:13px;line-height:1.4;">
  <span style="font-size:16px;">⚠️</span>
  <span style="flex:1;">
    <b>Hermes not detected — generating with Ollama instead.</b>
    Start Hermes (or check it's on your PATH), then switch the engine back.
  </span>
  <button class="btn btn-sm" onclick="retryHermes()"
          style="background:#ff8a3d;color:#1a0e07;border:none;font-weight:600;">
    Use Hermes
  </button>
</div>

<div class="main">

<!-- Dashboard -->
<div id="tab-dashboard" class="tab active">
  <div class="dash-wrap">
    <section class="dash-hero">
      <div>
        <div class="dash-eyebrow">Content operations</div>
        <h1 class="dash-title">Your social content,<br>ready in one place.</h1>
        <p class="dash-subtitle">Review the latest four carousel packages, see what is ready or scheduled, and hand everything off with one download.</p>
      </div>
      <div class="dash-actions">
        <button class="btn btn-ghost" onclick="showTab('calendar')">View calendar</button>
        <button class="btn btn-ghost" onclick="showTab('review')">Open review</button>
        <button class="btn btn-primary" id="dash-export" onclick="exportDashboard()">↓ Export all 4</button>
      </div>
    </section>
    <section class="dash-stats" aria-label="Content summary">
      <div class="dash-stat" style="--stat-color:rgba(57,208,195,.17)"><div class="dash-stat-label">All posts</div><div class="dash-stat-value" id="dash-total">—</div><div class="dash-stat-note">in this brand workspace</div></div>
      <div class="dash-stat" style="--stat-color:rgba(245,197,66,.15)"><div class="dash-stat-label">Awaiting review</div><div class="dash-stat-value" id="dash-pending">—</div><div class="dash-stat-note">need a decision</div></div>
      <div class="dash-stat" style="--stat-color:rgba(167,139,250,.16)"><div class="dash-stat-label">Scheduled</div><div class="dash-stat-value" id="dash-scheduled">—</div><div class="dash-stat-note">on the publishing calendar</div></div>
      <div class="dash-stat" style="--stat-color:rgba(71,215,161,.16)"><div class="dash-stat-label">Published</div><div class="dash-stat-value" id="dash-published">—</div><div class="dash-stat-note">successfully delivered</div></div>
    </section>
    <section>
      <div class="dash-section-head">
        <div><h2>Latest carousel pack</h2><p>Four complete posts with images, caption, and timing instructions.</p></div>
        <button class="btn btn-ghost btn-sm" onclick="loadDashboard()">↻ Refresh</button>
      </div>
      <div class="dash-grid" id="dash-posts">
        <div class="dash-empty"><strong>Loading your carousels…</strong>Preparing the latest content pack.</div>
      </div>
    </section>
  </div>
</div>

<!-- ═══════════ CREATE (MANUAL) ═══════════════════════════════════════════ -->
<div id="tab-create" class="tab">
  <div class="stories-toolbar" style="flex-wrap:wrap;">
    <b style="font-size:14px;color:#fff;">✍️ Create a post</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">Choose the platform and purpose, then give your idea and key points — the AI shapes the post for that audience.</span>
  </div>
  <div style="flex:1;overflow-y:auto;padding:22px;display:flex;justify-content:center;">
    <div style="width:100%;max-width:640px;display:flex;flex-direction:column;gap:16px;">

      <button class="btn btn-danger btn-sm" onclick="clearCreateSession()" style="align-self:flex-end;" title="Clear this brand's current working session">Start fresh</button>

      <div class="field-group">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
          <label style="margin:0;">Idea / topic <span style="color:var(--red);">*</span></label>
          <button class="btn btn-ghost btn-sm" id="m-suggest-btn" style="padding:2px 9px;font-size:11px;" onclick="manualSuggest()" title="Get a few alternative angles to choose from">💡 Suggest angles</button>
        </div>
        <input id="m-idea" class="url-inp" style="width:100%;font-size:14px;padding:11px 13px;"
               placeholder="e.g. 5 signs your website is quietly losing leads"
               onkeydown="if(event.key==='Enter'){event.preventDefault();manualGenerate();}">
        <div id="m-angles" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;"></div>
      </div>

      <div style="display:flex;gap:16px;flex-wrap:wrap;">
        <div class="field-group" style="flex:1;min-width:220px;">
          <label>Platform <span style="color:var(--red);">*</span></label>
          <select id="m-platform" class="src-sel" style="width:100%;" onchange="updateManualContext()">
            <option value="instagram" selected>Instagram</option>
            <option value="linkedin">LinkedIn</option>
            <option value="facebook">Facebook</option>
            <option value="x">X (Twitter)</option>
          </select>
        </div>
        <div class="field-group" style="flex:1;min-width:220px;">
          <label>Content type <span style="color:var(--red);">*</span></label>
          <select id="m-content-type" class="src-sel" style="width:100%;" onchange="updateManualContext()">
            <option value="">Loading brand options…</option>
          </select>
        </div>
      </div>
      <div id="m-context-hint" style="margin-top:-10px;padding:10px 12px;border:1px solid var(--border);border-radius:7px;background:rgba(255,255,255,.02);font-size:11px;line-height:1.5;color:var(--muted);">
        Loading content options for the active brand…
      </div>
      <div style="font-size:11px;color:var(--muted);">Manual source only: the AI uses the idea and notes entered here. RSS stories and previous posts are not included.</div>

      <div class="field-group">
        <label>Key points / notes <span style="color:var(--muted);font-weight:400;">(optional — the AI structures these into slides)</span></label>
        <textarea id="m-notes" rows="5" class="url-inp" style="width:100%;font-size:14px;padding:11px 13px;resize:vertical;"
                  placeholder="One thought per line — rough is fine:&#10;— slow load times&#10;— no clear call-to-action&#10;— bad on mobile"></textarea>
      </div>

      <div style="display:flex;gap:16px;flex-wrap:wrap;">
        <div class="field-group" style="flex:1;min-width:140px;">
          <label>Slides</label>
          <select id="m-slides" class="src-sel" style="width:100%;">
            <option>3</option><option selected>4</option><option>5</option>
            <option>6</option><option>7</option><option>8</option><option>9</option><option>10</option>
          </select>
        </div>
        <div class="field-group" style="flex:2;min-width:200px;">
          <label>Tone / stance <span style="color:var(--muted);font-weight:400;">(optional)</span></label>
          <input id="m-tone" class="url-inp" list="tone-presets" style="width:100%;" placeholder="e.g. helpful, bold, skeptical…">
        </div>
      </div>

      <div class="field-group">
        <label>Images <span style="color:var(--muted);font-weight:400;">(optional — assigned to your slides in order; you can also paste with Ctrl+V here)</span></label>
        <div id="m-img-pool" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:4px;"></div>
        <div style="margin-top:8px;">
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('m-img-input').click()">⬆ Add images</button>
          <input type="file" id="m-img-input" accept="image/png,image/jpeg,image/webp" multiple style="display:none" onchange="manualAddImages(this)">
        </div>
      </div>

      <button class="btn btn-green" id="m-gen" onclick="manualGenerate()" style="align-self:flex-start;font-size:14px;padding:11px 22px;">✨ Generate post</button>
      <div style="font-size:11px;color:var(--muted);">The post is written, illustrated, rendered, and added to Review automatically. It will appear on the Dashboard when ready.</div>
    </div>
  </div>
</div>

<!-- ═══════════ STORIES (AUTO / RSS) ══════════════════════════════════════ -->
<div id="tab-stories" class="tab">
  <div class="stories-toolbar">
    <button class="btn btn-primary" onclick="fetchStories()" id="btn-fetch">Fetch &amp; Score</button>
    <button class="btn btn-ghost btn-sm" onclick="resetUsed()" id="btn-reset-used" title="Allow already-generated stories to be served again">↺ Reset seen</button>
    <button class="btn btn-danger btn-sm" id="btn-cancel" style="display:none;" onclick="cancelJob()">⨯ Cancel</button>
    <div class="cat-tabs" id="cat-tabs">
      <button class="cat-btn active" onclick="selectCat('',this)">All</button>
    </div>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">fetch</span>
    <input type="number" id="fetch-limit" value="15" min="3" max="60" style="width:52px;background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:4px 6px;font-size:12px;" title="stories to fetch"></label>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">top</span>
    <input type="number" id="fetch-top" value="6" min="1" max="15" style="width:44px;background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:4px 6px;font-size:12px;" title="top N"></label>
    <button class="btn btn-ghost btn-sm" onclick="saveStorySet()" title="Save this fetched + scored set">💾 Save set</button>
    <button class="btn btn-ghost btn-sm" onclick="openStoryLibrary()" title="Load a saved set (no re-fetch)">📂 Saved</button>
    <span id="stories-status"></span>
  </div>

  <!-- Batch action bar -->
  <div class="stories-toolbar" id="batch-bar" style="display:none;background:rgba(0,200,150,.05);">
    <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text);font-weight:700;">
      <input type="checkbox" id="select-all" onchange="toggleAll(this)"> Select all
    </label>
    <span id="sel-count" style="font-size:12px;color:var(--muted);">0 selected</span>
    <div style="flex:1"></div>
    <span style="font-size:11px;color:var(--muted);">Apply formats to selected:</span>
    <div id="bulk-fmt-chips" style="display:flex;gap:4px;"></div>
    <select id="tone-sel" class="src-sel" title="Stance applied to generated copy">
      <option value="">Tone: Auto</option>
      <option value="positive, upbeat">Positive</option>
      <option value="negative, critical">Negative</option>
      <option value="neutral, factual">Neutral</option>
      <option value="hyped, exciting">Hype</option>
      <option value="analytical, measured">Analytical</option>
      <option value="skeptical, cautionary">Skeptical</option>
    </select>
    <select id="batch-src" class="src-sel"><option value="none">🚫 No images (fast)</option><option value="pexels">Pexels</option><option value="unsplash">Unsplash</option><option value="google">Google</option><option value="feed">📰 Article</option></select>
    <button class="btn btn-green" onclick="runBatch()" id="btn-batch">Generate All Selected</button>
    <button class="btn btn-danger btn-sm" id="btn-cancel-batch" style="display:none;" onclick="cancelJob()">⨯ Cancel</button>
  </div>

  <div class="stories-list" id="stories-list">
    <div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;line-height:1.7;">
      Select a category and click <b>Fetch &amp; Score</b>.<br>
      Your local AI model will rank stories from your configured feeds.
    </div>
  </div>
</div>

<!-- ═══════════ BULK (BOTH BRANDS) ════════════════════════════════════════ -->
<div id="tab-bulk" class="tab">
  <div class="stories-toolbar bulk-toolbar" style="flex-wrap:wrap;">
    <b style="font-size:14px;color:#fff;">⚡ Bulk — all brands</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">Fetch top stories per brand, tick what you want, then generate everything in one run.</span>
    <div class="tb-spacer"></div>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Tone</span>
    <select id="bulk-tone" class="src-sel" title="Stance applied to all generated copy">
      <option value="">Auto</option>
      <option value="positive, upbeat">Positive</option>
      <option value="negative, critical">Negative</option>
      <option value="neutral, factual">Neutral</option>
      <option value="hyped, exciting">Hype</option>
      <option value="analytical, measured">Analytical</option>
      <option value="skeptical, cautionary">Skeptical</option>
    </select></label>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Images</span>
    <select id="bulk-src" class="src-sel"><option value="none">🚫 No images (fast)</option><option value="pexels">Pexels</option><option value="unsplash">Unsplash</option><option value="google">Google</option><option value="feed">📰 Article</option></select></label>
    <span id="bulk-total" style="font-size:12px;color:var(--muted);font-weight:700;">0 posts</span>
    <button class="btn btn-green" onclick="runBulk()" id="btn-bulk-run">⚡ Generate All</button>
    <button class="btn btn-danger btn-sm" id="btn-bulk-cancel" style="display:none;" onclick="cancelJob()">⨯ Cancel</button>
  </div>
  <div id="bulk-progress" style="display:none;margin:0 0 10px;"></div>
  <div id="bulk-brands" class="bulk-cols">
    <div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">Loading brands…</div>
  </div>
  <div id="bulk-results"></div>
</div>

<!-- ═══════════ AGENT ════════════════════════════════════════════════════ -->
<div id="tab-agent" class="tab">
  <div class="stories-toolbar" style="flex-wrap:wrap;">
    <b style="font-size:14px;color:#fff;">🤖 Agent</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">Tell it what to make — it fetches, scores &amp; renders for you. Posts are saved for review.</span>
    <div class="tb-spacer"></div>
    <button class="btn btn-ghost btn-sm" onclick="resetAgent()" title="Clear the conversation">↺ New chat</button>
  </div>
  <div id="agent-log" class="agent-log">
    <div class="agent-msg bot">
      <div class="agent-bubble">Hi! Try: <i>"Fetch JKR's top 5 gaming stories and make carousels for the best 3"</i> or
      <i>"2 K2 LinkedIn posts about the top marketing stories, hyped tone."</i><br>
      <span style="color:var(--muted);font-size:11px;">Works with local OpenAI-compatible models. Tool-calling models are used natively; plain chat models use a JSON fallback.</span></div>
    </div>
  </div>
  <div id="agent-suggest" class="agent-suggest"></div>
  <div class="agent-input">
    <textarea id="agent-text" rows="1" placeholder="Ask the agent to make some posts…" onkeydown="agentKey(event)"></textarea>
    <button class="btn btn-green" id="agent-send" onclick="agentSend()">Send</button>
  </div>
</div>

<!-- ═══════════ REVIEW ═══════════════════════════════════════════════════ -->
<div id="tab-review" class="tab">
  <div class="stories-toolbar" style="flex-wrap:wrap;">
    <b style="font-size:14px;color:#fff;">✅ Review &amp; publish</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">Approve, then schedule on the calendar or publish straight to Instagram / Facebook.</span>
    <div class="tb-spacer"></div>
    <span id="meta-status" style="font-size:11px;"></span>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Show</span>
    <select id="review-filter" class="src-sel" onchange="loadReview()">
      <option value="pending">Pending</option>
      <option value="">All</option>
      <option value="approved">Approved</option>
      <option value="scheduled">Scheduled</option>
      <option value="published">Published</option>
      <option value="failed">Failed</option>
      <option value="rejected">Rejected</option>
    </select></label>
    <button class="btn btn-ghost btn-sm" onclick="loadReview()">↻ Refresh</button>
    <button class="btn btn-ghost btn-sm" onclick="showChannelHelp()" title="How to connect Instagram &amp; Facebook">📖 Connect accounts</button>
    <button class="btn btn-danger btn-sm" onclick="clearReview()" title="Remove rejected entries">🗑 Clear rejected</button>
  </div>
  <div id="review-list" class="stories-list"></div>
</div>

<!-- ═══════════ CALENDAR ═════════════════════════════════════════════════ -->
<div id="tab-calendar" class="tab" style="flex-direction:column;">
  <div class="stories-toolbar" style="flex-wrap:wrap;gap:8px;">
    <b style="font-size:14px;color:#fff;">🗓 Calendar</b>
    <div class="tb-group" style="gap:4px;">
      <button class="btn btn-ghost btn-sm" onclick="calShift(-1)" title="Previous month">◀</button>
      <b id="cal-label" style="font-size:13px;color:#fff;min-width:132px;text-align:center;">…</b>
      <button class="btn btn-ghost btn-sm" onclick="calShift(1)" title="Next month">▶</button>
      <button class="btn btn-ghost btn-sm" onclick="calGoToday()">Today</button>
    </div>
    <div class="tb-spacer"></div>
    <span id="cal-sched-status" style="font-size:11px;color:var(--muted);"></span>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Brand</span>
      <select id="cal-brand" class="src-sel" onchange="loadCalendar()">
        <option value="">All brands</option>
      </select></label>
    <button class="btn btn-primary btn-sm" onclick="calAutofill()" title="Drop every unscheduled post into the next free posting slots">✨ Auto-fill slots</button>
    <button class="btn btn-ghost btn-sm" onclick="calSettings()" title="Posting times &amp; days">⚙ Slots</button>
    <button class="btn btn-ghost btn-sm" onclick="loadCalendar()">↻</button>
  </div>
  <div class="cal-wrap">
    <div class="cal-head" id="cal-head"></div>
    <div class="cal-grid" id="cal-grid">
      <div style="grid-column:1/-1;color:var(--muted);padding:40px;text-align:center;font-size:13px;">Loading…</div>
    </div>
  </div>
  <div class="cal-unsched" id="cal-unsched" style="display:none;"></div>
</div>

<!-- ═══════════ CSV IMPORT ═══════════════════════════════════════════════ -->
<div id="tab-csv" class="tab" style="flex-direction:column;">
  <div class="stories-toolbar" style="flex-wrap:wrap;gap:8px;">
    <b style="font-size:14px;color:#fff;">📄 CSV Import</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">One row per post (or per slide) → carousels, straight into Review.</span>
    <div class="tb-spacer"></div>
    <a class="btn btn-ghost btn-sm" href="/api/csv/template" download>⬇ Template CSV</a>
    <button class="btn btn-ghost btn-sm" onclick="csvHelp()">📖 Columns</button>
  </div>
  <div style="flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:14px;">
    <div class="csv-drop" id="csv-drop" onclick="g('csv-file').click()">
      <div style="font-size:32px;margin-bottom:8px;">📄</div>
      <div style="font-size:14px;color:#fff;font-weight:600;">Drop a CSV here, or click to choose one</div>
      <div style="font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.6;">
        Columns are matched loosely — <code>title</code>, <code>slide1_heading</code>,
        <code>slide1_body</code>, <code>cta</code>, <code>caption</code>, <code>hashtags</code>,
        <code>image_query</code>, <code>schedule</code>.<br>
        A <code>post_id</code> + <code>order</code> sheet (one row per slide) works too.
      </div>
      <input type="file" id="csv-file" accept=".csv,text/csv" style="display:none;" onchange="csvUpload(this.files[0])">
    </div>
    <div id="csv-summary"></div>
    <div id="csv-controls" style="display:none;">
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;">
        <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
          <span style="font-size:11px;color:var(--muted);">Copy</span>
          <select id="csv-mode" class="src-sel" title="Use the sheet text as-is, or let the AI write from each row">
            <option value="direct">Use my text as-is</option>
            <option value="ai">AI writes from each row</option>
          </select></label>
        <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
          <span style="font-size:11px;color:var(--muted);">Format</span>
          <select id="csv-format" class="src-sel">
            <option value="carousel">Carousel</option>
            <option value="listicle">Listicle</option>
            <option value="square">Square</option>
            <option value="story">Story</option>
            <option value="quote">Quote</option>
          </select></label>
        <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
          <span style="font-size:11px;color:var(--muted);">Images</span>
          <select id="csv-source" class="src-sel">
            <option value="pexels">Pexels</option>
            <option value="unsplash">Unsplash</option>
            <option value="google">Google</option>
            <option value="none">None (fast)</option>
          </select></label>
        <label class="tb-group" style="align-self:center;gap:6px;">
          <input type="checkbox" id="csv-enqueue" checked>
          <span style="font-size:12px;">Send to Review</span></label>
        <label class="tb-group" style="align-self:center;gap:6px;">
          <input type="checkbox" id="csv-autoschedule">
          <span style="font-size:12px;" title="Place them on the calendar in the next free posting slots">Auto-schedule</span></label>
        <div class="tb-spacer" style="flex:1;"></div>
        <button class="btn btn-primary" id="csv-gen" onclick="csvGenerate()">⚡ Generate posts</button>
      </div>
    </div>
    <div id="csv-preview"></div>
    <div id="csv-results" class="stories-list" style="padding:0;gap:12px;"></div>
  </div>
</div>

<!-- ═══════════ LIBRARY ══════════════════════════════════════════════════ -->
<div id="tab-library" class="tab" style="flex-direction:column;">
  <div class="stories-toolbar" style="flex-wrap:wrap;gap:8px;">
    <b style="font-size:14px;color:#fff;">📚 Library</b>
    <div class="cat-tabs" style="margin-left:6px;">
      <button class="cat-btn active" id="lib-kind-plans" onclick="libSetKind('plans')">Plans</button>
      <button class="cat-btn" id="lib-kind-stories" onclick="libSetKind('stories')">Story sets</button>
    </div>
    <div class="tb-spacer"></div>
    <label class="tb-group"><span style="font-size:11px;color:var(--muted);">Sort</span>
      <select id="lib-sort" class="src-sel" onchange="renderLibrary()">
        <option value="new">Newest</option>
        <option value="old">Oldest</option>
        <option value="name">Name A–Z</option>
      </select>
    </label>
    <div class="view-toggle" title="View">
      <button class="vt-btn active" id="lib-view-grid" onclick="libSetView('grid')" title="Grid view">▦ Grid</button>
      <button class="vt-btn" id="lib-view-list" onclick="libSetView('list')" title="List view">☰ List</button>
    </div>
    <button class="btn btn-ghost btn-sm" onclick="loadLibraryTab()">↻ Refresh</button>
    <button class="btn btn-danger btn-sm" onclick="libClear()">🗑 Clear</button>
  </div>
  <div id="library-list" class="lib-body"></div>
</div>

<!-- ═══════════ SCRIPTS ══════════════════════════════════════════════════ -->
<div id="tab-scripts" class="tab" style="flex-direction:column;">
  <div class="stories-toolbar" style="flex-wrap:wrap;gap:8px;">
    <b style="font-size:14px;color:#fff;">📝 Scripts</b>
    <span class="tb-hint" style="font-size:11px;color:var(--muted);">Voiceover scripts from a story or <b>your own topic</b> — give an outline &amp; the AI writes timed, editable segments (hook → beats → CTA).</span>
    <div class="tb-spacer"></div>
  </div>
  <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;">
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;flex:1 1 300px;">
      <span style="font-size:11px;color:var(--muted);">Source story (optional)</span>
      <div style="display:flex;gap:6px;">
        <select id="scr-story" class="model-sel" style="flex:1;min-width:0;"><option value="">— Topic only —</option></select>
        <button class="btn btn-ghost btn-sm" id="btn-scr-suggest" onclick="suggestScriptStories()" title="Fetch top-ranked stories for this brand (takes a moment)" style="white-space:nowrap;">📥 Suggest</button>
      </div>
    </label>
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;flex:1 1 220px;">
      <span style="font-size:11px;color:var(--muted);">Topic (if no story)</span>
      <input id="scr-topic" class="model-sel" style="width:100%;" placeholder="e.g. why site speed wins local leads">
    </label>
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;flex:1 1 200px;">
      <span style="font-size:11px;color:var(--muted);">Keywords (comma-sep)</span>
      <input id="scr-keywords" class="model-sel" style="width:100%;" placeholder="performance, seo, conversion">
    </label>
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
      <span style="font-size:11px;color:var(--muted);">Platform</span>
      <select id="scr-platform" class="model-sel">
        <option value="instagram_reel">Instagram Reel</option>
        <option value="tiktok">TikTok</option>
        <option value="youtube_short">YouTube Short</option>
        <option value="linkedin">LinkedIn</option>
      </select>
    </label>
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
      <span style="font-size:11px;color:var(--muted);">Angle</span>
      <select id="scr-ctype" class="model-sel">
        <option value="educational">Educational</option>
        <option value="proof">Proof</option>
        <option value="testimonial">Testimonial</option>
        <option value="story">Story</option>
        <option value="tip">Tip</option>
      </select>
    </label>
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
      <span style="font-size:11px;color:var(--muted);">Variants</span>
      <select id="scr-num" class="model-sel"><option>3</option><option>1</option><option>2</option><option>4</option><option>5</option></select>
    </label>
    <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;">
      <span style="font-size:11px;color:var(--muted);">Seconds</span>
      <select id="scr-dur" class="model-sel"><option>30</option><option>15</option><option>20</option><option>45</option><option>60</option></select>
    </label>
    <button class="btn btn-primary" id="btn-scr-gen" onclick="generateScripts()">📝 Generate scripts</button>
  </div>
  <details style="border-bottom:1px solid var(--border);padding:0 18px;">
    <summary style="cursor:pointer;font-size:12px;color:var(--muted);padding:10px 0;">✍️ Your structure (optional) — an outline, hook &amp; CTA the AI will follow</summary>
    <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start;padding-bottom:12px;">
      <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;flex:2 1 320px;">
        <span style="font-size:11px;color:var(--muted);">Your outline / beats (one per line → one segment each)</span>
        <textarea id="scr-outline" class="model-sel" rows="4" style="width:100%;resize:vertical;font-family:inherit;" placeholder="open with the problem&#10;show the 3-step fix&#10;proof point&#10;wrap up"></textarea>
      </label>
      <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;flex:1 1 220px;">
        <span style="font-size:11px;color:var(--muted);">Your hook (optional)</span>
        <input id="scr-hook" class="model-sel" style="width:100%;" placeholder="the exact opening line">
      </label>
      <label class="tb-group" style="flex-direction:column;align-items:stretch;gap:3px;flex:1 1 220px;">
        <span style="font-size:11px;color:var(--muted);">Your CTA (optional)</span>
        <input id="scr-cta" class="model-sel" style="width:100%;" placeholder="the exact closing call to action">
      </label>
    </div>
  </details>
  <div id="scripts-list" class="stories-list">
    <div style="color:var(--muted);font-size:13px;text-align:center;padding:40px 20px;line-height:1.6;">
      <div style="font-size:34px;margin-bottom:10px;">📝</div>
      Pick a story (or type a topic), choose a platform &amp; angle, then <b>Generate scripts</b>.
    </div>
  </div>
</div>

<!-- ═══════════ EDITOR ═══════════════════════════════════════════════════ -->
<div id="tab-editor" class="tab">
  <div class="editor-left">
    <div class="panel-header">
      <h3>Plan Editor</h3>
      <button class="btn btn-ghost btn-sm" onclick="savePlan()" title="Save this plan to your library">💾 Save</button>
      <button class="btn btn-ghost btn-sm" onclick="openLibrary()" title="Load a saved plan">📂 Library</button>
      <button class="btn btn-ghost btn-sm" id="btn-source" onclick="viewSource()" title="Open the original source article this post was generated from (Auto/RSS posts only)">🔗 Source</button>
      <button class="btn btn-danger btn-sm" onclick="clearEditorSession()" title="Clear the current plan and its session images">Clear</button>
      <select id="slide-count-sel" style="background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:3px 6px;font-size:12px;font-family:inherit;">
        <option value="3">3 slides</option>
        <option value="4" selected>4 slides</option>
        <option value="5">5 slides</option>
        <option value="6">6 slides</option>
        <option value="7">7 slides</option>
        <option value="8">8 slides</option>
        <option value="9">9 slides</option>
        <option value="10">10 slides</option>
      </select>
    </div>
    <div class="plan-form" id="plan-form">
      <div style="color:var(--muted);text-align:center;padding:28px 10px;font-size:12px;line-height:1.7;">
        Pick a story → plan loads here.<br>
        <button class="btn btn-ghost btn-sm" style="margin-top:10px;" onclick="loadDummy()">Load Preview Plan</button>
      </div>
    </div>
  </div>

  <div class="editor-right">
    <div class="panel-header">
      <div class="preview-nav">
        <button class="btn btn-ghost btn-icon" onclick="prevSlide()">&#8249;</button>
        <span class="slide-cnt" id="slide-cnt">—/—</span>
        <button class="btn btn-ghost btn-icon" onclick="nextSlide()">&#8250;</button>
      </div>
      <button class="btn btn-primary btn-sm" onclick="previewCurrent()" id="btn-prev-slide">Preview</button>
      <div style="flex:1"></div>
      <!-- Image source -->
      <select id="img-source" class="src-sel">
        <option value="pexels">Pexels</option>
        <option value="unsplash">Unsplash</option>
        <option value="google">Google</option>
        <option value="feed">📰 Article</option>
      </select>
      <button class="btn btn-ghost btn-sm" onclick="fetchImages()" id="btn-fetch-img">Fetch Images</button>
      <button class="btn btn-danger btn-sm" onclick="clearImageCache()" title="Delete all cached background images">🗑 Cache</button>
      <button class="btn btn-green" onclick="renderFull()" id="btn-render">Render Carousel</button>
    </div>
    <div class="preview-pane">
      <div class="preview-wrap" id="preview-wrap">
        <div class="preview-placeholder">Load a plan and click Preview</div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════ CANVAS ════════════════════════════════════════════════════ -->
<div id="tab-canvas" class="tab">
  <!-- Tools -->
  <div class="canvas-left">
    <div>
      <h4>From current plan</h4>
      <div class="preset-btns" style="margin-top:6px;">
        <button class="btn btn-primary btn-sm" style="justify-content:flex-start;" onclick="loadSlideToCanvas()">Load generated slide</button>
        <span style="font-size:10px;color:var(--muted);line-height:1.4;">Loads a slide from the latest generated plan into the canvas.</span>
      </div>
    </div>
    <div>
      <h4>Blank presets</h4>
      <div class="preset-btns" style="margin-top:6px;">
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('title')">Title Card</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('content')">Content Slide</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('outro')">Outro / CTA</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="applyPreset('cover')">Brand Cover</button>
      </div>
    </div>
    <div>
      <h4>Add Elements</h4>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px;">
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('Headline',72,'bold')">+ Headline</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('Subheading',44,'bold')">+ Subheading</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText('Body text goes here',32,'normal')">+ Body Text</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addText(_bi().handle||'@handle',22,'bold')">+ Handle</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addRect()">+ Dark Panel</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addRule()">+ Teal Rule</button>
        <button class="btn btn-ghost btn-sm" style="justify-content:flex-start;" onclick="addLogo()">+ Logo Badge</button>
      </div>
    </div>
    <div>
      <h4>Background</h4>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px;">
        <button class="btn btn-ghost btn-sm" onclick="setBgNavy()">Navy (default)</button>
        <button class="btn btn-ghost btn-sm" onclick="setBgTeal()">Teal Gradient</button>
        <input type="file" id="bg-upload" accept="image/*" style="display:none" onchange="setBgFile(this)">
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('bg-upload').click()">Upload Image…</button>
        <div style="display:flex;gap:5px;margin-top:4px;">
          <input class="url-inp" id="canvas-img-url" placeholder="Image URL…" style="flex:1;font-size:11px;padding:4px 6px;">
          <button class="btn btn-ghost btn-sm" onclick="setBgUrl()">Set</button>
        </div>
        <div style="display:flex;gap:5px;margin-top:2px;">
          <input class="url-inp" id="canvas-pexels-q" placeholder="Pexels query…" style="flex:1;font-size:11px;padding:4px 6px;">
          <button class="btn btn-ghost btn-sm" onclick="setBgPexels()">Search</button>
        </div>
      </div>
    </div>
    <div>
      <h4>Canvas</h4>
      <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px;">
        <button class="btn btn-danger btn-sm" onclick="clearCanvas()">Clear All</button>
      </div>
    </div>
  </div>

  <!-- Canvas -->
  <div class="canvas-center">
    <div class="canvas-toolbar">
      <button class="btn btn-primary" onclick="exportCanvas()" id="btn-export-canvas">Export PNG (1080×1350)</button>
      <span id="canvas-status" style="font-size:11px;color:var(--muted);margin-left:4px;">Design freely, then export a ready-to-post PNG</span>
      <div style="flex:1;"></div>
      <label style="font-size:11px;color:var(--muted);">Overlay:</label>
      <input type="range" id="overlay-opacity" min="0" max="90" value="65" style="width:80px;" oninput="updateOverlay(this.value)">
      <span id="overlay-val" style="font-size:11px;color:var(--muted);min-width:28px;">65%</span>
    </div>
    <div class="canvas-area">
      <div class="canvas-wrap" id="canvas-wrap">
        <canvas id="design-canvas"></canvas>
        <canvas id="overlay-canvas" style="position:absolute;top:0;left:0;pointer-events:none;"></canvas>
      </div>
    </div>
  </div>

  <!-- Properties -->
  <div class="canvas-right">
    <h4>Text Properties</h4>
    <div class="prop-row">
      <label>Content</label>
      <textarea class="prop-inp" id="prop-text" rows="3" oninput="applyProp()"></textarea>
    </div>
    <div class="prop-row">
      <label>Font size</label>
      <input class="prop-inp" type="number" id="prop-size" value="46" min="8" max="200" oninput="applyProp()">
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-top:4px;">
      <button class="btn btn-ghost btn-sm" id="prop-bold" onclick="toggleBold()"><b>B</b></button>
      <button class="btn btn-ghost btn-sm" id="prop-italic" onclick="toggleItalic()"><i>I</i></button>
      <button class="btn btn-ghost btn-sm" id="prop-upper" onclick="toggleUpper()">AA</button>
    </div>
    <div class="prop-row" style="margin-top:8px;">
      <label>Colour</label>
      <div class="color-row" id="color-swatches">
        <div class="color-swatch active" style="background:#fff;" onclick="setColor('#ffffff',this)" title="White"></div>
        <div class="color-swatch" style="background:#00B4C8;" onclick="setColor('#00B4C8',this)" title="Teal"></div>
        <div class="color-swatch" style="background:#00C896;" onclick="setColor('#00C896',this)" title="Green"></div>
        <div class="color-swatch" style="background:#0A0F1E;" onclick="setColor('#0A0F1E',this)" title="Navy"></div>
        <div class="color-swatch" style="background:#6b7a96;" onclick="setColor('#6b7a96',this)" title="Muted"></div>
        <input type="color" id="custom-color" value="#ffffff" style="width:26px;height:26px;border:none;border-radius:5px;cursor:pointer;padding:0;" onchange="setColor(this.value)">
      </div>
    </div>
    <hr style="border:none;border-top:1px solid var(--border);margin:8px 0;">
    <h4>Position &amp; Size</h4>
    <div class="pos-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:4px;">
      <div class="prop-row"><label>X</label><input class="prop-inp" type="number" id="prop-x" oninput="applyPos()"></div>
      <div class="prop-row"><label>Y</label><input class="prop-inp" type="number" id="prop-y" oninput="applyPos()"></div>
      <div class="prop-row"><label>W</label><input class="prop-inp" type="number" id="prop-w" oninput="applySize()"></div>
      <div class="prop-row"><label>H</label><input class="prop-inp" type="number" id="prop-h" oninput="applySize()"></div>
    </div>
    <hr style="border:none;border-top:1px solid var(--border);margin:8px 0;">
    <button class="btn btn-danger btn-sm" onclick="deleteSelected()" style="width:100%;">Delete Selected</button>
    <button class="btn btn-ghost btn-sm" style="width:100%;margin-top:5px;" onclick="bringFront()">Bring to Front</button>
    <button class="btn btn-ghost btn-sm" style="width:100%;margin-top:5px;" onclick="sendBack()">Send to Back</button>
  </div>
</div>

<!-- ═══════════ TEMPLATES ═════════════════════════════════════════════════ -->
<div id="tab-templates" class="tab">
  <div class="tmpl-left">
    <div style="padding:12px 10px 5px;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;">Files</div>
    <div class="tmpl-files" id="tmpl-file-list"></div>
  </div>
  <div class="tmpl-right">
    <div class="panel-header">
      <h3 id="tmpl-filename">Select a file</h3>
      <button class="btn btn-ghost btn-sm" onclick="saveTemplate()">Save</button>
      <button class="btn btn-primary btn-sm" onclick="previewTemplate()">Preview</button>
      <span id="tmpl-status"></span>
    </div>
    <div class="code-area" id="code-area">
      <textarea id="code-editor"></textarea>
    </div>
  </div>
  <div class="tmpl-preview-pane">
    <div class="tmpl-preview-header">Preview</div>
    <div class="tmpl-preview-img-wrap" id="tmpl-preview-wrap">
      <div style="color:var(--muted);font-size:12px;text-align:center;">Save then click Preview</div>
    </div>
  </div>
</div>

</div><!-- .main -->
</div><!-- .app-main -->

<!-- Library / picker modal -->
<div id="modal-bg" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9998;align-items:center;justify-content:center;" onclick="if(event.target===this)closeModal()">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;width:560px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;overflow:hidden;">
    <div style="display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--border);">
      <h3 id="modal-title" style="font-size:15px;color:#fff;flex:1;">Library</h3>
      <button class="btn btn-ghost btn-sm" onclick="closeModal()">✕</button>
    </div>
    <div id="modal-body" style="padding:12px 14px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;"></div>
  </div>
</div>

<div id="toast"></div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let S = {
  stories: [],
  plan: null,
  imagePaths: {},
  manualImages: [],       // Create tab: uploaded/pasted images, assigned to slides in order
  reviewIndex: {},        // {rel: status} — last-known Review-queue status per rendered result
  slideIdx: 0,
  totalSlides: 0,
  selectedCat: '',
  formats: {},            // {key: name} from /api/formats
  selected: {},           // {storyIdx: {checked:bool, formats:Set}}
  bulkFormats: new Set(['carousel']),
  busy: null,             // name of in-flight model job, or null
  batch: [],              // last batch results (for Edit buttons)
  brandInfo: null,        // active brand {name,short,handle,tagline,logo,accent,navy,...}
  notify: false,          // desktop notifications enabled
  cancelRequested: false, // user asked to cancel the running loop job
  bulk: {},               // {brandKey: {name, stories:[], sel:{}, formats:Set, count:int}}
  results: [],            // last batch/bulk results (for ✨ Suggest / re-render)
  scripts: [],            // last generated script variants
  lib: { kind: 'plans', view: 'grid', plans: [], stories: [] },  // Library tab state
  cal: null,              // last /api/calendar payload {year, month, weeks, undated}
  calItems: [],           // every post on the visible month, for the detail modal
  reviewItems: [],        // last /api/review payload, for the schedule modal
  csv: null,              // last CSV preview {posts, mapping, shape, ...}
  meta: null,             // /api/meta/status — publishing readiness
};

// ═══════════════════════════════════════════════════════════════════════════
// User settings / defaults — persisted in localStorage, applied on every load
// so the app boots into your preferred model / tone / image source etc.
// ═══════════════════════════════════════════════════════════════════════════
const SETTINGS_DEFAULTS = {
  notify: true,           // desktop notifications ON by default
  model: '',              // '' → keep whatever the server/first option is
  tone: '',               // '' → Auto
  imageSource: 'pexels',  // pexels | unsplash | google
  slides: 4,              // default carousel slide count
  fetchLimit: 15,         // stories to fetch per run
  fetchTop: 6,            // top N to keep after scoring
};
let SETTINGS = {...SETTINGS_DEFAULTS};

function loadSettings() {
  try { SETTINGS = {...SETTINGS_DEFAULTS, ...JSON.parse(localStorage.getItem('k2_settings') || '{}')}; }
  catch(e) { SETTINGS = {...SETTINGS_DEFAULTS}; }
  // Migrate the old standalone notify flag the first time (before any settings save).
  const legacy = localStorage.getItem('k2_notify');
  if (legacy !== null && localStorage.getItem('k2_settings') === null) SETTINGS.notify = legacy === '1';
  return SETTINGS;
}
function saveSettings() { localStorage.setItem('k2_settings', JSON.stringify(SETTINGS)); }

// Push saved defaults onto the live controls (only when the control exists / the
// value is a real option). Called after models/formats have loaded.
function applySettings() {
  const setVal = (id, v) => { const el = g(id); if (el && v != null && v !== '') el.value = v; };
  setVal('bulk-tone',       SETTINGS.tone);
  setVal('bulk-src',        SETTINGS.imageSource);
  setVal('slide-count-sel', String(SETTINGS.slides));
  setVal('fetch-limit',     String(SETTINGS.fetchLimit));
  setVal('fetch-top',       String(SETTINGS.fetchTop));
  const ms = g('model-sel');
  if (ms && SETTINGS.model && [...ms.options].some(o => o.value === SETTINGS.model)) {
    if (ms.value !== SETTINGS.model) { ms.value = SETTINGS.model; setModel(SETTINGS.model); }
  }
}

// Ask the server to stop the running fetch/batch after the current item.
async function cancelJob() {
  S.cancelRequested = true;
  try {
    const d = await api('/api/cancel','POST');
    toast(d.ok ? 'Cancelling — stopping after the current item…' : 'Nothing to cancel');
  } catch(e){ toast(e.message,'err'); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', async () => {
  loadSettings();
  initNotify();
  await loadBrands();
  await loadBackends();
  await Promise.all([loadModels(), loadCategories(), loadFormats()]);
  applySettings();          // apply saved defaults now that controls/options exist
  await restoreSession();
  await loadDashboard();
  refreshReviewBadge();     // show any pending posts awaiting review
  restoreActiveTab();       // re-open the last tab after a refresh
  refreshResponsiveSurfaces();
});
window.addEventListener('resize', refreshResponsiveSurfaces);
window.addEventListener('orientationchange', () => setTimeout(refreshResponsiveSurfaces, 250));

// ── Desktop notifications ────────────────────────────────────────────────────
// Default-on: if the saved preference wants notifications and the browser hasn't
// decided yet, ask once on load so they "just work" without a manual toggle.
async function initNotify() {
  S.notify = false;
  if (SETTINGS.notify && ('Notification' in window)) {
    if (Notification.permission === 'granted') {
      S.notify = true;
    } else if (Notification.permission === 'default') {
      try { S.notify = (await Notification.requestPermission()) === 'granted'; } catch(e) {}
    }
  }
  updateNotifBtn();
}
function updateNotifBtn() {
  const b = g('notif-btn'); if(!b) return;
  b.textContent = S.notify ? '🔔 On' : '🔔 Off';
  b.style.color = S.notify ? 'var(--green)' : '';
}
async function toggleNotify() {
  if(!('Notification' in window)) { toast('This browser has no notifications','err'); return; }
  if(S.notify) { S.notify=false; SETTINGS.notify=false; saveSettings(); updateNotifBtn(); toast('Notifications off'); return; }
  let perm = Notification.permission;
  if(perm!=='granted') perm = await Notification.requestPermission();
  if(perm==='granted') {
    S.notify=true; SETTINGS.notify=true; saveSettings(); updateNotifBtn();
    notify('Notifications enabled', "You'll be pinged when each task finishes.");
  } else { toast('Notification permission denied — enable it in your browser site settings','err'); }
}
function notify(title, body) {
  try {
    if(S.notify && ('Notification' in window) && Notification.permission==='granted') {
      const icon = (S.brandInfo && S.brandInfo.logo) || '/static/logo.png';
      const n = new Notification(title, { body: body||'', icon, tag:'k2-task' });
      setTimeout(()=>{ try{ n.close(); }catch(e){} }, 6000);
    }
  } catch(e){}
}

// ── Brands ──────────────────────────────────────────────────────────────────
async function loadBrands() {
  const sel = document.getElementById('brand-sel');
  const data = await api('/api/brands').catch(() => ({ brands: {}, active: '' }));
  const entries = Object.entries(data.brands || {});
  if (!entries.length) { sel.innerHTML = '<option>—</option>'; return; }
  sel.innerHTML = entries.map(([k, name]) =>
    `<option value="${k}" ${k === data.active ? 'selected' : ''}>${esc(name)}</option>`).join('');
  syncCalBrands();
  applyBrandChrome(await api('/api/brand').catch(() => null));
}

function applyBrandChrome(b) {
  if (!b) return;
  S.brandInfo = b;
  const src = b.logo + '?t=' + Date.now();
  document.querySelectorAll('.js-brand-logo').forEach(img => {
    img.src = src;
    if (b.shape === 'wide') {   // full wordmark — show it whole, don't crop to a circle
      img.style.cssText = 'height:30px;width:auto;max-width:120px;border-radius:0;object-fit:contain;';
    } else {
      img.style.cssText = 'width:34px;height:34px;border-radius:50%;object-fit:cover;';
    }
  });
  document.querySelectorAll('.js-brand-name').forEach(txt => { txt.textContent = b.name || ''; });
  if (b.accent) document.documentElement.style.setProperty('--teal', b.accent);
  updateManualContentTypes(b);
}

async function switchBrand(key) {
  // Brand-scoped LLM jobs (fetch/generate) are safe to switch during — their
  // results land on the brand they were for. Heavier jobs (render/bulk) would
  // clobber, so block and revert the dropdown to the real active brand.
  const SAFE = [null, undefined, 'fetch stories', 'generate plan'];
  if (!SAFE.includes(S.busy)) {
    toast(`Wait — '${S.busy}' is still running`, 'err');
    const sel = g('brand-sel'); if (sel && S.brandInfo) sel.value = S.brandInfo.key || sel.value;
    return;
  }
  try {
    const b = await api('/api/brand', 'PUT', { brand: key });
    applyBrandChrome(b);
    S.batch = [];
    document.getElementById('batch-bar').style.display = 'none';
    // Each brand keeps its OWN work — restore this brand's fetched stories + plan.
    const s = await api('/api/session').catch(() => ({}));
    if (s && s.stories && s.stories.length) {
      S.stories = s.stories;
      renderStoriesList(s.stories);
    } else {
      S.stories = [];
      document.getElementById('stories-list').innerHTML =
        `<div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">
           Switched to <b>${esc(b.name)}</b>. Click <b>Fetch &amp; Score</b> to pull its feeds.</div>`;
    }
    await restoreBatchView(s && s.batch);   // if this brand had generated results, show them
    if (s && s.plan) {
      loadPlan(s.plan);
      S.imagePaths = s.image_paths || {};
      Object.entries(S.imagePaths).forEach(([i,p]) => {
        if (p) setThumb(parseInt(i), '/image_cache/' + p.split(/[/\\]/).pop()); });
    } else {
      S.plan = null; S.imagePaths = {};
      document.getElementById('plan-form').innerHTML =
        `<div style="color:var(--muted);text-align:center;padding:28px 10px;font-size:12px;">
           Pick a story → plan loads here.</div>`;
    }
    await Promise.all([loadCategories(true), loadFormats()]);
    await loadDashboard();
    toast(`Brand: ${b.name}`);
  } catch(e) { toast(e.message, 'err'); }
}

// Re-hydrate from server state so a refresh / back-navigation doesn't wipe your
// work (which previously forced you to regenerate, re-running the model).
async function restoreSession() {
  let s;
  try { s = await api('/api/session'); } catch(e) { return; }
  if (s.stories && s.stories.length) {
    S.stories = s.stories;
    renderStoriesList(s.stories);
  }
  await restoreBatchView(s.batch);   // restore generated results across refresh
  if (s.plan) {
    loadPlan(s.plan);
    S.imagePaths = s.image_paths || {};
    Object.entries(S.imagePaths).forEach(([i,p]) => {
      if (p) setThumb(parseInt(i), '/image_cache/' + p.split(/[/\\]/).pop());
    });
    toast('Restored your last plan');
  }
  if (s.busy) {
    S.busy = s.busy;
    toast(`A '${s.busy}' job is still running on the server…`, 'err');
  }
}

// Warn before leaving while a model job is in flight.
window.addEventListener('beforeunload', (e) => {
  if (S.busy) { e.preventDefault(); e.returnValue = ''; }
});

async function loadFormats() {
  const data = await api('/api/formats').catch(() => ({ formats: {} }));
  S.formats = data.formats || {};
  S.formatRestrict = data.restrict || {};   // {fmt: [allowed brand keys]}
  // bulk format chips
  const wrap = document.getElementById('bulk-fmt-chips');
  if (wrap) {
    wrap.innerHTML = Object.entries(S.formats).map(([k, name]) =>
      `<button class="cat-btn ${S.bulkFormats.has(k) ? 'active' : ''}" data-bk="${k}" onclick="toggleBulkFmt('${k}',this)">${name}</button>`
    ).join('');
  }
}

function toggleBulkFmt(key, btn) {
  if (S.bulkFormats.has(key)) S.bulkFormats.delete(key); else S.bulkFormats.add(key);
  btn.classList.toggle('active');
}

async function loadModels() {
  const sel = document.getElementById('model-sel');
  const data = await api('/api/models').catch(() => ({ models: [] }));
  const models = data.models || [];
  if (!models.length) { sel.innerHTML = '<option>No models found</option>'; return; }
  sel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
}

async function setModel(m) {
  await api('/api/model', 'PUT', { model: m });
  toast(`Model: ${m}`);
}

const BACKEND_LABELS = { hermes: 'Hermes', ollama: 'Ollama' };

async function loadBackends() {
  const sel = g('backend-sel');
  if (!sel) return;
  const data = await api('/api/backend').catch(() => ({ backends: [], backend: '' }));
  const list = data.backends || [];
  if (!list.length) { sel.innerHTML = '<option>—</option>'; return; }
  // Hermes is primary; annotate it when its CLI isn't available.
  sel.innerHTML = list.map(b => {
    let label = BACKEND_LABELS[b] || b;
    if (b === 'hermes' && data.hermes_available === false) label += ' (offline)';
    return `<option value="${b}">${label}</option>`;
  }).join('');
  if (data.backend) sel.value = data.backend;
  // Loud, persistent banner whenever we silently fell back to Ollama.
  const banner = g('fallback-banner');
  if (banner) banner.style.display = data.auto_fell_back ? 'flex' : 'none';
}

// "Use Hermes" button on the fallback banner: re-probe + switch live.
async function retryHermes() {
  const r = await api('/api/backend', 'PUT', { backend: 'hermes' }).catch(() => null);
  if (!r) { toast('Engine switch failed'); return; }
  if (r.hermes_available === false) {
    toast('Hermes still not detected — start it, then try again.');
    return;
  }
  toast('Engine: Hermes');
  await loadBackends();
  await loadModels();
  const ms = g('model-sel');
  if (r.model && ms && [...ms.options].some(o => o.value === r.model)) ms.value = r.model;
}

async function setBackend(b) {
  const r = await api('/api/backend', 'PUT', { backend: b }).catch(() => null);
  if (!r) { toast('Engine switch failed'); return; }
  toast(`Engine: ${BACKEND_LABELS[b] || b}`);
  // The model list differs per backend — refresh it and select the new default.
  await loadModels();
  const ms = g('model-sel');
  if (r.model && ms && [...ms.options].some(o => o.value === r.model)) ms.value = r.model;
}

async function loadCategories() {
  const data = await api('/api/categories').catch(() => ({ categories: {} }));
  const cats = data.categories || {};
  const tb = document.getElementById('cat-tabs');
  S.selectedCat = '';
  tb.innerHTML = '<button class="cat-btn active" onclick="selectCat(\'\',this)">All</button>'
    + '<button class="cat-btn" style="color:#ff8a3d;" onclick="selectCat(\'__trending__\',this)" title="Pull from Google Trends instead of RSS">🔥 Trending</button>';
  Object.entries(cats).forEach(([k, name]) => {
    const b = document.createElement('button');
    b.className = 'cat-btn';
    b.textContent = name;
    b.onclick = () => selectCat(k, b);
    tb.appendChild(b);
  });
}

function selectCat(key, btn) {
  S.selectedCat = key;
  // Scope to the category bar only — other .cat-btn (story format chips, bulk
  // format chips) keep their own active state, which is their Set's source of truth.
  document.querySelectorAll('#cat-tabs .cat-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// ═══════════════════════════════════════════════════════════════════════════
// Mobile nav drawer (hamburger)
// ═══════════════════════════════════════════════════════════════════════════
function toggleNav(force) {
  const open = (force === undefined) ? !document.body.classList.contains('nav-open') : !!force;
  document.body.classList.toggle('nav-open', open);
  const h = document.getElementById('hamburger');
  if (h) h.setAttribute('aria-expanded', open ? 'true' : 'false');
}
// Close the drawer on Escape.
document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleNav(false); });

// ═══════════════════════════════════════════════════════════════════════════
// Tabs
// ═══════════════════════════════════════════════════════════════════════════
function showTab(name, btn) {
  if (name === 'editor') name = 'dashboard';
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  // Highlight by tab name so it stays correct regardless of nav order / caller.
  const active = document.querySelector(`.nav-btn[onclick*="showTab('${name}'"]`) || btn;
  if (active) active.classList.add('active');
  if (name === 'library') loadLibraryTab();
  if (name === 'templates' && !window._cm) initCM();
  if (name === 'templates') loadTmplList();
  if (name === 'canvas' && !window._fc) initCanvas();
  if (name === 'bulk' && !window._bulkInit) { window._bulkInit = true; initBulk(); }
  if (name === 'agent') { renderAgentSuggestions(); const t = g('agent-text'); if (t) setTimeout(() => t.focus(), 60); }
  if (name === 'scripts') refreshScriptStorySelect();
  if (name === 'review') loadReview();
  if (name === 'calendar') { syncCalBrands(); loadCalendar(); }
  if (name === 'csv') initCsvDrop();
  if (name === 'create') renderManualImages();
  if (name === 'dashboard') loadDashboard();
  try { localStorage.setItem('k2_active_tab', name); } catch(e) {}   // remember across refresh
  toggleNav(false);   // collapse the mobile drawer after picking a tab
  requestAnimationFrame(refreshResponsiveSurfaces);
}

// Re-open the tab the user was last on (survives a page refresh).
function restoreActiveTab() {
  // The dashboard is intentionally the front door on every fresh visit.
  try { localStorage.setItem('k2_active_tab', 'dashboard'); } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════════════════
// Scripts — platform-specific voiceover script variants (/api/scripts/generate)
// ═══════════════════════════════════════════════════════════════════════════
function refreshScriptStorySelect() {
  const sel = g('scr-story'); if (!sel) return;
  const cur = sel.value;
  const opts = ['<option value="">— Topic only —</option>'].concat(
    (S.stories || []).map((s, i) => `<option value="${i}">${esc((s.title || '').slice(0, 80))}</option>`)
  );
  sel.innerHTML = opts.join('');
  if (cur && S.stories && S.stories[cur]) sel.value = cur;
}

async function suggestScriptStories() {
  if (S.busy) { toast(`Wait — '${S.busy}' is still running`, 'err'); return; }
  const btn = g('btn-scr-suggest'); const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Ranking…';
  S.busy = 'suggest stories';
  const t0 = Date.now();
  const tid = setInterval(() => { g('hdr-timer').textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's'; }, 200);
  try {
    const model = g('model-sel').value;
    const data = await api(`/api/stories/fetch?limit=24&top=8&category=&model=${encodeURIComponent(model)}&brand=${encodeURIComponent(curBrand())}`, 'POST');
    g('hdr-timer').textContent = data.elapsed + 's';
    S.stories = data.stories || [];
    refreshScriptStorySelect();
    if (S.stories.length) { g('scr-story').value = '0'; toast(`Loaded ${S.stories.length} top stories`); }
    else toast('No stories found', 'err');
  } catch (e) {
    toast(e.message || 'Failed', 'err');
  } finally {
    clearInterval(tid); btn.disabled = false; btn.innerHTML = old; S.busy = null;
  }
}

async function generateScripts() {
  if (S.busy) { toast(`Wait — '${S.busy}' is still running`, 'err'); return; }
  const idx   = g('scr-story').value;
  const topic = g('scr-topic').value.trim();
  let story = null;
  if (idx !== '' && S.stories[idx]) {
    const s = S.stories[idx];
    story = { title: s.title, summary: s.summary, url: s.url, published: s.published || '', image: s.image || '' };
  }
  if (!story && !topic) { toast('Pick a story or type a topic', 'err'); return; }

  const body = {
    story, topic,
    keywords:     g('scr-keywords').value,
    platform:     g('scr-platform').value,
    content_type: g('scr-ctype').value,
    num_variants: parseInt(g('scr-num').value || '3'),
    duration:     parseInt(g('scr-dur').value || '30'),
    model:        g('model-sel').value,
    brand:        curBrand(),
    outline:      g('scr-outline') ? g('scr-outline').value : '',   // your beats (one per line)
    hook:         g('scr-hook') ? g('scr-hook').value.trim() : '',
    cta:          g('scr-cta') ? g('scr-cta').value.trim() : '',
  };

  const btn = g('btn-scr-gen');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Writing…';
  S.busy = 'generate scripts';
  const list = g('scripts-list');
  list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px;">Generating script variants…</div>';
  const t0 = Date.now();
  const tid = setInterval(() => { g('hdr-timer').textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's'; }, 200);
  try {
    const data = await api('/api/scripts/generate', 'POST', body);
    g('hdr-timer').textContent = data.elapsed + 's';
    S.scripts = data.scripts || [];
    renderScripts(S.scripts);
    toast(`Generated ${S.scripts.length} script${S.scripts.length === 1 ? '' : 's'}`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);font-size:13px;text-align:center;padding:30px;">${esc(e.message || 'Failed')}</div>`;
  } finally {
    clearInterval(tid); btn.disabled = false; btn.innerHTML = '📝 Generate scripts'; S.busy = null;
  }
}

function renderScripts(scripts) {
  const list = g('scripts-list');
  if (!scripts || !scripts.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px;">No scripts.</div>';
    return;
  }
  list.innerHTML = scripts.map((s, i) => {
    const segs = s.segments || [];
    const segHtml = segs.map((sg, j) => `
      <div style="display:flex;gap:6px;align-items:flex-start;margin-bottom:6px;">
        <input class="model-sel" style="width:88px;font-size:11px;" value="${esc(sg.label)}" oninput="scrSegSet(${i},${j},'label',this.value)" title="Segment label">
        <input class="model-sel" type="number" step="0.5" min="0" style="width:64px;font-size:11px;" value="${sg.time}" oninput="scrSegSet(${i},${j},'time',parseFloat(this.value)||0)" title="Start (seconds)">
        <textarea class="model-sel" rows="2" style="flex:1;font-size:12px;resize:vertical;font-family:inherit;" oninput="scrSegSet(${i},${j},'text',this.value)">${esc(sg.text)}</textarea>
        <button class="btn btn-ghost btn-sm" onclick="scrSegDel(${i},${j})" title="Remove segment">✕</button>
      </div>`).join('');
    const caps  = (s.captions || []).map(c =>
      `<div style="display:flex;gap:8px;font-size:12px;"><span style="color:var(--teal);min-width:42px;">${c.time}s</span><span>${esc(c.text)}</span></div>`).join('');
    return `<div class="story-card" style="cursor:default;">
      <div class="s-top" style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <span class="score-pill" style="background:rgba(0,180,200,.15);color:var(--teal);">${esc(s.angle)}</span>
        <span style="font-size:11px;color:var(--muted);">${esc(s.platform)} · ${esc(s.content_type)} · ~${s.duration_seconds}s</span>
        <div style="flex:1;"></div>
        <button class="btn btn-ghost btn-sm" onclick='copyScript(${i})'>📋 Copy</button>
      </div>
      ${s.hook ? `<div style="font-size:13px;font-weight:700;color:#fff;margin-bottom:6px;">🎬 ${esc(s.hook)}</div>` : ''}
      ${segs.length ? `
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px;">Segments — edit the text or re-time any beat:</div>
        ${segHtml}
        <button class="btn btn-ghost btn-sm" onclick="scrSegAdd(${i})" style="margin-bottom:8px;">+ Add segment</button>
        <details style="margin-bottom:6px;"><summary style="font-size:11px;color:var(--muted);cursor:pointer;">Full voiceover (read-through)</summary>
          <div style="font-size:12px;line-height:1.5;color:var(--text);white-space:pre-wrap;margin-top:6px;">${esc((segs.map(x=>x.text).join(' ')) || s.voice_over)}</div></details>`
      : `<div style="font-size:13px;line-height:1.5;color:var(--text);white-space:pre-wrap;margin-bottom:8px;">${esc(s.voice_over)}</div>`}
      ${s.cta ? `<div style="font-size:12px;color:var(--green);margin-bottom:8px;">➡ ${esc(s.cta)}</div>` : ''}
      ${caps ? `<details style="margin-top:4px;"><summary style="font-size:11px;color:var(--muted);cursor:pointer;">Captions (${(s.captions || []).length})</summary><div style="margin-top:6px;display:flex;flex-direction:column;gap:3px;">${caps}</div></details>` : ''}
    </div>`;
  }).join('');
}

function scrSegSet(i, j, field, val) {
  const s = (S.scripts || [])[i]; if (!s || !s.segments || !s.segments[j]) return;
  s.segments[j][field] = val;
  if (field === 'text' || field === 'time') s.voice_over = s.segments.map(x => x.text).join(' ');
}
function scrSegAdd(i) {
  const s = (S.scripts || [])[i]; if (!s) return;
  s.segments = s.segments || [];
  const last = s.segments[s.segments.length - 1];
  s.segments.push({ label: 'Beat ' + (s.segments.length), time: last ? (last.time + 3) : 0, text: '' });
  renderScripts(S.scripts);
}
function scrSegDel(i, j) {
  const s = (S.scripts || [])[i]; if (!s || !s.segments) return;
  s.segments.splice(j, 1);
  s.voice_over = s.segments.map(x => x.text).join(' ');
  renderScripts(S.scripts);
}

function copyScript(i) {
  const s = (S.scripts || [])[i]; if (!s) return;
  const lines = [
    `[${s.angle}] ${s.platform} · ${s.content_type} · ~${s.duration_seconds}s`,
    s.hook ? `HOOK: ${s.hook}` : '',
    '', s.voice_over, '',
    s.cta ? `CTA: ${s.cta}` : '',
    s.caption_text ? `CAPTION: ${s.caption_text}` : '',
  ].filter(Boolean).join('\n');
  navigator.clipboard.writeText(lines).then(() => toast('Script copied')).catch(() => toast('Copy failed', 'err'));
}

// ═══════════════════════════════════════════════════════════════════════════
// Post calendar — the queue laid out by scheduled date (/api/calendar)
// ═══════════════════════════════════════════════════════════════════════════
const CAL_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function calShift(delta) {
  const c = S.cal || {};
  let y = c.year, m = c.month + delta;
  if (m < 1) { m = 12; y--; } else if (m > 12) { m = 1; y++; }
  loadCalendar(y, m);
}

function calGoToday() {
  const n = new Date();
  loadCalendar(n.getFullYear(), n.getMonth() + 1);
}

async function loadCalendar(year, month) {
  const grid = g('cal-grid'); if (!grid) return;
  const c = S.cal || {};
  year = year || c.year || new Date().getFullYear();
  month = month || c.month || (new Date().getMonth() + 1);
  const brand = g('cal-brand') ? g('cal-brand').value : '';

  g('cal-head').innerHTML = CAL_DAYS.map(d => `<div>${d}</div>`).join('');
  grid.innerHTML = '<div style="grid-column:1/-1;color:var(--muted);padding:40px;text-align:center;font-size:13px;">Loading…</div>';
  try {
    const data = await api(`/api/calendar?year=${year}&month=${month}` + (brand ? `&brand=${encodeURIComponent(brand)}` : ''));
    S.cal = data;
    // Every post the day cells and the schedule modal might need to read.
    S.calItems = [].concat(...data.weeks.map(w => [].concat(...w.map(d => d.posts)))).concat(data.undated);
    g('cal-label').textContent = data.label;

    grid.innerHTML = data.weeks.map(week => week.map(day => {
      const chips = day.posts.map(p => {
        const t = (p.scheduled_at || '').slice(11, 16);
        const cls = p.status === 'published' ? 'published' : (p.status === 'failed' ? 'failed' : '');
        return `<button class="cal-chip ${cls}" onclick="event.stopPropagation();calOpenPost('${p.id}')"
          title="${esc((p.title || '') + ' · ' + (p.brand || '') + ' · ' + p.status)}">
          <span class="t">${esc(t || '—')}</span><span class="n">${esc(p.title || 'post')}</span></button>`;
      }).join('');
      return `<div class="cal-day${day.in_month ? '' : ' out'}${day.today ? ' today' : ''}">
        <div class="cal-daynum">${day.day}
          <button class="cal-add" title="Schedule a post on this day" onclick="calAddOn('${day.date}')">＋</button></div>
        <div class="cal-chips">${chips}</div>
      </div>`;
    }).join('')).join('');

    renderUnscheduled(data.undated);
    calSchedulerStatus();
  } catch (e) {
    grid.innerHTML = `<div style="grid-column:1/-1;color:var(--red);padding:24px;text-align:center;">${esc(e.message)}</div>`;
  }
}

// The tray of approved-but-undated posts, so a day always has something to fill.
function renderUnscheduled(items) {
  const tray = g('cal-unsched');
  if (!items || !items.length) { tray.style.display = 'none'; return; }
  tray.style.display = '';
  tray.innerHTML = `<span style="font-size:11px;color:var(--muted);font-weight:700;">UNSCHEDULED (${items.length})</span>`
    + items.map(p => `<button class="cal-pill" onclick="openSchedule('${p.id}')" title="Click to schedule">
        <span class="rv-status ${p.status || 'pending'}" style="font-size:9px;">${esc(p.status || '')}</span>
        <span>${esc(p.title || 'post')}</span></button>`).join('');
}

function calOpenPost(id) {
  const p = (S.calItems || []).find(x => x.id === id);
  if (!p) return;
  openModal(p.title || 'Post');
  const imgs = (p.files || []).slice(0, 4).map(f =>
    `<img src="/outputs/${p.rel}/${f}" style="height:120px;border-radius:6px;border:1px solid var(--border);">`).join('');
  const pub = p.publish || {};
  const err = pub.error || (pub.errors || []).map(e => e.platform + ': ' + e.error).join(' · ');
  const done = p.status === 'published';
  g('modal-body').innerHTML = `
    <div style="display:flex;gap:6px;flex-wrap:wrap;">${imgs}</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px;">
      <span class="rv-status ${p.status}">${esc(p.status)}</span>
      <span style="font-size:11.5px;color:var(--muted);">${esc(p.brand || '')} · ${esc(p.format || '')}</span>
      ${p.scheduled_at ? `<span class="rv-when">🗓 ${esc(fmtWhen(p.scheduled_at))}</span>` : ''}
      ${(p.targets || []).length ? `<span style="font-size:11px;color:var(--muted);">→ ${esc(p.targets.join(' + '))}</span>` : ''}
    </div>
    <div class="rv-cap" style="margin-top:6px;">${esc(p.caption || '(no caption)')}</div>
    ${err ? `<div style="font-size:12px;color:var(--red);margin-top:6px;">${esc(err)}</div>` : ''}
    ${pub.permalink ? `<a href="${esc(pub.permalink)}" target="_blank" style="font-size:12px;color:var(--green);">View published post ↗</a>` : ''}
    <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;margin-top:12px;">
      ${done ? '' : `<button class="btn btn-ghost btn-sm" onclick="closeModal();unschedule('${p.id}')">✕ Unschedule</button>
      <button class="btn btn-primary btn-sm" onclick="openSchedule('${p.id}')">✎ Reschedule</button>
      <button class="btn btn-green btn-sm" onclick="closeModal();publishNow('${p.id}')">🚀 Publish now</button>`}
    </div>`;
}

// Clicking a day offers the unscheduled posts to drop into it.
function calAddOn(dateStr) {
  const pool = ((S.cal || {}).undated || []);
  if (!pool.length) { toast('Nothing unscheduled — generate or approve some posts first'); return; }
  openModal('Schedule on ' + dateStr);
  g('modal-body').innerHTML = `
    <div style="font-size:12.5px;color:var(--muted);">Pick a post to place on this day.</div>
    <div style="display:flex;flex-direction:column;gap:7px;margin-top:8px;">
      ${pool.map(p => `<button class="cal-pill" style="max-width:none;border-radius:8px;justify-content:flex-start;"
        onclick="openSchedule('${p.id}', '${dateStr}T09:00')">
        <span class="rv-status ${p.status || 'pending'}" style="font-size:9px;">${esc(p.status || '')}</span>
        <span>${esc(p.title || 'post')}</span>
        <span style="font-size:10.5px;color:var(--muted);margin-left:auto;">${esc(p.brand || '')}</span>
      </button>`).join('')}
    </div>`;
}

async function calAutofill() {
  const brand = g('cal-brand') ? g('cal-brand').value : '';
  try {
    const r = await api('/api/calendar/autofill', 'POST', brand ? { brand } : {});
    if (!r.scheduled) { toast('Nothing left to schedule'); return; }
    toast(`Scheduled ${r.scheduled} post${r.scheduled === 1 ? '' : 's'}`
      + (r.unplaced ? ` · ${r.unplaced} had no free slot` : ''));
    loadCalendar();
    updateBadges();
  } catch (e) { toast(e.message, 'err'); }
}

async function calSchedulerStatus() {
  const el = g('cal-sched-status'); if (!el) return;
  try {
    const s = await api('/api/scheduler/status');
    const next = s.next ? ` · next ${fmtWhen(s.next)}` : '';
    el.innerHTML = s.auto_publish
      ? `<span style="color:var(--green);">● auto-publish on</span><span style="color:var(--muted);">${esc(next)}</span>`
      : `<span style="color:var(--yellow);">‖ auto-publish off</span><span style="color:var(--muted);">${esc(next)}</span>`;
  } catch (e) { el.textContent = ''; }
}

// Posting slots — the times auto-fill uses, saved back into config.yaml.
async function calSettings() {
  openModal('⚙ Posting slots');
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Loading…</div>';
  try {
    const s = await api('/api/schedule/settings');
    const dayBox = ['mon','tue','wed','thu','fri','sat','sun'].map(d =>
      `<label class="tb-group" style="gap:5px;"><input type="checkbox" class="sl-day" value="${d}"
        ${s.days.includes(d) ? 'checked' : ''}><span style="font-size:12.5px;text-transform:capitalize;">${d}</span></label>`).join('');
    body.innerHTML = `
      <div style="font-size:12.5px;color:var(--muted);line-height:1.6;">
        Auto-fill drops queued posts into these times, on these days, in your local timezone.</div>
      <label style="font-size:12px;color:var(--muted);margin-top:10px;">Times (comma separated, HH:MM)</label>
      <input id="sl-times" class="url-inp" style="width:100%;font-size:14px;padding:9px 11px;" value="${esc(s.times.join(', '))}">
      <label style="font-size:12px;color:var(--muted);margin-top:10px;">Days</label>
      <div style="display:flex;gap:12px;flex-wrap:wrap;padding:4px 0;">${dayBox}</div>
      <label class="tb-group" style="gap:7px;margin-top:8px;">
        <input type="checkbox" id="sl-auto" ${s.auto_publish ? 'checked' : ''}>
        <span style="font-size:13px;">Publish scheduled posts automatically</span></label>
      <div style="font-size:11.5px;color:var(--muted);margin-top:2px;">Off = the calendar plans, but nothing is sent.</div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
        <button class="btn btn-ghost btn-sm" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary btn-sm" onclick="saveSlots()">Save</button>
      </div>`;
  } catch (e) { body.innerHTML = `<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function saveSlots() {
  const times = g('sl-times').value.split(',').map(s => s.trim()).filter(Boolean);
  const days = [...document.querySelectorAll('.sl-day:checked')].map(c => c.value);
  try {
    await api('/api/schedule/settings', 'PUT', { times, days, auto_publish: g('sl-auto').checked });
    closeModal();
    toast('Posting slots saved');
    calSchedulerStatus();
  } catch (e) { toast(e.message, 'err'); }
}

// The calendar's brand filter mirrors the brands in the header dropdown.
function syncCalBrands() {
  const src = g('brand-sel'), dst = g('cal-brand');
  if (!src || !dst) return;
  const cur = dst.value;
  dst.innerHTML = '<option value="">All brands</option>' +
    [...src.options].map(o => `<option value="${esc(o.value)}">${esc(o.textContent)}</option>`).join('');
  dst.value = cur;
}

// ═══════════════════════════════════════════════════════════════════════════
// CSV import — a content sheet becomes carousels (/api/csv/*)
// ═══════════════════════════════════════════════════════════════════════════
function initCsvDrop() {
  const drop = g('csv-drop'); if (!drop || drop._init) return;
  drop._init = true;
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) csvUpload(f);
  });
}

async function csvUpload(file) {
  if (!file) return;
  if (!/\.csv$/i.test(file.name)) { toast('Pick a .csv file', 'err'); return; }
  const sum = g('csv-summary');
  sum.innerHTML = '<div style="color:var(--muted);font-size:12.5px;"><span class="spin"></span> Reading…</div>';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/csv/preview', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Could not read that CSV');
    S.csv = data;
    renderCsvPreview(data);
    g('csv-controls').style.display = '';
  } catch (e) {
    sum.innerHTML = `<div style="color:var(--red);font-size:12.5px;">${esc(e.message)}</div>`;
    g('csv-controls').style.display = 'none';
  }
}

function renderCsvPreview(d) {
  const shape = d.shape === 'long' ? 'one row per slide' : 'one row per post';
  const warn = d.unmapped.length
    ? `<span class="badge badge-info" title="These columns are carried along but not used for slide copy">${d.unmapped.length} unused column${d.unmapped.length === 1 ? '' : 's'}</span>` : '';
  g('csv-summary').innerHTML = `
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12.5px;">
      <span class="badge badge-ok">✓ ${esc(d.filename || 'sheet')}</span>
      <span style="color:var(--muted);">${d.row_count} rows · <b style="color:#fff;">${d.post_count} posts</b> · ${esc(shape)}</span>
      ${warn}
      <span style="color:var(--muted);">Mapped: ${esc(Object.keys(d.mapping).join(', ') || 'none')}</span>
    </div>`;

  const posts = d.posts.slice(0, 12);
  g('csv-preview').innerHTML = `
    <div style="font-size:12px;color:var(--muted);margin-bottom:7px;">Preview${d.posts.length > 12 ? ` (first 12 of ${d.posts.length})` : ''}</div>
    <div class="csv-scroll" style="max-height:340px;">
      <table class="csv-table">
        <thead><tr><th>#</th><th>Title</th><th>Slides</th><th>Caption</th><th>Images</th><th>Schedule</th></tr></thead>
        <tbody>${posts.map((p, i) => `<tr>
          <td style="color:var(--muted);">${i + 1}</td>
          <td style="color:#fff;">${esc(p.title || '—')}</td>
          <td>${p.slides.length ? esc(p.slides.map(s => s.heading || s.body.slice(0, 18)).join(' · ')) : '<span style="color:var(--yellow);">brief only</span>'}</td>
          <td>${esc((p.caption || '').slice(0, 60) || '—')}</td>
          <td>${esc(p.image_query || '—')}</td>
          <td>${esc(p.schedule || '—')}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>`;
}

function csvHelp() {
  openModal('CSV columns');
  g('modal-body').innerHTML = `
    <div style="font-size:13px;line-height:1.65;">
      <p style="margin-top:0;">Column names are matched loosely — case, spaces, and underscores
      are ignored, and common aliases work (<code>headline</code> for <code>title</code>,
      <code>tags</code> for <code>hashtags</code>, and so on).</p>
      <p style="margin:0 0 6px;"><b>One row per post</b> (the usual shape):</p>
      <code style="display:block;background:#0d1828;padding:9px 11px;border-radius:6px;white-space:pre;overflow-x:auto;font-size:11.5px;">title, subtitle, slide1_heading, slide1_body,
slide2_heading, slide2_body, cta, caption,
hashtags, image_query, schedule</code>
      <p style="margin:10px 0 6px;"><b>One row per slide</b>, grouped by a post id:</p>
      <code style="display:block;background:#0d1828;padding:9px 11px;border-radius:6px;white-space:pre;overflow-x:auto;font-size:11.5px;">post_id, order, heading, body, image_query</code>
      <ul style="padding-left:18px;margin:12px 0 0;display:flex;flex-direction:column;gap:7px;font-size:12.5px;">
        <li><b>Use my text as-is</b> renders exactly what the sheet says — no AI, no rewriting.</li>
        <li><b>AI writes from each row</b> treats the row as a brief and runs the normal planner.
          A <code>notes</code> or <code>body</code> column is the brief.</li>
        <li><code>image_query</code> is the stock-photo search for that slide.</li>
        <li><code>schedule</code> (e.g. <code>2026-09-03 09:00</code>) puts the post straight on
          the calendar.</li>
        <li><code>brand</code>, <code>format</code>, and <code>tone</code> columns override the
          dropdowns for that row.</li>
      </ul>
      <div style="display:flex;justify-content:flex-end;margin-top:14px;">
        <a class="btn btn-primary btn-sm" href="/api/csv/template" download>⬇ Download template</a>
      </div>
    </div>`;
}

async function csvGenerate() {
  if (!S.csv) { toast('Upload a CSV first', 'err'); return; }
  if (S.busy) { toast(`Wait — '${S.busy}' is running`, 'err'); return; }
  const btn = g('csv-gen');
  const out = g('csv-results');
  const n = S.csv.post_count;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Generating…';
  S.busy = 'csv import';
  out.innerHTML = `<div style="color:var(--muted);font-size:13px;padding:18px;text-align:center;">Building ${n} post${n === 1 ? '' : 's'}…</div>`;
  try {
    const data = await api('/api/csv/generate', 'POST', {
      posts: S.csv.posts,
      brand: curBrand(),
      format: g('csv-format').value,
      mode: g('csv-mode').value,
      source: g('csv-source').value,
      enqueue: g('csv-enqueue').checked,
      autoschedule: g('csv-autoschedule').checked,
      model: g('model-sel') ? g('model-sel').value : undefined,
    });
    const ok = data.results.filter(r => r.ok);
    const bad = data.results.filter(r => !r.ok);
    out.innerHTML = ok.map(r => `<div class="rv-card">
        <div class="rv-imgs">${(r.files || []).slice(0, 4).map(f =>
          `<a href="/outputs/${r.rel}/${f}?t=${Date.now()}" target="_blank"><img src="/outputs/${r.rel}/${f}?t=${Date.now()}"></a>`).join('')}</div>
        <div class="rv-body">
          <b style="color:#fff;font-size:13px;">${esc(r.title)}</b>
          <span style="font-size:11px;color:var(--muted);">${esc(r.format)} · ${(r.files || []).length} slides${r.has_images ? '' : ' · no images'}</span>
          <div class="rv-cap">${esc(r.caption || '')}</div>
        </div></div>`).join('')
      + bad.map(r => `<div class="rv-card" style="border-color:var(--red);">
          <div class="rv-body"><b style="color:var(--red);font-size:13px;">${esc(r.title || 'row')}</b>
          <div style="font-size:12px;color:var(--muted);">${esc(r.error || 'failed')}</div></div></div>`).join('');
    toast(`${ok.length} post${ok.length === 1 ? '' : 's'} built in ${data.elapsed}s`
      + (data.queued ? ` · ${data.queued} sent to Review` : '')
      + (bad.length ? ` · ${bad.length} failed` : ''));
    updateBadges();
  } catch (e) {
    out.innerHTML = `<div style="color:var(--red);font-size:13px;padding:14px;">${esc(e.message)}</div>`;
    toast(e.message, 'err');
  } finally {
    btn.disabled = false; btn.innerHTML = '⚡ Generate posts'; S.busy = null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Agent chat — talks to the local assistant (/api/agent/chat)
// ═══════════════════════════════════════════════════════════════════════════
function agentKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); agentSend(); return; }
  const t = e.target;
  setTimeout(() => { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 140) + 'px'; }, 0);
}
function agentAppend(role, html) {
  const log = g('agent-log');
  const wrap = document.createElement('div');
  wrap.className = 'agent-msg ' + (role === 'me' ? 'me' : 'bot');
  wrap.innerHTML = `<div class="agent-bubble">${html}</div>`;
  log.appendChild(wrap); log.scrollTop = log.scrollHeight;
  return wrap;
}
function renderAgentTurn(data) {
  let html = esc(data.reply || '(no reply)');
  if (data.steps && data.steps.length) {
    html += `<div class="agent-steps">` + data.steps.map(s =>
      `<div class="agent-step"><b>${esc(s.tool)}</b> · ${esc(s.summary || '')}</div>`).join('') + `</div>`;
  }
  if (data.posts && data.posts.length) {
    html += `<div class="agent-posts">` + data.posts.map(p =>
      (p.files || []).slice(0, 1).map(f =>
        `<a href="/outputs/${p.rel}/" target="_blank" title="${esc(p.title)}"><img src="/outputs/${p.rel}/${f}?t=${Date.now()}"></a>`
      ).join('')).join('') + `</div>`;
    html += `<div style="font-size:11px;color:var(--muted);margin-top:6px;">→ ${data.posts.length} post(s) sent to ✅ Review.</div>`;
    refreshReviewBadge();
  }
  agentAppend('bot', html);
}
async function agentSend() {
  const ta = g('agent-text'); const text = (ta.value || '').trim();
  if (!text) return;
  if (S.busy) { toast('A job is already running — wait for it to finish','err'); return; }
  agentAppend('me', esc(text));
  ta.value = ''; ta.style.height = 'auto';
  const btn = g('agent-send'); btn.disabled = true; S.busy = 'agent';
  const thinking = agentAppend('bot', '<span class="spin"></span> working…');
  try {
    const model = g('model-sel').value;
    const data = await api('/api/agent/chat', 'POST', { message: text, model });
    thinking.remove();
    renderAgentTurn(data);
  } catch(e) {
    thinking.remove();
    agentAppend('bot', `<span style="color:var(--red);">${esc(e.message)}</span>`);
  } finally { S.busy = null; btn.disabled = false; }
}
async function resetAgent() {
  await api('/api/agent/reset', 'POST').catch(() => {});
  g('agent-log').innerHTML = '';
  agentAppend('bot', 'New chat started. What should I make?');
}

// One-tap prompt presets so you don't retype the same asks.
const AGENT_SUGGESTIONS = [
  "Fetch JKR's top 5 gaming stories and make carousels for the best 3",
  "2 K2 LinkedIn posts about the top marketing stories, hyped tone",
  "Make a breaking-news post for the #1 JKR story",
  "Top 3 stories for both brands as square posts",
  "Fetch K2 web-dev stories and make a listicle from the best one",
];
function renderAgentSuggestions() {
  const host = g('agent-suggest'); if (!host || host.dataset.done) return;
  host.innerHTML = AGENT_SUGGESTIONS.map(s => `<button class="chip" onclick="useSuggestion(this)">${esc(s)}</button>`).join('');
  host.dataset.done = '1';
}
function useSuggestion(btn) {
  const ta = g('agent-text'); ta.value = btn.textContent; ta.focus();
  ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px';
}

// ═══════════════════════════════════════════════════════════════════════════
// Dashboard — four ready-to-handoff carousel packages
async function loadDashboard() {
  const host = g('dash-posts');
  if (!host) return;
  try {
    const data = await api('/api/dashboard?brand=' + encodeURIComponent(curBrand()));
    const counts = data.counts || {};
    ['total','pending','scheduled','published'].forEach(k => {
      const el = g('dash-' + k); if (el) el.textContent = counts[k] || 0;
    });
    const posts = data.posts || [];
    const exportBtn = g('dash-export');
    if (exportBtn) {
      exportBtn.disabled = !posts.length;
      exportBtn.textContent = posts.length ? `↓ Export all ${posts.length}` : '↓ Nothing to export';
    }
    if (!posts.length) {
      host.innerHTML = `<div class="dash-empty"><strong>No carousel packages yet</strong>Create a post or generate from RSS. Finished posts will collect here automatically.<div style="margin-top:16px"><button class="btn btn-primary" onclick="showTab('create')">Create your first post</button></div></div>`;
      return;
    }
    host.innerHTML = posts.map(dashboardPostCard).join('');
  } catch (e) {
    host.innerHTML = `<div class="dash-empty"><strong>Dashboard unavailable</strong>${esc(e.message)}</div>`;
  }
}

function dashboardPostCard(it) {
  const files = it.files || [];
  const image = files[0] ? `/outputs/${it.rel}/${encodeURIComponent(files[0])}?t=${Date.now()}` : '';
  const status = it.status || 'ready';
  const when = it.scheduled_at
    ? `Scheduled · ${fmtWhen(it.scheduled_at)}`
    : (it.suggested_at ? `Suggested · ${fmtWhen(it.suggested_at)}` : 'Ready for your next available slot');
  return `<article class="post-card">
    <div class="post-cover">
      ${image ? `<img src="${image}" alt="${esc(it.title || 'Carousel cover')}" loading="lazy">` : '<div class="dash-empty">No preview</div>'}
      <span class="post-count">${files.length} slides</span>
    </div>
    <div class="post-body">
      <div class="post-meta"><span class="post-brand">${esc(it.brand || 'brand')}</span><span class="rv-status ${esc(status)}">${esc(status)}</span></div>
      <div class="post-title">${esc(it.title || 'Untitled carousel')}</div>
      <div class="post-caption">${esc(it.caption || 'No caption supplied')}</div>
      <div class="post-when">${esc(when)}</div>
      <div class="post-actions">
        <button class="btn btn-ghost" onclick="openDashboardPost('${it.id}','${status}')">Review</button>
        <a class="btn btn-primary" href="/api/export/${it.id}" download>↓ Download</a>
      </div>
    </div>
  </article>`;
}

function exportDashboard() {
  const brand = encodeURIComponent(curBrand());
  window.location.href = '/api/export-dashboard?brand=' + brand;
  toast('Preparing carousel pack…');
}

function openDashboardPost(id, status) {
  const filter = g('review-filter');
  if (filter) filter.value = status || '';
  showTab('review');
  setTimeout(() => g('rv-' + id)?.scrollIntoView({behavior:'smooth', block:'center'}), 250);
}

// Review queue — approve (→ publish via n8n) / reject generated posts
// ═══════════════════════════════════════════════════════════════════════════
async function loadReview() {
  const host = g('review-list');
  host.innerHTML = '<div style="color:var(--muted);padding:24px;text-align:center;">Loading…</div>';
  const status = g('review-filter') ? g('review-filter').value : 'pending';
  try {
    const data = await api('/api/review' + (status ? ('?status=' + encodeURIComponent(status)) : ''));
    updateReviewBadge(data.pending);
    if (!data.items.length) {
      host.innerHTML = '<div style="color:var(--muted);padding:32px;text-align:center;font-size:13px;">Nothing here. Generate posts in the 🤖 Agent tab or run autopilot.</div>';
      return;
    }
    S.reviewItems = data.items;
    host.innerHTML = data.items.map(reviewCard).join('');
    loadMetaStatus();
  } catch(e) { host.innerHTML = `<div style="color:var(--red);padding:20px;">${esc(e.message)}</div>`; }
}
function reviewCard(it) {
  const imgs = (it.files || []).map(f => `<a href="/outputs/${it.rel}/${f}?t=${Date.now()}" target="_blank"><img src="/outputs/${it.rel}/${f}?t=${Date.now()}"></a>`).join('');
  const st = it.status || 'pending';
  const pub = it.publish || {};
  // Show whatever the publisher last said — a Meta error is the useful part.
  let note = '';
  if (pub.error) note = `<span style="font-size:10.5px;color:var(--red);align-self:center;">${esc(pub.error)}</span>`;
  else if ((pub.errors || []).length) note = `<span style="font-size:10.5px;color:var(--red);align-self:center;">${esc(pub.errors.map(e => e.platform + ': ' + e.error).join(' · '))}</span>`;
  else if (pub.permalink) note = `<a href="${esc(pub.permalink)}" target="_blank" style="font-size:10.5px;color:var(--green);align-self:center;">View post ↗</a>`;
  else if (pub.reason && st !== 'rejected') note = `<span style="font-size:10.5px;color:var(--muted);align-self:center;">${esc(pub.reason)}</span>`;

  const when = it.scheduled_at
    ? `<span class="rv-when">🗓 ${esc(fmtWhen(it.scheduled_at))}</span>` : '';
  const targets = (it.targets && it.targets.length) ? it.targets : null;
  const tgt = targets ? `<span style="font-size:10.5px;color:var(--muted);">→ ${esc(targets.join(' + '))}</span>` : '';

  let actions;
  if (st === 'pending') {
    actions = `<button class="btn btn-green btn-sm" onclick="approveReview('${it.id}',this)">✓ Approve</button>
       <button class="btn btn-primary btn-sm" onclick="openSchedule('${it.id}')">🗓 Schedule</button>
       <button class="btn btn-ghost btn-sm" onclick="publishNow('${it.id}',this)">🚀 Publish now</button>
       <button class="btn btn-ghost btn-sm" onclick="rejectReview('${it.id}')">✕ Reject</button>`;
  } else if (st === 'approved' || st === 'failed') {
    actions = `<button class="btn btn-primary btn-sm" onclick="openSchedule('${it.id}')">🗓 Schedule</button>
       <button class="btn btn-green btn-sm" onclick="publishNow('${it.id}',this)">🚀 Publish now</button>
       <button class="btn btn-ghost btn-sm" onclick="delReview('${it.id}')">Delete</button>`;
  } else if (st === 'scheduled') {
    actions = `<button class="btn btn-ghost btn-sm" onclick="openSchedule('${it.id}')">✎ Reschedule</button>
       <button class="btn btn-ghost btn-sm" onclick="unschedule('${it.id}')">✕ Unschedule</button>
       <button class="btn btn-green btn-sm" onclick="publishNow('${it.id}',this)">🚀 Publish now</button>`;
  } else {
    actions = `<button class="btn btn-ghost btn-sm" onclick="delReview('${it.id}')">Delete</button>`;
  }

  return `<div class="rv-card" id="rv-${it.id}">
    <div class="rv-imgs">${imgs}</div>
    <div class="rv-body">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span class="rv-status ${st}">${esc(st)}</span>
        <b style="color:#fff;font-size:13px;">${esc(it.title || '')}</b>
        <span style="font-size:11px;color:var(--muted);">${esc(it.brand || '')} · ${esc(it.format || '')}</span>
        ${when}${tgt}
      </div>
      <div class="rv-cap">${esc(it.caption || '(no caption)')}</div>
      ${blockedNote(it)}
      <div class="rv-actions">${actions}<a class="btn btn-primary btn-sm" href="/api/export/${it.id}" download>↓ Export ZIP</a>${note}</div>
    </div></div>`;
}

// "2026-08-27T18:30:00" -> "Thu 27 Aug, 18:30" (local, matching the calendar).
function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { weekday: 'short', day: 'numeric',
    month: 'short', hour: '2-digit', minute: '2-digit' });
}

// datetime-local wants "YYYY-MM-DDTHH:MM" in local time, not a UTC ISO string.
function toLocalInput(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Publishing readiness, shown in the Review toolbar so a missing token or
// PUBLIC_BASE_URL is visible before you approve twenty posts.
async function loadMetaStatus() {
  const el = g('meta-status'); if (!el) return;
  try {
    const s = await api('/api/meta/status');
    S.meta = s;
    if (!s.configured) {
      el.innerHTML = `<span class="badge badge-err" title="Set META_ACCESS_TOKEN in .env">⚠ Not connected</span>`;
    } else if (!s.public_base_url) {
      el.innerHTML = `<span class="badge badge-err" title="Instagram needs PUBLIC_BASE_URL — Meta fetches the images from your machine">⚠ No public URL</span>`;
    } else {
      const n = (s.accounts || []).filter(a => a.token_set).length;
      el.innerHTML = `<span class="badge badge-ok" title="Publishing directly via the Meta Graph API">● Connected (${n})</span>`;
    }
  } catch (e) { el.innerHTML = ''; }
}

// In-app tutorial for connecting Instagram / Facebook with your own Meta app.
function showChannelHelp() {
  openModal('Connect Instagram & Facebook');
  const s = S.meta || {};
  const rows = (s.accounts || []).map(a =>
    `<li><b>${esc(a.brand)}</b> — IG ${a.instagram ? '✓' : '—'} · FB ${a.facebook ? '✓' : '—'} ·
      token ${a.token_set ? '✓' : '<span style="color:var(--red);">missing</span>'}</li>`).join('');
  g('modal-body').innerHTML = `
    <div style="font-size:13px;line-height:1.65;">
      <p style="margin-top:0;">This app publishes <b>directly through the Meta Graph API</b> with
      your own credentials — no third-party scheduler in between.</p>
      <ol style="padding-left:18px;display:flex;flex-direction:column;gap:9px;margin:0;">
        <li><b>Instagram must be a Business or Creator account</b>, linked to a Facebook Page
          (Instagram app → Settings → Account type).</li>
        <li><b>Create a Meta app</b> at <code>developers.facebook.com</code> and add the
          <i>Instagram Graph API</i> product.</li>
        <li><b>Get a token</b> in the Graph API Explorer with these scopes:
          <code style="display:block;background:#0d1828;padding:8px 10px;border-radius:6px;margin-top:5px;white-space:pre-wrap;">instagram_basic, instagram_content_publish,
pages_show_list, pages_read_engagement, pages_manage_posts</code>
          Make it long-lived: <code>python meta.py --exchange-token &lt;token&gt;</code>,
          then put it in <code>.env</code> as <code>META_ACCESS_TOKEN</code>.</li>
        <li><b>App id + secret</b> in <code>.env</code> — the exchange above needs them.
          A Meta app shows two different pairs, so copy the one matching your login flow:
          <code style="display:block;background:#0d1828;padding:8px 10px;border-radius:6px;margin-top:5px;white-space:pre;">META_APP_ID / META_APP_SECRET            # App settings &gt; Basic
INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET  # Instagram &gt; API setup</code>
          Instagram-login tokens exchange with
          <code>python meta.py --exchange-ig-token &lt;token&gt;</code> and extend with
          <code>--refresh-ig-token</code> (do it before day 60).</li>
        <li><b>Map each brand</b> in <code>config.yaml</code> → <code>meta.accounts</code>:
          <code style="display:block;background:#0d1828;padding:8px 10px;border-radius:6px;margin-top:5px;white-space:pre;">accounts:
  brand_a:
    ig_user_id: "17841400000000000"
    fb_page_id: "1234567890"
    targets: ["instagram", "facebook"]</code></li>
        <li><b>Set <code>PUBLIC_BASE_URL</code></b> in <code>.env</code> to a public https address that
          serves this app's <code>/outputs</code> folder. Instagram fetches the rendered images
          itself, so <code>localhost</code> can never work. Facebook does not need this.</li>
        <li><b>Restart</b>, then hit <b>Check connection</b> below.</li>
      </ol>
      ${rows ? `<p style="margin-bottom:4px;"><b>Configured accounts</b></p><ul style="padding-left:18px;margin:0;font-size:12.5px;">${rows}</ul>` : ''}
      <p style="font-size:12px;color:var(--muted);">Public URL:
        <code>${esc(s.public_base_url || 'not set')}</code><br>App credentials:
        <code>META_APP_*</code> ${s.app_credentials?.facebook_app ? '✓' : '—'} ·
        <code>INSTAGRAM_APP_*</code> ${s.app_credentials?.instagram_app ? '✓' : '—'}</p>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn btn-sm" onclick="runPreflight()">🧪 Run preflight</button>
        <button class="btn btn-primary btn-sm" onclick="verifyMeta()">🔌 Check connection</button>
      </div>
      <div id="meta-verify" style="font-size:12px;"></div>
      <p style="color:var(--muted);font-size:12px;margin-bottom:0;">Full guide:
        <code>docs/PUBLISHING_AND_CHANNELS.md</code></p>
    </div>`;
}

// A scheduled post that could not go out keeps its slot and records why. Say so
// on the card — an unexplained "scheduled" in the past is what sent the user
// looking through logs in the first place.
function blockedNote(it) {
  const reason = it.blocked_reason || ((it.publish || {}).reason || '');
  if (!reason || it.status === 'published') return '';
  const since = it.blocked_since ? ` since ${esc(fmtWhen(it.blocked_since))}` : '';
  return `<div class="rv-blocked" title="${esc(reason)}">⚠ Not published${since} — ${esc(reason)}
    <a href="#" onclick="event.preventDefault();runPreflight('${esc(it.id)}');showChannelHelp();">check</a></div>`;
}

async function verifyMeta() {
  const out = g('meta-verify');
  out.innerHTML = '<div style="color:var(--muted);padding:8px 0;"><span class="spin"></span> Asking Meta…</div>';
  try {
    const r = await api('/api/meta/verify', 'POST', {});
    const rows = (r.accounts || []).map(a => {
      if (!a.ok) return `<div style="color:var(--red);padding:4px 0;">✕ <b>${esc(a.brand)}</b> — ${esc(a.error || '')}</div>`;
      const ig = a.instagram ? `IG @${esc(a.instagram.username || '?')}` : 'no IG';
      const fb = a.facebook ? ` · FB ${esc(a.facebook.name || '?')}` : '';
      const q = a.quota ? ` · ${a.quota.remaining}/${a.quota.total} posts left today` : '';
      return `<div style="color:var(--green);padding:4px 0;">✓ <b>${esc(a.brand)}</b> — ${ig}${fb}${q}
        <span style="color:var(--muted);">· token expires ${esc(a.token_expires || '?')}</span></div>`;
    }).join('');
    out.innerHTML = (r.warning ? `<div style="color:var(--yellow);padding:4px 0;">⚠ ${esc(r.warning)}</div>` : '') + rows;
    loadMetaStatus();
  } catch (e) { out.innerHTML = `<div style="color:var(--red);padding:6px 0;">${esc(e.message)}</div>`; }
}

// Preflight: every publishing constraint, checked and listed. Answers "why is
// this post not going out" without having to read the server log.
async function runPreflight(id) {
  const out = g('meta-verify');
  if (!out) return;
  out.innerHTML = '<div style="color:var(--muted);padding:8px 0;"><span class="spin"></span> Checking…</div>';
  try {
    const body = id ? { id } : { brand: (S.brand || '') };
    const r = await api('/api/meta/preflight', 'POST', body);
    const rows = (r.checks || []).map(c => {
      if (c.ok) return `<div style="color:var(--muted);padding:2px 0;">✓ ${esc(c.name)}</div>`;
      const color = c.level === 'warn' ? 'var(--yellow)' : 'var(--red)';
      const mark = c.level === 'warn' ? '⚠' : '✕';
      return `<div style="color:${color};padding:3px 0;">${mark} <b>${esc(c.name)}</b> — ${esc(c.detail)}</div>`;
    }).join('');
    const head = r.ok
      ? '<div style="color:var(--green);padding:4px 0;"><b>Ready to publish.</b></div>'
      : `<div style="color:var(--red);padding:4px 0;"><b>Blocked</b> — ${(r.blocking || []).length} check(s) must pass first.</div>`;
    out.innerHTML = head + rows;
  } catch (e) { out.innerHTML = `<div style="color:var(--red);padding:6px 0;">${esc(e.message)}</div>`; }
}

// ── Scheduling from the Review tab ────────────────────────────────────────────
async function openSchedule(id, presetISO) {
  const items = (S.reviewItems || []).concat(S.calItems || []);
  const it = items.find(x => x.id === id) || {};
  const acct = ((S.meta || {}).accounts || []).find(a => a.brand === it.brand);
  const def = it.targets || (acct && acct.targets) || ['instagram'];
  const start = presetISO ? new Date(presetISO)
    : (it.scheduled_at ? new Date(it.scheduled_at) : new Date(Date.now() + 3600e3));

  openModal('🗓 Schedule post');
  g('modal-body').innerHTML = `
    <div style="font-size:13px;line-height:1.6;">
      <b style="color:#fff;">${esc(it.title || 'post')}</b>
      <span style="font-size:11px;color:var(--muted);"> · ${esc(it.brand || '')} · ${esc(it.format || '')}</span>
    </div>
    <label style="font-size:12px;color:var(--muted);margin-top:8px;">When (your local time)</label>
    <input type="datetime-local" id="sch-when" class="url-inp" value="${toLocalInput(start)}"
      style="width:100%;font-size:14px;padding:9px 11px;">
    <label style="font-size:12px;color:var(--muted);margin-top:10px;">Publish to</label>
    <div style="display:flex;gap:14px;padding:4px 0;">
      <label class="tb-group" style="gap:6px;"><input type="checkbox" id="sch-ig" ${def.includes('instagram') ? 'checked' : ''}><span style="font-size:13px;">Instagram</span></label>
      <label class="tb-group" style="gap:6px;"><input type="checkbox" id="sch-fb" ${def.includes('facebook') ? 'checked' : ''}><span style="font-size:13px;">Facebook Page</span></label>
    </div>
    <div id="sch-quick" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
      <button class="btn btn-ghost btn-sm" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="saveSchedule('${id}')">🗓 Schedule</button>
    </div>`;

  // Offer the next few configured posting slots as one-click choices.
  try {
    const s = await api('/api/schedule/settings');
    const chips = (s.times || []).map(t => {
      const d = new Date(start); const [hh, mm] = t.split(':');
      d.setHours(+hh, +mm, 0, 0);
      if (d < new Date()) d.setDate(d.getDate() + 1);
      return `<button class="btn btn-ghost btn-sm" onclick="g('sch-when').value='${toLocalInput(d)}'">${esc(t)}</button>`;
    }).join('');
    if (chips) g('sch-quick').innerHTML =
      `<span style="font-size:11px;color:var(--muted);align-self:center;">Slots:</span>${chips}`;
  } catch (e) {}
}

async function saveSchedule(id) {
  const when = g('sch-when').value;
  if (!when) { toast('Pick a date and time', 'err'); return; }
  const targets = [];
  if (g('sch-ig').checked) targets.push('instagram');
  if (g('sch-fb').checked) targets.push('facebook');
  if (!targets.length) { toast('Pick at least one destination', 'err'); return; }
  try {
    await api('/api/calendar/schedule', 'POST', { id, when, targets });
    closeModal();
    toast('Scheduled for ' + fmtWhen(when));
    refreshQueueViews();
  } catch (e) { toast(e.message, 'err'); }
}

async function unschedule(id) {
  try {
    await api('/api/calendar/unschedule', 'POST', { id });
    toast('Removed from the calendar');
    refreshQueueViews();
  } catch (e) { toast(e.message, 'err'); }
}

async function publishNow(id, btn) {
  if (!confirm('Publish this post now?')) return;
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>'; }
  try {
    const r = await api('/api/review/' + id + '/publish', 'POST', {});
    const p = r.publish || {};
    if (p.sent) toast('Published ✓' + (p.permalink ? ' — live on Meta' : ''));
    else toast(p.error || (p.errors || [])[0]?.error || p.reason || 'Not published', 'err');
    refreshQueueViews();
  } catch (e) {
    toast(e.message, 'err');
    if (btn) { btn.disabled = false; btn.innerHTML = '🚀 Publish now'; }
  }
}

// Review and Calendar read the same queue, so one action refreshes both.
function refreshQueueViews() {
  if (g('tab-review') && g('tab-review').classList.contains('active')) loadReview();
  if (g('tab-calendar') && g('tab-calendar').classList.contains('active')) loadCalendar();
  updateBadges();
}

async function updateBadges() {
  try {
    const data = await api('/api/review');
    updateReviewBadge(data.items.filter(i => i.status === 'pending').length);
    const n = data.items.filter(i => i.status === 'scheduled').length;
    const b = g('cal-badge');
    if (b) { b.textContent = n; b.style.display = n ? '' : 'none'; }
  } catch (e) {}
}

async function approveReview(id, btn) {
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  try {
    const r = await api('/api/review/' + id + '/approve', 'POST', {});
    const p = r.publish || {};
    if (p.sent) toast('Approved & published ✓');
    else toast('Approved — schedule it on the calendar or publish now');
    loadReview();
    updateBadges();
  } catch(e) { toast(e.message, 'err'); btn.disabled = false; btn.innerHTML = '✓ Approve'; }
}

async function rejectReview(id) { await api('/api/review/' + id + '/reject', 'POST').catch(() => {}); loadReview(); }
async function delReview(id)    { await api('/api/review/' + id, 'DELETE').catch(() => {}); loadReview(); }
async function clearReview()    { if (!confirm('Remove all rejected entries?')) return; await api('/api/review/clear', 'POST', { status: 'rejected' }).catch(() => {}); loadReview(); }
function updateReviewBadge(n) {
  const b = g('review-badge'); if (!b) return;
  if (n > 0) { b.textContent = n; b.style.display = 'inline-flex'; } else b.style.display = 'none';
}
async function refreshReviewBadge() {
  try { const d = await api('/api/review?status=pending'); updateReviewBadge(d.pending); } catch(e) {}
  updateBadges();
}

function refreshResponsiveSurfaces() {
  try { if (window._cm) window._cm.refresh(); } catch(e) {}
  try {
    if (fc) {
      fc.calcOffset();
      fc.requestRenderAll();
    }
  } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════════════════
// Bulk — generate posts for every brand in one run
// ═══════════════════════════════════════════════════════════════════════════
async function initBulk() {
  let brands = {};
  try { brands = (await api('/api/brands')).brands || {}; } catch(e){}
  const host = g('bulk-brands');
  host.innerHTML = '';
  for (const [key, name] of Object.entries(brands)) {
    S.bulk[key] = { name, stories: [], sel: {}, formats: new Set(['carousel']), count: 25 };
    // per-brand categories (resolved without switching the active brand)
    let cats = {};
    try { cats = (await api('/api/categories?brand='+encodeURIComponent(key))).categories || {}; } catch(e){}
    const catOpts = ['<option value="">All RSS feeds</option>',
                     '<option value="__trending__">🔥 Google Trending</option>']
      .concat(Object.entries(cats).map(([k,n])=>`<option value="${k}">${esc(n)}</option>`)).join('');
    const restrict = S.formatRestrict || {};
    const fmtChips = Object.entries(S.formats)
      .filter(([fk]) => !restrict[fk] || restrict[fk].includes(key))   // hide brand-locked formats
      .map(([fk,fn]) =>
        `<button class="cat-btn bfmt" data-bk="${key}" data-fk="${fk}" onclick="bulkToggleFmt('${key}','${fk}',this)" style="padding:2px 9px;font-size:11px;">${esc(fn)}</button>`
      ).join('');
    const col = document.createElement('div');
    col.className = 'bulk-col';
    col.innerHTML = `
      <div class="bulk-col-head">
        <img src="/api/brand-logo?brand=${encodeURIComponent(key)}" onerror="this.style.display='none'" alt="">
        <b style="color:#fff;font-size:14px;">${esc(name)}</b>
        <div style="flex:1"></div>
        <select class="src-sel" id="bulk-cat-${key}">${catOpts}</select>
        <input type="number" id="bulk-count-${key}" value="25" min="1" max="60" style="width:52px;background:#0d1828;border:1px solid var(--border);border-radius:5px;color:#fff;padding:4px 6px;font-size:12px;" title="how many top stories">
        <button class="btn btn-primary btn-sm" onclick="bulkFetch('${key}')" id="bulk-fetch-${key}">Fetch top</button>
        <button class="btn btn-ghost btn-sm" onclick="bulkClear('${key}')" id="bulk-clear-${key}" title="Clear fetched stories" style="display:none;">✕ Clear</button>
        <span id="bulk-status-${key}" style="font-size:11px;color:var(--muted);"></span>
      </div>
      <div class="bulk-fmts" id="bulk-fmts-${key}">
        <span style="font-size:11px;color:var(--muted);align-self:center;">formats:</span>${fmtChips}
      </div>
      <div class="bulk-list" id="bulk-list-${key}">
        <div style="color:var(--muted);font-size:12px;text-align:center;padding:20px;">Pick a source and click <b>Fetch top</b>.</div>
      </div>`;
    host.appendChild(col);
    // reflect the default-on carousel chip
    col.querySelectorAll(`.bfmt[data-bk="${key}"]`).forEach(b => {
      if (S.bulk[key].formats.has(b.dataset.fk)) b.classList.add('active');
    });
  }
}

function bulkToggleFmt(key, fk, btn) {
  const st = S.bulk[key]; if (!st) return;
  if (st.formats.has(fk)) st.formats.delete(fk); else st.formats.add(fk);
  btn.classList.toggle('active');
  bulkUpdateTotal();
}

async function bulkFetch(key) {
  const st = S.bulk[key]; if (!st) return;
  const cat = g('bulk-cat-'+key).value;
  const count = parseInt(g('bulk-count-'+key).value || '25');
  st.count = count;
  const btn = g('bulk-fetch-'+key); const status = g('bulk-status-'+key);
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  status.textContent = 'fetching…';
  const model = g('model-sel').value;
  try {
    // brand= makes the server score/trend for this brand (per-request brand, no UI switch)
    const data = await api(`/api/stories/fetch?limit=${count}&top=${count}&category=${encodeURIComponent(cat)}&model=${encodeURIComponent(model)}&brand=${encodeURIComponent(key)}`, 'POST');
    st.stories = data.stories || [];
    st.sel = {};
    st.stories.forEach((_,i)=> st.sel[i] = true);  // pre-select all
    renderBulkList(key);
    const clr = g('bulk-clear-'+key); if (clr) clr.style.display = st.stories.length ? 'inline-flex' : 'none';
    status.innerHTML = `<span class="badge badge-ok">${st.stories.length} ready</span>`;
  } catch(e) {
    status.innerHTML = `<span class="badge badge-err">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.innerHTML = 'Fetch top';
  }
}

// ✕ Clear a column's fetched stories back to the empty state.
function bulkClear(key) {
  const st = S.bulk[key]; if (!st) return;
  st.stories = []; st.sel = {};
  const list = g('bulk-list-'+key);
  if (list) list.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px;">Pick a source and click <b>Fetch top</b>.</div>';
  const status = g('bulk-status-'+key); if (status) status.textContent = '';
  const clr = g('bulk-clear-'+key); if (clr) clr.style.display = 'none';
  bulkUpdateTotal();
}

function renderBulkList(key) {
  const st = S.bulk[key]; const list = g('bulk-list-'+key);
  if (!st.stories.length) { list.innerHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:20px;">No stories.</div>'; bulkUpdateTotal(); return; }
  list.innerHTML = st.stories.map((s,i)=>`
    <label class="bulk-item">
      <input type="checkbox" ${st.sel[i]?'checked':''} onchange="bulkToggleStory('${key}',${i},this)" style="width:15px;height:15px;margin-top:2px;flex-shrink:0;">
      <div>
        <div class="bt">${esc(s.title)}</div>
        <div class="bs">${s.score!=null?('score '+s.score+' · '):''}${esc((s.reason||'').slice(0,90))}</div>
      </div>
    </label>`).join('');
  bulkUpdateTotal();
}

function bulkToggleStory(key, i, chk) {
  S.bulk[key].sel[i] = chk.checked;
  bulkUpdateTotal();
}

function bulkSelectedItems(key) {
  const st = S.bulk[key]; if (!st) return [];
  const fmts = [...st.formats];
  if (!fmts.length) return [];
  const total = parseInt(g('slide-count-sel')?.value || '4');
  const items = [];
  st.stories.forEach((s,i)=>{
    if (!st.sel[i]) return;
    items.push({ story:{title:s.title,summary:s.summary,url:s.url,published:s.published||'',image:s.image||''},
                 formats: fmts, total_slides: total });
  });
  return items;
}

function bulkUpdateTotal() {
  let posts = 0;
  Object.keys(S.bulk).forEach(k => {
    bulkSelectedItems(k).forEach(it => posts += it.formats.length);
  });
  const el = g('bulk-total'); if (el) el.textContent = posts + ' posts';
  return posts;
}

async function runBulk() {
  const groups = Object.keys(S.bulk)
    .map(k => ({ brand:k, items: bulkSelectedItems(k) }))
    .filter(g => g.items.length);
  if (!groups.length) { toast('Select at least one story','err'); return; }
  const totalPosts = bulkUpdateTotal();

  const btn = g('btn-bulk-run');
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Generating ${totalPosts}…`;
  S.busy = 'bulk'; S.cancelRequested = false;
  const cancelBtn = g('btn-bulk-cancel'); if (cancelBtn) cancelBtn.style.display='inline-flex';
  const prog = g('bulk-progress'); prog.style.display='block';
  const poll = setInterval(pollBulkProgress, 700);
  const t0 = Date.now();
  const tid = setInterval(()=>{ g('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s'; },200);
  try {
    const model = g('model-sel').value;
    const source = g('bulk-src').value;
    const tone = g('bulk-tone').value || '';
    const data = await api('/api/bulk/run','POST',{groups,model,source,tone});
    g('hdr-timer').textContent = data.elapsed+'s';
    await refreshReviewIndex();
    showBulkResults(data);
    const ok = data.results.filter(r=>r.ok).length;
    const note = data.cancelled ? ' (cancelled)' : '';
    toast(`Bulk done${note}: ${ok}/${data.results.length} posts in ${data.elapsed}s`);
    notify(data.cancelled?'Bulk cancelled':'Bulk complete', `${ok}/${data.results.length} posts in ${data.elapsed}s`);
  } catch(e) {
    toast(e.message,'err'); notify('Bulk failed', e.message);
  } finally {
    S.busy = null; clearInterval(tid); clearInterval(poll);
    prog.style.display='none';
    if (cancelBtn) cancelBtn.style.display='none';
    btn.disabled = false; btn.innerHTML = '⚡ Generate All';
  }
}

async function pollBulkProgress() {
  try {
    const s = await api('/api/session');
    const p = s.progress;
    if (!p) return;
    const pct = p.total ? Math.round(p.done/p.total*100) : 0;
    g('bulk-progress').innerHTML = `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;">
        <div style="display:flex;justify-content:space-between;font-size:12px;color:#fff;margin-bottom:6px;">
          <span>${p.done} / ${p.total} posts ${p.brand?('· '+esc(p.brand)):''}</span>
          <span style="color:var(--muted);">${esc((p.current||'').slice(0,60))}</span>
        </div>
        <div style="height:7px;background:#0d1828;border-radius:4px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:var(--green);transition:width .3s;"></div>
        </div>
      </div>`;
  } catch(e){}
}

function showBulkResults(data) {
  S.results = data.results || [];
  const host = g('bulk-results');
  // group results by brand
  const byBrand = {};
  S.results.forEach((r,ri)=>{ (byBrand[r.brand] = byBrand[r.brand] || []).push(ri); });
  const dirs = (data.batch_dirs||[]).join(' · ');
  let html = `<div style="display:flex;align-items:center;gap:10px;margin:4px 18px 10px;">
      <h3 style="font-size:15px;color:#fff;">Bulk results</h3>
      <span class="badge badge-ok">${S.results.filter(r=>r.ok).length} ok</span>
      ${S.results.some(r=>!r.ok)?`<span class="badge badge-err">${S.results.filter(r=>!r.ok).length} failed</span>`:''}
      <span style="font-size:11px;color:var(--muted);">${esc(dirs)}</span>
    </div>`;
  Object.entries(byBrand).forEach(([bk, idxs])=>{
    html += `<div style="margin:0 18px 6px;font-size:13px;font-weight:700;color:var(--teal);">${esc((S.bulk[bk]&&S.bulk[bk].name)||bk)}</div>
      <div style="display:flex;flex-direction:column;gap:8px;padding:0 18px 14px;">
      ${idxs.map(ri=>resultCard(ri)).join('')}</div>`;
  });
  host.innerHTML = html;
}

// Shared result-card renderer (used by single-brand batch + bulk). ri indexes S.results.
// Pull the current Review-queue status for every item (keyed by output folder
// "rel", which is unique per render) so bulk/batch cards can show a live
// pending/approved/published/rejected indicator instead of just a used button.
async function refreshReviewIndex() {
  try {
    const data = await api('/api/review');   // no status filter = all items
    const idx = {};
    (data.items || []).forEach(it => { if (it.rel) idx[it.rel] = it.status; });
    S.reviewIndex = idx;
  } catch (e) { /* non-fatal — cards just render without a badge */ }
}

function reviewBadgeHtml(rel) {
  const st = rel && S.reviewIndex[rel];
  if (!st) return '';
  const icons = { pending: '📋 Pending Review', approved: '✅ Approved',
                 scheduled: '🗓 Scheduled', published: '📤 Published',
                 failed: '⚠ Failed', rejected: '✕ Rejected' };
  return `<span class="rv-status ${esc(st)}" style="margin-left:auto;">${icons[st] || esc(st)}</span>`;
}

function resultCard(ri) {
  const r = S.results[ri];
  if (!r.ok) return `
    <div class="story-card" style="border-color:${r.skipped?'var(--border)':'var(--red)'};cursor:default;">
      <div class="s-top"><span class="score-pill badge ${r.skipped?'badge-info':'badge-err'}">${esc(r.format)}${r.skipped?' skipped':' failed'}</span><span class="story-title">${esc(r.title)}</span></div>
      <div class="story-reason" style="color:${r.skipped?'var(--muted)':'var(--red)'};">${esc(r.error||'')}</div>
    </div>`;
  const isCarousel = (r.format==='carousel' || r.format==='listicle');
  return `
    <div class="story-card" style="cursor:default;" id="rc-${ri}">
      <div class="s-top">
        <span class="score-pill badge badge-info">${esc(S.formats[r.format]||r.format)}</span>
        <span class="story-title">${esc(r.title)}</span>
        <span id="rc-noimg-${ri}" class="rv-status rejected" title="Rendered with no background images — generated in 'No images (fast)' mode" style="${r.has_images === false ? '' : 'display:none;'}">⚠️ NO IMAGE</span>
        <span id="rc-badge-${ri}">${reviewBadgeHtml(r.rel)}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;" id="rc-imgs-${ri}">
        ${r.files.map(f=>`<a href="/outputs/${r.rel}/${f}?t=${Date.now()}" target="_blank"><img src="/outputs/${r.rel}/${f}?t=${Date.now()}" style="height:120px;border-radius:5px;border:1px solid var(--border);"></a>`).join('')}
      </div>
      <div class="story-reason" style="white-space:pre-wrap;">${esc(r.caption||'')}</div>
      <div class="story-actions" style="margin-top:8px;flex-wrap:wrap;">
        ${isCarousel ? `<span class="rv-status approved">Ready package</span>`
                     : `<button class="btn btn-green btn-sm" onclick="suggestForResult(${ri})">✨ Suggest images</button>`}
        <button class="btn btn-green btn-sm" onclick="sendResultToReview(${ri}, this)">✓ Send to Review</button>
        <a href="/outputs/${r.rel}/" target="_blank" class="btn btn-ghost btn-sm">Open folder ↗</a>
        <button class="btn btn-danger btn-sm" onclick="discardResult(${ri})" title="Delete this post — removes the rendered files and any Review-queue entry" style="margin-left:auto;">🗑 Discard</button>
      </div>
    </div>`;
}

// Discard a result you don't plan to use: deletes its rendered files + any
// Review-queue entry, and removes the card from view (no full re-render, so
// other cards / their onclick indices stay valid).
async function discardResult(ri) {
  const r = S.results[ri];
  if (!r || !r.rel) return;
  if (!confirm(`Discard "${r.title}"? This deletes the rendered files${S.reviewIndex[r.rel] ? ' and its Review-queue entry' : ''}.`)) return;
  try {
    await api('/api/outputs/' + r.rel.split('/').map(encodeURIComponent).join('/'), 'DELETE');
    document.getElementById('rc-' + ri)?.remove();
    delete S.reviewIndex[r.rel];
    toast('Discarded');
  } catch (e) { toast(e.message, 'err'); }
}

// Load a result's carousel/listicle plan into the editor.
async function editResultPlan(ri) {
  const r = S.results[ri];
  if (!r || !r.plan) { toast('No editable plan for this result','err'); return; }
  // Switch brand FIRST and wait — switchBrand clears S.plan + server session,
  // so loading the plan before it finishes would get wiped by the race.
  if (r.brand && r.brand !== curBrand()) {
    const sel = g('brand-sel'); if (sel) sel.value = r.brand;
    await switchBrand(r.brand);
  }
  loadPlan(r.plan);
  S.imagePaths = {};
  S.editingResultIndex = ri;   // so a later Render here can refresh THIS card's thumbnails/badge
  await api('/api/plan','PUT',r.plan).catch(()=>{});
  showTab('editor', document.querySelectorAll('.nav-btn')[2]);
  toast('Loaded into editor — tweak then Render');
}

// ✨ Suggest images for a single-card result → swap + re-render just that post.
async function suggestForResult(ri) {
  const r = S.results[ri];
  const q = r.image_query || r.title || '';
  const source = g('bulk-src')?.value || g('batch-src')?.value || 'pexels';
  openModal(`✨ Suggest images · ${source} · "${esc(q)}"`);
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Searching…</div>';
  try {
    const data = await api('/api/images/search','POST',{query:q, source, count:12});
    const results = data.results || [];
    if (!results.length) { body.innerHTML = '<div style="color:var(--muted);font-size:12px;">No results.</div>'; return; }
    body.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">${results.length} images. Click one to swap it in and re-render this post.</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
        ${results.map(rr=>`
          <div style="cursor:pointer;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:#0d1828;" onclick='pickForResult(${ri}, ${JSON.stringify(rr.url)}, ${JSON.stringify(source)})' title="${esc(rr.title||'')}">
            <img src="${esc(rr.thumb||rr.url)}" style="width:100%;height:120px;object-fit:cover;display:block;" loading="lazy" onerror="this.style.opacity=.3">
          </div>`).join('')}
      </div>`;
  } catch(e){ body.innerHTML = `<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function pickForResult(ri, url, source) {
  const r = S.results[ri];
  const body = g('modal-body');
  if (body) body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Swapping & re-rendering…</div>';
  try {
    const dl = await api('/api/images/from-url','POST',{ url, name:`${r.slug||'post'}`, filter: source==='google' });
    const re = await api('/api/bulk/rerender','POST',{
      plan: r.plan, format: r.format, brand: r.brand, image_paths: { 0: dl.path },
    });
    r.rel = re.rel; r.files = re.files;
    const imgs = g('rc-imgs-'+ri);
    if (imgs) imgs.innerHTML = r.files.map(f=>`<a href="/outputs/${r.rel}/${f}?t=${Date.now()}" target="_blank"><img src="/outputs/${r.rel}/${f}?t=${Date.now()}" style="height:120px;border-radius:5px;border:1px solid var(--border);"></a>`).join('');
    closeModal();
    toast('Image swapped & re-rendered');
  } catch(e){ toast(e.message,'err'); closeModal(); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Stories
// ═══════════════════════════════════════════════════════════════════════════
async function fetchStories() {
  const btn = document.getElementById('btn-fetch');
  const status = document.getElementById('stories-status');
  const model = document.getElementById('model-sel').value;
  const limit = document.getElementById('fetch-limit').value;
  const top   = document.getElementById('fetch-top').value;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Fetching…';
  status.innerHTML = '<span class="badge badge-info">Scoring with local AI…</span>';
  S.busy = 'fetch stories'; S.cancelRequested = false;
  const cancelBtn = g('btn-cancel'); if(cancelBtn) cancelBtn.style.display='inline-flex';
  const t0 = Date.now();
  const tid = setInterval(() => {
    document.getElementById('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s';
  }, 200);
  try {
    const fetchBrand = curBrand();   // the brand this fetch is for
    const data = await api(`/api/stories/fetch?limit=${limit}&top=${top}&category=${S.selectedCat}&model=${encodeURIComponent(model)}&brand=${encodeURIComponent(fetchBrand)}`, 'POST');
    if (curBrand() !== fetchBrand) {  // user switched brands mid-fetch — results were stashed server-side
      toast(`${fetchBrand} fetch done (saved) — you're on ${curBrand()} now`);
      return;                          // don't clobber the current brand's view
    }
    S.stories = data.stories;
    renderStoriesList(data.stories);
    const cancelNote = S.cancelRequested ? ' · cancelled' : '';
    const skipNote = data.excluded ? ` · ${data.excluded} already used` : '';
    status.innerHTML = `<span class="badge badge-ok">${data.stories.length} ranked · ${data.elapsed}s${skipNote}${cancelNote}</span>`;
    document.getElementById('hdr-timer').textContent = data.elapsed+'s';
    notify(S.cancelRequested ? 'Fetch cancelled' : 'Fetch & Score complete',
           `${data.stories.length} ${curBrand()||''} stories ranked in ${data.elapsed}s`);
  } catch(e) {
    status.innerHTML = `<span class="badge badge-err">${e.message}</span>`;
    toast(e.message, 'err');
  } finally {
    S.busy = null;
    clearInterval(tid);
    if(cancelBtn) cancelBtn.style.display='none';
    btn.disabled = false; btn.innerHTML = 'Fetch &amp; Score';
  }
}

async function resetUsed() {
  try {
    const r = await api('/api/session/reset','POST',{});
    toast(r.cleared ? `Reset — ${r.cleared} story(ies) can appear again` : 'Nothing to reset');
  } catch(e){ toast(e.message,'err'); }
}

function renderStoriesList(stories) {
  const list = document.getElementById('stories-list');
  if (!stories.length) { list.innerHTML = '<div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">No stories returned.</div>'; return; }
  S.selected = {};
  document.getElementById('batch-bar').style.display = 'flex';
  list.innerHTML = stories.map((s,i) => {
    const cls = s.score>=70?'sh':s.score>=40?'sm':'sl';
    const fmtChips = Object.entries(S.formats).map(([k,name]) =>
      `<button class="cat-btn" data-si="${i}" data-fk="${k}" onclick="toggleStoryFmt(${i},'${k}',this)" style="padding:2px 9px;font-size:11px;">${name}</button>`
    ).join('');
    return `<div class="story-card" id="sc-${i}">
      <div class="s-top">
        <input type="checkbox" class="story-chk" onchange="toggleStory(${i},this)" style="width:16px;height:16px;flex-shrink:0;">
        <span class="score-pill badge ${cls}">${s.score}/100</span>
        <span class="story-title">${esc(s.title)}</span>
      </div>
      <div class="story-reason">${esc(s.reason)}</div>
      <div class="story-actions" style="flex-wrap:wrap;align-items:center;">
        <button class="btn btn-primary btn-sm" onclick="useStor(${i})">Edit as Carousel</button>
        <a href="${esc(s.url)}" target="_blank" class="btn btn-ghost btn-sm">Source ↗</a>
        <span style="font-size:11px;color:var(--muted);margin-left:4px;">formats:</span>
        ${fmtChips}
      </div>
    </div>`;
  }).join('');
  updateSelCount();
}

// ── Batch selection ──────────────────────────────────────────────────────────
function ensureSel(i) {
  if (!S.selected[i]) S.selected[i] = { checked:false, formats:new Set([...S.bulkFormats]) };
  return S.selected[i];
}
function toggleStory(i, chk) {
  const sel = ensureSel(i);
  sel.checked = chk.checked;
  document.getElementById('sc-'+i)?.classList.toggle('selected', chk.checked);
  // reflect this story's formats on its chips
  document.querySelectorAll(`[data-si="${i}"]`).forEach(b => {
    b.classList.toggle('active', sel.formats.has(b.dataset.fk));
  });
  updateSelCount();
}
function toggleStoryFmt(i, key, btn) {
  const sel = ensureSel(i);
  if (sel.formats.has(key)) sel.formats.delete(key); else sel.formats.add(key);
  btn.classList.toggle('active');
}
function toggleAll(chk) {
  document.querySelectorAll('.story-chk').forEach((c,i) => { c.checked = chk.checked; toggleStory(i,c); });
}
function updateSelCount() {
  const n = Object.values(S.selected).filter(s=>s.checked).length;
  document.getElementById('sel-count').textContent = `${n} selected`;
}

async function runBatch() {
  const items = [];
  const total = parseInt(document.getElementById('slide-count-sel')?.value || '4');
  Object.entries(S.selected).forEach(([i,sel]) => {
    if (!sel.checked) return;
    const s = S.stories[i];
    const formats = sel.formats.size ? [...sel.formats] : [...S.bulkFormats];
    items.push({ story:{title:s.title,summary:s.summary,url:s.url,published:s.published||'',image:s.image||''}, formats, total_slides: total });
  });
  if (!items.length) { toast('Select at least one story','err'); return; }

  const totalPosts = items.reduce((a,it)=>a+it.formats.length,0);
  const btn = document.getElementById('btn-batch');
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Generating ${totalPosts}…`;
  S.busy = 'batch'; S.cancelRequested = false;
  const cancelBtn = g('btn-cancel-batch'); if(cancelBtn) cancelBtn.style.display='inline-flex';
  const t0 = Date.now();
  const tid = setInterval(()=>{ document.getElementById('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s'; },200);
  try {
    const model = document.getElementById('model-sel').value;
    const source = document.getElementById('batch-src').value;
    const tone = document.getElementById('tone-sel')?.value || '';
    const data = await api('/api/batch/run','POST',{items,model,source,tone,brand:curBrand()});
    document.getElementById('hdr-timer').textContent = data.elapsed+'s';
    await refreshReviewIndex();
    showBatchResults(data);
    const ok = data.results.filter(r=>r.ok).length;
    const note = data.cancelled ? ' (cancelled)' : '';
    toast(`Batch done${note}: ${ok}/${data.results.length} posts in ${data.elapsed}s`);
    notify(data.cancelled ? 'Batch cancelled' : 'Batch complete',
           `${ok}/${data.results.length} posts rendered in ${data.elapsed}s`);
  } catch(e) {
    toast(e.message,'err');
    notify('Batch failed', e.message);
  } finally {
    S.busy = null;
    clearInterval(tid);
    if(cancelBtn) cancelBtn.style.display='none';
    btn.disabled = false; btn.innerHTML = 'Generate All Selected';
  }
}

// Restore a saved batch view (per-brand) after a switch/refresh, if present.
async function restoreBatchView(batch) {
  if (batch && batch.length) {
    await refreshReviewIndex();
    showBatchResults({ results: batch, batch_dir: '' });
  }
}

function showBatchResults(data) {
  S.batch = data.results || [];
  S.results = S.batch;   // shared store so ✨ Suggest / re-render work by index
  const list = document.getElementById('stories-list');
  list.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
      <h3 style="font-size:15px;color:#fff;">Batch results</h3>
      <span class="badge badge-ok">${data.results.filter(r=>r.ok).length} ok</span>
      ${data.results.some(r=>!r.ok)?`<span class="badge badge-err">${data.results.filter(r=>!r.ok).length} failed</span>`:''}
      <span style="font-size:11px;color:var(--muted);">${data.batch_dir}</span>
      <div style="flex:1"></div>
      <button class="btn btn-ghost btn-sm" onclick="renderStoriesList(S.stories)">← Back to stories</button>
    </div>
    ${data.results.map((r,ri) => r.ok ? resultCard(ri) : `
      <div class="story-card" style="border-color:var(--red);">
        <div class="s-top"><span class="score-pill badge badge-err">${esc(r.format)} failed</span><span class="story-title">${esc(r.title)}</span></div>
        <div class="story-reason" style="color:var(--red);">${esc(r.error||'')}</div>
        <div class="story-actions" style="margin-top:8px;">
          <button class="btn btn-ghost btn-sm" onclick="retryBatchItem(${ri})">↻ Retry this one</button>
        </div>
      </div>`).join('')}
  `;
}

// Load a finished batch result's plan into the Editor for tweaking + re-render.
function editBatchPlan(ri) {
  const r = S.batch[ri];
  if (!r || !r.plan) { toast('No editable plan for this result', 'err'); return; }
  loadPlan(r.plan);
  S.imagePaths = {};
  api('/api/plan', 'PUT', r.plan).catch(()=>{});   // sync server session for preview/render
  showTab('editor', document.querySelectorAll('.nav-btn')[2]);
  toast('Loaded into editor — tweak then Render');
}

// Re-run a single failed batch item (most failures are flaky model JSON).
async function retryBatchItem(ri) {
  const r = S.batch[ri];
  if (!r || !r.story) { toast('Cannot retry this item', 'err'); return; }
  if (S.busy) { toast(`Wait — '${S.busy}' is still running`, 'err'); return; }
  S.busy = 'batch';
  toast(`Retrying ${r.format}…`);
  try {
    const model  = document.getElementById('model-sel').value;
    const source = document.getElementById('batch-src').value;
    const total  = parseInt(document.getElementById('slide-count-sel')?.value || '4');
    const tone   = document.getElementById('tone-sel')?.value || '';
    const data = await api('/api/batch/run','POST',{
      items:[{story:r.story, formats:[r.format], total_slides:total, tone}], model, source, brand:curBrand(),
    });
    const nr = (data.results||[])[0];
    if (nr) { S.batch[ri] = nr; showBatchResults({results:S.batch, batch_dir:data.batch_dir}); }
    toast(nr && nr.ok ? `${r.format} succeeded` : `${r.format} failed again`, nr && nr.ok ? 'ok':'err');
  } catch(e) { toast(e.message,'err'); }
  finally { S.busy = null; }
}

async function useStor(i) {
  const s = S.stories[i];
  document.querySelectorAll('.story-card').forEach(c=>c.classList.remove('selected'));
  document.getElementById('sc-'+i)?.classList.add('selected');
  const model  = document.getElementById('model-sel').value;
  const total  = parseInt(document.getElementById('slide-count-sel').value);
  const tone   = document.getElementById('tone-sel')?.value || '';
  toast('Generating plan…');
  S.busy = 'generate plan';
  const t0 = Date.now();
  const tid = setInterval(() => {
    document.getElementById('hdr-timer').textContent = ((Date.now()-t0)/1000).toFixed(1)+'s';
  }, 200);
  try {
    const planBrand = curBrand();   // the brand this plan is for
    const data = await api('/api/plan/generate', 'POST', {
      story: {title:s.title,summary:s.summary,url:s.url,published:s.published||'',image:s.image||''},
      total_slides: total,
      model,
      tone,
      brand: planBrand,
    });
    document.getElementById('hdr-timer').textContent = data.elapsed+'s';
    if (curBrand() !== planBrand) {   // switched brands mid-generate — plan stashed server-side
      toast(`${planBrand} plan ready (saved) — you're on ${curBrand()} now`);
      return;
    }
    loadPlan(data.plan);
    await finishPlanAutomatically(g('batch-src')?.value || 'pexels', true);
    toast(`Carousel ready (${data.elapsed}s)`);
    notify('Carousel ready', `${(data.plan.title_card&&data.plan.title_card.headline)||s.title.slice(0,60)} · ${data.elapsed}s`);
  } catch(e) {
    toast(e.message,'err');
  } finally {
    S.busy = null;
    clearInterval(tid);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Editor / Plan form
// ═══════════════════════════════════════════════════════════════════════════
function resetEditorView() {
  S.plan = null;
  S.imagePaths = {};
  S.slideIdx = 0;
  S.totalSlides = 0;
  S.lastRender = null;
  S.editingResultIndex = null;
  g('plan-form').innerHTML = '<div style="color:var(--muted);text-align:center;padding:28px 10px;font-size:12px;line-height:1.7;">No plan loaded. Create a new post or load one from Library.</div>';
  g('preview-wrap').innerHTML = '<div class="preview-placeholder">Load a plan and click Preview</div>';
  updateCnt();
}

async function clearEditorSession() {
  if (S.busy) { toast(`Wait - '${S.busy}' is still running`, 'err'); return; }
  if (!confirm('Clear the current Editor plan and its session images? Saved Library items and rendered outputs will stay.')) return;
  try {
    await api('/api/session/clear', 'POST', {scope:'editor'});
    resetEditorView();
    toast('Editor session cleared');
  } catch(e) { toast(e.message, 'err'); }
}

async function clearCreateSession() {
  if (S.busy) { toast(`Wait - '${S.busy}' is still running`, 'err'); return; }
  if (!confirm('Start fresh for this brand? This clears the current draft, fetched stories and generated-result session. Library items and rendered outputs will stay.')) return;
  try {
    await api('/api/session/clear', 'POST', {scope:'all'});
    resetEditorView();
    S.stories = []; S.batch = []; S.results = []; S.selected = {};
    S.manualImages = [];
    ['m-idea','m-notes','m-tone'].forEach(id => { if (g(id)) g(id).value = ''; });
    if (g('m-platform')) g('m-platform').value = 'instagram';
    if (g('m-slides')) g('m-slides').value = String(SETTINGS.slides || 4);
    if (g('m-angles')) g('m-angles').innerHTML = '';
    renderManualImages(); updateManualContext();
    if (g('batch-bar')) g('batch-bar').style.display = 'none';
    if (g('stories-list')) g('stories-list').innerHTML = '<div style="color:var(--muted);text-align:center;padding:40px;font-size:13px;">Session cleared. Fetch stories when you want to start again.</div>';
    toast('Fresh session ready');
  } catch(e) { toast(e.message, 'err'); }
}

function loadPlan(plan) {
  S.plan = plan;
  S.imagePaths = {};
  S.editingResultIndex = null;   // reset the batch-card link; editResultPlan() re-sets it after calling this
  S.slideIdx = 0;
  S.totalSlides = 1 + (plan.content_slides||[]).length + 1;
  if (g('slide-count-sel')) g('slide-count-sel').value = String(S.totalSlides);
  renderForm(plan);
  updateCnt();
  document.getElementById('preview-wrap').innerHTML = '<div class="preview-placeholder">Click Preview to render slide</div>';
}

function renderForm(plan) {
  const slides = plan.content_slides || [];
  document.getElementById('plan-form').innerHTML = `
    <div class="slide-sec">
      <div class="slide-sec-title">Title Card</div>
      <div class="field-group"><label>Headline</label><input id="f-hl" value="${esc(plan.title_card?.headline||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Subhead</label><input id="f-sh" value="${esc(plan.title_card?.subhead||'')}" oninput="syncPlan()"></div>
    </div>
    ${slides.map((sl,i)=>`
    <div class="slide-sec">
      <div class="slide-sec-title">Content ${i+1}</div>
      <div class="field-group"><label>Heading (ALL CAPS)</label><input id="f-sh-${i}" value="${esc(sl.heading||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Body</label><textarea id="f-bd-${i}" rows="4" oninput="syncPlan()">${esc(sl.body||'')}</textarea></div>
      <div class="field-group"><label>Image Query</label>
        <div class="img-row">
          <div class="img-thumb empty" id="thumb-${i}">🖼</div>
          <input class="url-inp" id="f-iq-${i}" value="${esc(sl.image_query||'')}" placeholder="search images…" style="flex:1">
          <button class="btn btn-ghost btn-sm" onclick="searchImagesFor(${i})" title="Search and pick from a grid">🔍 Search</button>
          <button class="btn btn-ghost btn-sm" onclick="swapImage(${i})" title="Auto-use the top result">Swap</button>
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('f-up-${i}').click()" title="Upload an image from your computer">⬆ Upload</button>
          <button class="btn btn-ghost btn-sm" onclick="pasteImage(${i})" title="Paste an image copied to your clipboard">📋 Paste</button>
          <button class="btn btn-ghost btn-sm" onclick="removeImage(${i})" title="Remove image (blank background)">✕</button>
          <input type="file" id="f-up-${i}" accept="image/png,image/jpeg,image/webp" style="display:none" onchange="uploadImage(${i}, this)">
        </div>
      </div>
      <div class="field-group"><label>Image from URL</label>
        <div class="url-row">
          <input class="url-inp" id="f-url-${i}" placeholder="https://…">
          <button class="btn btn-ghost btn-sm" onclick="imgFromUrl(${i})">Use</button>
        </div>
      </div>
    </div>`).join('')}
    <div class="slide-sec">
      <div class="slide-sec-title">Outro / CTA</div>
      <div class="field-group"><label>CTA Text</label><input id="f-cta" value="${esc(plan.outro_card?.cta||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Handle</label><input id="f-hdl" value="${esc(plan.outro_card?.handle||'')}" oninput="syncPlan()"></div>
    </div>

    <div class="slide-sec">
      <div class="slide-sec-title">Layout & Brand Furniture</div>
      <div class="field-group"><label>Bottom "brand badge" position</label>
        <select id="f-badge" onchange="syncLayout()">
          <option value="center">Center</option>
          <option value="left">Bottom-left</option>
          <option value="right">Bottom-right</option>
        </select>
      </div>
      <div class="field-group"><label>Background logo position</label>
        <select id="f-wmpos" onchange="syncLayout()">
          <option value="center">Center</option>
          <option value="top">Top</option>
          <option value="bottom">Bottom</option>
          <option value="left">Left</option>
          <option value="right">Right</option>
          <option value="top-left">Top-left</option>
          <option value="top-right">Top-right</option>
          <option value="bottom-left">Bottom-left</option>
          <option value="bottom-right">Bottom-right</option>
        </select>
      </div>
      <div class="field-group"><label>Background logo visibility: <span id="wmop-val">5</span>%</label>
        <input type="range" id="f-wmop" min="0" max="20" value="5" oninput="document.getElementById('wmop-val').textContent=this.value;syncLayout()">
      </div>
    </div>

    <div class="slide-sec">
      <div class="slide-sec-title">Caption & Hashtags</div>
      <div class="field-group">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
          <label style="margin:0;">Caption</label>
          <div style="display:flex;gap:6px;">
            <button class="btn btn-ghost btn-sm" style="padding:2px 8px;font-size:11px;" onclick="copyField('f-cap','Caption')" title="Copy caption">📋 Copy</button>
            <button class="btn btn-ghost btn-sm" style="padding:2px 8px;font-size:11px;" onclick="copyCaptionAndTags()" title="Copy caption + hashtags together">📋 Caption + tags</button>
          </div>
        </div>
        <textarea id="f-cap" rows="3" oninput="syncPlan()">${esc(plan.caption||'')}</textarea>
      </div>
      <div class="field-group">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
          <label style="margin:0;">Hashtags (comma-separated)</label>
          <button class="btn btn-ghost btn-sm" style="padding:2px 8px;font-size:11px;" onclick="copyField('f-tags','Hashtags','#')" title="Copy hashtags">📋 Copy</button>
        </div>
        <input id="f-tags" value="${esc((plan.hashtags||[]).join(', '))}" oninput="syncPlan()">
      </div>
      <div class="field-group"><label>DM Keyword</label><input id="f-dm" value="${esc(plan.dm_keyword||'')}" oninput="syncPlan()"></div>
      <div class="field-group"><label>Tone / stance (optional)</label>
        <input id="f-tone" list="tone-presets" value="${esc(plan.tone||'')}" placeholder="e.g. positive, negative, hyped, skeptical…" oninput="syncPlan()">
        <datalist id="tone-presets">
          <option value="positive, upbeat"></option>
          <option value="negative, critical"></option>
          <option value="neutral, factual"></option>
          <option value="hyped, exciting"></option>
          <option value="analytical, measured"></option>
          <option value="skeptical, cautionary"></option>
        </datalist>
        <div style="font-size:11px;color:var(--muted);margin-top:3px;">Saved with the post. Applies on the next ✨ caption regen; for new copy, re-generate from Stories with this tone.</div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="regenCaption()" id="btn-regen-cap" style="align-self:flex-start;">✨ Generate caption + hashtags</button>
    </div>
  `;
  // reflect saved layout into the controls
  const L = plan.layout || {};
  if (g('f-badge')) g('f-badge').value = L.badge_align || 'center';
  if (g('f-wmpos')) g('f-wmpos').value = L.wm_pos || 'center';
  if (g('f-wmop'))  { g('f-wmop').value = (L.wm_opacity ?? 5); g('wmop-val').textContent = (L.wm_opacity ?? 5); }
}

function syncLayout() {
  if (!S.plan) return;
  S.plan.layout = {
    badge_align: g('f-badge')?.value || 'center',
    wm_pos:      g('f-wmpos')?.value || 'center',
    wm_opacity:  parseInt(g('f-wmop')?.value ?? '5'),
  };
  api('/api/plan','PUT',S.plan).catch(()=>{});
  previewCurrent();   // live-reflect the layout change
}

async function regenCaption() {
  if (!S.plan) { toast('Load a plan first','err'); return; }
  syncPlan();
  const btn = g('btn-regen-cap');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Generating…';
  try {
    const data = await api('/api/plan/caption','POST',{tone:g('f-tone')?.value||'',brand:curBrand()});
    S.plan.caption = data.caption; S.plan.hashtags = data.hashtags;
    if (g('f-cap'))  g('f-cap').value  = data.caption || '';
    if (g('f-tags')) g('f-tags').value = (data.hashtags||[]).join(', ');
    api('/api/plan','PUT',S.plan).catch(()=>{});
    toast('Caption + hashtags regenerated');
    notify('Caption ready', 'New caption + hashtags generated');
  } catch(e){ toast(e.message,'err'); }
  finally { btn.disabled=false; btn.innerHTML='✨ Generate caption + hashtags'; }
}

function syncPlan() {
  if (!S.plan) return;
  const slides = S.plan.content_slides || [];
  S.plan.title_card = {
    headline: g('f-hl')?.value||'',
    subhead:  g('f-sh')?.value||'',
  };
  slides.forEach((_,i) => {
    slides[i] = {
      ...slides[i],
      heading:     g(`f-sh-${i}`)?.value||'',
      body:        g(`f-bd-${i}`)?.value||'',
      image_query: g(`f-iq-${i}`)?.value||'',
    };
  });
  S.plan.outro_card = { cta: g('f-cta')?.value||'', handle: g('f-hdl')?.value||'' };
  S.plan.caption    = g('f-cap')?.value||'';
  S.plan.hashtags   = (g('f-tags')?.value||'').split(',').map(h=>h.trim().replace(/^#/,'')).filter(Boolean);
  S.plan.dm_keyword = g('f-dm')?.value||'';
  if (g('f-tone')) S.plan.tone = g('f-tone').value||'';
  api('/api/plan','PUT',S.plan).catch(()=>{});
}

async function loadDummy() {
  try {
    const data = await api('/api/plan/dummy', 'POST');
    loadPlan(data.plan);
    toast('Preview plan loaded');
  } catch(e) { toast(e.message, 'err'); }
}

function updateCnt() {
  document.getElementById('slide-cnt').textContent =
    S.totalSlides ? `${S.slideIdx+1}/${S.totalSlides}` : '—/—';
}
function prevSlide() { if(S.slideIdx>0){S.slideIdx--;updateCnt();previewCurrent();} }
function nextSlide() { if(S.slideIdx<S.totalSlides-1){S.slideIdx++;updateCnt();previewCurrent();} }

async function previewCurrent() {
  if (!S.plan) { toast('Load a plan first','err'); return; }
  syncPlan();
  const btn = g('btn-prev-slide');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const wrap = g('preview-wrap');
  wrap.innerHTML='<div class="preview-placeholder"><span class="spin"></span> Rendering…</div>';
  try {
    const blob = await fetch(`/api/preview/${S.slideIdx}?brand=${encodeURIComponent(curBrand())}`).then(r=>{if(!r.ok)throw new Error('render failed');return r.blob();});
    const url  = URL.createObjectURL(blob);
    wrap.innerHTML = `<div class="preview-image-shell">
      <img src="${url}" alt="slide ${S.slideIdx}">
      <button class="btn btn-ghost btn-sm" onclick="copyImageUrl('${url}','Slide')" title="Copy this slide image to clipboard"
              style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.65);">📋 Copy</button>
    </div>`;
  } catch(e) {
    wrap.innerHTML=`<div class="preview-placeholder" style="color:var(--red);">${e.message}</div>`;
    toast(e.message,'err');
  } finally {
    btn.disabled=false; btn.innerHTML='Preview';
  }
}

async function fetchImages() {
  syncPlan();
  const source = g('img-source').value;
  const btn = g('btn-fetch-img');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const t0 = Date.now();
  try {
    const data = await api('/api/images/fetch','POST',{source});
    S.imagePaths = data.image_paths;
    Object.entries(data.image_paths).forEach(([i,p])=>{
      if(p) setThumb(parseInt(i),'/image_cache/'+p.split(/[/\\]/).pop());
    });
    toast(`Images fetched (${data.elapsed}s)`);
    notify('Images fetched', `Background images ready in ${data.elapsed}s`);
  } catch(e) { toast(e.message,'err'); }
  finally { btn.disabled=false; btn.innerHTML='Fetch Images'; }
}

async function clearImageCache() {
  if (!confirm('Delete ALL cached background images? (Plans are kept; you can re-fetch images.)')) return;
  try {
    const d = await api('/api/images/cache/clear','POST');
    S.imagePaths = {};
    // blank out any thumbnails still shown in the form
    document.querySelectorAll('.img-thumb').forEach(el=>{
      const id = el.id; if(id){ el.outerHTML = `<div class="img-thumb empty" id="${id}">🖼</div>`; }
    });
    toast(`Cleared ${d.removed} cached images (${d.freed_mb} MB freed)`);
  } catch(e){ toast(e.message,'err'); }
}

async function swapImage(idx) {
  const q = g(`f-iq-${idx}`)?.value||'';
  if(!q){toast('Enter a search query','err');return;}
  const source = g('img-source').value;
  try {
    const data = await api(`/api/images/swap/${idx}`,'POST',{query:q,source});
    S.imagePaths[idx] = data.path;
    setThumb(idx,'/image_cache/'+data.filename);
    toast(`Slide ${idx+1} image updated`);
    showSlidePreview(idx+1);
  } catch(e){toast(e.message,'err');}
}

async function removeImage(idx) {
  try {
    await api(`/api/images/${idx}`, 'DELETE');
    delete S.imagePaths[idx];
    delete S.imagePaths[String(idx)];
    const el = g(`thumb-${idx}`);
    if (el) el.outerHTML = `<div class="img-thumb empty" id="thumb-${idx}">🖼</div>`;
    toast(`Slide ${idx+1} image removed`);
    if (S.slideIdx === idx+1) showSlidePreview(idx+1);   // refresh preview if open
  } catch(e){ toast(e.message,'err'); }
}

// Shared: POST an image blob to a slide and reflect it in the thumb + preview.
async function _setSlideImageFromBlob(idx, blob, filename) {
  const fd = new FormData();
  fd.append('file', blob, filename);
  const r = await fetch(`/api/images/upload/${idx}`, {method:'POST', body:fd})
    .then(r => { if(!r.ok) return r.json().then(j=>{throw new Error(j.detail||'upload failed')}); return r.json(); });
  S.imagePaths[idx] = r.path;
  setThumb(idx, '/image_cache/' + r.filename);
  showSlidePreview(idx+1);
  return r;
}

async function uploadImage(idx, input) {
  const f = input.files && input.files[0];
  if (!f) return;
  try { await _setSlideImageFromBlob(idx, f, f.name); toast(`Slide ${idx+1} image uploaded`); }
  catch(e){ toast(e.message,'err'); }
  finally { input.value = ''; }   // allow re-uploading the same file
}

// Paste an image from the clipboard into a slide (per-slide 📋 Paste button).
async function pasteImage(idx) {
  if (!navigator.clipboard || !navigator.clipboard.read) {
    toast('Clipboard paste needs a Chromium browser on localhost — use ⬆ Upload or Ctrl+V', 'err');
    return;
  }
  try {
    const items = await navigator.clipboard.read();
    for (const it of items) {
      const type = it.types.find(t => t.startsWith('image/'));
      if (type) {
        const blob = await it.getType(type);
        const ext  = (type.split('/')[1] || 'png').replace('jpeg', 'jpg');
        await _setSlideImageFromBlob(idx, blob, `paste-${idx}.${ext}`);
        toast(`Slide ${idx+1} image pasted`);
        return;
      }
    }
    toast('No image found in clipboard', 'err');
  } catch(e){ toast('Clipboard blocked: ' + e.message, 'err'); }
}

// Ctrl+V anywhere in the Editor pastes the clipboard image onto the slide
// currently shown in the preview (use ‹ › to pick the slide first).
document.addEventListener('paste', (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  const createActive = document.getElementById('tab-create')?.classList.contains('active');
  const editorActive = document.getElementById('tab-editor')?.classList.contains('active');
  if (!createActive && !editorActive) return;
  for (const it of items) {
    if (it.type && it.type.startsWith('image/')) {
      const blob = it.getAsFile();
      if (createActive) {                         // add to the Create image pool
        e.preventDefault();
        S.manualImages = S.manualImages || [];
        S.manualImages.push(blob); renderManualImages();
        toast('Image added'); return;
      }
      const n  = (S.plan && S.plan.content_slides ? S.plan.content_slides.length : 0);
      const ci = S.slideIdx - 1;                  // content-slide index of the previewed slide
      if (ci < 0 || ci >= n) { toast('Pick a content slide (use ‹ ›), then paste', 'err'); return; }
      e.preventDefault();
      const ext = (it.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
      _setSlideImageFromBlob(ci, blob, `paste-${ci}.${ext}`)
        .then(() => toast(`Slide ${ci+1} image pasted`))
        .catch(err => toast(err.message, 'err'));
      return;
    }
  }
});

// ═══ Create (Manual) ═══════════════════════════════════════════════════════
const MANUAL_PLATFORM_HINTS = {
  instagram: 'short, visual, mobile-friendly slides with a save, share, follow or DM CTA',
  linkedin: 'professional, insight-led copy with business relevance and a restrained CTA',
  facebook: 'conversational, context-rich copy designed to invite a natural discussion',
  x: 'very concise, direct copy with a caption under 240 characters',
};
const MANUAL_TYPE_HINTS = {
  educational: 'practical steps, principles or mistakes the reader can act on',
  thought_leadership: 'a clear point of view, supporting reasoning and a useful implication',
  promotional: 'audience benefits, the problem solved and one conversion-focused CTA',
  announcement: 'what is new, who it affects, why it matters and what happens next',
  case_study: 'the problem, approach and real result or lesson without invented metrics',
  storytelling: 'a grounded hook, turning point and useful lesson',
};
function updateManualContentTypes(brand){
  const sel = g('m-content-type');
  if(!sel) return;
  const types = Array.isArray(brand?.content_types) ? brand.content_types : [];
  const previous = sel.value;
  sel.innerHTML = types.length
    ? types.map(t => `<option value="${esc(t.value)}">${esc(t.label)}</option>`).join('')
    : '<option value="general">General post</option>';
  if(types.some(t => t.value === previous)) sel.value = previous;
  updateManualContext();
}
function updateManualContext(){
  const platform = g('m-platform')?.value || 'instagram';
  const type = g('m-content-type')?.value || 'general';
  const platformName = g('m-platform')?.selectedOptions?.[0]?.textContent || 'Instagram';
  const typeName = g('m-content-type')?.selectedOptions?.[0]?.textContent || 'General post';
  const typeSpec = (S.brandInfo?.content_types || []).find(t => t.value === type);
  const typeHint = typeSpec?.description || MANUAL_TYPE_HINTS[type] || 'use the clearest framing for the supplied material';
  const hint = g('m-context-hint');
  if(hint) hint.textContent = `${platformName} · ${typeName}: ${typeHint} ${MANUAL_PLATFORM_HINTS[platform]}.`;
}

function manualAddImages(input){
  S.manualImages = S.manualImages || [];
  for (const f of input.files) if (f.type.startsWith('image/')) S.manualImages.push(f);
  input.value=''; renderManualImages();
}
function manualRemoveImage(i){ (S.manualImages||[]).splice(i,1); renderManualImages(); }
function renderManualImages(){
  const pool = g('m-img-pool'); if(!pool) return;
  S.manualImages = S.manualImages || [];
  if(!S.manualImages.length){
    pool.innerHTML = '<span style="font-size:11px;color:var(--muted);">No images yet — the AI will suggest stock images per slide. Add or paste your own to override.</span>';
    return;
  }
  pool.innerHTML = S.manualImages.map((f,i)=>`
    <div style="position:relative;">
      <img src="${URL.createObjectURL(f)}" style="width:58px;height:58px;object-fit:cover;border-radius:6px;border:1px solid var(--border);display:block;">
      <button onclick="manualRemoveImage(${i})" title="Remove" style="position:absolute;top:-7px;right:-7px;background:var(--red);color:#fff;border:none;border-radius:50%;width:18px;height:18px;font-size:12px;cursor:pointer;line-height:1;">×</button>
      <span style="position:absolute;bottom:0;left:0;right:0;text-align:center;font-size:9px;color:#fff;background:rgba(0,0,0,.5);">slide ${i+1}</span>
    </div>`).join('');
}

async function manualSuggest(){
  const idea = (g('m-idea')?.value||'').trim();
  if(!idea){ toast('Type a rough idea first','err'); g('m-idea')?.focus(); return; }
  const btn = g('m-suggest-btn'); const lbl = btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span> Thinking…';
  try {
    const data = await api('/api/manual/suggest','POST',{
      idea,
      tone:g('m-tone')?.value||'',
      platform:g('m-platform')?.value||'instagram',
      content_type:g('m-content-type')?.value||'general',
      brand:curBrand(),
    });
    const angles = data.angles || [];
    const box = g('m-angles');
    box.innerHTML = angles.length
      ? '<span style="font-size:11px;color:var(--muted);width:100%;">Tap one to use it:</span>' + angles.map(a=>
          `<button class="btn btn-ghost btn-sm" style="font-size:11px;" onclick="g('m-idea').value=${JSON.stringify(a)};">${esc(a)}</button>`).join('')
      : '<span style="font-size:11px;color:var(--muted);">No suggestions — try a more specific idea.</span>';
  } catch(e){ toast(e.message,'err'); }
  finally { btn.disabled=false; btn.innerHTML=lbl; }
}

async function manualGenerate(){
  const idea = (g('m-idea')?.value||'').trim();
  if(!idea){ toast('Enter an idea or topic','err'); g('m-idea')?.focus(); return; }
  const notes  = (g('m-notes')?.value||'').trim();
  const slides = parseInt(g('m-slides')?.value||'4');
  const tone   = (g('m-tone')?.value||'').trim();
  const platform = g('m-platform')?.value||'instagram';
  const contentType = g('m-content-type')?.value||'general';
  const btn = g('m-gen'); const lbl = btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span> Writing post…';
  const t0=Date.now(); const tid=setInterval(()=>{
    const elapsed = (Date.now()-t0)/1000;
    const h=g('hdr-timer'); if(h) h.textContent=elapsed.toFixed(1)+'s';
    if(elapsed > 45) btn.innerHTML='<span class="spin"></span> Still writing — local AI can take a minute…';
  },200);
  try {
    const data = await api('/api/plan/generate','POST',{
      story:{title:idea, summary: notes||idea, url:'', published:'', image:''},
      total_slides: slides, model: g('model-sel')?.value||'', tone,
      platform, content_type:contentType, manual:true, brand: curBrand(),
    });
    clearInterval(tid); const h=g('hdr-timer'); if(h) h.textContent=data.elapsed+'s';
    loadPlan(data.plan);
    btn.innerHTML='<span class="spin"></span> Finding images…';
    try {
      const fetched = await api('/api/images/fetch','POST',{source:'pexels'});
      S.imagePaths = fetched.image_paths || {};
    } catch (imageError) { /* render still works with uploaded or empty backgrounds */ }
    // assign pooled images to the content slides, in order
    const imgs = S.manualImages || [];
    for(let i=0;i<imgs.length;i++){
      try { await _setSlideImageFromBlob(i, imgs[i], imgs[i].name||`manual-${i}.png`); } catch(err){}
    }
    S.manualImages = []; renderManualImages();
    btn.innerHTML='<span class="spin"></span> Rendering package…';
    await finishPlanAutomatically('pexels', false);
    toast(`Post package ready (${data.elapsed}s)`);
    notify('Post generated', (data.plan.title_card&&data.plan.title_card.headline)||idea.slice(0,60));
  } catch(e){ clearInterval(tid); toast(e.message,'err'); }
  finally { btn.disabled=false; btn.innerHTML=lbl; }
}

async function finishPlanAutomatically(source='pexels', fetchBackgrounds=true) {
  if (!S.plan) throw new Error('No generated plan is available.');
  if (fetchBackgrounds) {
    try {
      const images = await api('/api/images/fetch','POST',{source});
      S.imagePaths = images.image_paths || {};
    } catch (e) { /* rendering without a background is still a valid fallback */ }
  }
  const rendered = await api('/api/render','POST',{brand:curBrand()});
  S.lastRender = {rel:rendered.rel, files:rendered.files};
  const p = S.plan;
  await api('/api/review/enqueue','POST',{
    brand:curBrand(),
    title:(p.title_card && p.title_card.headline) || p.slug || 'Untitled',
    format:p.format || 'carousel',
    rel:rendered.rel,
    files:rendered.files,
    caption:p.caption || '',
  });
  await loadDashboard();
  showTab('dashboard');
  return rendered;
}

// Copy a field's text to the clipboard. mode '#' formats CSV tags as "#a #b".
async function copyField(id, label, mode) {
  const el = g(id); if (!el) return;
  let text = el.value || '';
  if (mode === '#') {
    text = text.split(',').map(t=>t.trim()).filter(Boolean)
               .map(t=> t.startsWith('#') ? t : '#'+t).join(' ');
  }
  await _copyText(text, label);
}

function copyCaptionAndTags() {
  const cap  = g('f-cap')?.value || '';
  const tags = (g('f-tags')?.value || '').split(',').map(t=>t.trim()).filter(Boolean)
                 .map(t=> t.startsWith('#') ? t : '#'+t).join(' ');
  _copyText(cap + (tags ? '\n\n' + tags : ''), 'Caption + hashtags');
}

// Copy a rendered slide/screenshot (PNG) to the clipboard as an image.
async function copyImageUrl(url, label) {
  try {
    const blob = await fetch(url).then(r => { if(!r.ok) throw new Error('could not load image'); return r.blob(); });
    if (!navigator.clipboard || !window.ClipboardItem) throw new Error('image copy needs a Chromium browser');
    await navigator.clipboard.write([ new ClipboardItem({ 'image/png': blob }) ]);
    toast((label || 'Image') + ' copied');
  } catch(e) { toast('Copy image failed: ' + e.message, 'err'); }
}

async function _copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast((label||'Text') + ' copied');
  } catch(e) {
    // fallback for browsers/contexts that block the async clipboard API
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast((label||'Text') + ' copied'); }
    catch(_) { toast('Copy failed', 'err'); }
    document.body.removeChild(ta);
  }
}

async function imgFromUrl(idx) {
  const url = g(`f-url-${idx}`)?.value||'';
  if(!url){toast('Enter an image URL','err');return;}
  try {
    const data = await api('/api/images/from-url','POST',{url,slide_idx:idx,name:`slide${idx+1}`});
    S.imagePaths[idx] = data.path;
    setThumb(idx,'/image_cache/'+data.filename);
    toast(`Slide ${idx+1} image set from URL`);
    showSlidePreview(idx+1);
  } catch(e){toast(e.message,'err');}
}

// Search a source (Pexels/Unsplash/Google) and let the user pick from a grid.
// Open the picker for a slide. The query + source can be changed inside the modal
// so you can search Pexels / Unsplash / Google without closing it.
function searchImagesFor(idx) {
  const q = g(`f-iq-${idx}`)?.value || '';
  const source = g('img-source')?.value || 'pexels';
  openModal(`Pick an image · slide ${idx + 1}`);
  const body = g('modal-body');
  const opt = (v, l) => `<option value="${v}" ${v === source ? 'selected' : ''}>${l}</option>`;
  body.innerHTML = `
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
      <input id="picker-q" value="${esc(q)}" placeholder="Search images…"
        style="flex:1 1 200px;min-width:0;background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:8px 10px;font-size:13px;"
        onkeydown="if(event.key==='Enter')runImageSearch(${idx})">
      <select id="picker-source" class="src-sel" style="min-width:118px;max-width:none;" onchange="runImageSearch(${idx})">
        ${opt('pexels','Pexels')}${opt('unsplash','Unsplash')}${opt('google','Google')}
      </select>
      <button class="btn btn-primary btn-sm" onclick="runImageSearch(${idx})">🔍 Search</button>
    </div>
    <div id="picker-results"></div>`;
  runImageSearch(idx);
}

async function runImageSearch(idx) {
  const q = (g('picker-q')?.value || '').trim();
  const source = g('picker-source')?.value || 'pexels';
  if (g('img-source')) g('img-source').value = source;   // keep the toolbar source in sync
  const out = g('picker-results');
  if (!out) return;
  if (!q) { out.innerHTML = '<div style="color:var(--muted);font-size:12px;">Type a query and press Search.</div>'; return; }
  out.innerHTML = `<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Searching ${esc(source)}…</div>`;
  try {
    const data = await api('/api/images/search','POST',{query:q, source, count:12});
    const results = data.results || [];
    if (!results.length) {
      out.innerHTML = `<div style="color:var(--muted);font-size:12px;">No results${source==='google'?' — check GOOGLE_API_KEY / GOOGLE_CSE_ID in .env':''}.</div>`;
      return;
    }
    out.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">
        ${results.length} from ${esc(source)}${source==='google'?' · web images get a slight filter baked in':''}. Click one for slide ${idx+1}.</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
        ${results.map(r=>`
          <div style="cursor:pointer;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:#0d1828;" onclick='pickImage(${idx}, ${JSON.stringify(r.url)}, ${JSON.stringify(source)})' title="${esc(r.title||'')}">
            <img src="${esc(r.thumb||r.url)}" style="width:100%;height:120px;object-fit:cover;display:block;" loading="lazy" onerror="this.style.opacity=.3">
          </div>`).join('')}
      </div>`;
  } catch(e){ out.innerHTML = `<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function pickImage(idx, url, source) {
  const body = g('modal-body');
  if (body) body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Downloading…</div>';
  try {
    // Web (Google) picks get the bake-in filter; curated stock stays as-is.
    const data = await api('/api/images/from-url','POST',{
      url, slide_idx:idx, name:`slide${idx+1}`, filter: source==='google',
    });
    S.imagePaths[idx] = data.path;
    setThumb(idx,'/image_cache/'+data.filename);
    closeModal();
    toast(`Slide ${idx+1} image set`);
    showSlidePreview(idx+1);
  } catch(e){ toast(e.message,'err'); closeModal(); }
}

// Jump the preview pane to a content slide and re-render it so image changes show
// immediately (content slide N lives at overall index N).
function showSlidePreview(slideIdx) {
  if (slideIdx < 0 || slideIdx >= S.totalSlides) return;
  S.slideIdx = slideIdx;
  updateCnt();
  previewCurrent();
}

function setThumb(idx,url) {
  const el = g(`thumb-${idx}`);
  if(el) el.outerHTML=`<img class="img-thumb" id="thumb-${idx}" src="${url}?t=${Date.now()}" alt="">`;
}

async function renderFull() {
  syncPlan();
  const btn=g('btn-render');
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> Rendering…';
  const t0=Date.now();
  const tid=setInterval(()=>{document.getElementById('hdr-timer').textContent=((Date.now()-t0)/1000).toFixed(1)+'s';},200);
  try {
    const data = await api('/api/render','POST',{brand:curBrand()});
    clearInterval(tid);
    document.getElementById('hdr-timer').textContent=data.elapsed+'s';
    toast(`✓ Rendered ${data.files.length} slides in ${data.elapsed}s`);
    notify('Carousel rendered', `${data.files.length} slides saved in ${data.elapsed}s`);
    S.lastRender = {rel:data.rel, files:data.files};
    // If this render came from "Edit in Editor" on a Bulk/Batch result card, sync
    // that card's thumbnails + "NO IMAGE" badge in place — otherwise they'd keep
    // showing the stale pre-edit state (wrong files AND a badge that never clears).
    if (S.editingResultIndex != null && S.results && S.results[S.editingResultIndex]) {
      const idx = S.editingResultIndex;
      const rr = S.results[idx];
      rr.rel = data.rel; rr.files = data.files;
      rr.has_images = Object.values(S.imagePaths || {}).some(Boolean);
      const imgsEl = g('rc-imgs-'+idx);
      if (imgsEl) imgsEl.innerHTML = rr.files.map(f=>`<a href="/outputs/${rr.rel}/${f}?t=${Date.now()}" target="_blank"><img src="/outputs/${rr.rel}/${f}?t=${Date.now()}" style="height:120px;border-radius:5px;border:1px solid var(--border);"></a>`).join('');
      const noImgEl = g('rc-noimg-'+idx);
      if (noImgEl) noImgEl.style.display = rr.has_images ? 'none' : '';
    }
    g('preview-wrap').innerHTML=`
      <div style="text-align:center;padding:16px;line-height:1.8;">
        <div style="font-size:16px;font-weight:700;color:var(--green);margin-bottom:6px;">Carousel saved!</div>
        <div style="font-size:12px;color:var(--muted);">${data.output_dir}</div>
        <div style="margin-top:12px;display:flex;flex-direction:column;gap:5px;align-items:center;">
          ${data.files.map(f=>`<div style="display:flex;align-items:center;gap:8px;">
            <a href="/outputs/${data.rel}/${f}" target="_blank" style="color:var(--teal);font-size:11px;">${f}</a>
            <button class="btn btn-ghost btn-sm" style="font-size:10px;padding:1px 7px;" onclick="copyImageUrl('/outputs/${data.rel}/${f}','${f}')" title="Copy this slide image">📋</button>
          </div>`).join('')}
        </div>
        <div style="margin-top:14px;">
          <button class="btn btn-green btn-sm" onclick="sendEditorToReview(this)">✓ Send to Review</button>
        </div>
      </div>`;
  } catch(e){
    clearInterval(tid);
    toast(e.message,'err');
    notify('Render failed', e.message);
  } finally {
    btn.disabled=false; btn.innerHTML='Render Carousel';
  }
}

// Push the just-rendered Editor carousel into the Review queue (→ approve → publish).
async function sendEditorToReview(btn) {
  if (!S.plan || !S.lastRender) { toast('Render a carousel first','err'); return; }
  if (btn) { btn.disabled=true; btn.innerHTML='<span class="spin"></span>'; }
  const p = S.plan;
  try {
    await api('/api/review/enqueue','POST',{
      brand:   curBrand(),
      title:   (p.title_card && p.title_card.headline) || p.slug || 'Untitled',
      format:  p.format || 'carousel',
      rel:     S.lastRender.rel,
      files:   S.lastRender.files,
      caption: p.caption || '',
    });
    toast('Sent to Review ✓');
    if (btn) { btn.disabled=true; btn.innerHTML='✓ Sent to Review'; }
    // Reflect on the originating Bulk/Batch card, if this editor session came from one.
    if (S.editingResultIndex != null) {
      if (!S.reviewIndex) S.reviewIndex = {};
      S.reviewIndex[S.lastRender.rel] = 'pending';
      const badge = g('rc-badge-' + S.editingResultIndex);
      if (badge) badge.innerHTML = reviewBadgeHtml(S.lastRender.rel);
    }
  } catch(e) {
    toast(e.message,'err');
    if (btn) { btn.disabled=false; btn.innerHTML='✓ Send to Review'; }
  }
}

// Push a Bulk/Batch result card into the Review queue.
async function sendResultToReview(ri, btn) {
  const r = S.results[ri];
  if (!r || !r.ok) { toast('Nothing to send','err'); return; }
  if (btn) { btn.disabled=true; btn.innerHTML='<span class="spin"></span>'; }
  try {
    await api('/api/review/enqueue','POST',{
      brand:r.brand, title:r.title, format:r.format, rel:r.rel, files:r.files, caption:r.caption||''
    });
    toast('Sent to Review ✓');
    if (btn) { btn.disabled=true; btn.innerHTML='✓ Sent'; }
    S.reviewIndex[r.rel] = 'pending';                        // reflect it immediately…
    const badge = g('rc-badge-' + ri);
    if (badge) badge.innerHTML = reviewBadgeHtml(r.rel);      // …without a full list re-render
  } catch(e) {
    toast(e.message,'err');
    if (btn) { btn.disabled=false; btn.innerHTML='✓ Send to Review'; }
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Canvas (Fabric.js visual editor)
// ═══════════════════════════════════════════════════════════════════════════
const CW=540, CH=675;   // display size (50% of 1080x1350)
let fc=null, overlayRect=null;

const RMONO = "'Roboto Mono', monospace";

function initCanvas() {
  window._fc = true;
  fc = new fabric.Canvas('design-canvas', {
    width: CW, height: CH,
    backgroundColor: '#0A0F1E',
    preserveObjectStacking: true,
  });
  document.getElementById('overlay-canvas').width  = CW;
  document.getElementById('overlay-canvas').height = CH;
  drawOverlay(65);
  fc.on('selection:created', selectionChanged);
  fc.on('selection:updated', selectionChanged);
  fc.on('selection:cleared',  ()=>clearProps());
  fc.on('object:modified',    selectionChanged);
  // Make sure Roboto Mono is loaded before first paint so canvas == editor.
  const draw = () => applyPreset('title');
  if (document.fonts && document.fonts.load) {
    Promise.all([
      document.fonts.load("700 40px 'Roboto Mono'"),
      document.fonts.load("400 20px 'Roboto Mono'"),
    ]).then(draw).catch(draw);
  } else { draw(); }
}

// Active brand info (with safe fallback) for canvas drawing.
function _bi() {
  return S.brandInfo || {
    name:'Your Brand', short:'YB', handle:'@yourbrand',
    tagline:'Your tagline goes here',
    pitch:'What you do, in one line.', services:'Service 1 · Service 2 · Service 3',
    location:'Your City', website:'yourbrand.com', category:'YOUR NICHE',
    logo:'/static/logo.png', accent:'#00B4C8', accent2:'#00C896', navy:'#0A0F1E', text:'#FFFFFF',
  };
}
function _mkText(text,o){ return new fabric.Text(text,Object.assign({fontFamily:RMONO,fill:'#fff',selectable:true},o)); }
function _mkIText(text,o){ return new fabric.IText(text,Object.assign({fontFamily:RMONO,fill:'#fff',selectable:true,editable:true},o)); }

function addWatermark() {
  const bi=_bi();
  fabric.Image.fromURL(bi.logo, img=>{
    img.scaleToWidth(380);
    img.set({left:CW/2, top:CH*0.55, originX:'center', originY:'center', opacity:0.06, selectable:false, evented:false});
    fc.add(img); fc.sendToBack(img); fc.renderAll();
  });
}
function addFrame() {
  fc.add(new fabric.Rect({left:15, top:15, width:CW-30, height:CH-30, fill:'',
    stroke:'rgba(255,255,255,0.12)', strokeWidth:1, rx:8, ry:8, selectable:false, evented:false}));
}
function addLockup(x,y) {
  const bi=_bi();
  fc.add(_mkText(bi.name, {left:x+38, top:y+1, fontSize:14, fontWeight:'700'}));
  fc.add(_mkText(bi.tagline, {left:x+38, top:y+19, fontSize:8, fontWeight:'400', fontStyle:'italic', fill:'rgba(255,255,255,0.42)'}));
  fabric.Image.fromURL(bi.logo, img=>{ img.scaleToWidth(30); img.set({left:x, top:y, selectable:true}); fc.add(img); fc.renderAll(); });
}
function addBadge(cy) {
  const bi=_bi();
  fabric.Image.fromURL(bi.logo, img=>{
    img.scaleToWidth(23); img.set({left:0, top:0, originY:'center'});
    const name=_mkText(bi.name, {left:30, top:0, originY:'center', fontSize:12.5, fontWeight:'700'});
    const grp=new fabric.Group([img,name], {originX:'center', left:CW/2, top:cy, selectable:true});
    fc.add(grp); fc.renderAll();
  });
}

function applyPreset(type) {
  if(!fc) return;
  fc.clear();
  const bi=_bi(), plan=S.plan;
  fc.backgroundColor = bi.navy;

  if(type==='title') {
    const P=42;
    addWatermark();
    addLockup(P,P);
    fc.add(_mkIText(plan?.title_card?.headline||'Your headline goes here', {left:P, top:248, fontSize:35, fontWeight:'700', fill:'#fff', width:CW-2*P}));
    fc.add(new fabric.Rect({left:P, top:360, width:29, height:2, fill:bi.accent, selectable:true, strokeWidth:0}));
    fc.add(_mkIText(plan?.title_card?.subhead||'Your subhead goes here.', {left:P, top:374, fontSize:13, fontWeight:'400', fill:'rgba(255,255,255,0.62)', width:CW-2*P}));
    fc.add(_mkText(bi.handle, {left:CW/2, top:CH-46, originX:'center', fontSize:9, fontWeight:'400', fill:'rgba(255,255,255,0.45)'}));
    addFrame();
  } else if(type==='content') {
    const P=38;
    addWatermark();
    const meta=[bi.handle].filter(Boolean).join(' · ');
    fc.add(_mkText(meta?('· '+meta):'', {left:CW/2, top:P, originX:'center', fontSize:9, fontWeight:'400', fill:'rgba(255,255,255,0.45)'}));
    fc.add(_mkText('01', {left:P, top:248, fontSize:10.5, fontWeight:'700', fill:bi.accent, charSpacing:200}));
    fc.add(_mkIText('SLIDE HEADING', {left:P, top:270, fontSize:31, fontWeight:'700', fill:'#fff', width:CW-2*P}));
    fc.add(_mkIText('— Key point one\n— Key point two', {left:P, top:330, fontSize:16, fontWeight:'400', fill:'rgba(255,255,255,0.88)', width:CW-2*P}));
    addBadge(CH-44);
    addFrame();
  } else if(type==='outro') {
    const P=38;
    const grad=new fabric.Gradient({type:'linear', coords:{x1:0,y1:0,x2:CW,y2:CH},
      colorStops:[{offset:0,color:bi.accent},{offset:0.62,color:bi.navy}]});
    fc.setBackgroundColor(grad, fc.renderAll.bind(fc));
    addWatermark();
    fc.add(_mkText('· '+(bi.website||bi.handle||''), {left:CW/2, top:P, originX:'center', fontSize:9, fontWeight:'400', fill:'rgba(255,255,255,0.55)'}));
    fc.add(_mkIText(plan?.outro_card?.cta||'Follow for updates', {left:P, top:250, fontSize:29, fontWeight:'700', fill:'#fff', width:CW-2*P}));
    fc.add(_mkIText(plan?.outro_card?.handle||bi.handle, {left:P, top:330, fontSize:17, fontWeight:'500', fill:'rgba(255,255,255,0.9)', width:CW-2*P}));
    addBadge(CH-44);
    addFrame();
  } else if(type==='cover') {
    addWatermark();
    fc.add(new fabric.Rect({left:42, top:46, width:9, height:9, fill:bi.accent, selectable:true}));
    fc.add(_mkText((bi.category||'').toUpperCase(), {left:58, top:42, fontSize:13, fontWeight:'700', charSpacing:120}));
    fc.add(_mkText(bi.handle, {left:CW-42, top:44, originX:'right', fontSize:11, fontWeight:'500', fill:'rgba(255,255,255,0.6)'}));
    fc.add(_mkText(bi.short, {left:CW/2, top:CH*0.40, originX:'center', originY:'center', fontSize:120, fontWeight:'700'}));
    if((bi.name||'').indexOf(' ')>=0)
      fc.add(_mkText(bi.name.split(' ').slice(1).join(' ').toUpperCase(), {left:CW/2, top:CH*0.40+78, originX:'center', fontSize:28, fontWeight:'400', charSpacing:300, fill:'rgba(255,255,255,0.82)'}));
    fc.add(new fabric.Rect({left:CW/2-52, top:CH*0.40+118, width:46, height:2, fill:bi.accent, selectable:true}));
    fc.add(new fabric.Rect({left:CW/2+6,  top:CH*0.40+118, width:46, height:2, fill:bi.accent2||bi.accent, selectable:true}));
    fc.add(_mkText(bi.pitch||'', {left:CW/2, top:CH*0.40+138, originX:'center', fontSize:15, fontWeight:'500', fontStyle:'italic', fill:'rgba(255,255,255,0.85)'}));
    fc.add(_mkText(bi.services||'', {left:CW/2, top:CH*0.40+162, originX:'center', fontSize:12, fontWeight:'400', fill:'rgba(255,255,255,0.5)'}));
    const loc=[bi.location,bi.website].filter(Boolean).join(' · ');
    fc.add(_mkText(loc, {left:CW/2, top:CH-46, originX:'center', fontSize:10, fontWeight:'400', fill:'rgba(255,255,255,0.45)'}));
    addFrame();
  }
  fc.renderAll();
}

function drawOverlay(pct) {
  const oc = document.getElementById('overlay-canvas');
  const ctx = oc.getContext('2d');
  ctx.clearRect(0,0,CW,CH);
  const grad = ctx.createLinearGradient(0,0,0,CH);
  const op = pct/100;
  grad.addColorStop(0, `rgba(10,15,30,${(op*0.62).toFixed(2)})`);
  grad.addColorStop(1, `rgba(10,15,30,${Math.min(0.97,op).toFixed(2)})`);
  ctx.fillStyle = grad;
  ctx.fillRect(0,0,CW,CH);
}

function updateOverlay(v) {
  document.getElementById('overlay-val').textContent = v+'%';
  drawOverlay(parseInt(v));
}

// Mirror the slide currently open in the Editor (real text + background image)
function loadSlideToCanvas() {
  if(!fc) return;
  if(!S.plan){ toast('Generate/edit a plan first','err'); return; }
  syncPlan();
  const idx = S.slideIdx, n = (S.plan.content_slides||[]).length;
  const type = idx===0 ? 'title' : (idx<=n ? 'content' : 'outro');
  applyPreset(type);   // lays out the brand furniture
  // now overwrite the editable text objects with the real slide content
  const tc = S.plan.title_card||{}, oc = S.plan.outro_card||{};
  const texts = fc.getObjects().filter(o=>o.type==='i-text');
  if(type==='title'){
    if(texts[0]) texts[0].set('text', tc.headline||'');
    if(texts[1]) texts[1].set('text', tc.subhead||'');
  } else if(type==='content'){
    const sl = S.plan.content_slides[idx-1]||{};
    // preset content objects: [heading(itext), body(itext)] ; slide-num is static
    const statics = fc.getObjects().filter(o=>o.type==='text');
    // update the "01" number static
    const num = statics.find(o=>/^[0-9]{1,2}$/.test(o.text));
    if(num) num.set('text', String(idx).padStart(2,'0'));
    if(texts[0]) texts[0].set('text', (sl.heading||'').toUpperCase());
    if(texts[1]) texts[1].set('text', sl.body||'');
    // background image for this content slide
    const p = S.imagePaths[idx-1] || S.imagePaths[String(idx-1)];
    if(p) _setBgImage('/image_cache/'+p.split(/[/\\]/).pop());
  } else {
    if(texts[0]) texts[0].set('text', oc.cta||'');
    if(texts[1]) texts[1].set('text', oc.handle||'');
  }
  fc.renderAll();
  toast(`Loaded ${type} slide ${idx+1} into canvas`);
}

function addLogoImg(x,y,size) {
  fabric.Image.fromURL(_bi().logo, img=>{
    img.scaleToWidth(size);
    img.set({left:x,top:y,selectable:true});
    fc.add(img);fc.renderAll();
  });
}

function addStaticText(text,x,y,size,weight,color) {
  fc.add(new fabric.Text(text,{
    left:x,top:y,fontSize:size,fontFamily:RMONO,
    fill:color,fontWeight:weight,selectable:true
  }));
}

function addEditableText(text,x,y,size,weight,color,maxW,upperCase=false) {
  const t=new fabric.IText(text,{
    left:x,top:y,fontSize:size,fontFamily:RMONO,
    fill:color,fontWeight:weight,width:maxW||CW-80,
    selectable:true,editable:true,
  });
  fc.add(t);
  return t;
}

function addText(text,size,weight) {
  if(!fc) return;
  const t=new fabric.IText(text,{
    left:60,top:100,fontSize:size/2,fontFamily:RMONO,
    fill:'#ffffff',fontWeight:weight,selectable:true,editable:true,
  });
  fc.add(t);fc.setActiveObject(t);fc.renderAll();
}

function addRect() {
  if(!fc) return;
  const r=new fabric.Rect({left:30,top:200,width:400,height:120,fill:'rgba(10,15,30,0.82)',strokeWidth:0,selectable:true});
  fc.add(r);fc.setActiveObject(r);fc.renderAll();
}

function addRule() {
  if(!fc) return;
  const r=new fabric.Rect({left:38,top:300,width:58,height:3,fill:'#00B4C8',strokeWidth:0,selectable:true});
  fc.add(r);fc.setActiveObject(r);fc.renderAll();
}

function addLogo() { if(fc) addLogoImg(30,CH-60,50); }

function clearCanvas() { if(fc){fc.clear();fc.backgroundColor='#0A0F1E';fc.renderAll();} }

// Background setters
function setBgNavy()  { if(fc){fc.backgroundColor='#0A0F1E';fc.renderAll();} }
function setBgTeal()  {
  if(!fc) return;
  const grad=new fabric.Gradient({type:'linear',coords:{x1:0,y1:0,x2:CW,y2:0},colorStops:[{offset:0,color:'#00B4C8'},{offset:1,color:'#0A0F1E'}]});
  fc.setBackgroundColor(grad,fc.renderAll.bind(fc));
}
function setBgFile(inp) {
  if(!inp.files[0]||!fc) return;
  const url=URL.createObjectURL(inp.files[0]);
  _setBgImage(url);
}
async function setBgUrl() {
  const url=g('canvas-img-url')?.value||'';
  if(!url) return;
  try {
    const data=await api('/api/images/from-url','POST',{url,name:'canvas-bg'});
    _setBgImage('/image_cache/'+data.filename);
    toast('Background set');
  } catch(e){toast(e.message,'err');}
}
async function setBgPexels() {
  const q=g('canvas-pexels-q')?.value||'';
  if(!q) return;
  const src=g('img-source')?.value||'pexels';
  try {
    const data=await api(`/api/images/swap/999`,'POST',{query:q,source:src});
    _setBgImage('/image_cache/'+data.filename);
    toast('Background set');
  } catch(e){toast(e.message,'err');}
}
function _setBgImage(url) {
  if(!fc) return;
  fabric.Image.fromURL(url,img=>{
    img.scaleToWidth(CW);
    img.scaleToHeight(CH);
    fc.setBackgroundImage(img,fc.renderAll.bind(fc));
  },{crossOrigin:'anonymous'});
}

// Export
async function exportCanvas() {
  if(!fc){toast('Open the Canvas tab first','err');return;}
  const btn=g('btn-export-canvas');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> Exporting…';
  // Apply overlay to the canvas before export
  const overlayOpacity=parseInt(g('overlay-opacity')?.value||'65')/100;
  const oc=new fabric.Rect({left:0,top:0,width:CW,height:CH,fill:`rgba(10,15,30,${(overlayOpacity*0.62).toFixed(2)})`,selectable:false,evented:false,opacity:1});
  fc.add(oc); fc.sendToBack(oc); fc.renderAll();

  const dataUrl=fc.toDataURL({format:'png',multiplier:2,quality:1});
  fc.remove(oc);fc.renderAll();

  const link=document.createElement('a');
  link.href=dataUrl;
  link.download=`k2-slide-${Date.now()}.png`;
  link.click();
  btn.disabled=false;btn.innerHTML='Export PNG (1080×1350)';
  toast('PNG exported!');
}


// Property panel
function selectionChanged() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const isText=(obj.type==='i-text'||obj.type==='text');
  g('prop-text').value = isText?(obj.text||''):'';
  g('prop-size').value = isText?(obj.fontSize||46):46;
  g('prop-x').value=Math.round(obj.left);
  g('prop-y').value=Math.round(obj.top);
  g('prop-w').value=Math.round(obj.width*(obj.scaleX||1));
  g('prop-h').value=Math.round(obj.height*(obj.scaleY||1));
  const bold=isText&&obj.fontWeight==='bold';
  g('prop-bold').style.background=bold?'var(--teal)':'';
  g('prop-bold').style.color=bold?'#000':'';
}
function clearProps() { g('prop-text').value='';g('prop-size').value='46'; }
function applyProp() {
  const obj=fc.getActiveObject();
  if(!obj||(obj.type!=='i-text'&&obj.type!=='text')) return;
  obj.set({text:g('prop-text').value,fontSize:parseInt(g('prop-size').value)||46});
  fc.renderAll();
}
function applyPos() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const x=parseFloat(g('prop-x').value),y=parseFloat(g('prop-y').value);
  if(!isNaN(x)&&!isNaN(y)){obj.set({left:x,top:y});fc.renderAll();}
}
function applySize() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const w=parseFloat(g('prop-w').value),h=parseFloat(g('prop-h').value);
  if(!isNaN(w)&&!isNaN(h)){obj.set({scaleX:w/obj.width,scaleY:h/obj.height});fc.renderAll();}
}
function toggleBold() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  const bold=obj.fontWeight==='bold';
  obj.set('fontWeight',bold?'normal':'bold');fc.renderAll();
  g('prop-bold').style.background=bold?'':'var(--teal)';
  g('prop-bold').style.color=bold?'':'#000';
}
function toggleItalic() {
  const obj=fc.getActiveObject();
  if(!obj) return;
  obj.set('fontStyle',obj.fontStyle==='italic'?'normal':'italic');fc.renderAll();
}
function toggleUpper() {
  const obj=fc.getActiveObject();
  if(!obj||(obj.type!=='i-text'&&obj.type!=='text')) return;
  const txt=obj.text;
  obj.set('text',txt===txt.toUpperCase()?txt.toLowerCase():txt.toUpperCase());
  fc.renderAll();
}
function setColor(hex,el) {
  if(el){document.querySelectorAll('.color-swatch').forEach(s=>s.classList.remove('active'));el.classList.add('active');}
  const obj=fc.getActiveObject();
  if(!obj) return;
  obj.set('fill',hex);fc.renderAll();
}
function deleteSelected() { const o=fc.getActiveObject();if(o){fc.remove(o);fc.renderAll();} }
function bringFront() { const o=fc.getActiveObject();if(o){fc.bringToFront(o);fc.renderAll();} }
function sendBack()   { const o=fc.getActiveObject();if(o){fc.sendToBack(o);fc.renderAll();} }

// ═══════════════════════════════════════════════════════════════════════════
// Templates tab
// ═══════════════════════════════════════════════════════════════════════════
let _cm=null, _curFile=null;
function initCM() {
  const ta=g('code-editor');
  _cm=CodeMirror.fromTextArea(ta,{lineNumbers:true,indentUnit:2,tabSize:2,lineWrapping:false,mode:'htmlmixed'});
  _cm.getWrapperElement().style.cssText='background:#0d1828;color:#c9d1e0;height:100%;';
  window._cm=_cm;
}
async function loadTmplList() {
  const data=await api('/api/templates');
  g('tmpl-file-list').innerHTML=data.files.map(f=>`
    <button class="tmpl-file-btn ${f===_curFile?'active':''}" onclick="openTmpl('${f}')">${f}</button>
  `).join('');
}
async function openTmpl(fn) {
  _curFile=fn;
  const data=await api(`/api/template/${fn}`);
  g('tmpl-filename').textContent=fn;
  if(!_cm) initCM();
  _cm.setOption('mode',fn.endsWith('.css')?'css':'htmlmixed');
  _cm.setValue(data.content);
  _cm.refresh();
  loadTmplList();
}
async function saveTemplate() {
  if(!_curFile||!_cm){toast('Open a file first','err');return;}
  const st=g('tmpl-status');
  st.innerHTML='<span class="spin"></span>';
  try {
    await api(`/api/template/${_curFile}`,'PUT',{content:_cm.getValue()});
    st.innerHTML='<span class="badge badge-ok">Saved</span>';
    toast(`${_curFile} saved`);
    setTimeout(()=>st.innerHTML='',2000);
  } catch(e){st.innerHTML=`<span class="badge badge-err">${e.message}</span>`;toast(e.message,'err');}
}
async function previewTemplate() {
  if(!_curFile){toast('Open a file first','err');return;}
  const btn=document.querySelector('#tab-templates .btn-primary');
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spin"></span>';}
  const wrap=g('tmpl-preview-wrap');
  wrap.innerHTML='<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> Rendering…</div>';
  try {
    const r=await fetch(`/api/template/preview/${_curFile}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    if(!r.ok) throw new Error(await r.text());
    const url=URL.createObjectURL(await r.blob());
    wrap.innerHTML=`<img src="${url}" style="max-width:100%;max-height:100%;border-radius:4px;">`;
  } catch(e){wrap.innerHTML=`<div style="color:var(--red);font-size:12px;padding:8px;">${e.message}</div>`;toast(e.message,'err');}
  finally{if(btn){btn.disabled=false;btn.innerHTML='Preview';}}
}

// ═══════════════════════════════════════════════════════════════════════════
// Library — save / load generated content
// ═══════════════════════════════════════════════════════════════════════════
function closeModal(){ g('modal-bg').style.display='none'; }
function openModal(title){ g('modal-title').textContent=title; g('modal-bg').style.display='flex'; }

// In-app text prompt (replaces the browser prompt()). Resolves to the entered
// string, or null on cancel.
function promptModal(title, label, defaultValue='', okLabel='Save'){
  return new Promise(resolve => {
    openModal(title);
    const body = g('modal-body');
    body.innerHTML = `
      <label style="font-size:12px;color:var(--muted);">${label}</label>
      <input id="prompt-input" class="url-inp" style="width:100%;font-size:14px;padding:10px 12px;margin-top:6px;">
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px;">
        <button class="btn btn-ghost btn-sm" id="prompt-cancel">Cancel</button>
        <button class="btn btn-green btn-sm" id="prompt-ok">${esc(okLabel)}</button>
      </div>`;
    const input = g('prompt-input');
    input.value = defaultValue || '';
    setTimeout(()=>{ input.focus(); input.select(); }, 30);
    const done = (val)=>{ closeModal(); resolve(val); };
    g('prompt-ok').onclick     = ()=> done(input.value.trim() || defaultValue);
    g('prompt-cancel').onclick = ()=> done(null);
    input.onkeydown = (e)=>{
      if(e.key==='Enter'){ e.preventDefault(); done(input.value.trim() || defaultValue); }
      else if(e.key==='Escape'){ done(null); }
    };
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Settings panel — edit the defaults that get applied on every load.
// ═══════════════════════════════════════════════════════════════════════════
function openSettings() {
  toggleNav(false);
  openModal('⚙ Settings & defaults');
  const body = g('modal-body');
  const opt = (v, l, cur) => `<option value="${esc(v)}" ${String(v)===String(cur)?'selected':''}>${esc(l)}</option>`;
  const tones = [['','Auto'],['positive, upbeat','Positive'],['negative, critical','Negative'],
                 ['neutral, factual','Neutral'],['hyped, exciting','Hype'],
                 ['analytical, measured','Analytical'],['skeptical, cautionary','Skeptical']];
  const ms = g('model-sel');
  const models = ms ? [...ms.options].map(o=>o.value)
      .filter(v=>v && v!=='Loading…' && v!=='No models found') : [];
  const modelOpts = ['<option value="">(server default)</option>']
      .concat(models.map(m=>opt(m,m,SETTINGS.model))).join('');
  const row = (label, hint, control) => `
    <div class="set-row">
      <div class="set-label">${label}${hint?`<span class="set-hint">${hint}</span>`:''}</div>
      <div class="set-control">${control}</div>
    </div>`;
  const sel = (id, optsHtml) => `<select id="${id}" class="src-sel" style="width:100%;max-width:none;min-height:38px;">${optsHtml}</select>`;
  const num = (id, val, min, max) => `<input type="number" id="${id}" value="${val}" min="${min}" max="${max}" style="width:100%;background:#0d1828;border:1px solid var(--border);border-radius:6px;color:#fff;padding:7px 8px;font-size:14px;">`;
  body.innerHTML = `
    ${row(`<label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
        <input type="checkbox" id="set-notify" ${SETTINGS.notify?'checked':''} style="width:16px;height:16px;">
        Desktop notifications</label>`, 'On by default — pings you when a run finishes', '')}
    ${row('Default model', 'Used for scoring & copy', sel('set-model', modelOpts))}
    ${row('Default tone', 'Stance applied to generated copy', sel('set-tone', tones.map(([v,l])=>opt(v,l,SETTINGS.tone)).join('')))}
    ${row('Default image source', '', sel('set-source', [['pexels','Pexels'],['unsplash','Unsplash'],['google','Google']].map(([v,l])=>opt(v,l,SETTINGS.imageSource)).join('')))}
    ${row('Default carousel slides', '', sel('set-slides', [3,4,5,6].map(n=>opt(n,n+' slides',SETTINGS.slides)).join('')))}
    ${row('Stories to fetch', '', num('set-fetch', SETTINGS.fetchLimit, 3, 60))}
    ${row('Top N to keep', '', num('set-top', SETTINGS.fetchTop, 1, 15))}
    <div style="display:flex;justify-content:space-between;gap:8px;margin-top:6px;border-top:1px solid var(--border);padding-top:12px;">
      <button class="btn btn-ghost btn-sm" onclick="resetSettings()">↺ Reset to defaults</button>
      <button class="btn btn-green" onclick="saveSettingsForm()">Save settings</button>
    </div>`;
}

async function saveSettingsForm() {
  const wantNotify = g('set-notify').checked;
  SETTINGS.model       = g('set-model').value;
  SETTINGS.tone        = g('set-tone').value;
  SETTINGS.imageSource = g('set-source').value;
  SETTINGS.slides      = parseInt(g('set-slides').value) || 4;
  SETTINGS.fetchLimit  = parseInt(g('set-fetch').value) || 15;
  SETTINGS.fetchTop    = parseInt(g('set-top').value) || 6;
  // Reconcile the notification toggle with the actual browser permission.
  if (wantNotify && !S.notify && ('Notification' in window)) {
    let p = Notification.permission;
    if (p !== 'granted') p = await Notification.requestPermission();
    S.notify = p === 'granted';
    if (p !== 'granted') toast('Allow notifications in your browser to enable them','err');
  } else if (!wantNotify) {
    S.notify = false;
  }
  SETTINGS.notify = wantNotify;
  saveSettings();
  applySettings();
  updateNotifBtn();
  closeModal();
  toast('Settings saved');
}

function resetSettings() {
  SETTINGS = {...SETTINGS_DEFAULTS};
  saveSettings();
  applySettings();
  updateNotifBtn();
  openSettings();   // re-render the form with defaults
  toast('Reset to defaults');
}

function viewSource() {
  const u = S.plan && (S.plan.source_url || S.plan.url);
  if (!u) { toast('No original link for this plan (try generating from a story)','err'); return; }
  window.open(u, '_blank', 'noopener');
}

async function savePlan() {
  if (!S.plan) { toast('No plan to save','err'); return; }
  syncPlan();
  const name = await promptModal('Save plan', 'Save this plan to your library as:',
                                 S.plan.title_card?.headline || S.plan.slug || 'plan');
  if (name === null) return;
  try {
    await api('/api/library/plan','POST',{plan:S.plan, name});
    toast('Plan saved to library');
  } catch(e){ toast(e.message,'err'); }
}

async function openLibrary() {
  openModal('Saved plans');
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;">Loading…</div>';
  try {
    const data = await api('/api/library/plans');
    if (!data.plans.length){ body.innerHTML='<div style="color:var(--muted);font-size:12px;">No saved plans yet.</div>'; return; }
    body.innerHTML = `<div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
        <button class="btn btn-danger btn-sm" onclick="clearLibrary('plans')">🗑 Clear all plans</button>
      </div>` + data.plans.map(p=>`
      <div class="story-card" style="cursor:default;">
        <div class="s-top">
          <span class="score-pill badge badge-info">${esc(p.brand||'')}</span>
          <span class="story-title">${esc(p.name||p.id)}</span>
        </div>
        <div class="story-reason">${esc(p.format||'carousel')} · ${esc((p.when||'').replace('T',' '))}</div>
        <div class="story-actions">
          <button class="btn btn-primary btn-sm" onclick="loadSavedPlan('${esc(p.id)}')">Load</button>
          <button class="btn btn-danger btn-sm" onclick="delSavedPlan('${esc(p.id)}',this)">Delete</button>
        </div>
      </div>`).join('');
  } catch(e){ body.innerHTML=`<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}

async function loadSavedPlan(id) {
  try {
    const data = await api('/api/library/plan/'+id);
    loadPlan(data.plan);                       // resets S.imagePaths to {}
    // Restore the saved images + thumbnails so the plan is render-ready.
    S.imagePaths = data.image_paths || {};
    Object.entries(S.imagePaths).forEach(([i,p]) => {
      if (p) setThumb(parseInt(i), '/image_cache/' + p.split(/[/\\]/).pop());
    });
    const n = Object.keys(S.imagePaths).length;
    closeModal();
    toast('Rendering saved plan…');
    await finishPlanAutomatically('pexels', !n);
    toast('Saved plan rendered and added to Dashboard');
  } catch(e){ toast(e.message,'err'); }
}
async function delSavedPlan(id, btn) {
  await api('/api/library/plan/'+id,'DELETE').catch(()=>{});
  btn.closest('.story-card')?.remove();
}

async function clearLibrary(kind) {
  const label = kind==='plans' ? 'saved plans' : 'saved story sets';
  if (!confirm(`Delete ALL ${label}? This cannot be undone.`)) return;
  try {
    const r = await api('/api/library/clear','POST',{kind});
    toast(`Cleared ${r.removed} item(s)`);
    kind==='plans' ? openLibrary() : openStoryLibrary();
  } catch(e){ toast(e.message,'err'); }
}

async function saveStorySet() {
  if (!S.stories.length){ toast('Fetch stories first','err'); return; }
  try { await api('/api/library/stories','POST',{}); toast('Story set saved'); }
  catch(e){ toast(e.message,'err'); }
}

async function openStoryLibrary() {
  openModal('Saved story sets');
  const body = g('modal-body');
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;">Loading…</div>';
  try {
    const data = await api('/api/library/stories');
    if (!data.stories.length){ body.innerHTML='<div style="color:var(--muted);font-size:12px;">No saved sets yet.</div>'; return; }
    body.innerHTML = `<div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
        <button class="btn btn-danger btn-sm" onclick="clearLibrary('stories')">🗑 Clear all sets</button>
      </div>` + data.stories.map(s=>`
      <div class="story-card" style="cursor:default;">
        <div class="s-top">
          <span class="score-pill badge badge-info">${esc(s.brand||'')}</span>
          <span class="story-title">${esc(s.name||s.id)}</span>
        </div>
        <div class="story-reason">${esc(s.count||'?')} stories · ${esc((s.when||'').replace('T',' '))}</div>
        <div class="story-actions">
          <button class="btn btn-primary btn-sm" onclick="loadSavedStories('${esc(s.id)}')">Load</button>
          <button class="btn btn-danger btn-sm" onclick="delSavedStories('${esc(s.id)}',this)">Delete</button>
        </div>
      </div>`).join('');
  } catch(e){ body.innerHTML=`<div style="color:var(--red);font-size:12px;">${esc(e.message)}</div>`; }
}
async function loadSavedStories(id) {
  try {
    const data = await api('/api/library/stories/'+id);
    S.stories = data.stories || [];
    renderStoriesList(S.stories);
    closeModal();
    toast(`Loaded ${S.stories.length} stories (no re-fetch)`);
  } catch(e){ toast(e.message,'err'); }
}
async function delSavedStories(id, btn) {
  await api('/api/library/stories/'+id,'DELETE').catch(()=>{});
  btn.closest('.story-card')?.remove();
}

// ═══════════════════════════════════════════════════════════════════════════
// Library tab — saved plans + story sets, date-sorted, grid / list views
// ═══════════════════════════════════════════════════════════════════════════
async function loadLibraryTab() {
  const list = g('library-list');
  list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:30px;">Loading…</div>';
  try {
    const [p, s] = await Promise.all([api('/api/library/plans'), api('/api/library/stories')]);
    S.lib.plans   = p.plans || [];
    S.lib.stories = s.stories || [];
    renderLibrary();
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);font-size:13px;text-align:center;padding:30px;">${esc(e.message || 'Failed')}</div>`;
  }
}

function libSetKind(kind) {
  S.lib.kind = kind;
  g('lib-kind-plans').classList.toggle('active', kind === 'plans');
  g('lib-kind-stories').classList.toggle('active', kind === 'stories');
  renderLibrary();
}

function libSetView(view) {
  S.lib.view = view;
  g('lib-view-grid').classList.toggle('active', view === 'grid');
  g('lib-view-list').classList.toggle('active', view === 'list');
  renderLibrary();
}

function _libSort(items) {
  const mode = g('lib-sort').value;
  const arr = items.slice();
  if (mode === 'name') arr.sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
  else arr.sort((a, b) => (b.when || '').localeCompare(a.when || ''));   // newest first
  if (mode === 'old') arr.reverse();
  return arr;
}

function renderLibrary() {
  const list = g('library-list');
  const kind = S.lib.kind;
  const items = _libSort(kind === 'plans' ? S.lib.plans : S.lib.stories);
  if (!items.length) {
    list.innerHTML = `<div style="color:var(--muted);font-size:13px;text-align:center;padding:40px;line-height:1.6;">
      <div style="font-size:30px;margin-bottom:8px;">📚</div>No saved ${kind === 'plans' ? 'plans' : 'story sets'} yet.<br>
      ${kind === 'plans' ? 'Saved generated plans will appear here.' : 'Save a fetched set from Stories (💾 Save set).'}</div>`;
    return;
  }
  const grid = S.lib.view === 'grid';
  list.className = 'lib-body';
  const inner = items.map(it => grid ? libCard(it, kind) : libRow(it, kind)).join('');
  list.innerHTML = `<div class="${grid ? 'lib-grid' : 'lib-list'}">${inner}</div>`;
}

function _libWhen(w) { return esc((w || '').replace('T', ' ').slice(0, 16)); }

function libCard(it, kind) {
  const sub = kind === 'plans' ? esc(it.format || 'carousel') : `${esc(it.count || '?')} stories`;
  return `<div class="lib-card">
    <div class="lib-meta"><span class="lib-badge">${esc(it.brand || '—')}</span><span class="lib-badge" style="background:rgba(107,122,150,.18);color:var(--muted);">${sub}</span></div>
    <div class="lib-name">${esc(it.name || it.id)}</div>
    <div class="lib-when">${_libWhen(it.when)}</div>
    <div style="display:flex;gap:6px;margin-top:2px;">
      <button class="btn btn-primary btn-sm" onclick="libLoad('${esc(it.id)}')">Load</button>
      <button class="btn btn-danger btn-sm" onclick="libDel('${esc(it.id)}',this)">Delete</button>
    </div>
  </div>`;
}

function libRow(it, kind) {
  const sub = kind === 'plans' ? esc(it.format || 'carousel') : `${esc(it.count || '?')} stories`;
  return `<div class="lib-row">
    <span class="lib-badge">${esc(it.brand || '—')}</span>
    <span class="lib-name">${esc(it.name || it.id)}</span>
    <span class="lib-badge" style="background:rgba(107,122,150,.18);color:var(--muted);">${sub}</span>
    <span class="lib-when">${_libWhen(it.when)}</span>
    <button class="btn btn-primary btn-sm" onclick="libLoad('${esc(it.id)}')">Load</button>
    <button class="btn btn-danger btn-sm" onclick="libDel('${esc(it.id)}',this)">Delete</button>
  </div>`;
}

async function libLoad(id) {
  try {
    if (S.lib.kind === 'plans') {
      const data = await api('/api/library/plan/' + id);
      loadPlan(data.plan);
      S.imagePaths = data.image_paths || {};
      toast('Rendering saved plan…');
      await finishPlanAutomatically('pexels', !Object.keys(S.imagePaths).length);
      toast('Saved plan rendered and added to Dashboard');
    } else {
      const data = await api('/api/library/stories/' + id);
      S.stories = data.stories || [];
      renderStoriesList(S.stories);
      showTab('stories');
      toast(`Loaded ${S.stories.length} stories`);
    }
  } catch (e) { toast(e.message, 'err'); }
}

async function libDel(id, btn) {
  const path = S.lib.kind === 'plans' ? '/api/library/plan/' : '/api/library/stories/';
  await api(path + id, 'DELETE').catch(() => {});
  (S.lib.kind === 'plans')
    ? S.lib.plans = S.lib.plans.filter(p => p.id !== id)
    : S.lib.stories = S.lib.stories.filter(s => s.id !== id);
  renderLibrary();
}

async function libClear() {
  const kind = S.lib.kind;
  if (!confirm(`Delete ALL saved ${kind === 'plans' ? 'plans' : 'story sets'}? This cannot be undone.`)) return;
  try {
    const r = await api('/api/library/clear', 'POST', { kind });
    toast(`Cleared ${r.removed} item(s)`);
    loadLibraryTab();
  } catch (e) { toast(e.message, 'err'); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════════════════
function g(id){return document.getElementById(id);}
// The brand dropdown is the single source of truth for which brand every action uses.
function curBrand(){ return document.getElementById('brand-sel')?.value || ''; }
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

async function api(url, method='GET', body=null) {
  const opts={method,headers:{}};
  if(body!==null){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
  const r=await fetch(url,opts);
  const ct=r.headers.get('content-type')||'';
  if(!ct.includes('application/json'))return r;
  const data=await r.json();
  if(!r.ok) throw new Error(data.detail||JSON.stringify(data));
  return data;
}

function toast(msg,type='ok'){
  const el=g('toast');el.textContent=msg;el.className='show '+type;
  clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),3200);
}
</script>
</body>
</html>
"""

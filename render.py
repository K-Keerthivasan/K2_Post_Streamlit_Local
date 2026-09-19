"""Render a carousel plan to a folder of PNGs using Jinja2 + Playwright."""
from __future__ import annotations
import argparse
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from brands import resolve_brand, theme_css, brand_template, active_key


# ── helpers ─────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Watermark position presets (editor-controllable). Values are inline CSS for .wm.
_WM_POS = {
    "center":       "left:50%;top:55%;transform:translate(-50%,-50%)",
    "top":          "left:50%;top:6%;transform:translate(-50%,0)",
    "bottom":       "left:50%;top:100%;transform:translate(-50%,-62%)",
    "left":         "left:2%;top:55%;transform:translate(-38%,-50%)",
    "right":        "left:98%;top:55%;transform:translate(-62%,-50%)",
    "top-left":     "left:4%;top:6%;transform:translate(-35%,-30%)",
    "top-right":    "left:96%;top:6%;transform:translate(-65%,-30%)",
    "bottom-left":  "left:4%;top:100%;transform:translate(-35%,-66%)",
    "bottom-right": "left:96%;top:100%;transform:translate(-65%,-66%)",
}
_BADGE_JUSTIFY = {"left": "flex-start", "center": "center", "right": "flex-end"}


def _layout_vars(plan: dict | None) -> dict:
    """Editor-controllable layout: watermark position/opacity + badge alignment."""
    layout = (plan or {}).get("layout") or {}
    pos = _WM_POS.get(layout.get("wm_pos", "center"), _WM_POS["center"])
    try:
        opacity = float(layout.get("wm_opacity", 5)) / 100
    except (TypeError, ValueError):
        opacity = 0.05
    return {
        "wm_style":      f"{pos};opacity:{opacity:.3f}",
        "badge_justify": _BADGE_JUSTIFY.get(layout.get("badge_align", "center"), "center"),
    }


def _uri(path: str | Path | None) -> str | None:
    """Convert a local path to a file:// URI Playwright can load."""
    if not path:
        return None
    p = Path(path)
    return p.resolve().as_uri() if p.exists() else None


def _write_temp_html(html: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    )
    tmp.write(html)
    tmp.close()
    return Path(tmp.name)


# ── core render ──────────────────────────────────────────────────────────────

def render_slide(
    page: Any,
    env: Environment,
    template_name: str,
    variables: dict,
    out_path: Path,
    width: int = 1080,
    height: int = 1350,
) -> None:
    tmpl = env.get_template(template_name)
    html = tmpl.render(**variables)
    tmp = _write_temp_html(html)
    try:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(tmp.as_uri(), wait_until="networkidle")
        page.screenshot(path=str(out_path), type="png")
    finally:
        tmp.unlink(missing_ok=True)


def render_slide_to_bytes(
    template_name: str,
    variables: dict,
    width: int = 1080,
    height: int = 1350,
) -> bytes:
    """Render one slide and return raw PNG bytes (for live preview)."""
    env = Environment(loader=FileSystemLoader("templates"))
    tmpl = env.get_template(template_name)
    html = tmpl.render(**variables)
    tmp = _write_temp_html(html)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": width, "height": height})
            page = ctx.new_page()
            page.goto(tmp.as_uri(), wait_until="networkidle")
            png = page.screenshot(type="png")
            browser.close()
        return png
    finally:
        tmp.unlink(missing_ok=True)


# ── full carousel ────────────────────────────────────────────────────────────

def generate_carousel(plan: dict, image_paths: dict | None = None,
                      out_root: Path | None = None,
                      brand_key: str | None = None,
                      templates: dict | None = None) -> Path:
    """Render all slides for a plan. Returns the output directory path.

    ``brand_key`` renders for a specific brand regardless of the global active
    brand (used by cross-brand bulk runs). ``templates`` overrides the
    title/content/outro template names (used by the listicle format).
    """
    config    = _load_config()
    brand     = resolve_brand(config, brand_key)
    out_cfg   = config.get("output", {})
    width     = out_cfg.get("width",  1080)
    height    = out_cfg.get("height", 1350)
    css_uri   = _uri(Path("static/brand.css"))
    logo_uri  = _uri(Path(brand.get("logo_path", "static/logo.png")))
    author    = brand.get("author", "")
    handle    = brand.get("handle", "")

    slug      = plan.get("slug", "post")
    bkey      = brand_key or active_key(config) or "default"
    fmt_label = plan.get("format", "carousel")
    root      = out_root or (Path(out_cfg.get("directory", "outputs")) / bkey)
    out_dir   = root / f"{date.today().isoformat()}_{slug}_{fmt_label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # A slug can be rendered more than once on the same day. Remove the prior
    # slide set first; otherwise shortening a carousel leaves stale PNGs behind.
    for old_slide in out_dir.glob("*.png"):
        old_slide.unlink()

    content_slides = plan.get("content_slides", [])
    total_slides   = 1 + len(content_slides) + 1

    env = Environment(loader=FileSystemLoader("templates"))

    base = {
        "css_path":     css_uri,
        "logo_path":    logo_uri,
        "author":       author,
        "handle":       handle,
        "brand":        brand,
        "theme_css":    theme_css(brand),
        "plan":         plan,
        "total_slides": total_slides,
        **_layout_vars(plan),
    }

    over      = templates or {}
    t_title   = over.get("title")   or brand_template(brand, "title",   "title.html")
    t_content = over.get("content") or brand_template(brand, "content", "content.html")
    t_outro   = over.get("outro")   or brand_template(brand, "outro",   "outro.html")
    # Image-forward brands (e.g. JKR) show a hero image on the title card; reuse
    # the first content slide's image so the cover leads with a visual.
    title_img = None
    if brand.get("image_forward"):
        first = (image_paths or {}).get(0)
        title_img = _uri(first) if first else None

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx     = browser.new_context(viewport={"width": width, "height": height})
        page    = ctx.new_page()

        # 1 – Title card
        out_file = out_dir / "01_title.png"
        render_slide(page, env, t_title,
                     {**base, "background_image": title_img},
                     out_file, width, height)
        print(f"  [ok] 01_title.png")

        # 2..N – Content cards
        for i, slide in enumerate(content_slides):
            img_path = (image_paths or {}).get(i)
            out_file = out_dir / f"{i+2:02d}_content.png"
            render_slide(page, env, t_content,
                         {**base,
                          "slide":            slide,
                          "slide_number":     i + 1,
                          "background_image": _uri(img_path)},
                         out_file, width, height)
            print(f"  [ok] {out_file.name}")

        # Last – Outro card
        out_file = out_dir / f"{total_slides:02d}_outro.png"
        render_slide(page, env, t_outro,
                     {**base, "background_image": None},
                     out_file, width, height)
        print(f"  [ok] {out_file.name}")

        browser.close()

    _write_caption(plan, out_dir, "carousel")
    return out_dir


# ── single-card formats (square / story / x) ────────────────────────────────

def format_config(fmt: str, config: dict | None = None) -> dict:
    """Return {name,width,height,type,template} for a format key."""
    if config is None:
        config = _load_config()
    return config.get("formats", {}).get(fmt, {
        "name": fmt, "width": 1080, "height": 1080,
        "type": "single", "template": f"{fmt}.html",
    })


def generate_single(
    plan: dict,
    fmt: str,
    image_path: str | Path | None = None,
    out_root: Path | None = None,
    brand_key: str | None = None,
) -> Path:
    """Render a single-card post (square/story/x). Returns the output directory."""
    config = _load_config()
    brand  = resolve_brand(config, brand_key)
    fcfg   = format_config(fmt, config)
    width  = fcfg.get("width", 1080)
    height = fcfg.get("height", 1080)
    tmpl   = fcfg.get("template", f"{fmt}.html")

    slug    = plan.get("slug", "post")
    bkey    = brand_key or active_key(config) or "default"
    root    = out_root or (Path(config.get("output", {}).get("directory", "outputs")) / bkey)
    out_dir = root / f"{date.today().isoformat()}_{slug}_{fmt}"
    out_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "css_path":         _uri(Path("static/brand.css")),
        "logo_path":        _uri(Path(brand.get("logo_path", "static/logo.png"))),
        "handle":           brand.get("handle", ""),
        "author":           brand.get("author", ""),
        "brand":            brand,
        "theme_css":        theme_css(brand),
        "plan":             plan,
        "post":             plan,
        "background_image": _uri(image_path),
        **_layout_vars(plan),
    }

    env = Environment(loader=FileSystemLoader("templates"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx     = browser.new_context(viewport={"width": width, "height": height})
        page    = ctx.new_page()
        out_file = out_dir / f"{fmt}.png"
        render_slide(page, env, tmpl, variables, out_file, width, height)
        print(f"  [ok] {out_file.name} ({width}x{height})")
        browser.close()

    _write_caption(plan, out_dir, fmt)
    return out_dir


def _write_caption(plan: dict, out_dir: Path, fmt: str = "carousel") -> None:
    parts = [plan.get("caption", "")]
    if fmt == "x" and plan.get("tweet_text"):
        parts = [plan["tweet_text"], "", "--- caption ---", plan.get("caption", "")]
    tags = " ".join(f"#{h}" for h in plan.get("hashtags", []))
    if tags:
        parts.append(tags)
    dm = plan.get("dm_keyword", "")
    if dm:
        parts.append(f"DM '{dm}' for details.")
    (out_dir / "caption.txt").write_text("\n\n".join(p for p in parts if p is not None),
                                         encoding="utf-8")


# Multi-slide formats that render through the carousel pipeline, with their own
# content template. (title/outro fall back to the brand defaults.)
CAROUSEL_FORMATS = {
    "carousel": {},
    "listicle": {"content": "listicle_content.html"},
}


def generate_post(
    plan: dict,
    fmt: str = "carousel",
    image_paths: dict | None = None,
    out_root: Path | None = None,
    brand_key: str | None = None,
) -> Path:
    """Dispatch render by format. image_paths is a dict for carousel, or {0: path}/path for single."""
    if fmt in CAROUSEL_FORMATS:
        plan.setdefault("format", fmt)
        return generate_carousel(plan, image_paths, out_root=out_root,
                                 brand_key=brand_key,
                                 templates=CAROUSEL_FORMATS[fmt] or None)
    img = None
    if isinstance(image_paths, dict):
        img = image_paths.get(0) or image_paths.get("0")
    else:
        img = image_paths
    return generate_single(plan, fmt, image_path=img, out_root=out_root,
                           brand_key=brand_key)


# ── base vars helper (reused by app.py) ─────────────────────────────────────

def _base_vars(plan: dict, image_paths: dict | None = None,
               brand_key: str | None = None) -> dict:
    config   = _load_config()
    brand    = resolve_brand(config, brand_key)
    return {
        "css_path":     _uri(Path("static/brand.css")),
        "logo_path":    _uri(Path(brand.get("logo_path", "static/logo.png"))),
        "author":       brand.get("author", ""),
        "handle":       brand.get("handle", ""),
        "brand":        brand,
        "theme_css":    theme_css(brand),
        "plan":         plan,
        "total_slides": 1 + len(plan.get("content_slides", [])) + 1,
        **_layout_vars(plan),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Render a K2 Press carousel.")
    parser.add_argument("--plan",         help="Path to a saved plan JSON file")
    parser.add_argument("--story-index",  type=int, default=0,
                        help="Story index from sample feed (used when --plan is omitted)")
    parser.add_argument("--no-images",    action="store_true",
                        help="Skip Pexels image fetch (faster, blank backgrounds)")
    parser.add_argument("--config-feeds", action="store_true")
    args = parser.parse_args()

    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    else:
        from feeds import fetch_stories, configured_feed_urls, load_config as fc
        from plan import plan_story
        config = fc()
        urls   = configured_feed_urls(config, use_config_feeds=args.config_feeds)
        if not urls:
            raise SystemExit("No feed URLs found.")
        stories = fetch_stories(urls)
        story   = stories[args.story_index]
        print(f"Story: {story.title}\n")
        print("Planning with Ollama...")
        plan = plan_story(story, config)
        print(json.dumps(plan, indent=2, ensure_ascii=False))

    image_paths: dict = {}
    if not args.no_images:
        from images import fetch_images_for_plan
        print("\nFetching images from Pexels...")
        image_paths = fetch_images_for_plan(plan)

    print("\nRendering slides...")
    out_dir = generate_carousel(plan, image_paths)
    print(f"\nCarousel saved -> {out_dir.resolve()}")
    print(f"Caption        -> {(out_dir / 'caption.txt').resolve()}")


if __name__ == "__main__":
    main()

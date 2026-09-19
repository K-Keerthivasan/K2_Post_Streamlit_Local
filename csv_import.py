"""csv_import.py — turn a spreadsheet of content into carousel posts.

You keep your post ideas in a sheet; this reads that sheet and produces plans the
renderer already understands. Two shapes are supported, auto-detected from the
header row:

WIDE  — one row per post, slides in numbered columns::

    title,subtitle,slide1_heading,slide1_body,slide2_heading,slide2_body,cta,caption,hashtags,image_query
    5 SEO Myths,What actually moves rankings,KEYWORD STUFFING,"— Hurts readability
    — Google ignores it",BACKLINKS ONLY,"— Content still wins",Save this,Full breakdown below,seo marketing,laptop desk

LONG  — one row per slide, rows grouped by a post id::

    post_id,order,heading,body,image_query
    seo-myths,1,KEYWORD STUFFING,— Hurts readability,laptop desk
    seo-myths,2,BACKLINKS ONLY,— Content still wins,office team

Column names are matched loosely (case/spacing/underscores ignored) and common
aliases are accepted, so a sheet exported from Notion/Sheets usually works as-is.

Two build modes:
  direct — the CSV text becomes the slide copy verbatim. No LLM, instant, exact.
  ai     — each row is treated as a brief and the normal planner writes the copy.

CLI:
  python csv_import.py --file content.csv --inspect
  python csv_import.py --template > template.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

# Aliases per logical field. Matching is done on a normalised key (lowercase,
# alphanumerics only), so "Slide 1 Heading" == "slide1_heading" == "SLIDE1HEADING".
_ALIASES: dict[str, tuple[str, ...]] = {
    "title":       ("title", "headline", "posttitle", "hook", "topic", "name"),
    "subtitle":    ("subtitle", "subhead", "subheadline", "tagline", "hooksub"),
    "cta":         ("cta", "calltoaction", "outro", "closing", "endcard"),
    "caption":     ("caption", "postcaption", "description", "copy", "igcaption"),
    "hashtags":    ("hashtags", "tags", "hashtag"),
    "image_query": ("imagequery", "image", "imagesearch", "photo", "visual", "imageprompt"),
    "notes":       ("notes", "brief", "summary", "details", "source", "idea"),
    "format":      ("format", "type", "posttype", "template"),
    "brand":       ("brand", "account", "page"),
    "tone":        ("tone", "voice", "stance"),
    "schedule":    ("schedule", "scheduledat", "date", "publishat", "when", "datetime"),
    "post_id":     ("postid", "group", "groupid", "post", "id", "slug"),
    "order":       ("order", "slide", "slideno", "slidenumber", "seq", "sequence", "position"),
    "heading":     ("heading", "slideheading", "slidetitle", "header"),
    "body":        ("body", "slidebody", "text", "slidetext", "points", "bullets", "content"),
}

# slide1_heading / slide_1_body / s3heading ... -> (index, field)
_SLIDE_RE = re.compile(r"^(?:slide|card|s)(\d+)(heading|title|body|text|image|imagequery)?$")

TEMPLATE_CSV = (
    "title,subtitle,slide1_heading,slide1_body,slide2_heading,slide2_body,"
    "slide3_heading,slide3_body,cta,caption,hashtags,image_query\n"
    "\"5 SEO myths that cost you leads\",\"What actually moves local rankings\","
    "\"KEYWORD STUFFING\",\"— Hurts readability\n— Search engines ignore it\","
    "\"BACKLINKS ONLY\",\"— Relevance beats volume\n— Content still wins\","
    "\"IGNORING SPEED\",\"— Slow pages lose bookings\n— Fix images first\","
    "\"Save this for your next audit\",\"The full breakdown is in the carousel.\","
    "\"seo,localbusiness,marketing\",\"laptop desk office\"\n"
)


class CsvImportError(Exception):
    """Bad file, unusable header, or no usable rows."""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _field_for(header: str) -> str | None:
    key = _norm(header)
    # A column named exactly after a field always wins, so a sheet with both
    # "notes" and "body" columns keeps them apart regardless of alias overlap.
    if key in _ALIASES:
        return key
    for field, names in _ALIASES.items():
        if key in names:
            return field
    return None


def _slide_col(header: str) -> tuple[int, str] | None:
    """('slide2_body') -> (2, 'body'). Bare 'slide2' is treated as its body."""
    m = _SLIDE_RE.match(_norm(header))
    if not m:
        return None
    idx = int(m.group(1))
    part = m.group(2) or "body"
    part = {"title": "heading", "text": "body",
            "image": "image_query", "imagequery": "image_query"}.get(part, part)
    return idx, part


def read_csv(data: bytes | str, *, delimiter: str = "") -> tuple[list[str], list[dict]]:
    """Decode + parse a CSV upload into (headers, rows). Tolerates UTF-8 BOM
    (Excel writes one), CRLF, and semicolon/tab separated exports."""
    if isinstance(data, bytes):
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise CsvImportError("Could not decode the file — save it as UTF-8 CSV.")
    else:
        text = data.lstrip("﻿")

    if not text.strip():
        raise CsvImportError("The file is empty.")

    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
    if not headers:
        raise CsvImportError("No header row found — the first line must name the columns.")

    rows: list[dict] = []
    for raw in reader:
        # Quoted multi-line cells keep the sheet's CRLF; templates only want \n.
        row = {(k or "").strip(): (v or "").replace("\r\n", "\n").replace("\r", "\n").strip()
               for k, v in raw.items() if k and k.strip()}
        if any(row.values()):                 # skip the blank rows sheets leave behind
            rows.append(row)
    if not rows:
        raise CsvImportError("The file has a header but no data rows.")
    return headers, rows


def detect_shape(headers: list[str]) -> str:
    """'long' when the sheet has one row per slide, else 'wide'."""
    fields = {_field_for(h) for h in headers}
    if "post_id" in fields and ("heading" in fields or "body" in fields):
        return "long"
    if any(_slide_col(h) for h in headers):
        return "wide"
    return "wide"


def _get(row: dict, mapping: dict[str, str], field: str, default: str = "") -> str:
    col = mapping.get(field)
    return (row.get(col, "") if col else "") or default


def build_mapping(headers: list[str], overrides: dict | None = None) -> dict[str, str]:
    """logical field -> actual column name, with user overrides winning."""
    mapping: dict[str, str] = {}
    for h in headers:
        field = _field_for(h)
        if field and field not in mapping:
            mapping[field] = h
    for field, col in (overrides or {}).items():
        if col and col in headers:
            mapping[field] = col
    return mapping


def _split_tags(value: str) -> list[str]:
    return [t.strip().lstrip("#") for t in re.split(r"[,\s]+", value or "") if t.strip()]


def _slug(text: str, fallback: str = "post") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40].rstrip("-") or fallback)


def _slides_from_wide(row: dict, headers: list[str]) -> list[dict]:
    """Collect slideN_* columns into ordered content slides."""
    buckets: dict[int, dict] = {}
    for h in headers:
        sc = _slide_col(h)
        if not sc:
            continue
        idx, part = sc
        val = (row.get(h) or "").strip()
        if val:
            buckets.setdefault(idx, {})[part] = val
    return [buckets[i] for i in sorted(buckets) if buckets[i]]


def to_posts(headers: list[str], rows: list[dict], *,
             mapping: dict[str, str] | None = None,
             shape: str = "") -> list[dict]:
    """Normalise a parsed sheet into per-post dicts:

        {title, subtitle, cta, caption, hashtags[], image_query, notes,
         format, brand, tone, schedule, slides:[{heading, body, image_query}]}
    """
    mapping = mapping or build_mapping(headers)
    shape = shape or detect_shape(headers)
    posts: list[dict] = []

    if shape == "long":
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        for row in rows:
            gid = _get(row, mapping, "post_id") or _get(row, mapping, "title") or "post"
            if gid not in groups:
                groups[gid] = []
                order.append(gid)
            groups[gid].append(row)
        for gid in order:
            grp = groups[gid]

            def _sort_key(r: dict) -> tuple[int, int]:
                raw = _get(r, mapping, "order")
                try:
                    return (0, int(float(raw)))
                except (TypeError, ValueError):
                    return (1, grp.index(r))

            grp = sorted(grp, key=_sort_key)
            head = grp[0]
            slides = [{
                "heading": _get(r, mapping, "heading"),
                "body": _get(r, mapping, "body"),
                "image_query": _get(r, mapping, "image_query"),
            } for r in grp if _get(r, mapping, "heading") or _get(r, mapping, "body")]
            posts.append(_finish_post(head, mapping, slides, gid))
    else:
        for row in rows:
            slides = _slides_from_wide(row, headers)
            posts.append(_finish_post(row, mapping, slides, ""))

    return [p for p in posts if p["title"] or p["slides"] or p["notes"]]


def _finish_post(row: dict, mapping: dict[str, str], slides: list[dict],
                 gid: str) -> dict:
    title = _get(row, mapping, "title") or gid
    # A wide sheet with no slide columns but a body/content column is a brief,
    # not slide copy — feed it to the planner as notes.
    notes = _get(row, mapping, "notes") or ("" if slides else _get(row, mapping, "body"))
    return {
        "title": title,
        "subtitle": _get(row, mapping, "subtitle"),
        "cta": _get(row, mapping, "cta"),
        "caption": _get(row, mapping, "caption"),
        "hashtags": _split_tags(_get(row, mapping, "hashtags")),
        "image_query": _get(row, mapping, "image_query"),
        "notes": notes,
        "format": (_get(row, mapping, "format") or "").lower(),
        "brand": _get(row, mapping, "brand"),
        "tone": _get(row, mapping, "tone"),
        "schedule": _get(row, mapping, "schedule"),
        "slug": _slug(title, _slug(gid)),
        "slides": [{"heading": s.get("heading", ""), "body": s.get("body", ""),
                    "image_query": s.get("image_query", "")} for s in slides],
    }


def to_plan(post: dict, brand: dict, *, fmt: str = "carousel") -> dict:
    """Build a render-ready plan straight from CSV text (the 'direct' mode).

    Deliberately skips the LLM: when someone has already written the copy in a
    sheet, rewriting it is a bug, not a feature."""
    handle = brand.get("handle", "@handle")
    slides = post["slides"] or [{"heading": "", "body": post.get("notes", ""),
                                 "image_query": post.get("image_query", "")}]
    content = [{
        "heading": (s["heading"] or "").upper(),
        "body": s["body"],
        "image_query": s["image_query"] or post.get("image_query", ""),
    } for s in slides]

    if fmt in ("carousel", "listicle"):
        return {
            "format": fmt,
            "slug": post["slug"],
            "slide_count": len(content) + 2,
            "title_card": {"headline": post["title"],
                           "subhead": post["subtitle"] or (content[0]["body"][:90] if content else "")},
            "content_slides": content,
            "outro_card": {"cta": post["cta"] or "Follow for more", "handle": handle},
            "caption": post["caption"] or post["title"],
            "hashtags": post["hashtags"] or brand.get("hashtags", []),
            "image_query": post["image_query"] or (content[0]["image_query"] if content else ""),
            "platform": "instagram",
            "content_type": "general",
            "origin": "csv",
        }

    # Single-card formats reuse the same copy in the shape those templates expect.
    body = content[0]["body"] if content else post.get("notes", "")
    return {
        "format": fmt,
        "slug": post["slug"],
        "headline": post["title"],
        "subhead": post["subtitle"],
        "body": body,
        "cta": post["cta"] or "Follow for more",
        "handle": handle,
        "caption": post["caption"] or post["title"],
        "hashtags": post["hashtags"] or brand.get("hashtags", []),
        "image_query": post["image_query"],
        "platform": "instagram",
        "content_type": "general",
        "origin": "csv",
    }


def to_story(post: dict) -> dict:
    """A CSV row as a feeds.Story-shaped dict, for the 'ai' build mode where the
    normal planner writes the copy from the row as a brief."""
    notes = post.get("notes", "")
    if not notes:
        parts = [post.get("subtitle", "")] + [
            f"{s['heading']}: {s['body']}".strip(": ") for s in post["slides"]]
        notes = "\n".join(p for p in parts if p)
    return {
        "title": post["title"],
        "summary": notes or post["title"],
        "url": "",
        "source": "CSV import",
        "published": "",
        "image": "",
    }


def inspect(headers: list[str], rows: list[dict]) -> dict:
    """What the UI shows before generating: detected shape, mapping, preview."""
    mapping = build_mapping(headers)
    shape = detect_shape(headers)
    posts = to_posts(headers, rows, mapping=mapping, shape=shape)
    unmapped = [h for h in headers
                if _field_for(h) is None and _slide_col(h) is None]
    return {
        "shape": shape,
        "headers": headers,
        "mapping": mapping,
        "unmapped": unmapped,
        "row_count": len(rows),
        "post_count": len(posts),
        "posts": posts,
        "fields": sorted(_ALIASES),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inspect a content CSV.")
    ap.add_argument("--file", help="CSV to read")
    ap.add_argument("--inspect", action="store_true", help="print the parsed posts")
    ap.add_argument("--template", action="store_true", help="print a starter CSV")
    args = ap.parse_args(argv)

    if args.template:
        print(TEMPLATE_CSV)
        return 0
    if not args.file:
        ap.error("--file is required (or use --template)")
    try:
        headers, rows = read_csv(Path(args.file).read_bytes())
        import json
        print(json.dumps(inspect(headers, rows), indent=2, ensure_ascii=False))
        return 0
    except CsvImportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

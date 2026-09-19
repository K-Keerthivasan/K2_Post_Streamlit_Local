"""meta.py — direct Instagram + Facebook publisher (Meta Graph API).

Replaces the Postiz adapter. This module talks to Meta's Graph API with your own
credentials — no third-party scheduler in the middle — so what you approve is
exactly what gets posted.

It renders nothing. Rendering stays in render.py. It receives finished PNGs, a
caption, and a content type, then publishes them to the Instagram Business
account and/or Facebook Page mapped to the post's brand.

Graph API facts this module relies on:
  Base            https://graph.facebook.com/v21.0
  IG container    POST /{ig_user_id}/media            image_url + caption
  IG carousel     POST /{ig_user_id}/media            media_type=CAROUSEL&children=..
  IG story        POST /{ig_user_id}/media            media_type=STORIES
  IG publish      POST /{ig_user_id}/media_publish    creation_id
  IG quota        GET  /{ig_user_id}/content_publishing_limit   (25 posts / 24h)
  FB photo        POST /{page_id}/photos              url= or multipart source=
  FB multi-photo  POST /{page_id}/feed                attached_media=[{media_fbid}]
  Token check     GET  /debug_token
  Long-lived tok  GET  /oauth/access_token?grant_type=fb_exchange_token

IMPORTANT — Instagram publishing needs a PUBLIC image URL. Meta's servers fetch
the bytes themselves; they cannot see localhost. Set PUBLIC_BASE_URL in .env to a
publicly reachable https base (a Tailscale Serve / Funnel URL, a tunnel, a CDN)
that serves this app's /outputs directory. Facebook does not need this — photos
are uploaded as multipart bytes.

CLI (engine without the UI):
  python meta.py --verify                       # token + account sanity check
  python meta.py --list-accounts                # brands -> IG/FB ids from config
  python meta.py --quota --brand k2             # IG posts left in the 24h window
  python meta.py --type carousel --brand k2 --asset a.png --asset b.png --caption cap.txt
  python meta.py --type post --brand k2 --asset card.png --caption "Hello" --targets facebook
  python meta.py --type story --brand k2 --asset card.png --dry-run
  python meta.py --exchange-token <short-lived-fb-token>     # META_APP_ID/SECRET
  python meta.py --exchange-ig-token <short-lived-ig-token>  # INSTAGRAM_APP_*
  python meta.py --refresh-ig-token <long-lived-ig-token>    # extend by ~60 days
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path("config.yaml")
RATE_STATE = Path("library/meta_rate.json")

# Instagram Login lives on its own host. It is the flow behind the Meta app
# dashboard's "Instagram app ID / Instagram app secret" pair, and its token
# endpoints are unversioned and separate from graph.facebook.com.
IG_LOGIN_BASE = "https://graph.instagram.com"

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}

# Instagram's published image rules. Breaking one of these is the most common
# reason a container is accepted and then fails at publish time with a vague
# message, so preflight() checks them locally before anything is sent.
IG_MIN_WIDTH = 320                  # narrower is rejected outright
IG_RECOMMENDED_MAX_WIDTH = 1440     # wider is downscaled by Meta (warning only)
IG_MAX_BYTES = 8 * 1024 * 1024      # 8 MB per image
IG_ASPECT_MIN = 0.80                # 4:5 portrait
IG_ASPECT_MAX = 1.91                # 1.91:1 landscape
IG_MAX_HASHTAGS = 30

# Meta's own ceilings, enforced client-side so a bad call fails fast and local.
IG_CAROUSEL_MIN = 2
IG_CAROUSEL_MAX = 10
IG_CAPTION_MAX = 2200
IG_DAILY_POSTS = 25

# How long to wait for Meta to finish ingesting an image container.
CONTAINER_POLL_TRIES = 20
CONTAINER_POLL_DELAY = 3.0


class MetaError(Exception):
    """Validation, config, credential, or Graph API failure (clean CLI exit)."""


# -- Config --------------------------------------------------------------------

_DEFAULTS = {
    "api_version": "v21.0",
    "token_env": "META_ACCESS_TOKEN",
    "accounts": {},
    "publish": {
        "default_targets": ["instagram"],
        "on_approve": "hold",       # hold | now  (hold = wait for schedule/publish)
        "allow_now": True,
    },
    "rate_limit": {
        "max_posts_per_day": IG_DAILY_POSTS,
        "safety_margin": 2,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(config: dict | None = None) -> dict:
    """Read the ``meta:`` block from config.yaml (or a passed-in config), layered
    over sane defaults so a missing key never crashes.

    Accepts either the whole config or an already-resolved meta block, because
    functions pass their resolved cfg down to each other and unwrapping it twice
    silently yields the empty defaults - i.e. "no accounts configured" on a
    machine that has them."""
    if isinstance(config, dict) and "accounts" in config and "meta" not in config:
        return _merge(_DEFAULTS, config)
    if config is None:
        try:
            config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            config = {}
    return _merge(_DEFAULTS, (config or {}).get("meta", {}) or {})


def account_for(brand: str, cfg: dict) -> dict:
    """Resolve a brand key to its {ig_user_id, fb_page_id, token_env, targets}.

    An unmapped brand is a config error, not a silent no-op — publishing to the
    wrong account is worse than failing loudly."""
    accounts = cfg.get("accounts") or {}
    acct = accounts.get(brand)
    if not acct:
        known = ", ".join(sorted(accounts)) or "(none configured)"
        raise MetaError(
            f"No Meta account mapped for brand '{brand}'. "
            f"Add it under meta.accounts in config.yaml. Known brands: {known}"
        )
    return dict(acct)


def token_for(acct: dict, cfg: dict) -> str:
    """The access token for an account: its own token_env, else the global one.

    Tokens live in the environment only — never in config.yaml, which is shared
    and (for the example file) committed."""
    env_name = (acct.get("token_env") or cfg.get("token_env") or "META_ACCESS_TOKEN").strip()
    token = os.environ.get(env_name, "").strip()
    if not token:
        raise MetaError(f"{env_name} is not set — put your Meta access token in .env")
    return token


def app_credentials() -> tuple[str, str, str]:
    """The app id/secret pair used for token exchange, plus which app it came from.

    A Meta app hands out two different pairs and they are not interchangeable:
    the Facebook app id/secret (App settings -> Basic) drives the
    graph.facebook.com exchange, while the Instagram product's "Instagram app ID
    / Instagram app secret" (Instagram -> API setup) drives graph.instagram.com.
    Both live in .env; META_* wins when both are filled in, because the publish
    path in this module is the Facebook one.
    """
    meta_id = os.environ.get("META_APP_ID", "").strip()
    meta_secret = os.environ.get("META_APP_SECRET", "").strip()
    if meta_id and meta_secret:
        return meta_id, meta_secret, "facebook"
    ig_id = os.environ.get("INSTAGRAM_APP_ID", "").strip()
    ig_secret = os.environ.get("INSTAGRAM_APP_SECRET", "").strip()
    if ig_id and ig_secret:
        return ig_id, ig_secret, "instagram"
    raise MetaError(
        "No app credentials in .env - set META_APP_ID/META_APP_SECRET (Facebook "
        "app) or INSTAGRAM_APP_ID/INSTAGRAM_APP_SECRET (Instagram product)."
    )


def app_credentials_status() -> dict:
    """Which app pairs are present, for the UI's setup panel (no network call)."""
    fb_ok = bool(os.environ.get("META_APP_ID", "").strip()
                 and os.environ.get("META_APP_SECRET", "").strip())
    ig_ok = bool(os.environ.get("INSTAGRAM_APP_ID", "").strip()
                 and os.environ.get("INSTAGRAM_APP_SECRET", "").strip())
    return {"facebook_app": fb_ok, "instagram_app": ig_ok, "any": fb_ok or ig_ok}


def _base(cfg: dict) -> str:
    return f"https://graph.facebook.com/{cfg.get('api_version', 'v21.0')}"


def account_base(acct: dict, cfg: dict) -> str:
    """The API host for one account.

    Which host serves a token depends on how the token was minted, not on
    preference: tokens from Facebook Login live on graph.facebook.com, tokens
    from Instagram Login live on graph.instagram.com, and each host rejects the
    other's tokens. Set ``login: instagram`` on the account in config.yaml when
    the token came from the Instagram business login flow."""
    if str(acct.get("login") or "facebook").lower().startswith("instagram"):
        return f"{IG_LOGIN_BASE}/{cfg.get('api_version', 'v21.0')}"
    return _base(cfg)


def configured(cfg: dict | None = None) -> bool:
    """True when at least one account is mapped and its token is present."""
    cfg = cfg if cfg and "accounts" in cfg else load_config(cfg)
    for acct in (cfg.get("accounts") or {}).values():
        env_name = (acct.get("token_env") or cfg.get("token_env") or "META_ACCESS_TOKEN")
        if os.environ.get(env_name, "").strip():
            return True
    return False


# -- Rate limiting (local, mirrors Meta's 25 posts / 24h IG ceiling) ------------

def _rate_load() -> dict:
    try:
        return json.loads(RATE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _rate_note(key: str) -> None:
    """Record one published post for an account, keeping a 24h rolling window."""
    state = _rate_load()
    cutoff = time.time() - 86400
    stamps = [t for t in state.get(key, []) if t > cutoff]
    stamps.append(time.time())
    state[key] = stamps
    RATE_STATE.parent.mkdir(parents=True, exist_ok=True)
    RATE_STATE.write_text(json.dumps(state), encoding="utf-8")


def _rate_check(key: str, cfg: dict) -> None:
    rl = cfg.get("rate_limit", {})
    ceiling = int(rl.get("max_posts_per_day", IG_DAILY_POSTS)) - int(rl.get("safety_margin", 2))
    cutoff = time.time() - 86400
    stamps = [t for t in _rate_load().get(key, []) if t > cutoff]
    if len(stamps) >= max(1, ceiling):
        oldest = datetime.fromtimestamp(min(stamps)) + timedelta(days=1)
        raise MetaError(
            f"Local rate guard: {len(stamps)} posts published to this account in the "
            f"last 24h (ceiling {ceiling}). Next slot ~{oldest:%Y-%m-%d %H:%M}."
        )


# -- Graph API plumbing --------------------------------------------------------

def _graph(method: str, path: str, cfg: dict, *, token: str,
           params: dict | None = None, files: dict | None = None,
           timeout: int = 60, base: str = "") -> dict:
    """One Graph API call. Raises MetaError carrying Meta's own message on
    failure — those messages ("The image URL is not accessible", "media type not
    supported") are the useful half of debugging a failed publish."""
    url = f"{base or _base(cfg)}/{path.lstrip('/')}"
    payload = dict(params or {})
    payload["access_token"] = token
    try:
        if method.upper() == "GET":
            r = requests.get(url, params=payload, timeout=timeout)
        else:
            r = requests.post(url, data=payload, files=files, timeout=timeout)
    except requests.RequestException as e:
        raise MetaError(f"Graph API unreachable: {e}") from e

    try:
        data = r.json()
    except ValueError:
        raise MetaError(f"Graph API returned non-JSON ({r.status_code}): {r.text[:200]}")

    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        msg = err.get("error_user_msg") or err.get("message") or r.text[:300]
        code = err.get("code")
        sub = err.get("error_subcode")
        detail = f"Meta API error {code}" + (f"/{sub}" if sub else "") + f": {msg}"
        raise MetaError(detail)
    return data


def _ig_login(path: str, params: dict, timeout: int = 30) -> dict:
    """One call to graph.instagram.com - the Instagram Login token endpoints.

    Kept apart from _graph() because this host is unversioned, takes the app
    secret in the query string, and reports errors in its own shape."""
    url = f"{IG_LOGIN_BASE}/{path.lstrip('/')}"
    try:
        r = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise MetaError(f"graph.instagram.com unreachable: {e}") from e
    try:
        data = r.json()
    except ValueError:
        raise MetaError(
            f"graph.instagram.com returned non-JSON ({r.status_code}): {r.text[:200]}")
    if r.status_code >= 400 or "error" in data or "error_message" in data:
        err = data.get("error") or {}
        msg = err.get("message") or data.get("error_message") or r.text[:300]
        raise MetaError(f"Instagram API error: {msg}")
    return data


# -- Assets --------------------------------------------------------------------

def _validate_assets(assets: list[str]) -> list[Path]:
    paths = []
    for a in assets:
        p = Path(a)
        if not p.exists():
            raise MetaError(f"Asset not found: {p}")
        if p.suffix.lower() not in IMAGE_EXTS:
            raise MetaError(
                f"Unsupported asset '{p.name}'. Instagram accepts JPEG/PNG images "
                f"(this build publishes images only)."
            )
        paths.append(p)
    return paths


def public_base() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def _require_public_urls(asset_urls: list[str]) -> list[str]:
    """Instagram fetches the bytes itself, so localhost URLs can never work."""
    if not asset_urls:
        raise MetaError(
            "Instagram publishing needs public image URLs. Set PUBLIC_BASE_URL in "
            ".env to an https address that serves this app's /outputs folder "
            "(e.g. a Tailscale Funnel URL), then republish."
        )
    bad = [u for u in asset_urls
           if u.startswith("http://localhost") or u.startswith("http://127.0.0.1")]
    if bad:
        raise MetaError(
            f"PUBLIC_BASE_URL points at localhost ({bad[0]}). Meta's servers must be "
            f"able to fetch the image — use a publicly reachable https URL."
        )
    return asset_urls


def _clean_caption(caption: str, hashtags: list[str] | None = None) -> str:
    text = (caption or "").strip()
    if hashtags:
        tags = " ".join(t if t.startswith("#") else f"#{t}" for t in hashtags)
        text = f"{text}\n\n{tags}".strip()
    if len(text) > IG_CAPTION_MAX:
        text = text[:IG_CAPTION_MAX - 1].rstrip() + "…"
    return text


# -- Instagram -----------------------------------------------------------------

def _ig_container(ig_id: str, cfg: dict, token: str, params: dict,
                  base: str = "") -> str:
    return str(_graph("POST", f"{ig_id}/media", cfg, token=token, params=params,
                      base=base)["id"])


def _ig_wait_ready(container_id: str, cfg: dict, token: str, base: str = "") -> None:
    """Poll a container until Meta finishes downloading/validating the image.

    Publishing a container that is still IN_PROGRESS fails with an unhelpful
    message — so wait for FINISHED and surface ERROR reasons directly."""
    for _ in range(CONTAINER_POLL_TRIES):
        data = _graph("GET", container_id, cfg, token=token,
                      params={"fields": "status_code,status"}, base=base)
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise MetaError(f"Meta rejected the image: {data.get('status') or 'unknown error'}")
        time.sleep(CONTAINER_POLL_DELAY)
    raise MetaError("Timed out waiting for Instagram to ingest the image container.")


def _ig_publish_container(ig_id: str, container_id: str, cfg: dict, token: str,
                          base: str = "") -> str:
    res = _graph("POST", f"{ig_id}/media_publish", cfg, token=token,
                 params={"creation_id": container_id}, base=base)
    return str(res.get("id", ""))


def publish_instagram(*, ig_id: str, token: str, cfg: dict, ptype: str,
                      asset_urls: list[str], caption: str, base: str = "") -> dict:
    """Publish images to an Instagram Business account and return its media id."""
    _require_public_urls(asset_urls)
    _rate_check(f"ig:{ig_id}", cfg)

    if ptype == "carousel":
        if not (IG_CAROUSEL_MIN <= len(asset_urls) <= IG_CAROUSEL_MAX):
            raise MetaError(
                f"Instagram carousels take {IG_CAROUSEL_MIN}-{IG_CAROUSEL_MAX} images "
                f"(got {len(asset_urls)})."
            )
        children = []
        for url in asset_urls:
            cid = _ig_container(ig_id, cfg, token,
                                {"image_url": url, "is_carousel_item": "true"}, base)
            _ig_wait_ready(cid, cfg, token, base)
            children.append(cid)
        parent = _ig_container(ig_id, cfg, token, {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
        }, base)
        _ig_wait_ready(parent, cfg, token, base)
        media_id = _ig_publish_container(ig_id, parent, cfg, token, base)
    elif ptype == "story":
        cid = _ig_container(ig_id, cfg, token,
                            {"image_url": asset_urls[0], "media_type": "STORIES"}, base)
        _ig_wait_ready(cid, cfg, token, base)
        media_id = _ig_publish_container(ig_id, cid, cfg, token, base)
    else:                                   # single feed image
        cid = _ig_container(ig_id, cfg, token,
                            {"image_url": asset_urls[0], "caption": caption}, base)
        _ig_wait_ready(cid, cfg, token, base)
        media_id = _ig_publish_container(ig_id, cid, cfg, token, base)

    _rate_note(f"ig:{ig_id}")
    permalink = ""
    try:
        permalink = _graph("GET", media_id, cfg, token=token,
                           params={"fields": "permalink"}, base=base).get("permalink", "")
    except MetaError:
        pass                                # a missing permalink never fails a publish
    return {"platform": "instagram", "media_id": media_id, "permalink": permalink,
            "post_type": ptype}


def ig_quota(ig_id: str, cfg: dict, token: str, base: str = "") -> dict:
    """Meta's own view of the 24h publishing quota for this account."""
    data = _graph("GET", f"{ig_id}/content_publishing_limit", cfg, token=token,
                  params={"fields": "config,quota_usage"}, base=base)
    row = (data.get("data") or [{}])[0]
    quota = (row.get("config") or {}).get("quota_total", IG_DAILY_POSTS)
    used = row.get("quota_usage", 0)
    return {"used": used, "total": quota, "remaining": max(0, quota - used)}


# -- Facebook Pages ------------------------------------------------------------

def _fb_upload_photo(page_id: str, cfg: dict, token: str, *, path: Path | None = None,
                     url: str = "", published: bool = True,
                     caption: str = "") -> str:
    """Upload one photo to a Page. Unpublished uploads become multi-photo children."""
    params: dict = {"published": "true" if published else "false"}
    if caption:
        params["caption"] = caption
    if url:
        params["url"] = url
        return str(_graph("POST", f"{page_id}/photos", cfg, token=token,
                          params=params)["id"])
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    with path.open("rb") as fh:
        return str(_graph("POST", f"{page_id}/photos", cfg, token=token,
                          params=params, files={"source": (path.name, fh, mime)})["id"])


def publish_facebook(*, page_id: str, token: str, cfg: dict, ptype: str,
                     assets: list[Path], asset_urls: list[str], caption: str,
                     scheduled_unix: int | None = None) -> dict:
    """Publish to a Facebook Page. Uses multipart bytes when local files are
    available so Facebook works even without PUBLIC_BASE_URL; falls back to
    public URLs otherwise."""
    _rate_check(f"fb:{page_id}", cfg)
    use_urls = bool(asset_urls) and not assets

    def _upload(i: int, published: bool, cap: str = "") -> str:
        if use_urls:
            return _fb_upload_photo(page_id, cfg, token, url=asset_urls[i],
                                    published=published, caption=cap)
        return _fb_upload_photo(page_id, cfg, token, path=assets[i],
                                published=published, caption=cap)

    n = len(asset_urls) if use_urls else len(assets)
    if not n:
        raise MetaError("No assets to publish to Facebook.")

    if n == 1 and not scheduled_unix:
        post_id = _upload(0, True, caption)
    else:
        # Multi-photo (and every scheduled post) goes through /feed so the whole
        # set lands as one story with one message.
        media = [{"media_fbid": _upload(i, False)} for i in range(n)]
        params = {"message": caption, "attached_media": json.dumps(media)}
        if scheduled_unix:
            params["published"] = "false"
            params["scheduled_publish_time"] = str(scheduled_unix)
        post_id = str(_graph("POST", f"{page_id}/feed", cfg, token=token,
                             params=params)["id"])

    _rate_note(f"fb:{page_id}")
    return {"platform": "facebook", "media_id": post_id, "post_type": ptype,
            "permalink": f"https://www.facebook.com/{post_id.replace('_', '/posts/')}"}


# -- Public entry point --------------------------------------------------------

def publish(*, brand: str, ptype: str = "post", assets: list[str] | None = None,
            asset_urls: list[str] | None = None, caption: str = "",
            hashtags: list[str] | None = None, targets: list[str] | None = None,
            scheduled_unix: int | None = None, config: dict | None = None,
            dry_run: bool = False) -> dict:
    """Publish one finished post to every configured target for ``brand``.

    Returns {"ok": bool, "results": [...], "errors": [...]} — a per-target report,
    because Instagram succeeding while Facebook fails is a normal outcome and the
    UI needs to show both."""
    cfg = load_config(config)
    acct = account_for(brand, cfg)
    token = token_for(acct, cfg)
    targets = [t.lower() for t in (targets or acct.get("targets")
                                   or cfg["publish"].get("default_targets") or ["instagram"])]

    paths = _validate_assets(assets or [])
    urls = list(asset_urls or [])
    if not paths and not urls:
        raise MetaError("Nothing to publish — no assets given.")
    text = _clean_caption(caption, hashtags)

    ptype = (ptype or "post").lower()
    if ptype not in ("post", "carousel", "story"):
        raise MetaError(f"Unknown post type '{ptype}' (post | carousel | story).")

    if dry_run:
        return {"ok": True, "dry_run": True, "brand": brand, "targets": targets,
                "type": ptype, "assets": [p.name for p in paths] or urls,
                "caption_chars": len(text)}

    results: list[dict] = []
    errors: list[dict] = []
    for target in targets:
        try:
            if target == "instagram":
                ig_id = str(acct.get("ig_user_id") or "").strip()
                if not ig_id:
                    raise MetaError(f"Brand '{brand}' has no ig_user_id in config.yaml.")
                results.append(publish_instagram(
                    ig_id=ig_id, token=token, cfg=cfg, ptype=ptype,
                    asset_urls=urls, caption=text, base=account_base(acct, cfg)))
            elif target == "facebook":
                page_id = str(acct.get("fb_page_id") or "").strip()
                if not page_id:
                    raise MetaError(f"Brand '{brand}' has no fb_page_id in config.yaml.")
                if ptype == "story":
                    raise MetaError("Facebook Page stories are not supported by this build.")
                fb_token = os.environ.get(acct.get("fb_token_env", ""), "").strip() or token
                results.append(publish_facebook(
                    page_id=page_id, token=fb_token, cfg=cfg, ptype=ptype,
                    assets=paths, asset_urls=urls, caption=text,
                    scheduled_unix=scheduled_unix))
            else:
                raise MetaError(f"Unknown target '{target}' (instagram | facebook).")
        except MetaError as e:
            errors.append({"platform": target, "error": str(e)})

    return {"ok": bool(results) and not errors, "brand": brand, "type": ptype,
            "results": results, "errors": errors}


# -- Preflight -----------------------------------------------------------------
# Everything that must be true before a post can reach Instagram, checked
# locally (and optionally against the network) so a failure names its own cause
# instead of surfacing as a silent no-op an hour after the slot passed.

def _check(name: str, ok: bool, detail: str, level: str = "block") -> dict:
    return {"name": name, "ok": bool(ok), "level": level,
            "detail": detail if not ok else ""}


def _image_facts(path: Path) -> dict:
    """Width/height/bytes for one asset, or an error we can report as a check."""
    facts = {"bytes": path.stat().st_size, "width": 0, "height": 0, "error": ""}
    try:
        from PIL import Image
        with Image.open(path) as im:
            facts["width"], facts["height"] = im.size
    except Exception as e:
        facts["error"] = str(e)
    return facts


def preflight(brand: str, *, ptype: str = "post", assets: list[str] | None = None,
              asset_urls: list[str] | None = None, caption: str = "",
              hashtags: list[str] | None = None, targets: list[str] | None = None,
              config: dict | None = None, network: bool = True) -> dict:
    """Check every publishing constraint for one post and report each verdict.

    Returns {"ok", "blocking", "checks": [{name, ok, level, detail}]}. ``level``
    is "block" (publishing cannot succeed) or "warn" (it will work, but Meta will
    alter the result). Set network=False to skip the calls that leave the
    machine — the local checks alone catch most misconfiguration."""
    cfg = load_config(config)
    checks: list[dict] = []

    # -- account + credentials --------------------------------------------------
    acct: dict = {}
    token = ""
    try:
        acct = account_for(brand, cfg)
        checks.append(_check("account.mapped", True, ""))
    except MetaError as e:
        checks.append(_check("account.mapped", False, str(e)))
    try:
        if acct:
            token = token_for(acct, cfg)
        checks.append(_check("token.present", bool(token),
                             "No access token in .env. An app ID and app secret are "
                             "not a token - mint one (see --ig-login-url) and put it "
                             "in META_ACCESS_TOKEN."))
    except MetaError as e:
        checks.append(_check("token.present", False, str(e)))

    tgts = [t.lower() for t in (targets or acct.get("targets")
                                or cfg["publish"].get("default_targets") or ["instagram"])]
    want_ig = "instagram" in tgts
    want_fb = "facebook" in tgts
    checks.append(_check("targets.known", all(t in ("instagram", "facebook") for t in tgts),
                         f"Unknown target in {tgts} (instagram | facebook)."))
    if want_ig:
        checks.append(_check("account.ig_user_id", bool(str(acct.get("ig_user_id") or "").strip()),
                             f"meta.accounts.{brand}.ig_user_id is empty in config.yaml. "
                             f"It is the numeric Instagram Business account id (17841...), "
                             f"not the @handle."))
    if want_fb:
        checks.append(_check("account.fb_page_id", bool(str(acct.get("fb_page_id") or "").strip()),
                             f"meta.accounts.{brand}.fb_page_id is empty in config.yaml."))

    # -- public URL (Instagram fetches the bytes itself) ------------------------
    base = public_base()
    if want_ig:
        checks.append(_check("public_url.set", bool(base),
                             "PUBLIC_BASE_URL is empty. Instagram downloads the image "
                             "from a public address; localhost can never work."))
        if base:
            checks.append(_check("public_url.https", base.startswith("https://"),
                                 f"PUBLIC_BASE_URL is {base} - Meta requires https."))
            checks.append(_check("public_url.public",
                                 not any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0")),
                                 f"PUBLIC_BASE_URL points at this machine ({base}); "
                                 f"Meta's servers cannot reach it."))

    # -- assets -----------------------------------------------------------------
    paths: list[Path] = []
    try:
        paths = _validate_assets(assets or [])
        checks.append(_check("assets.exist", True, ""))
    except MetaError as e:
        checks.append(_check("assets.exist", False, str(e)))

    urls = list(asset_urls or [])
    count = len(paths) or len(urls)
    if ptype == "carousel":
        checks.append(_check("assets.count", IG_CAROUSEL_MIN <= count <= IG_CAROUSEL_MAX,
                             f"A carousel takes {IG_CAROUSEL_MIN}-{IG_CAROUSEL_MAX} "
                             f"images; this post has {count}."))
    else:
        checks.append(_check("assets.count", count >= 1, "No image to publish."))

    for path in paths:
        f = _image_facts(path)
        tag = path.name
        if f["error"]:
            checks.append(_check(f"asset.readable[{tag}]", False,
                                 f"Could not read the image: {f['error']}"))
            continue
        checks.append(_check(f"asset.size[{tag}]", f["bytes"] <= IG_MAX_BYTES,
                             f"{f['bytes'] / 1048576:.1f} MB exceeds Instagram's "
                             f"{IG_MAX_BYTES // 1048576} MB limit."))
        checks.append(_check(f"asset.width[{tag}]", f["width"] >= IG_MIN_WIDTH,
                             f"{f['width']}px wide; Instagram needs at least "
                             f"{IG_MIN_WIDTH}px."))
        if f["width"] > IG_RECOMMENDED_MAX_WIDTH:
            checks.append(_check(f"asset.width_max[{tag}]", False,
                                 f"{f['width']}px will be downscaled to "
                                 f"{IG_RECOMMENDED_MAX_WIDTH}px by Meta.", "warn"))
        ratio = (f["width"] / f["height"]) if f["height"] else 0
        checks.append(_check(f"asset.aspect[{tag}]", IG_ASPECT_MIN <= ratio <= IG_ASPECT_MAX,
                             f"aspect ratio {ratio:.2f} is outside Instagram's "
                             f"{IG_ASPECT_MIN}-{IG_ASPECT_MAX} range "
                             f"({f['width']}x{f['height']})."))

    # -- caption ----------------------------------------------------------------
    text = _clean_caption(caption, hashtags)
    checks.append(_check("caption.present", bool(text.strip()),
                         "Caption is empty.", "warn"))
    checks.append(_check("caption.length", len(text) <= IG_CAPTION_MAX,
                         f"{len(text)} characters; Instagram truncates above "
                         f"{IG_CAPTION_MAX}.", "warn"))
    tags = text.count("#")
    checks.append(_check("caption.hashtags", tags <= IG_MAX_HASHTAGS,
                         f"{tags} hashtags; Instagram allows {IG_MAX_HASHTAGS}."))

    # -- local rate guard -------------------------------------------------------
    if want_ig and acct.get("ig_user_id"):
        try:
            _rate_check(f"ig:{acct['ig_user_id']}", cfg)
            checks.append(_check("rate.local", True, ""))
        except MetaError as e:
            checks.append(_check("rate.local", False, str(e)))

    # -- network: can Meta actually fetch the image, and is the token live? -----
    if network and want_ig and urls:
        url = urls[0]
        try:
            r = requests.head(url, timeout=15, allow_redirects=True)
            if r.status_code == 405 or (r.status_code >= 400 and r.status_code != 404):
                r = requests.get(url, timeout=15, stream=True)
            ctype = r.headers.get("content-type", "")
            checks.append(_check("public_url.reachable", r.status_code == 200,
                                 f"GET {url} returned {r.status_code}. Meta will get the "
                                 f"same response and refuse the image."))
            checks.append(_check("public_url.image", ctype.startswith("image/"),
                                 f"{url} served content-type '{ctype}' rather than an image."))
        except Exception as e:
            checks.append(_check("public_url.reachable", False,
                                 f"Could not fetch {url}: {e}"))

    if network and token and acct.get("ig_user_id"):
        try:
            info = _graph("GET", str(acct["ig_user_id"]), cfg, token=token,
                          params={"fields": "username"}, base=account_base(acct, cfg))
            checks.append(_check("token.works", True, ""))
            checks.append(_check("account.reachable", bool(info.get("username")),
                                 "The token is valid but returned no username for this id."))
        except MetaError as e:
            checks.append(_check("token.works", False, str(e)))

    blocking = [c for c in checks if not c["ok"] and c["level"] == "block"]
    return {"ok": not blocking, "brand": brand, "type": ptype,
            "blocking": [c["name"] for c in blocking], "checks": checks}


def preflight_text(report: dict) -> str:
    """The preflight report as terminal lines - one per check, failures last."""
    icon = {"block": "FAIL", "warn": "WARN"}
    lines = [f"preflight: brand={report.get('brand')} type={report.get('type')} "
             f"-> {'READY' if report.get('ok') else 'BLOCKED'}"]
    for c in report.get("checks", []):
        mark = "  ok  " if c["ok"] else f" {icon.get(c['level'], 'FAIL')} "
        lines.append(f"[{mark}] {c['name']}" + (f" - {c['detail']}" if c["detail"] else ""))
    return "\n".join(lines)


# -- Diagnostics ---------------------------------------------------------------

def verify(brand: str = "", config: dict | None = None) -> dict:
    """Check the token and every mapped account so setup problems surface here
    rather than halfway through a publish."""
    cfg = load_config(config)
    accounts = cfg.get("accounts") or {}
    keys = [brand] if brand else sorted(accounts)
    out: dict = {"public_base_url": public_base() or None,
                 "app_credentials": app_credentials_status(), "accounts": []}
    if not out["public_base_url"]:
        out["warning"] = ("PUBLIC_BASE_URL is not set — Instagram publishing will fail "
                          "because Meta cannot fetch images from this machine.")
    for key in keys:
        row: dict = {"brand": key}
        try:
            acct = account_for(key, cfg)
            token = token_for(acct, cfg)
            base = account_base(acct, cfg)
            row["login"] = "instagram" if base.startswith(IG_LOGIN_BASE) else "facebook"
            if row["login"] == "facebook":
                # debug_token exists only on graph.facebook.com.
                dbg = _graph("GET", "debug_token", cfg, token=token,
                             params={"input_token": token})
                info = dbg.get("data", {})
                expires = info.get("expires_at", 0)
                row["token_valid"] = bool(info.get("is_valid"))
                row["token_expires"] = ("never" if not expires else
                                        datetime.fromtimestamp(expires, timezone.utc)
                                        .strftime("%Y-%m-%d %H:%M UTC"))
                row["scopes"] = info.get("scopes", [])
            else:
                me = _graph("GET", "me", cfg, token=token,
                            params={"fields": "user_id,username"}, base=base)
                row["token_valid"] = bool(me.get("username") or me.get("user_id"))
                row["username"] = me.get("username")
            if acct.get("ig_user_id"):
                ig = _graph("GET", str(acct["ig_user_id"]), cfg, token=token,
                            params={"fields": "username,followers_count"}, base=base)
                row["instagram"] = {"id": acct["ig_user_id"],
                                    "username": ig.get("username"),
                                    "followers": ig.get("followers_count")}
                row["quota"] = ig_quota(str(acct["ig_user_id"]), cfg, token, base)
            if acct.get("fb_page_id"):
                fb = _graph("GET", str(acct["fb_page_id"]), cfg, token=token,
                            params={"fields": "name,fan_count"})
                row["facebook"] = {"id": acct["fb_page_id"], "name": fb.get("name"),
                                   "fans": fb.get("fan_count")}
            row["ok"] = True
        except MetaError as e:
            row["ok"] = False
            row["error"] = str(e)
        out["accounts"].append(row)
    return out


def exchange_long_lived(short_token: str, config: dict | None = None) -> dict:
    """Trade a short-lived Facebook user token for a ~60-day one.

    Uses whichever app pair .env carries (see app_credentials)."""
    cfg = load_config(config)
    app_id, secret, source = app_credentials()
    data = _graph("GET", "oauth/access_token", cfg, token=short_token, params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": secret, "fb_exchange_token": short_token,
    })
    ttl = int(data.get("expires_in", 0))
    return {"access_token": data.get("access_token", ""),
            "app": source,
            "expires_in_days": round(ttl / 86400, 1) if ttl else "never"}


def exchange_instagram_token(short_token: str) -> dict:
    """Trade a short-lived Instagram Login token for a ~60-day one.

    This is the graph.instagram.com flow used by "Instagram API with Instagram
    business login". It accepts only the Instagram app secret - the Facebook app
    secret is rejected here, which is why the two pairs stay separate."""
    secret = os.environ.get("INSTAGRAM_APP_SECRET", "").strip()
    if not secret:
        raise MetaError(
            "Set INSTAGRAM_APP_SECRET in .env - the graph.instagram.com exchange "
            "only accepts the Instagram app secret (Meta app -> Instagram -> API setup)."
        )
    data = _ig_login("access_token", {
        "grant_type": "ig_exchange_token",
        "client_secret": secret,
        "access_token": short_token,
    })
    ttl = int(data.get("expires_in", 0))
    return {"access_token": data.get("access_token", ""),
            "token_type": data.get("token_type", "bearer"),
            "expires_in_days": round(ttl / 86400, 1) if ttl else "unknown"}


def refresh_instagram_token(long_token: str) -> dict:
    """Extend a long-lived Instagram Login token by another ~60 days.

    Meta only refreshes a token that is at least 24h old and not yet expired, so
    run this well before the 60-day mark."""
    data = _ig_login("refresh_access_token", {
        "grant_type": "ig_refresh_token",
        "access_token": long_token,
    })
    ttl = int(data.get("expires_in", 0))
    return {"access_token": data.get("access_token", ""),
            "token_type": data.get("token_type", "bearer"),
            "expires_in_days": round(ttl / 86400, 1) if ttl else "unknown"}


IG_SCOPES = ("instagram_business_basic",
             "instagram_business_content_publish")


def ig_login_url(redirect_uri: str, scopes: list[str] | None = None) -> str:
    """The authorize URL that starts Instagram business login.

    An app ID and app secret alone cannot publish - they only identify the app.
    Opening this URL, approving with the Instagram account, and exchanging the
    returned ?code= (see ig_exchange_code) is what produces a token. The
    redirect_uri must match one registered on the app exactly."""
    app_id = os.environ.get("INSTAGRAM_APP_ID", "").strip()
    if not app_id:
        raise MetaError("Set INSTAGRAM_APP_ID in .env first.")
    if not redirect_uri.startswith("https://"):
        raise MetaError("Instagram only accepts an https redirect URI.")
    from urllib.parse import urlencode
    query = urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes or IG_SCOPES),
        "response_type": "code",
    })
    return f"https://www.instagram.com/oauth/authorize?{query}"


def ig_exchange_code(code: str, redirect_uri: str) -> dict:
    """Trade the ?code= from the redirect for a token, then make it long-lived.

    The code is single-use and expires in about a minute, so run this right after
    the redirect. Instagram appends '#_' to the code in the address bar - it is
    stripped here because pasting it verbatim is the usual first failure."""
    app_id = os.environ.get("INSTAGRAM_APP_ID", "").strip()
    secret = os.environ.get("INSTAGRAM_APP_SECRET", "").strip()
    if not (app_id and secret):
        raise MetaError("Set INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET in .env.")
    code = code.strip().rstrip("#_")
    try:
        r = requests.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": app_id, "client_secret": secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri, "code": code,
        }, timeout=30)
        data = r.json()
    except requests.RequestException as e:
        raise MetaError(f"api.instagram.com unreachable: {e}") from e
    except ValueError:
        raise MetaError(f"api.instagram.com returned non-JSON: {r.text[:200]}")
    if r.status_code >= 400 or "error_message" in data or "error" in data:
        raise MetaError("Code exchange failed: "
                        + str(data.get("error_message") or data.get("error") or r.text[:300]))
    short = data.get("access_token", "")
    user_id = data.get("user_id") or (data.get("data") or [{}])[0].get("user_id", "")
    out = {"ig_user_id": str(user_id)}
    out.update(exchange_instagram_token(short))
    out["next"] = ("Put access_token in .env as META_ACCESS_TOKEN, ig_user_id in "
                   "config.yaml under meta.accounts.<brand>.ig_user_id, and set "
                   "login: instagram on that account.")
    return out


def test_post(brand: str, *, image: str = "", caption: str = "",
              config: dict | None = None, dry_run: bool = False) -> dict:
    """Publish one throwaway image to Instagram to prove the pipeline end to end.

    Runs preflight first and refuses to send when anything blocks, so a failure
    names its cause instead of arriving as a Graph API riddle. This posts to the
    real account - there is no sandbox for Instagram publishing."""
    cfg = load_config(config)
    caption = caption or f"Connection test from K2 PostTool - {brand}."
    if image:
        assets = [image]
    else:
        assets = [str(_make_test_image(brand))]
    rel_urls = []
    base = public_base()
    if base:
        name = Path(assets[0]).name
        parent = Path(assets[0]).parent.name
        rel_urls = [f"{base}/outputs/{parent}/{name}"] if parent != "outputs" else \
                   [f"{base}/outputs/{name}"]

    report = preflight(brand, ptype="post", assets=assets, asset_urls=rel_urls,
                       caption=caption, config=cfg)
    if not report["ok"] or dry_run:
        return {"ok": False if not report["ok"] else True, "published": False,
                "dry_run": dry_run, "preflight": report,
                "asset": assets[0], "asset_url": rel_urls[0] if rel_urls else ""}

    res = publish(brand=brand, ptype="post", assets=assets, asset_urls=rel_urls,
                  caption=caption, config=cfg)
    return {"ok": bool(res.get("ok")), "published": bool(res.get("results")),
            "preflight": report, "result": res,
            "asset": assets[0], "asset_url": rel_urls[0] if rel_urls else ""}


def _make_test_image(brand: str) -> Path:
    """A 1080x1080 card written into outputs/_test so it is served like any post."""
    from PIL import Image, ImageDraw
    out = Path("outputs/_test")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"connection_test_{brand or 'default'}.png"
    img = Image.new("RGB", (1080, 1080), (13, 24, 40))
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, 1020, 1020], outline=(71, 215, 161), width=6)
    d.text((110, 500), "K2 PostTool", fill=(255, 255, 255))
    d.text((110, 540), "publishing connection test", fill=(150, 170, 190))
    img.save(path, "PNG", optimize=True)
    return path


# -- CLI -----------------------------------------------------------------------

def _read_caption(value: str) -> str:
    p = Path(value)
    try:
        if p.exists() and p.suffix.lower() in (".txt", ".md"):
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publish finished posts to Instagram / Facebook.")
    ap.add_argument("--brand", default="", help="brand key from config.yaml -> meta.accounts")
    ap.add_argument("--type", dest="ptype", default="post",
                    choices=["post", "carousel", "story"])
    ap.add_argument("--asset", action="append", default=[], help="image path (repeatable)")
    ap.add_argument("--asset-url", action="append", default=[], help="public image URL (repeatable)")
    ap.add_argument("--caption", default="", help="caption text, or a path to a .txt file")
    ap.add_argument("--targets", default="", help="comma list: instagram,facebook")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="check token + accounts")
    ap.add_argument("--list-accounts", action="store_true")
    ap.add_argument("--quota", action="store_true", help="IG posts left in the 24h window")
    ap.add_argument("--exchange-token", default="",
                    help="short-lived Facebook token -> long-lived")
    ap.add_argument("--exchange-ig-token", default="",
                    help="short-lived Instagram Login token -> long-lived")
    ap.add_argument("--refresh-ig-token", default="",
                    help="extend a long-lived Instagram Login token by ~60 days")
    ap.add_argument("--preflight", action="store_true",
                    help="check every publishing constraint without sending anything")
    ap.add_argument("--offline", action="store_true",
                    help="with --preflight: skip the checks that need the network")
    ap.add_argument("--test-post", action="store_true",
                    help="publish one throwaway image to prove the pipeline works")
    ap.add_argument("--ig-login-url", default="",
                    help="print the Instagram business login URL for this redirect URI")
    ap.add_argument("--ig-code", default="",
                    help="exchange the ?code= from that redirect for a token")
    ap.add_argument("--redirect-uri", default="",
                    help="the redirect URI registered on the app (used with --ig-code)")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
        if args.list_accounts:
            print(json.dumps(cfg.get("accounts", {}), indent=2))
            return 0
        if args.exchange_token:
            print(json.dumps(exchange_long_lived(args.exchange_token), indent=2))
            return 0
        if args.exchange_ig_token:
            print(json.dumps(exchange_instagram_token(args.exchange_ig_token), indent=2))
            return 0
        if args.refresh_ig_token:
            print(json.dumps(refresh_instagram_token(args.refresh_ig_token), indent=2))
            return 0
        if args.ig_login_url:
            print(ig_login_url(args.ig_login_url))
            return 0
        if args.ig_code:
            if not args.redirect_uri:
                raise MetaError("--ig-code also needs --redirect-uri (the same one "
                                "you authorised with).")
            print(json.dumps(ig_exchange_code(args.ig_code, args.redirect_uri), indent=2))
            return 0
        if args.preflight:
            brand = args.brand or next(iter(cfg.get("accounts", {})), "")
            report = preflight(
                brand, ptype=args.ptype, assets=args.asset,
                asset_urls=args.asset_url, caption=_read_caption(args.caption),
                targets=[t.strip() for t in args.targets.split(",") if t.strip()] or None,
                config=None, network=not args.offline)
            print(preflight_text(report))
            return 0 if report["ok"] else 1
        if args.test_post:
            brand = args.brand or next(iter(cfg.get("accounts", {})), "")
            res = test_post(brand, image=(args.asset or [""])[0],
                            caption=_read_caption(args.caption), dry_run=args.dry_run)
            if not res.get("published"):
                print(preflight_text(res["preflight"]))
            print(json.dumps({k: v for k, v in res.items() if k != "preflight"}, indent=2))
            return 0 if res.get("ok") else 1
        if args.verify:
            print(json.dumps(verify(args.brand), indent=2))
            return 0
        if args.quota:
            brand = args.brand or next(iter(cfg.get("accounts", {})), "")
            acct = account_for(brand, cfg)
            print(json.dumps(ig_quota(str(acct["ig_user_id"]), cfg, token_for(acct, cfg)), indent=2))
            return 0

        if not args.brand:
            raise MetaError("--brand is required to publish (see --list-accounts).")
        res = publish(
            brand=args.brand, ptype=args.ptype, assets=args.asset,
            asset_urls=args.asset_url, caption=_read_caption(args.caption),
            targets=[t.strip() for t in args.targets.split(",") if t.strip()] or None,
            dry_run=args.dry_run,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    except MetaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

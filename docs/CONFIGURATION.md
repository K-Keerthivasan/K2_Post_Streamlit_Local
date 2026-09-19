# Configuration reference (`config.yaml`)

Everything brand- and behaviour-specific lives in `config.yaml`. Start from
`config.example.yaml` (a generic single-brand template). `config.yaml` is gitignored.

## Top level

```yaml
app:
  name: "My Studio"          # browser tab title
active_brand: demo           # which brand key is selected on boot
```

## `meta:` — publishing to Instagram & Facebook

See [PUBLISHING_AND_CHANNELS.md](PUBLISHING_AND_CHANNELS.md) for the full guide.
Credentials never live here — the token is read from the environment.

```yaml
meta:
  api_version: "v21.0"
  token_env: "META_ACCESS_TOKEN"       # env var name holding the long-lived token
  accounts:                            # one entry per brand key you publish for
    demo:
      ig_user_id: "17841400000000000"  # Instagram Business account id, not the @handle
      fb_page_id: "1234567890"         # linked Page (optional; needed for facebook)
      targets: ["instagram"]           # instagram | facebook | both
      # token_env: "META_ACCESS_TOKEN_DEMO"   # optional per-brand token
  publish:
    default_targets: ["instagram"]     # used when an account sets no targets
    on_approve: "hold"                 # hold = approve, then schedule/publish yourself
                                       # now  = approving publishes immediately
    allow_now: true
  rate_limit:
    max_posts_per_day: 25              # Instagram's own 24h ceiling
    safety_margin: 2                   # effective ceiling = max - margin
```

An unmapped brand raises an error rather than falling back to another account.

## `schedule:` — the post calendar

```yaml
schedule:
  times: ["09:00", "13:00", "18:00"]   # slots "Auto-fill" uses, local time
  days:  ["mon", "tue", "wed", "thu", "fri"]
  auto_publish: true                   # false = the calendar plans, nothing is sent
  tick_seconds: 60                     # how often to check for due posts
```

Editable in the UI from **🗓 Calendar → ⚙ Slots**, which writes back to this file.

## `brands:` — one block per brand

The brand **key** (e.g. `demo`, `k2`, `jkr`) is what you reference in `meta.accounts`
and the Brand dropdown.

```yaml
brands:
  demo:
    name:      "Your Brand"       # display name (and sidebar logo text)
    short:     "YB"
    author:    "Your Name"        # byline
    handle:    "@yourbrand"       # shown on cards
    instagram: "yourbrand"
    tagline:   "Your tagline"
    pitch:     "What you do, in one line."
    services:  "Service 1 · Service 2 · Service 3"
    location:  "Your City"
    website:   "yourbrand.com"
    email:     "hello@yourbrand.com"
    category:  "YOUR NICHE"
    logo_path: "static/logo.png"  # your logo file
    logo_shape: "wide"            # optional: 'wide' = full wordmark (don't crop to circle)
    image_forward: true           # optional: image-led layouts (image+text split)
    hashtags:  ["yourbrand", "marketing"]
    templates:                    # optional: override shared templates per brand
      title:   custom_title.html
      content: custom_content.html
      outro:   custom_outro.html
    theme:
      navy:    "#0A0F1E"          # background base
      navy2:   "#0C1426"
      accent:  "#00B4C8"          # primary accent
      accent2: "#00C896"          # secondary accent
      text:    "#FFFFFF"
    profile: >
      Describe your niche + audience + what should score high. Drives 0-100 story scoring.
    personality: >
      Describe your brand voice. The AI rewrites copy in this voice on a final pass.
    feeds:
      categories:
        tech:
          name:    "Tech"
          enabled: true
          urls:
            - "https://techcrunch.com/feed"
        youtube:
          name:    "YouTube"
          enabled: true
          urls:
            # channel feed: https://www.youtube.com/feeds/videos.xml?channel_id=<ID>
            - "https://www.youtube.com/feeds/videos.xml?channel_id=UC2Xd-TjJByJyK2w1zNwY0zQ"
```

## `llm:` — local model

```yaml
llm:
  base_url: "hermes://cli"       # active backend on boot (env K2_LLM_* overrides)
  model:    "hermes:gpt-5.5"
  format:   "json"
  backends:                      # selectable in the header Engine dropdown
    hermes: { base_url: "hermes://cli",                 model: "hermes:gpt-5.5" }
    ollama: { base_url: "http://localhost:11434/v1",    model: "qwen3:8b" }
```

## `output:` / `images:` / `slides:`

```yaml
output:  { width: 1080, height: 1350, directory: "outputs" }

images:                          # filter baked into web/Google images on download
  filter: { enabled: true, saturation: 0.85, brightness: 0.95, contrast: 1.05 }

slides:
  min_content_cards: 1           # min total = 3 (title + 1 + outro)
  max_content_cards: 8           # max total = 10
  default_total:     4
  short_story: { max_summary_words: 220, content_cards: 2 }
  long_story:  { min_summary_words: 221, content_cards: 4 }
```

## `formats:` — post types

Each format has a size and template; `type: multi` = carousel-style, `single` = one card.
Add `brands: ["key1","key2"]` to restrict a format to specific brands.

```yaml
formats:
  carousel:  { name: "Carousel",       width: 1080, height: 1350, type: multi }
  square:    { name: "Square Post",    width: 1080, height: 1080, type: single, template: square.html }
  story:     { name: "Story",          width: 1080, height: 1920, type: single, template: story.html }
  # … quote / comparison / breaking / listicle / linkedin / x / cover
  linkedin:
    name: "LinkedIn"
    width: 1200
    height: 1200
    type: single
    template: linkedin.html
    # brands: ["demo"]           # uncomment to restrict
```

Templates live in `templates/` (Jinja2 HTML/CSS); brand colours come from the `theme`
block via CSS variables. Edit templates live in the **Templates** tab.

# Publishing & channels

How a finished post gets from the Review queue to Instagram and Facebook, and what you
have to set up once to make that work.

The app publishes **directly through the Meta Graph API** using your own credentials.
There is no third-party scheduler in the middle, so what you approve is exactly what gets
posted, at exactly the time you set.

---

## 1. What you need

| Requirement | Why |
|---|---|
| Instagram **Business** or **Creator** account | The Content Publishing API refuses personal accounts. |
| A **Facebook Page** linked to that Instagram account | Meta routes Instagram API access through the Page. |
| A **Meta app** with the Instagram Graph API product | Where your token comes from. |
| A **long-lived access token** with the scopes below | Authorises publishing. |
| A **public `PUBLIC_BASE_URL`** | Instagram fetches the images from you — see §4. |

Scopes the token needs:

```
instagram_basic
instagram_content_publish
pages_show_list
pages_read_engagement
pages_manage_posts
```

---

## 2. One-time setup

**a. Make the account eligible.** In the Instagram app: Settings → Account type and tools
→ switch to Business or Creator, and connect it to your Facebook Page.

**b. Create the Meta app.** At [developers.facebook.com](https://developers.facebook.com)
→ My Apps → Create App → Business. Add the **Instagram Graph API** product (and
**Facebook Login for Business** if you want to run the OAuth flow yourself).

**c. Get a token.** Open the Graph API Explorer, select your app, request the scopes
above, and generate a user token. It is short-lived (about an hour). Exchange it:

```bash
# needs META_APP_ID and META_APP_SECRET in .env
python meta.py --exchange-token <short-lived-token>
```

A Meta app shows **two** app id/secret pairs and they are not interchangeable:

| Where you copy it from | `.env` keys | Used by |
|---|---|---|
| App settings → Basic | `META_APP_ID` / `META_APP_SECRET` | `--exchange-token` (graph.facebook.com) |
| Instagram → API setup | `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET` | `--exchange-ig-token`, `--refresh-ig-token` (graph.instagram.com) |

If your token came from *Instagram API with Instagram business login* rather than
the Graph API Explorer, exchange and renew it on the Instagram host instead:

```bash
python meta.py --exchange-ig-token <short-lived-token>   # -> ~60-day token
python meta.py --refresh-ig-token  <long-lived-token>    # +60 days, run before day 60
```

Set both pairs if you have them — `META_APP_*` wins for the Facebook exchange, and
`python meta.py --verify` reports which ones it found.

Put the resulting long-lived token (~60 days) in `.env`:

```
META_ACCESS_TOKEN=EAAG...
```

**d. Find your account ids.** In the Graph API Explorer:

```
GET /me/accounts                              -> your Page id
GET /{page-id}?fields=instagram_business_account   -> your ig_user_id
```

**e. Map each brand** in `config.yaml`:

```yaml
meta:
  api_version: "v21.0"
  token_env: "META_ACCESS_TOKEN"
  accounts:
    brand_a:
      ig_user_id: "17841400000000000"
      fb_page_id: "1234567890"
      targets: ["instagram", "facebook"]
    brand_b:
      ig_user_id: "17841400000000001"
      targets: ["instagram"]
      token_env: "META_ACCESS_TOKEN_B"   # optional: a different token per brand
```

Every brand you publish for needs its own entry. An unmapped brand fails loudly rather
than falling back to another account — posting brand B's content to brand A's Instagram
is a worse outcome than an error.

**f. Check it.**

```bash
python meta.py --verify
```

or press **📖 Connect accounts → 🔌 Check connection** in the Review tab. You get token
validity, expiry, the account usernames, and the remaining daily quota.

---


## Getting a token from an Instagram app ID + secret

If you have the *Instagram* app ID/secret (Meta app → Instagram → API setup)
rather than a Graph API Explorer token, mint the token yourself:

```bash
# 1. Print the login URL (the redirect URI must be registered on the app, https only)
python meta.py --ig-login-url https://your.domain/callback

# 2. Open it, approve with the Instagram account, copy the ?code= from the redirect
python meta.py --ig-code <code> --redirect-uri https://your.domain/callback
```

That returns a ~60-day token plus the account's `ig_user_id`. Put the token in
`.env` as `META_ACCESS_TOKEN`, the id in `config.yaml`, and mark the account as
using Instagram login — tokens from this flow only work on `graph.instagram.com`:

```yaml
meta:
  accounts:
    k2:
      ig_user_id: "17841400000000000"
      login: instagram          # omit for Graph API Explorer / Facebook Login tokens
      targets: ["instagram"]
```

Renew before day 60 with `python meta.py --refresh-ig-token <token>`.

## 3. Publishing a post

In the **✅ Review** tab each pending post offers:

| Action | Effect |
|---|---|
| **✓ Approve** | Marks it ready. Nothing is sent — the default (`meta.publish.on_approve: hold`). |
| **🗓 Schedule** | Pick a date, time, and destinations. It lands on the calendar. |
| **🚀 Publish now** | Sends it to Meta immediately. |
| **✕ Reject** | Discards it. |

Set `meta.publish.on_approve: "now"` if you would rather approving publish straight away.

Post types are chosen from the format automatically:

| Format | Instagram | Facebook |
|---|---|---|
| `carousel`, `listicle` | Carousel (2–10 images) | Multi-photo post |
| `square`, `xpost`, `quote`, … | Single image | Single photo |
| `story` | Story | not supported |

---

## 4. `PUBLIC_BASE_URL` — the one thing people get stuck on

Instagram does not accept uploaded bytes. You give Meta a **URL**, and Meta's servers
download the image themselves. That means `http://localhost:8000/...` can never work, no
matter how well the app runs locally.

Set `.env`:

```
PUBLIC_BASE_URL=https://your-host.example.ts.net
```

It must serve this app's `/outputs` folder publicly over https. A
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) URL is the usual answer; a
Cloudflare tunnel or any reverse proxy works too. The app refuses localhost URLs up front
with a clear message rather than passing them to Meta and getting back something vague.

**Facebook does not need this** — Page photos are uploaded as multipart bytes. If you have
no public URL yet, set `targets: ["facebook"]` and publish there while you sort it out.

---

## 5. The calendar

The **🗓 Calendar** tab is the same queue laid out by date.

- Click **＋** on a day, or a post in the **Unscheduled** tray, to place it.
- Click a scheduled post to reschedule, unschedule, or publish it now.
- **✨ Auto-fill slots** drops every unscheduled post into the next free posting slots.
- **⚙ Slots** edits those times and days (saved to `config.yaml`):

```yaml
schedule:
  times: ["09:00", "13:00", "18:00"]
  days:  ["mon", "tue", "wed", "thu", "fri"]
  auto_publish: true      # false = the calendar plans, but nothing is sent
  tick_seconds: 60
```

A background loop checks every `tick_seconds` for posts whose slot has passed and
publishes them. All times are the machine's **local** time — the same clock the grid
shows. **The app must be running** for a scheduled post to go out; a slot that passes
while it is closed publishes on the next start, not silently never.

Turn `auto_publish` off to use the calendar purely for planning.

---

## 6. Limits and guards

- **Instagram allows 25 published posts per rolling 24 hours** per account. `meta.py`
  tracks its own count in `library/meta_rate.json` and stops at
  `max_posts_per_day - safety_margin`, so you never hit Meta's wall mid-carousel. Check
  the real figure with `python meta.py --quota --brand <key>`.
- **Captions** are trimmed to 2,200 characters.
- **Carousels** must have 2–10 images; anything else is rejected before upload.
- **Tokens expire** (~60 days). `python meta.py --verify` shows the expiry date; re-run
  the exchange before it lapses.

---

## 7. CLI

The publisher works without the UI:

```bash
python meta.py --verify                        # token + accounts + quota
python meta.py --list-accounts                 # what config.yaml maps
python meta.py --quota --brand brand_a
python meta.py --type carousel --brand brand_a \
  --asset outputs/brand_a/post/01.png --asset outputs/brand_a/post/02.png \
  --caption caption.txt
python meta.py --type post --brand brand_a --asset card.png \
  --caption "Hello" --targets facebook
python meta.py --type story --brand brand_a --asset card.png --dry-run
```

`--dry-run` validates everything and reports what *would* be sent without calling the
publish endpoints.

---

## 8. Troubleshooting

| Message | Cause |
|---|---|
| `META_ACCESS_TOKEN is not set` | No token in `.env`. |
| `No Meta account mapped for brand 'x'` | Add it under `meta.accounts` in `config.yaml`. |
| `Instagram publishing needs public image URLs` | `PUBLIC_BASE_URL` is unset — see §4. |
| `PUBLIC_BASE_URL points at localhost` | Meta cannot reach your machine; use a public https URL. |
| `Meta API error 190` | Token invalid or expired — re-exchange it. |
| `Meta API error 10` / permission errors | Missing scope, or the account is not Business/Creator. |
| `The image URL is not accessible` | Your public URL is not actually reachable from outside. Open it in a phone browser on mobile data. |
| `Local rate guard: N posts in the last 24h` | You hit the self-imposed ceiling; it clears as the window rolls. |

---

## Verifying before you schedule

`--verify` asks Meta whether the token works. `--preflight` answers the more
useful question: *can this specific post go out?* It checks everything locally
(and, unless you pass `--offline`, fetches the image URL the way Meta will) and
prints one line per constraint:

```bash
python meta.py --preflight --brand k2
python meta.py --preflight --brand k2 --type carousel --asset a.png --asset b.png --caption cap.txt
```

| Check | Blocks publishing when |
|---|---|
| `account.mapped` | the brand has no entry under `meta.accounts` |
| `token.present` / `token.works` | no access token, or Meta rejects it |
| `account.ig_user_id` | the numeric IG Business id is missing |
| `public_url.set/https/public/reachable/image` | `PUBLIC_BASE_URL` is unset, not https, points at localhost, or does not serve the image |
| `assets.exist` / `assets.count` | a file is missing, or a carousel is outside 2–10 images |
| `asset.size` / `asset.width` / `asset.aspect` | over 8 MB, under 320px wide, or outside the 0.80–1.91 aspect range |
| `caption.hashtags` | more than 30 hashtags |
| `rate.local` | the local 24h guard is already at its ceiling |

Warnings (`caption.present`, `caption.length`, `asset.width_max`) never block —
they say what Meta will change. The same checks run inside the app: **Review →
Connect Instagram & Facebook → Run preflight**, and the scheduler runs them
before every publish.

Once preflight is clean, prove the whole chain with a real post:

```bash
python meta.py --test-post --brand k2            # publishes one throwaway image
python meta.py --test-post --brand k2 --dry-run  # preflight only, sends nothing
```

There is no sandbox for Instagram publishing — `--test-post` posts to the real
account, then prints the permalink. Delete it from the app afterwards.

## A scheduled post did not go out

A post whose slot has passed but which never reached Instagram stays **scheduled**
and carries a `blocked_reason`; the Review card shows it, and the scheduler
retries every tick. It is not marked `failed` — that status is reserved for posts
Meta actually saw and rejected. Check what is missing with:

```bash
python meta.py --preflight --brand k2
curl -s localhost:8000/api/scheduler/status     # last_tick, overdue, blocked_reason
```

The usual causes, in the order they bite:

1. **No access token.** An app ID and app secret are not a token — see below.
2. **`ig_user_id` empty** in `config.yaml` under `meta.accounts.<brand>`.
3. **`PUBLIC_BASE_URL` unset**, so Instagram has nowhere to fetch the image from.
4. **`auto_publish: false`** in the `schedule:` block — the calendar plans, but
   nothing is ever sent.
5. **The app was not restarted** after fixing any of the above; `.env` is read
   once at startup.

## 9. Optional: n8n fallback

If no Meta token is configured and `N8N_WEBHOOK_URL` is set, approving posts the payload
(brand, title, caption, image URLs) to that webhook instead. It is a legacy escape hatch —
the Meta path is the supported one.

# X Video Bot

Telegram bot that downloads videos from **public** posts on X.com (Twitter),
YouTube, and Instagram, and sends them back in chat.

## Features

- Inline platform menu (`/start`) + automatic URL detection
- Non-blocking downloads (yt-dlp in a worker thread), global concurrency
  limit + one-download-at-a-time per user
- Videos over Telegram's 50 MB bot limit are auto-compressed with ffmpeg
- Credit system: renewable free plan + purchased credits via Telegram Stars
- VIP tier: larger file limit, queue priority — sold via Stars or granted by admin
- Real download queue: priority lanes, /status position, /cancel while queued
- SQLite persistence: users, bans, download history, payment transactions
- Admin tools: stats, user list, ban/unban, direct message, broadcast,
  manual credit, Stars refunds
- YouTube bot-check mitigation: PO token provider, alternate player clients,
  optional cookies and proxy
- Fully containerized — bot + PO token provider via docker compose

## Credits & payments

Every user has two credit pockets:

| Pocket    | Amount                  | Expires?                              |
|-----------|-------------------------|---------------------------------------|
| Free      | `FREE_CREDITS` (5)      | Refills every `FREE_PERIOD_DAYS` (7)  |
| Purchased | whatever the user buys  | Never                                 |

- Download costs (per successful download): X = `CREDIT_COST_X`,
  Instagram = `CREDIT_COST_INSTAGRAM`, YouTube = `CREDIT_COST_YOUTUBE`
  (YouTube is priced higher to throttle the riskiest platform).
- Free credits are spent first; purchased credits are only touched when
  free runs out. Failed downloads never cost anything.
- The free refill is a per-user rolling window (7 days from that user's
  last refill), evaluated lazily — no scheduler needed.
- Top-ups use Telegram Stars (`/buy`): packages come from `STAR_PACKAGES`
  as `credits:stars` pairs. Every payment is stored in the `transactions`
  table with its `charge_id`; `/refund <charge_id>` reverses a payment via
  the Stars API and removes the purchased credits.
- The admin (`ADMIN_ID`) is exempt from credits entirely.

## VIP tier

- Granted permanently by the admin (`/vip <id|@username>`, revoke with
  `/unvip`) or purchased for `VIP_DAYS` via `/buy` (`VIP_PRICE_STARS`);
  repeat purchases stack remaining time.
- Benefits: source-video limit of `VIP_SIZE_LIMIT_MB` (vs
  `USER_SIZE_LIMIT_MB`) and the priority lane in the download queue.
- VIP status is shown in `/start` and `/balance` (⭐), in `/list`, and
  counted in `/stats`.

## Download queue

Downloads run through a queue consumed by `MAX_CONCURRENT_DOWNLOADS`
workers. VIP/admin jobs go to the priority lane and are always served
first. Users see their queue position when submitting, get notified when
the download starts, and can `/status` (position) or `/cancel` (only while
still queued — active downloads can't be cancelled). `/queue` gives the
admin lane sizes and active jobs.

## Size limits

Source-video size is checked after download: normal users
`USER_SIZE_LIMIT_MB` (50), VIP `VIP_SIZE_LIMIT_MB` (100), admin unlimited.
Over-limit videos are rejected and never cost credits. Separately,
Telegram bots can upload at most 50 MB, so anything between the Telegram
cap and the user's limit is compressed with ffmpeg to fit.

## Commands

User: `/start` (platform menu), `/balance`, `/status`, `/cancel`, `/buy`,
`/terms`, `/paysupport`, `/help`

Admin: `/stats`, `/list`, `/queue`, `/credit <id> <amount>`,
`/vip <id|@username>`, `/unvip <id|@username>`, `/refund <charge_id>`,
`/ban <id>`, `/unban <id>`, `/send <id> <msg>`, `/broadcast <msg>`

## Project structure

```
main.py             # entry point, wiring only
bot/config.py       # settings loaded from .env
bot/downloader.py   # platform registry + yt-dlp wrapper
bot/handlers.py     # Telegram handlers
Dockerfile
docker-compose.yml  # bot + bgutil PO token provider
```

## Running on a VPS (recommended)

Requirements: Docker + Docker Compose plugin.

```bash
# 1. Install Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# 2. Clone and configure
git clone <your-repo-url> x-video-bot
cd x-video-bot
cp .env.example .env
nano .env          # set BOT_TOKEN

# 3. Build and start
docker compose up -d --build

# 4. Watch logs
docker compose logs -f bot
```

The compose file starts two containers:

| Service        | Purpose                                              |
|----------------|------------------------------------------------------|
| `bot`          | The Telegram bot                                     |
| `pot-provider` | Generates YouTube PO tokens (no account/cookies)     |

`POT_PROVIDER_URL` is wired between them automatically.

### Updating

yt-dlp breaks periodically as sites change — rebuild to pull the latest:

```bash
docker compose build --no-cache && docker compose up -d
```

### If YouTube still says "Sign in to confirm you're not a bot"

The check is IP-reputation based; VPS/datacenter IPs are the main trigger.
Escalate in this order:

1. Rebuild (updates yt-dlp) — the provider container auto-updates on pull.
2. Set `PROXY_URL` in `.env` to a **residential** proxy.
3. Last resort: export `cookies.txt` from a **throwaway** Google account,
   place it next to the compose file, uncomment the volume in
   `docker-compose.yml`, and set `COOKIES_FILE=/data/cookies.txt`.

## Running locally without Docker

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg
cp .env.example .env   # set BOT_TOKEN
python main.py
```

For YouTube, also run the PO token provider locally and set
`POT_PROVIDER_URL=http://127.0.0.1:4416`:

```bash
docker run -d -p 4416:4416 --restart unless-stopped \
  brainicism/bgutil-ytdlp-pot-provider
```

## Configuration reference

| Variable               | Default | Description                              |
|------------------------|---------|------------------------------------------|
| `BOT_TOKEN`            | —       | **Required.** Token from @BotFather      |
| `MAX_FILE_SIZE_MB`     | 50      | Telegram bot upload limit                |
| `DOWNLOAD_TIMEOUT_SEC` | 120     | Read/write timeout for API calls         |
| `UPLOAD_TIMEOUT_SEC`   | 300     | Media upload timeout                     |
| `LOG_LEVEL`            | INFO    | Python logging level                     |
| `COOKIES_FILE`         | —       | Path to Netscape cookies.txt (optional)  |
| `PROXY_URL`            | —       | Proxy for yt-dlp traffic (optional)      |
| `POT_PROVIDER_URL`     | —       | bgutil PO token provider base URL        |
| `ADMIN_ID`             | —       | Numeric Telegram ID of the admin         |
| `DB_PATH`              | bot.db  | SQLite path (compose sets /data/bot.db)  |
| `FREE_CREDITS`         | 5       | Free credits per refill period           |
| `FREE_PERIOD_DAYS`     | 7       | Free refill period in days               |
| `CREDIT_COST_X`        | 1       | Credits per X.com download               |
| `CREDIT_COST_INSTAGRAM`| 1       | Credits per Instagram download           |
| `CREDIT_COST_YOUTUBE`  | 3       | Credits per YouTube download             |
| `MAX_CONCURRENT_DOWNLOADS` | 3   | Simultaneous downloads across all users  |
| `STAR_PACKAGES`        | 5:25,15:70,40:170 | Stars packages, credits:stars  |
| `USER_SIZE_LIMIT_MB`   | 50      | Max source-video size, normal users      |
| `VIP_SIZE_LIMIT_MB`    | 100     | Max source-video size, VIP users         |
| `VIP_PRICE_STARS`      | 150     | VIP membership price (0 disables)        |
| `VIP_DAYS`             | 30      | VIP membership duration per purchase     |

## Notes & limits

- Telegram bots can't upload files over **50 MB** (a local Bot API server
  raises this to 2 GB if you ever need it).
- Only public posts work. Private/protected content fails with a clear message.
- Downloading may violate the platforms' terms of service — use responsibly
  and only for content you have the right to save.

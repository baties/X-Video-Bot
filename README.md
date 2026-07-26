# X Video Bot

Telegram bot that downloads videos from **public** posts on X.com (Twitter),
YouTube, and Instagram, and sends them back in chat.

## Features

- Inline platform menu (`/start`) + automatic URL detection
- Non-blocking downloads (yt-dlp in a worker thread)
- YouTube bot-check mitigation: PO token provider, alternate player clients,
  optional cookies and proxy
- Fully containerized — bot + PO token provider via docker compose

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

## Notes & limits

- Telegram bots can't upload files over **50 MB** (a local Bot API server
  raises this to 2 GB if you ever need it).
- Only public posts work. Private/protected content fails with a clear message.
- Downloading may violate the platforms' terms of service — use responsibly
  and only for content you have the right to save.

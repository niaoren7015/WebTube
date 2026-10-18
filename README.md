# WebTube

A personal, self-hosted YouTube downloader UI built with FastAPI, yt-dlp and FFmpeg. Use only for content you own, have permission to download, or are otherwise legally allowed to save. Respect YouTube's terms and copyright law.

## Features

- Chinese responsive UI; paste a single YouTube video URL.
- Inspect metadata and available formats before downloading.
- MP4 video download with FFmpeg merge; MP3 audio extraction.
- Task status/progress, expiring downloads, automatic temporary-file cleanup.
- Shared access token (`APP_SECRET`) and strict YouTube-host validation.
- Anonymous-only operation: the service does not request, upload, or store account cookies.
- Docker image runs as a non-root user; GitHub Actions builds the image and runs media smoke checks.

## Deploy on Render (free tier, best-effort)

1. Push this repository to GitHub.
2. In Render, create **New → Web Service**, connect the repository, and select **Docker** as the runtime.
3. Choose the free instance if available in your account/region.
4. Add environment variable `APP_SECRET` with a long, unique random value (at least 32 random characters). Do not commit `.env` or secrets.
5. Deploy. Render provides an `onrender.com` subdomain.
6. Open `/api/health` and verify `ok`, `yt_dlp_version`, `ffmpeg_available: true`, `pot_provider_installed: true`, and `pot_script_home_available: true`. This confirms the provider is installed, not that YouTube will accept requests.
7. Check the GitHub Actions run. The anonymous real-media download test must pass before claiming the current environment can download YouTube media.

### Free-tier caveats

- Render free services can sleep when idle; first request may be slow.
- Local disk is ephemeral. Completed downloads can disappear on restart/redeploy and do not persist reliably.
- Free services have resource limits; long videos and high resolutions may fail or time out.
- YouTube may change its delivery behavior or block cloud-hosted IP ranges. PO Token generation does not guarantee access from a blocked data-center IP. A green Docker build alone does not prove live YouTube downloads work.
- Do not deploy this as an unauthenticated public service. `APP_SECRET` is required for protected API actions. Do not put cookies or account credentials in the service.

## Local Docker test

```sh
cp .env.example .env
# Set APP_SECRET to a long random secret in .env
docker compose up --build
```

Open `http://localhost:8080`. To verify the media tools inside the image:

```sh
docker compose exec webtube yt-dlp --version
docker compose exec webtube ffmpeg -version
```

## Updating yt-dlp

Dependabot or manual updates can bump the lower-bound in `requirements.txt`. Rebuild and rerun the GitHub Actions media smoke test after updates.

## Security and limitations

- This is a single-user MVP, not a hardened multi-tenant service. The token is shared by all users.
- Use a unique `APP_SECRET`, HTTPS, and a host with suitable resource limits.
- Do not upload account cookies; this service is intentionally anonymous-only. Do not accept arbitrary URLs.
- Download files are temporary; do not rely on the service for long-term storage.

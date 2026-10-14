# Media Ingestion Engine

A headless FastAPI service polls configured provider categories, stores discovered work in SQLite, and dispatches one download at a time per enabled provider account to a remote storage server over SSH. Downloads go to a remote staging directory, are checked with `ffprobe`, then moved into the watch folder. A React dashboard manages accounts, categories, proxies, the SSH node, and the queue. The dashboard/API require the configured admin login.

Use this software only with media you own or are authorized to access, and with provider-approved APIs and download methods. Provider API paths and stream URL formats vary; the built-in adapter assumes an Xtream-style `player_api.php` API and may need adapting for your provider.

## Project structure

```text
backend/
  app/
    main.py                 FastAPI routes and service lifecycle
    models.py               SQLite / SQLAlchemy models
    config.py               Environment configuration
    security.py             Fernet encryption and secret masking
    provider.py             Provider API and stream URL adapter
    services/
      poller.py             Category polling and queue ingestion
      worker_pool.py        One worker per enabled account
      dispatcher.py         SSH, remote download, probe and promotion
  .env.example              Backend configuration template
  media-ingestion.service   systemd unit
  requirements.txt          Python dependencies
frontend/
  src/App.jsx               Single-page dashboard
  src/index.css             Tailwind and global styles
  package.json              Frontend scripts and dependencies
  vite.config.js            Development API proxy
setup.sh                    Install axel, aria2 and ffmpeg on the storage server
.gitignore
```

## Ports and network paths

| Port / path | Purpose |
| --- | --- |
| `127.0.0.1:8000` | FastAPI / Uvicorn; systemd binds to localhost |
| `localhost:5173` | Vite development server; `/api` requests proxy to port 8000 |
| `22/tcp` (outbound from daemon) | SSH from the orchestration VPS to the storage server; set another port in the SSH settings if needed |
| `443/tcp` (outbound) | HTTPS to the provider and, when testing a proxy, to the IP-check service |
| Remote staging and watch paths | Both must be on the same filesystem for the final rename to be atomic |
| Session cookie | `HttpOnly`, `SameSite=Strict`, `SESSION_TTL_SECONDS`; set `SESSION_COOKIE_SECURE=true` when served over HTTPS |

Port 8000 is bound to localhost by the supplied systemd unit. API endpoints (except health and login/session bootstrap) require an authenticated admin session. Keep the API bound to localhost and place TLS and, preferably, an additional authenticated reverse proxy/VPN in front of remote dashboard access. If serving the frontend and `/api` from the same origin, the browser does not need CORS.

## Admin login and sessions

Set `ADMIN_USERNAME`, `ADMIN_PASSWORD` (at least 12 characters), and a random `SESSION_SECRET_KEY` of at least 32 characters in `backend/.env`. The admin username/password are server-side environment credentials; no default password is shipped. Login uses a signed, time-limited, HTTP-only, SameSite=Strict cookie. Mutating API requests also require a CSRF header. Login attempts are rate limited per client address.

Generate a session signing key:

```bash
/opt/media-ingestion/venv/bin/python -c \
  'import secrets; print(secrets.token_urlsafe(48))'
```

Use a unique administrator password and protect `.env` with mode `0600`. In production behind HTTPS set `SESSION_COOKIE_SECURE=true`; it is `false` in the example only for local HTTP development. `SESSION_TTL_SECONDS` defaults to `28800` (8 hours). Changing the session signing key invalidates current sessions. The same-origin reverse proxy pattern below keeps cookies and API calls on one origin.

## Storage paths and content organization

In **Settings & Accounts → Remote storage node**, set the remote **staging directory** and **default storage root** (absolute Linux paths, such as `/srv/media/staging` and `/srv/media/library`). Use **Test connection & save** to create/verify those directories and check write access. Staging and each final destination must be on the same filesystem for atomic promotion.

Each watched category can inherit the default root (leave its override blank) or set a category-specific absolute remote path in **Category Watcher**. Category and default roots are read when each queued job starts, so changing them routes pending/retried jobs to the new path; already-running and completed jobs are not moved automatically. Category paths must be absolute Linux paths, cannot be `/` or contain `..`, and are created remotely as needed.

The dispatcher does not match items by title alone. It sanitizes names and uses the provider's stable IDs and metadata to create paths like:

```text
<destination root>/
  Series/<series name> [<series id>-<source digest>]/Season 01/<series - S01E01 [episode id].mkv>
  Movies/<movie name> [<stream id>-<source digest>]/<movie name>.mkv
```

Series and movies are separated, and same-named shows/movies with different provider IDs get separate directories. Queue deduplication keys include provider base URL, content type, and stream ID so a movie ID cannot suppress a series episode with the same ID. A no-clobber atomic move prevents an existing final file from being silently overwritten. The app does not infer identity from a title or merge folders with similar names.

## VPS setup

The examples below assume a Debian/Ubuntu orchestration VPS, a separate Linux storage server, and deployment under `/opt/media-ingestion`. Replace hostnames, usernames, paths, and domains with your own values.

### 1. Install runtime and create the service account

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nodejs npm git
sudo useradd --system --home /opt/media-ingestion --create-home \
  --shell /usr/sbin/nologin media-ingestion
sudo install -d -o media-ingestion -g media-ingestion /opt/media-ingestion
```

Clone or copy this repository into `/opt/media-ingestion`. The unit file expects `backend/` and `frontend/` immediately below this directory.

```bash
sudo git clone https://github.com/mohammedabdullaga/vod-orch.git /opt/media-ingestion
sudo chown -R media-ingestion:media-ingestion /opt/media-ingestion
```

For an existing clone, use `git pull` instead of cloning over it.

### 2. Install remote storage binaries

Run this on the **storage server**, not the orchestration VPS:

```bash
sudo bash /path/to/vod-orch/setup.sh
```

The script installs `axel`, `aria2` (needed for SOCKS5 proxy URLs), and `ffmpeg`/`ffprobe`. Create the staging and final directories on that server, and ensure the SSH user configured in the dashboard can create, write, and rename files in both. The dispatcher checks that the two directories are on the same filesystem before promotion.

### 3. Configure SSH key trust and access

Create or select a dedicated SSH key pair for the daemon. Install its public key in the storage account's `authorized_keys`; copy the private key to the orchestration VPS and restrict its permissions to the service account. For example:

```bash
sudo install -d -o media-ingestion -g media-ingestion -m 0700 \
  /var/lib/media-ingestion/ssh
sudo install -o media-ingestion -g media-ingestion -m 0600 \
  /secure/source/id_ed25519 /var/lib/media-ingestion/ssh/id_ed25519
```

Pin the storage server's SSH host key in `/var/lib/media-ingestion/ssh/known_hosts`. Verify its fingerprint using a trusted, out-of-band source before adding it; do not blindly trust an unverified `ssh-keyscan` result. The API rejects unknown host keys.

### 4. Install Python dependencies and environment

The backend requires Python 3.10 or newer.

```bash
sudo -u media-ingestion python3 -m venv /opt/media-ingestion/venv
sudo -u media-ingestion /opt/media-ingestion/venv/bin/pip install \
  -r /opt/media-ingestion/backend/requirements.txt
sudo -u media-ingestion cp /opt/media-ingestion/backend/.env.example \
  /opt/media-ingestion/backend/.env
```

Generate a Fernet key and put it in `APP_ENCRYPTION_KEY` in `backend/.env`:

```bash
/opt/media-ingestion/venv/bin/python -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Keep this key backed up separately and stable. Losing or changing it prevents decryption of saved account passwords, proxy URLs, and queued stream URLs.

Configure at minimum:

```dotenv
APP_ENCRYPTION_KEY=<generated-fernet-key>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<unique-password-of-at-least-12-characters>
SESSION_SECRET_KEY=<random-session-signing-key>
SESSION_COOKIE_SECURE=true
DATABASE_URL=sqlite:///./ingestion.db
CORS_ORIGINS=https://dashboard.example.com
SSH_KNOWN_HOSTS=/var/lib/media-ingestion/ssh/known_hosts
```

Set the `.env` file to owner-readable only:

```bash
sudo chown media-ingestion:media-ingestion /opt/media-ingestion/backend/.env
sudo chmod 0600 /opt/media-ingestion/backend/.env
```

Other supported settings are `SESSION_TTL_SECONDS` (default `28800`), `POLL_INTERVAL_SECONDS` (default `300`), `WORKER_POLL_SECONDS` (default `2`), `MAX_RETRIES` (default `5`), `REQUEST_TIMEOUT_SECONDS` (default `30`), and `REMOTE_JOB_TIMEOUT_SECONDS` (default `86400`).

### 5. Install and start systemd service

The checked-in unit assumes the paths above and runs Uvicorn on `127.0.0.1:8000`.

```bash
sudo install -m 0644 /opt/media-ingestion/backend/media-ingestion.service \
  /etc/systemd/system/media-ingestion.service
sudo systemctl daemon-reload
sudo systemctl enable --now media-ingestion
sudo systemctl status media-ingestion
curl http://127.0.0.1:8000/health
```

Service logs:

```bash
sudo journalctl -u media-ingestion -f
```

The SQLite database is relative to the service working directory: by default it is `/opt/media-ingestion/backend/ingestion.db`. The systemd unit permits writes under the backend directory for SQLite and its WAL files.

### 6. Build and serve the dashboard

For local development on the VPS or a trusted workstation:

```bash
cd /opt/media-ingestion/frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to `http://127.0.0.1:8000`; this development proxy avoids browser CORS for API calls.

For production, build static assets with `npm ci && npm run build` in `frontend/`, serve `frontend/dist/` with a web server, and reverse-proxy `/api/` and `/health` to `http://127.0.0.1:8000`. Terminate TLS and enable authentication at the proxy. For example, an Nginx virtual host should serve the SPA from `frontend/dist` and include:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location = /health {
    proxy_pass http://127.0.0.1:8000/health;
}

location / {
    try_files $uri $uri/ /index.html;
}
```

If the dashboard and API are served from different browser origins, set `CORS_ORIGINS` to a comma-separated list of exact frontend origins, including scheme and port, e.g. `https://dashboard.example.com` or `http://localhost:5173`, then restart the service. Do not use `*` as a substitute for an authenticated deployment. CORS is a browser policy, not API authentication.

## First-time application configuration

1. Open the dashboard through the trusted local/VPN/authenticated reverse-proxy endpoint and sign in using `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
2. Add an authorized provider account. The password is encrypted at rest.
3. In **Remote storage node**, enter the storage server host, SSH port, username, private-key path on the VPS, staging directory, and default storage root. Use **Test connection & save**.
4. Add optional HTTP(S) or SOCKS5 proxies. Proxy URLs are encrypted at rest.
5. In **Category Watcher**, enable only categories you are authorized to ingest and optionally set a distinct remote destination path for each.
6. Monitor **Overview & Queue** for download, verification, completion, and retry status.

## API routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness check |
| `POST` | `/api/auth/login` | Start an admin session |
| `GET` | `/api/auth/session` | Check current session / CSRF token |
| `POST` | `/api/auth/logout` | End the admin session |
| `GET` | `/api/status` | Worker, SSH, and queue counters |
| `GET` | `/api/categories` | Provider categories and monitoring state |
| `POST` | `/api/categories/toggle` | Enable/disable monitoring and set an optional destination override |
| `GET` | `/api/queue` | List queue items |
| `POST` | `/api/queue/retry` | Retry a failed item |
| `GET` / `POST` / `DELETE` | `/api/accounts` | Provider account management |
| `GET` / `POST` / `DELETE` | `/api/proxies` | Proxy pool management |
| `POST` | `/api/proxies/{proxy_id}/test` | Test proxy IP and latency |
| `GET` / `POST` | `/api/settings/ssh` | Read/save SSH node and optionally test connection |

The interactive OpenAPI documentation is at `/docs` on the API origin; keep it behind the same access controls as the API.

## Operational notes

- A worker is created for each enabled provider account, enforcing one active download per account.
- Queue stream IDs are unique. Existing queue items are not re-created when polled again.
- Failed jobs are retried with exponential backoff up to `MAX_RETRIES`; manual retry is also available.
- SSH host-key checking is strict. The daemon must trust the storage server key before a test or download can succeed.
- The remote node needs `axel`, `aria2c` for SOCKS5, and `ffprobe`. Remote SSH egress and provider/proxy network policy must allow the requested stream.
- Back up both `backend/ingestion.db` and the encryption key securely.

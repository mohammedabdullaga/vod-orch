# Media Ingestion Engine

A headless FastAPI service polls configured provider categories, stores discovered work in SQLite, and dispatches one download at a time per enabled provider account to a remote storage server over SSH. Downloads go to a remote staging directory, are checked with `ffprobe`, then moved into the watch folder. A React dashboard manages accounts, categories, proxies, the SSH node, and the queue. The dashboard/API require the configured admin login.

Use this software only with media you own or are authorized to access, and with provider-approved APIs and download methods. Provider API paths and stream URL formats vary; the built-in adapter assumes an Xtream-style `player_api.php` API and may need adapting for your provider.

## Project structure

```text
backend/
  app/
    main.py                 FastAPI routes and service lifecycle
    models.py               SQLite / SQLAlchemy models, provider cache and storage routing
    config.py               Environment configuration
    security.py             Fernet encryption and secret masking
    provider.py             Provider API and stream URL adapter
    routers/
      content.py            Content discovery and manual queue endpoints
    services/
      content_paths.py      Shared identity-safe relative destinations
      poller.py             Category polling and queue ingestion
      worker_pool.py        One worker per enabled account
      dispatcher.py         SSH, remote download, probe and promotion
  .env.example              Backend configuration template
  media-ingestion.service   systemd unit
  requirements.txt          Python dependencies
frontend/
  src/App.jsx               Authenticated dashboard and tab navigation
  src/ContentBrowser.jsx    Grid/list browser, selection, episode inspection and batch actions
  src/index.css             Tailwind and global styles
  package.json              Frontend scripts and dependencies
  vite.config.js            Development API proxy
setup.sh                    Install axel, aria2 and ffmpeg on the storage server
DOWNLOADING_AND_CONNECTIONS.md
                            Download lifecycle and VPS/storage network flow
.gitignore
```

For a detailed description of the download lifecycle, network connections, and transfer direction, see [Downloading and server connections](./DOWNLOADING_AND_CONNECTIONS.md).

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

In **Settings & Accounts → Storage servers & routing**, add each remote server with its own SSH credentials, remote **staging directory**, and **storage root** (absolute Linux paths, such as `/srv/media/staging` and `/srv/media/library`). Use **Test connection & save** to verify the connection and writable directories. Staging and each final destination must be on the same filesystem for atomic promotion. Assign one server to **Series** and one to **Movies**; either server may be assigned to both types.

Existing single-server installations are migrated automatically: the existing server is assigned to both Movies and Series. Add the second server and update the two assignments in the dashboard. A node cannot be removed while it is assigned; reassign or clear its content routes first. Pending jobs use the current assignment when a worker starts them.

Each watched category can inherit its assigned server's storage root (leave its override blank) or set a category-specific absolute remote path in **Category Watcher**. Category and server roots are read when each queued job starts, so changing them routes pending/retried jobs to the new path; already-running and completed jobs are not moved automatically. Category paths must be absolute Linux paths, cannot be `/` or contain `..`, and are created on the assigned server as needed. Jobs with no server assigned remain pending until a route is configured.

For a watched **series** category, enable **Match new episodes to an existing series folder** to search immediate subfolders of that category's custom destination path. The worker compares the provider series title with existing directory names, applies the category's configured strip prefixes first, and routes an episode into the best close match only when it is unique. No match or an ambiguous match fails explicitly instead of silently creating or choosing a potentially incorrect series folder. Prefixes are comma-separated leading labels (for example `4K-ar-fast` or `AR-SUBS`); those configured prefixes and recognized release-tag chains such as `AR-SUBS` and `4K-ar-fast` are stripped from category, series, and movie names when building new paths and filenames. Automatic detection is deliberately limited to recognizable tag patterns; add unusual labels in the category's prefix setting rather than stripping arbitrary words. Existing completed files and folders are not renamed.

The **Browse & Ingest** tab supports poster-grid and compact-list views. Choose **Provider API proxy** to route category, movie/series list, and series-detail requests through an enabled proxy, or choose **Direct connection**. Provider responses are cached in SQLite by browsing account, action, and provider identifiers; catalog categories and lists default to a 30-minute cache, and series episode details default to a 6-hour cache. Polling and browsing reuse these mappings, including the provider's original IDs, instead of querying series details on every poll. The **Refresh from provider** button forces a refresh for the selected categories and their title lists. When refresh fails but a stored response exists, the service serves that stale response and logs the refresh failure; API responses identify it with `X-Provider-Catalog-Cache: stale`. Configure `PROVIDER_CATALOG_TTL_SECONDS` and `PROVIDER_SERIES_DETAILS_TTL_SECONDS` to adjust these intervals. The same provider proxy selection is available in **Category Watcher** and applies to its provider category list. The selection is stored in SQLite and also applies to automatic background polling; API requests without an explicit proxy choice use the saved selection, while `proxy_id=0` explicitly requests a direct connection. Download proxy routing is separate and is selected when queueing. **Browse selected** checks each selected category's destination on its routed storage server over SSH, then compares provider movie titles with actual video files and series titles with files under matching series folders. Series are marked **Partial** unless stored `SxxEyy` files cover the provider's known episode count; missing items are highlighted and can be filtered before queueing. Results are otherwise marked **On storage**, **Missing**, or **Storage unknown**. A failed or unavailable SSH scan is shown as unknown, never as missing. Browse again to refresh the inventory after downloads finish. Clicking a title card or row selects it; use **Inspect / Episodes** to inspect series seasons and choose individual episodes. **Ingest Selected via Workers** queues content immediately through the configured type-to-server route. **Export / Run as Direct Bash Script** downloads a standalone script for execution on that storage server (for example inside `tmux`); it is not executed by the VPS. The export contains provider stream URLs with account credentials, so confirm the warning, store the file securely, and delete it after use.

In **Browse & Ingest**, choose which enabled Xtream account fetches the categories, titles, and series episode metadata. Before a browser batch is queued, choose one or more idle download accounts; these may have different Xtream usernames, passwords, or provider endpoints from the browsing account. Each queued stream keeps the browsed provider's stream ID and metadata, while the assigned worker constructs the download URL with its own account credentials. This relies on the provider accounts exposing matching stream IDs and content. Selected jobs are distributed across the download accounts, each of which handles only one active job at a time. The request rechecks account availability before enqueueing. Choose either a direct connection or any enabled proxy from the proxy pool for that batch. The selected proxy ID is stored per queue item and revalidated when the worker starts; disabling or deleting it before dispatch fails the job explicitly rather than silently switching routes. Automatic watched downloads continue to use the routed server's configured proxy when enabled.

Each storage server defaults to 12 Axel connections (configurable from 1 to 12 per node or by setting `DEFAULT_AXEL_CONNECTIONS` in `backend/.env` for new nodes). Existing nodes keep their saved connection counts; the earlier one-time migration changed only nodes that still had the old default of 4. New accounts and download commands default to the standard `Mozilla` User-Agent; an account's explicitly configured User-Agent is preserved. Daemon Axel jobs use quiet mode without ANSI progress output, while the standalone script uses the terminal-friendly Axel output. Wget uses the reference script's resume/output, 99-retry, retry-on-connection-refused, User-Agent, and explicit proxy/direct settings; it downloads each file over one connection and cannot use SOCKS5. SOCKS5 with the default choice uses `aria2c`; direct and HTTP(S)-proxy jobs with the default choice use Axel. File size and current transfer rate are sampled from remote staging-file growth, so they are available for both downloaders; percentage is shown when the selected downloader reports one. The speed is the latest sample after transfer completion. The queue supports cancelling pending and active downloads; active cancellation stops the remote process and removes its staging partial and Axel state file. Completed queue rows show the final remote path.

Use **Clear downloaded list** in **Overview & Queue** to hide completed items from the dashboard and completed count. This does not remove media files or completion records, so cleared streams remain protected from duplicate queueing.

Each storage root contains a folder per provider category. Movie files are placed directly inside their category folder; series use a series folder and season subfolders:

```text
<destination root>/
  <movie category>/<movie title>.mkv
  <series category>/<series name> [<series id>-<source digest>]/Season 01/S01E01.mkv
```

If a category-specific destination path is set, that path is used as the category folder itself. Otherwise the cleaned category name is created beneath the storage root. Movie titles never create their own directories. Queue deduplication keys include provider base URL, content type, and stream ID, and a no-clobber atomic move prevents an existing final file from being silently overwritten. The app does not infer identity from a title or merge series folders with similar names.

Staging follows the same relative directory layout as the final destination. Each temporary filename has a `.part-<job id>` suffix so concurrent work cannot collide; after `ffprobe` succeeds, the file is atomically promoted to its final name.

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

The script installs `axel`, `aria2` (needed for SOCKS5 proxy URLs), `wget` (available as an optional manual-batch downloader), and `ffmpeg`/`ffprobe`. Create the staging and final directories on that server, and ensure the SSH user configured in the dashboard can create, write, and rename files in both. The dispatcher checks that the two directories are on the same filesystem before promotion.

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

Other supported settings are `SESSION_TTL_SECONDS` (default `28800`), `POLL_INTERVAL_SECONDS` (default `300`), `PROVIDER_CATALOG_TTL_SECONDS` (default `1800`), `PROVIDER_SERIES_DETAILS_TTL_SECONDS` (default `21600`), `WORKER_POLL_SECONDS` (default `2`), `MAX_RETRIES` (default `5`), `REQUEST_TIMEOUT_SECONDS` (default `30`), and `REMOTE_JOB_TIMEOUT_SECONDS` (default `86400`).

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
| `POST` | `/api/queue/{item_id}/cancel` | Cancel a pending or active download |
| `POST` | `/api/queue/stop-all-and-clear` | Stop active jobs and hide all queue entries without deleting media files |
| `GET` | `/api/content/categories?type=series\|movie` | Provider categories and watch state |
| `GET` | `/api/content/items?type=series\|movie&category_ids=1,2` | Titles in selected categories |
| `GET` | `/api/content/series/{series_id}/details` | Seasons and normalized episode metadata |
| `POST` | `/api/queue/manual-batch` | Queue selected movies or episodes immediately |
| `POST` | `/api/content/generate-script` | Download a standalone credential-bearing Bash script for selected titles/episodes |
| `GET` / `POST` / `DELETE` | `/api/accounts` | Provider account management |
| `GET` | `/api/accounts/availability?content_type=SERIES\|MOVIE` | List idle accounts and enabled proxy choices for the current provider |
| `GET` / `POST` / `DELETE` | `/api/proxies` | Proxy pool management |
| `PUT` | `/api/proxies/{proxy_id}/enabled` | Enable/disable a proxy assigned to a storage node |
| `POST` | `/api/proxies/{proxy_id}/test` | Test proxy IP and latency |
| `GET` / `POST` | `/api/settings/ssh` | Legacy single-node settings compatibility |
| `GET` / `PUT` | `/api/settings/provider-proxy` | Persist the provider API proxy used for browsing and background polling |
| `GET` / `POST` / `PUT` / `DELETE` | `/api/nodes` | List, add, update, or remove storage servers |
| `GET` / `PUT` | `/api/settings/storage-routing` | Assign Series and Movies to their storage server |

The interactive OpenAPI documentation is at `/docs` on the API origin; keep it behind the same access controls as the API.

## Operational notes

- A worker is created for each enabled provider account and reconciled on every worker-pool poll; adding an account wakes the pool immediately. Each queue item is claimed by one idle worker for the same provider endpoint, and its stream URL is rebuilt with that worker's credentials.
- Manual browser queue requests wake the account worker immediately and use the same provider/type/stream identity and safe destination layout as polled items. Repeated queued/completed items are reported as skipped.
- On daemon startup, interrupted `DOWNLOADING` and `VERIFYING` jobs are reset to `PENDING` for recovery.
- Queue stream IDs are unique. Existing queue items are not re-created when polled again.
- Failed jobs are retried with exponential backoff up to `MAX_RETRIES`; manual retry is also available.
- **Stop all & clear all** cancels active work, cancels pending work and hides every queue entry. Records remain internally as deduplication tombstones; completed media files are never deleted by this action. Monitored categories remain enabled and can enqueue future new streams.
- SSH host-key checking is strict. The daemon must trust the storage server key before a test or download can succeed.
- The remote node needs `axel`, `aria2c` for SOCKS5, and `ffprobe`. Remote SSH egress and provider/proxy network policy must allow the requested stream.
- Back up both `backend/ingestion.db` and the encryption key securely.

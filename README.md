# Media Ingestion Engine

A headless FastAPI service polls configured provider categories, stores discovered work in SQLite, and dispatches one download at a time per enabled provider account to a remote storage server over SSH. Downloads go to a remote staging directory, are checked with `ffprobe`, then moved into the watch folder. A React dashboard manages accounts, categories, proxies, the SSH node, and the queue. The dashboard/API require the configured admin login.

Use this software only with media you own or are authorized to access, and with provider-approved APIs and download methods. Provider API paths and stream URL formats vary; the built-in adapter assumes an Xtream-style `player_api.php` API and may need adapting for your provider.

## Project structure

```text
backend/
  app/
    main.py                 FastAPI routes, authenticated task-log stream and lifecycle
    models.py               SQLite / SQLAlchemy models, queue logs, provider cache and routing
    config.py               Environment configuration
    security.py             Fernet encryption and secret masking
    provider.py             Compatibility exports for the provider client
    routers/
      content.py            Content discovery and manual queue endpoints
    services/
      provider_client.py    Xtream API requests, catalog cache and stream URLs
      catalog_index.py     Provider-profile-scoped normalized catalog upserts
      catalog_sync_worker.py  Background Xtream category/title/episode sync
      m3u.py                M3U fetching, parsing and catalog persistence
      content_paths.py      Shared identity-safe relative destinations
      poller.py             Category polling and queue ingestion
      worker_pool.py        One worker per enabled account
      dispatcher.py         Bounded SSH, live output capture, remote download, probe and promotion
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

The queue's **View log** action streams each task's latest output and dispatched command from the authenticated API. The command contains provider credentials and is encrypted at rest in the local SQLite database; it is decrypted only for authenticated administrators. Protect the database, encryption key, and admin session accordingly.

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

Existing single-server installations are migrated automatically: the existing server is assigned to both Movies and Series. Add the second server and update the two assignments in the dashboard. A node cannot be removed while it is assigned or selected by an unfinished manually routed task; reassign or clear its content routes/tasks first. Pending jobs use the current automatic assignment when a worker starts them.

Each watched category can inherit its assigned server's storage root (leave its override blank) or set a category-specific absolute remote path in **Category Watcher**. Category and server roots are read when each queued job starts, so changing them routes pending/retried jobs to the new path; already-running and completed jobs are not moved automatically. Category paths must be absolute Linux paths, cannot be `/` or contain `..`, and are created on the assigned server as needed. Jobs with no server assigned remain pending until a route is configured.

For a watched **series** category, enable **Match new episodes to an existing series folder** to search immediate subfolders of that category's custom destination path. The worker compares the provider series title with existing directory names, applies the category's configured strip prefixes first, and routes an episode into the best close match only when it is unique. No match or an ambiguous match fails explicitly instead of silently creating or choosing a potentially incorrect series folder. New series folders use the cleaned human-readable series title, with a hidden identity marker to keep same-title series from different providers separate. A legacy ID-suffixed folder is recognized by its provider-specific suffix and renamed to the readable title after the last queued episode for that series finishes, but only when the readable destination is unused; conflicts remain separate rather than being merged. Prefixes are comma-separated leading labels (for example `4K-ar-fast` or `AR-SUBS`); those configured prefixes and recognized release-tag chains such as `AR-SUBS`, `AR-IN-D`, and `4K-ar-fast` are stripped from category, series, and movie names when building new paths and filenames. The known generated `[provider-id-hash]` suffix is also removed from names before creating paths. Automatic detection is deliberately limited to recognizable tag patterns; add unusual labels in the category's prefix setting rather than stripping arbitrary words.

Browse requests are accepted as short background jobs and polled by the dashboard. Browsing reads only the selected profile's normalized SQLite catalog; it never fetches provider categories or title lists. The background browse job checks storage inventory over SSH, while paginated title responses are read from the local index.

The **Browse & Ingest** tab supports poster-grid and compact-list views. Categories and titles are stored by provider profile (normalized provider base URL), not by credential. `GET /api/content/items` accepts `type`, comma-separated `category_ids`, optional `account_id` to select a credential within the profile, `search`, `inventory_status`, `limit` (default 50, maximum 100), `offset`, and a backward-compatible `refresh` parameter; it returns an `{items, total, limit, offset, has_more}` page from SQLite. A missing local catalog returns `409` with instructions to sync the profile. The dashboard fetches another 50 titles only when requested and sends title searches and inventory filters to the server.

Xtream catalog sync refreshes the profile's categories and titles from the provider, indexes them in SQLite, and caches episode mappings for series. Sync runs in explicit order: fetch and persist series/movie categories; request each complete `get_vod_streams` and `get_series` catalog without a `category_id` filter, as the Android client does; partition the returned rows locally by their `category_id`; then fetch each series detail response to cache seasons, episodes, and episode IDs. A stream whose category ID is missing from the category response causes sync to fail rather than report a partial catalog as complete. The progress bar advances across these stages as category rows are indexed and series details are processed. The counters also report cached seasons, episodes, and IDs. It is the only dashboard action that updates the local catalog; browsing, searching, pagination, and category selection use local rows. Series detail requests are throttled and cached; the UI selects the first enabled credential in the profile for catalog sync and browsing. The service assumes credentials grouped under the same normalized provider URL expose compatible category and stream IDs. Only add sibling credentials to a profile when that assumption is true. When queueing, enabled credentials in the selected profile can be chosen as download workers; the selection is persisted per task and each credential runs at most one active download. Bulk series queueing prepares one series at a time; missing-only batches scan each category destination once. Provider API responses remain cached per credential for request efficiency, but normalized browse rows are shared at profile scope. When a profile is deleted, all its credentials, catalog/index/cache rows, category settings, and queue history are cleared after active work has stopped. A one-time migration seeds the new profile catalog from the lowest-ID credential's previous indexed rows; sync the profile to refresh it from the provider. Configure `PROVIDER_CATALOG_TTL_SECONDS` and `PROVIDER_SERIES_DETAILS_TTL_SECONDS` to adjust provider-response cache freshness. The provider proxy selection applies to sync and background polling; download proxy routing is selected separately when queueing. Xtream stream URLs are assembled using the configured provider host and the selected worker credential, then passed to the storage-side downloader so redirects are resolved by the storage server. **Browse selected** scans category destinations over SSH, compares movie titles with actual files, and matches series folders conservatively; ambiguous matches remain unknown. The series inspector can select only missing episodes and queueing rechecks inventory to avoid duplicates. A missing or unreadable storage inventory is never treated as evidence that all episodes are absent. **Ingest Selected via Workers** supports automatic and manual storage destinations. Pending/retried manually routed jobs keep their chosen server and path. Standalone scripts contain provider credentials; store them securely and delete them after use.

### Xtream catalog synchronization

In **Settings & Accounts**, add credentials under a provider profile. Enter credentials manually or select **M3U link(s)** to extract the provider URL and credentials from Xtream `get.php` links. A profile is keyed by its normalized provider base URL. Sync and browse use its first enabled credential; manual sync fetches categories, the unfiltered movie and series catalogs, and then each series' season/episode details in sequence. Stream rows are assigned locally using `category_id`; browsing never makes category/title-list requests to the provider. The job persists stage progress and cached season, episode, and episode-ID counts in SQLite, and the dashboard displays a progress bar. A failed sync can be retried with another enabled profile credential. Provider responses are cached by provider URL, username, action, and identifiers; normalized categories and titles are shared by profile. This design assumes same-profile credentials expose compatible IDs and catalogs. Only group credentials that satisfy that assumption. In **Browse & Ingest**, choose a provider profile, search and page through its local index, inspect series episodes, and select any enabled credentials in the same profile as download workers. Each selected credential is limited to one active download.

Use **Delete profile & catalog** to remove every credential in a profile and clear its local catalog, provider response cache, category settings, and queue history. The dashboard asks for confirmation, and deletion is blocked while any profile download is active. Individual credentials can be removed while other credentials remain; deleting the last credential requires deleting the profile. A one-time migration seeds the profile catalog from the lowest-ID credential's previous indexed rows. Run a sync after migration to update it from the provider. Completed media files on storage are not deleted.

M3U tables and routes remain available for compatibility with existing saved playlists and older clients, but M3U import and browsing have been removed from the primary dashboard workflow. Existing saved M3U data is not automatically deleted.

Xtream jobs retain their source profile URL and provider stream ID. Eligible worker credential IDs are recorded when manually queueing; automatic polling uses enabled credentials in that profile. Retries stay within the same eligible profile pool, and each account runs at most one active download. The request rechecks worker availability before enqueueing. Choose either a direct connection or any enabled proxy for the batch. The selected proxy ID is stored per queue item and revalidated when the worker starts; disabling or deleting it before dispatch fails the job explicitly rather than silently switching routes. Automatic watched downloads continue to use the routed server's configured proxy when enabled.

Failed queue items offer **Clean partial** to stop the known remote download process, delete only that job's staging partial/state/PID files, and remove empty directories created along its known destination. It never recursively removes folders or deletes existing media files. After cleanup, use **Retry** to try a different eligible provider account if one is available. Remote transfer attempts also fail after `REMOTE_DOWNLOAD_IDLE_TIMEOUT_SECONDS` without byte progress (default 300 seconds); `REMOTE_JOB_TIMEOUT_SECONDS` remains the overall command limit.

Each storage server defaults to 12 Axel connections (configurable from 1 to 12 per node or by setting `DEFAULT_AXEL_CONNECTIONS` in `backend/.env` for new nodes). Existing nodes keep their saved connection counts; the earlier one-time migration changed only nodes that still had the old default of 4. Axel download commands on all storage nodes and in generated standalone scripts use the configurable `MASTER_USER_AGENT` from `backend/.env`, defaulting to `VLC/3.0.18 LibVLC/3.0.18`. Account-specific User-Agents continue to apply to Wget/aria2c downloads and provider API requests. Daemon Axel jobs use quiet mode without ANSI progress output, while the standalone script uses the terminal-friendly Axel output. Wget uses the reference script's resume/output, 99-retry, retry-on-connection-refused, User-Agent, and explicit proxy/direct settings; it downloads each file over one connection and cannot use SOCKS5. SOCKS5 with the default choice uses `aria2c`; direct and HTTP(S)-proxy jobs with the default choice use Axel. File size and current transfer rate are sampled from remote staging-file growth, so they are available for both downloaders; percentage is shown when the selected downloader reports one. The speed is the latest sample after transfer completion. The queue supports cancelling pending and active downloads; active cancellation stops the remote process and removes its staging partial and Axel state file. Completed queue rows show the final remote path.

Use **Clear downloaded list** in **Overview & Queue** to hide completed items from the dashboard and completed count. This does not remove media files or completion records, so cleared streams remain protected from duplicate queueing.

Each storage root contains a folder per provider category. Movie files are placed directly inside their category folder; series use a series folder and season subfolders:

```text
<destination root>/
  <movie category>/<movie title>.mkv
  <series category>/<series name> [<series id>-<source digest>]/Season 01/S01E01.mkv
```

If a category-specific destination path is set, that path is used as the category folder itself. Otherwise the cleaned category name is created beneath the storage root. Movie titles never create their own directories. Queue deduplication keys include provider base URL, content type, and stream ID, and a no-clobber atomic move prevents an existing final file from being silently overwritten. Readable series folder names are backed by a hidden provider/series identity marker; a title collision uses the prior ID-qualified folder name instead of merging different series.

Each task stages in its own directory under `<staging root>/.vod-orch-jobs/<task id>/`, with a `.part-<job id>` filename. Isolating task directories prevents concurrent cleanup from removing another download's directory or PID file. Older task cleanup also checks the previous relative-layout staging location. After `ffprobe` succeeds, the file is atomically promoted to its final name.

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

Other supported settings are `SESSION_TTL_SECONDS` (default `28800`), `POLL_INTERVAL_SECONDS` (default `300`), `PROVIDER_CATALOG_TTL_SECONDS` (default `1800`), `PROVIDER_SERIES_DETAILS_TTL_SECONDS` (default `21600`), `WORKER_POLL_SECONDS` (default `2`), `MAX_RETRIES` (default `20`), `REQUEST_TIMEOUT_SECONDS` (default `30`), and `REMOTE_JOB_TIMEOUT_SECONDS` (default `86400`).

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
2. Add an authorized provider account. The password is encrypted at rest. Use **Sync catalog** on the account to cache the provider categories, title lists, and series episode metadata on the VPS.
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
| `GET` | `/api/queue` | List the latest queue items; optional `status` filter fetches that status independently |
| `POST` | `/api/queue/retry` | Retry a failed item |
| `POST` | `/api/queue/requeue-pending` | Reset pending assignments/retry history, wake workers, and report route/account eligibility |
| `POST` | `/api/queue/{item_id}/cancel` | Cancel a pending or active download |
| `POST` | `/api/queue/stop-all-and-clear` | Stop active jobs and hide all queue entries without deleting media files |
| `POST` | `/api/tasks/purge-history` | Delete selected queue history and reconcile stale worker state |
| `GET` | `/api/content/categories?type=series\|movie&account_id=1` | Local provider-profile categories and watch state |
| `GET` | `/api/content/items?type=series\|movie&category_ids=1,2&limit=50&offset=0&search=title` | Local profile-scoped searchable title page with total and `has_more` |
| `GET` | `/api/content/series/{series_id}/details` | Seasons and normalized episode metadata |
| `POST` | `/api/content/catalog-sync` | Enqueue background Xtream catalog sync for an account |
| `GET` | `/api/content/catalog-sync` | Latest sync job for each provider profile |
| `GET` | `/api/content/catalog-sync/{job_id}` | Progress and result for one sync job |
| `POST` | `/api/content/catalog-sync/{job_id}/stop` | Stop a queued or running profile sync |
| `POST` | `/api/content/catalog-sync/{job_id}/retry` | Retry a failed sync with the next unused enabled profile account and selected proxy/direct route |
| `POST` | `/api/queue/manual-batch` | Queue selected movies or episodes immediately |
| `POST` | `/api/content/generate-script` | Download a standalone credential-bearing Bash script for selected titles/episodes |
| `GET` / `POST` / `DELETE` | `/api/accounts` | Provider account management |
| `DELETE` | `/api/provider-profiles` | Confirm and clear one provider profile and its catalog/queue data |
| `GET` | `/api/accounts/availability?content_type=SERIES\|MOVIE&source_account_id=1` | List idle workers in the selected provider profile |
| `GET` / `POST` / `DELETE` | `/api/proxies` | Proxy pool management |
| `PUT` | `/api/proxies/{proxy_id}/enabled` | Enable/disable a proxy assigned to a storage node |
| `POST` | `/api/proxies/{proxy_id}/test` | Test proxy IP and latency |
| `GET` / `POST` | `/api/settings/ssh` | Legacy single-node settings compatibility |
| `GET` / `PUT` | `/api/settings/provider-proxy` | Persist the provider API proxy used for catalog sync and background polling |
| `GET` / `POST` / `PUT` / `DELETE` | `/api/nodes` | List, add, update, or remove storage servers |
| `GET` / `PUT` | `/api/settings/storage-routing` | Assign Series and Movies to their storage server |
| `POST` | `/api/playlists/import` | Legacy: enqueue an M3U fetch on the VPS and return a job ID |
| `POST` | `/api/playlists/{playlist_id}/refresh` | Legacy: enqueue an M3U refresh on the VPS |
| `GET` | `/api/playlists/jobs/{job_id}` | Legacy: read M3U fetch/import job status and result |

The interactive OpenAPI documentation is at `/docs` on the API origin; keep it behind the same access controls as the API.

## Operational notes

- A worker is created for each enabled provider account and reconciled on every worker-pool poll; adding an account wakes the pool immediately. Xtream jobs are scoped to a provider profile (normalized provider URL), and the storage URL is rebuilt with the selected eligible worker's credentials. Manually queued jobs persist the selected worker IDs; automatic profile jobs can use any enabled credential in that profile. Legacy URL-only items remain restricted to a matching provider URL.
- An account is marked busy only while a download is `DOWNLOADING`, `VERIFYING`, or `CANCELLING`. `PENDING` items are queued reservations, not active transfers; additional work can be queued for the same account and its worker processes those items serially.
- Manual browser queue requests wake the account worker immediately and use the same provider/type/stream identity and safe destination layout as polled items. Repeated queued/completed items are reported as skipped.
- On daemon startup and every 15 seconds, the worker pool reconciles interrupted or orphaned active jobs. Recovered jobs return to `PENDING` with their attempted-account history cleared, so an account that previously claimed the task cannot strand it. Account busy state is in-memory; reconciliation releases stale worker flags rather than updating a persisted account lease.
- Pending tasks older than 60 seconds are automatically requeued when an eligible idle worker exists. Xtream workers must belong to the source profile, and manually selected worker IDs are honored; M3U tasks retain the shared worker pool. The scheduler releases stale account assignments, logs recovery in the task log, and wakes workers.
- The **Force requeue pending** action on the queue resets the account assignment, retry history, and last error for all visible pending tasks, logs the manual reset, and immediately wakes workers. The response reports how many items have a configured storage route and compatible enabled account, plus any blocked content kinds and worker-pool health; requeueing itself does not decrement the pending count until a worker claims a task.
- Xtream catalog sync runs in a VPS backend worker and persists its cache, status, and progress in SQLite; the dashboard polls progress instead of holding a Cloudflare-proxied request open. Interrupted syncs are re-queued from the beginning after daemon restart. M3U imports remain supported by legacy API routes but are not exposed in the primary dashboard.
- Queue stream IDs are unique. Existing queue items are not re-created when polled again.
- Failed jobs are retried automatically with exponential backoff up to `MAX_RETRIES` (default `20`, with delays capped at one hour); due retries are released to an eligible worker in the same profile. The queue shows whether a retry is scheduled, the next attempt number, or whether retries are exhausted. Manual retry starts a fresh retry cycle within the item's eligible profile worker set.
- **Stop all & clear all** cancels active work, cancels pending work and hides every queue entry. Records remain internally as deduplication tombstones; completed media files are never deleted by this action. Monitored categories remain enabled and can enqueue future new streams.
- **Clean Task History & Reset Queue** in Settings can delete all history, only failed/completed items, only pending items, or reconcile stale worker state without deleting task records. Selecting both deletion scopes performs a full reset: it stops active jobs, batches staging cleanup by storage server to avoid reconnecting for every task, deletes every queue record and its deduplication history, and reconciles stale account/worker state. The same content can then be queued again. Existing final media is preserved and is not overwritten. API requests taking at least five seconds are logged with method, path, status, and elapsed time to help diagnose proxy timeouts.
- SSH host-key checking is strict. The daemon must trust the storage server key before a test or download can succeed.
- The remote node needs `axel`, `aria2c` for SOCKS5, and `ffprobe`. Remote SSH egress and provider/proxy network policy must allow the requested stream.
- Back up both `backend/ingestion.db` and the encryption key securely.

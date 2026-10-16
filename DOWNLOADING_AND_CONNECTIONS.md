# Downloading and VPS-to-storage connections

This guide describes how a download moves through the Media Ingestion Engine and which machine handles each connection. The orchestration VPS coordinates work; the storage server downloads and stores the media bytes.

## Machines and responsibilities

### Orchestration VPS

The VPS runs the FastAPI application, SQLite database, category poller, and asynchronous worker pool. It:

- Connects to the provider's player API to discover categories, titles, and episodes.
- Stores queue state and encrypted provider/proxy credentials in SQLite.
- Assigns at most one active queue item to each enabled provider account.
- Opens an SSH connection to the storage server to run and observe the remote job.
- Receives progress and size/rate samples over the SSH command channel.
- Serves the authenticated dashboard/API, commonly through a local reverse proxy.

The VPS does **not** relay or proxy the media file bytes between the provider and the storage server.

### Remote storage server

The storage server is the download worker host. It needs:

- SSH access for the daemon's configured Linux user and private key.
- `axel`, `aria2c` (for SOCKS5 proxy jobs using the default downloader), `wget` (optional manual-batch downloader), and `ffprobe`.
- Write access to both the configured staging directory and the final storage destination.
- Outbound network access to the provider stream host, directly or through the selected/configured proxy.

The storage server connects directly to the provider stream host (or selected proxy) and writes the media bytes to its own disk.

## Connection map and ports

```text
Admin browser
    | HTTPS (recommended, usually TCP 443)
    v
Reverse proxy on VPS
    | HTTP to 127.0.0.1:8000
    v
FastAPI + poller + workers on VPS
    | HTTPS to provider API (usually TCP 443; provider may use HTTP/TCP 80)
    | SSH over TCP 22 (or configured SSH port) to storage server
    v
Remote storage server
    | HTTP/HTTPS to provider stream host (usually TCP 80/443)
    | Optional HTTP(S) or SOCKS5 proxy, according to the chosen job settings
    | local disk: staging -> ffprobe -> atomic rename into final library
```

| Connection | Initiator | Purpose |
| --- | --- | --- |
| Browser -> VPS reverse proxy | Browser | Dashboard pages and authenticated `/api` requests |
| VPS -> provider API | VPS | Category polling and provider metadata/content discovery |
| VPS -> storage SSH | VPS | Start the downloader, report metrics, verify, move, and cancel jobs |
| Storage server -> provider stream host | Storage server | Transfer the actual media file |
| Storage server -> selected proxy (optional) | Storage server | Route a job's stream traffic through its explicitly selected proxy |

The Vite development server uses `localhost:5173` and forwards `/api` to the backend during local development. In production, serve the dashboard and `/api` on one HTTPS origin where possible. The supplied systemd unit binds FastAPI to `127.0.0.1:8000`; do not expose this port publicly. Allow outbound SSH from the VPS to the storage server's SSH port and allow the storage server's required outbound provider/proxy traffic. CORS should list only trusted browser origins when the UI and API use different origins; credentialed requests cannot safely use a wildcard origin.

## Download lifecycle

1. **Discover or select content**
   - The poller on the VPS checks enabled provider accounts and only requests categories marked as monitored.
   - A browser user can instead browse provider categories and queue episodes immediately.
   - The provider stream ID and provider endpoint form the stable deduplication identity. Already queued/completed IDs are not automatically queued a second time.

2. **Claim an account-bound job**
   - The worker pool reconciles enabled accounts dynamically.
   - An idle worker claims one eligible `PENDING` item for its storage route. Provider-ID Xtream jobs are rebuilt using the assigned worker account's host and credentials; legacy URL-only jobs retain their provider-host restriction. One account cannot run two active downloads at once; multiple accounts allow different files to download concurrently.
   - Manual batches can select free accounts, downloader, and direct/proxy mode before enqueueing.

3. **Open the SSH control connection**
   - The VPS uses Paramiko with the storage node's host, port, SSH username, and private-key path.
   - SSH host-key verification is strict. The storage host key must already be trusted by the daemon via system known-hosts or the configured `SSH_KNOWN_HOSTS` file.
   - The daemon sends safely quoted shell commands over SSH. Provider credentials in the stream URL are not placed in the dashboard URL; stream URLs are encrypted in SQLite and constructed for the assigned worker account when needed.

4. **Prepare remote staging**
   - A unique per-job partial file is created under the configured staging directory, mirroring the relative content path.
   - The target parent directory is prepared on the storage server. For watched series with existing-folder matching enabled, only the single unambiguous matching folder under that category's custom path is selected.
   - Staging and target must be on the same filesystem so the final rename is atomic.

5. **Download the bytes on the storage server**
   - Default **Axel** jobs use the storage node's configured connection count (1-12; new nodes default to `DEFAULT_AXEL_CONNECTIONS=12`). Remote stdout/stderr is collected continuously for the dashboard task log; transfer speed and size are also sampled from staging-file growth. Existing stage files and Axel `.st` state files are removed before a fresh attempt.
   - Manually queued **Wget** jobs use resume/output, retry, User-Agent, and explicit direct or HTTP(S)-proxy settings. Wget uses one connection per file and cannot use SOCKS5.
   - New accounts use the `Mozilla` User-Agent unless another value is explicitly entered. Commands use the provider's configured stream host directly, adding explicit default HTTP/HTTPS ports when missing. The VPS does not follow redirects or resolve the edge URL; the storage server's downloader receives the primary provider URL and follows any redirect from its own network.
   - Only the explicitly selected/configured proxy is supplied to the remote downloader. Direct jobs clear inherited proxy environment variables before starting the downloader.
   - Movie files are placed directly in their category directory; series are organized as category / series / season / episode. A category-specific destination path is treated as that category's directory. Leading `4K-` labels and configured category prefixes are removed from new category, series, and movie names. The SSH command monitors the staging-file size and returns transfer metrics to the VPS. The file content itself travels from the provider (or proxy) to the storage server, not through the VPS.

6. **Verify and promote**
   - After the downloader exits successfully, the VPS asks the storage server to run `ffprobe` against the staged file.
   - If a video stream is found, the remote file is renamed into the final destination. The rename is performed on the storage server and is atomic when staging and target are on the same filesystem.
   - Failed verification or transfer marks the queue item failed and cleans up the staging partial and any adjacent Axel `.st` resume state where the remote server remains reachable. Completed rows record the final remote path.
   - SSH exec requests have a bounded channel-open timeout; the worker also detects dropped SSH transports and enforces the configured remote-job and transfer-idle timeouts. A 15-second worker-pool reconciliation returns persisted active records with no live worker to `PENDING`.
   - **View log** streams output through `GET /api/tasks/{task_id}/logs/stream`; the authenticated `POST /api/tasks/{task_id}/abort` endpoint requests remote process-group termination and staging cleanup. Logs are retained as a bounded 256 KiB tail in SQLite.
   - The exact dispatched command is encrypted at rest with the configured application key and returned to authenticated administrators by the log viewer. It contains provider credentials; protect the SQLite database, backups, encryption key, and administrator sessions as secrets.

7. **Cancel or clear**
   - Cancelling one active job signals its worker. The SSH command handler terminates the remote downloader process group and removes the partial staging file. The task log viewer provides the same action as **Abort job**.
   - **Stop all & clear all** prevents workers from claiming new pending jobs, cancels pending jobs, signals active jobs, waits for worker cancellation to finish, and hides all queue entries. It does not delete completed media files. Queue records remain internally as hidden deduplication tombstones; selected cancelled items can be queued again manually through the browser. Monitored categories remain enabled and may enqueue future new streams.
   - If SSH is unavailable, the VPS cannot guarantee that it killed an already-running remote process immediately. The bulk operation reports a timeout rather than clearing the list while tracked jobs are still running.

## Storage path configuration

Configure each storage node in **Settings & Accounts -> Storage servers & routing**:

- **SSH host, port, username, and private-key path** identify the remote account.
- **Staging path** is the temporary download root.
- **Storage root** is the default final library root.
- Series and Movies routes can point to different storage nodes.
- A watched category can inherit its route's storage root or use its own absolute path on that same routed node.

The daemon account's private key is local to the VPS; the SSH user's permissions and directories are on the storage server. Do not confuse the local private-key path with a remote path. Use **Test connection** to validate SSH and writable directories before starting jobs.

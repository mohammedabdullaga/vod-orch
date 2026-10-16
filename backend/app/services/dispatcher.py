import re
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

import paramiko

from app.config import settings
from app.models import Node
from app.services.content_paths import match_series_directory


@dataclass(frozen=True)
class DispatchResult:
    final_path: str


class DispatchCancelled(Exception):
    pass


_ANSI_ESCAPE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_FILE_SIZE = re.compile(rb"File size:.*?\(([\d,]+)\s+bytes\)", re.IGNORECASE)
_WGET_FILE_SIZE = re.compile(rb"Length:\s*([\d,]+)(?:\s|$)", re.IGNORECASE)
_SAMPLED_SPEED = re.compile(rb"MEDIA_TRANSFER speed_bytes_per_second=(\d+)")
_FINAL_SIZE = re.compile(rb"MEDIA_TRANSFER file_size_bytes=(\d+)")
_TRANSFER_SPEED = re.compile(
    rb"\[\s*([\d.]+)\s*(B|KB|KiB|MB|MiB|GB|GiB)\/s\s*\]",
    re.IGNORECASE,
)
_PERCENTAGE = re.compile(rb"(?<!\d)(\d{1,3})%")


def _parse_transfer_metrics(
    output: bytes,
) -> tuple[float | None, float | None, int | None]:
    clean_output = _ANSI_ESCAPE.sub(b"", output).replace(b"\r", b"\n")
    percentages = _PERCENTAGE.findall(clean_output[-4096:])
    speeds = _TRANSFER_SPEED.findall(clean_output[-4096:])
    file_sizes = _FILE_SIZE.findall(clean_output[-16384:])
    wget_file_sizes = _WGET_FILE_SIZE.findall(clean_output[-16384:])
    sampled_speeds = _SAMPLED_SPEED.findall(clean_output[-4096:])
    final_sizes = _FINAL_SIZE.findall(clean_output[-4096:])
    progress = min(float(percentages[-1]), 100.0) if percentages else None
    speed = float(sampled_speeds[-1]) if sampled_speeds else None
    if speed is None and speeds:
        value, unit = speeds[-1]
        unit_name = unit.decode("ascii").lower()
        multiplier = 1024 if "i" in unit_name else 1000
        power = 0 if unit_name == "b" else 1 if unit_name.startswith("k") else 2 if unit_name.startswith("m") else 3
        speed = float(value) * multiplier**power
    size_candidates = final_sizes or file_sizes or wget_file_sizes
    file_size = (
        int(size_candidates[-1].replace(b",", b"")) if size_candidates else None
    )
    return progress, speed, file_size


class RemoteDispatcher:
    def __init__(self, known_hosts_path: str = "", command_timeout_seconds: int = 86400) -> None:
        self.known_hosts_path = known_hosts_path
        self.command_timeout_seconds = command_timeout_seconds

    def _connect(self, node: Node) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            if self.known_hosts_path:
                client.load_host_keys(self.known_hosts_path)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=node.host,
                port=node.port,
                username=node.username,
                key_filename=node.private_key_path,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
                look_for_keys=False,
                allow_agent=False,
            )
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
        except Exception:
            client.close()
            raise
        return client

    def _run(
        self,
        client: paramiko.SSHClient,
        command: str,
        progress: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
        cancel_remote: Callable[[], None] | None = None,
        on_transfer_metrics: Callable[[float | None, float | None, int | None], None] | None = None,
        on_output: Callable[[str], None] | None = None,
        max_output_bytes: int = 65536,
        idle_timeout_seconds: int | None = None,
    ) -> str:
        if max_output_bytes < 1:
            raise ValueError("Remote command output limit must be positive.")
        _, stdout, _ = client.exec_command(
            command, get_pty=False, timeout=15
        )
        channel = stdout.channel
        output = bytearray()
        error_output = bytearray()
        started = time.monotonic()
        last_transfer_at = started
        last_metrics: tuple[float | None, float | None, int | None] = (None, None, None)
        last_metrics_at = 0.0
        while not channel.exit_status_ready():
            transport = client.get_transport()
            if not transport or not transport.is_active():
                raise ConnectionError("SSH transport disconnected while command was running.")
            if cancel_event and cancel_event.is_set():
                try:
                    if cancel_remote:
                        cancel_remote()
                finally:
                    channel.close()
                raise DispatchCancelled("Download cancelled by request.")
            if time.monotonic() - started > self.command_timeout_seconds:
                channel.close()
                raise TimeoutError(
                    f"Remote command exceeded {self.command_timeout_seconds} seconds."
                )
            if (
                idle_timeout_seconds is not None
                and time.monotonic() - last_transfer_at > idle_timeout_seconds
            ):
                channel.close()
                raise TimeoutError(
                    f"Remote download had no transfer progress for {idle_timeout_seconds} seconds."
                )
            received = bytearray()
            if channel.recv_ready():
                data = channel.recv(65536)
                output.extend(data)
                received.extend(data)
                del output[:-max_output_bytes]
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(65536)
                error_output.extend(data)
                received.extend(data)
                del error_output[:-65536]
            if received and on_output:
                clean = _ANSI_ESCAPE.sub(b"", bytes(received)).decode(
                    "utf-8", errors="replace"
                )
                on_output(clean)
            parsed_progress, speed, file_size = _parse_transfer_metrics(output + error_output)
            metrics = (parsed_progress, speed, file_size)
            now = time.monotonic()
            if speed is not None and speed > 0:
                last_transfer_at = now
            if parsed_progress is not None and progress:
                progress(parsed_progress)
            if (
                on_transfer_metrics
                and metrics != last_metrics
                and (now - last_metrics_at >= 1.0 or file_size != last_metrics[2])
            ):
                on_transfer_metrics(*metrics)
                last_metrics = metrics
                last_metrics_at = now
            time.sleep(0.1)
        while channel.recv_ready():
            data = channel.recv(65536)
            output.extend(data)
            if on_output:
                on_output(
                    _ANSI_ESCAPE.sub(b"", data).decode("utf-8", errors="replace")
                )
            del output[:-max_output_bytes]
        while channel.recv_stderr_ready():
            data = channel.recv_stderr(65536)
            error_output.extend(data)
            if on_output:
                on_output(
                    _ANSI_ESCAPE.sub(b"", data).decode("utf-8", errors="replace")
                )
            del error_output[:-65536]
        final_metrics = _parse_transfer_metrics(output + error_output)
        if on_transfer_metrics:
            on_transfer_metrics(*final_metrics)
        if progress and final_metrics[0] is not None:
            progress(final_metrics[0])
        code = channel.recv_exit_status()
        if code:
            detail = (error_output or output).decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"Remote command failed with exit code {code}: {detail.strip()}")
        return output.decode("utf-8", errors="replace")

    def test_connection(self, node: Node) -> None:
        client = self._connect(node)
        try:
            self._run(client, "printf 'connection-ok'")
            self._run(
                client,
                f"mkdir -p {shlex.quote(node.staging_path)} {shlex.quote(node.watch_path)} "
                f"&& test -w {shlex.quote(node.staging_path)} "
                f"&& test -w {shlex.quote(node.watch_path)} "
                f"&& [ \"$(stat -c %d {shlex.quote(node.staging_path)})\" = "
                f"\"$(stat -c %d {shlex.quote(node.watch_path)})\" ]",
            )
        finally:
            client.close()

    def scan_library_files(
        self,
        node: Node,
        roots: dict[str, str],
    ) -> dict[str, list[str]]:
        if not roots:
            return {}
        command_parts = []
        for index, root in enumerate(roots.values()):
            quoted_root = shlex.quote(root)
            command_parts.append(
                f"printf '\\036MEDIA_LIBRARY_BEGIN_{index}\\0'; "
                f"if [ -e {quoted_root} ]; then "
                f"if [ ! -d {quoted_root} ] || [ ! -r {quoted_root} ]; then "
                f"printf 'Library directory is not readable.\\n' >&2; exit 23; fi; "
                f"find {quoted_root} -type f \\( "
                "-iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' "
                "-o -iname '*.mov' -o -iname '*.m4v' -o -iname '*.ts' "
                "-o -iname '*.webm' -o -iname '*.mpg' -o -iname '*.mpeg' "
                "-o -iname '*.wmv' -o -iname '*.flv' "
                f"\\) -printf '%P\\0'; "
                "find_status=$?; "
                "if [ \"$find_status\" -ne 0 ]; then exit \"$find_status\"; fi; "
                "fi; "
                f"printf '\\036MEDIA_LIBRARY_END_{index}\\0'"
            )
        client = self._connect(node)
        try:
            output = self._run(
                client,
                "; ".join(command_parts),
                max_output_bytes=16 * 1024 * 1024,
            )
        finally:
            client.close()

        records = output.split("\0")
        entries: dict[str, list[str]] = {}
        offset = 0
        for index, key in enumerate(roots):
            begin = f"\x1eMEDIA_LIBRARY_BEGIN_{index}"
            end = f"\x1eMEDIA_LIBRARY_END_{index}"
            if offset >= len(records) or records[offset] != begin:
                raise RuntimeError("Remote library inventory was incomplete.")
            offset += 1
            try:
                end_offset = records.index(end, offset)
            except ValueError as exc:
                raise RuntimeError("Remote library inventory was incomplete.") from exc
            entries[key] = [entry for entry in records[offset:end_offset] if entry]
            offset = end_offset + 1
        if any(record for record in records[offset:]):
            raise RuntimeError("Remote library inventory contained unexpected data.")
        return entries

    def cleanup_failed_job(
        self,
        node: Node,
        job_id: int,
        file_name: str,
        destination_root: str | None = None,
        prune_empty_final_directories: bool = True,
        match_existing_series: bool = False,
    ) -> None:
        relative_path = PurePosixPath(file_name)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in ("", ".", "..") for part in relative_path.parts)
            or "\\" in file_name
        ):
            raise ValueError("Content destination must be a safe relative POSIX path.")
        if match_existing_series:
            if len(relative_path.parts) < 3:
                raise ValueError("Series destination path is invalid for folder matching.")
            relative_path = PurePosixPath(*relative_path.parts[-2:])

        staging_job_dir = str(PurePosixPath(node.staging_path) / relative_path.parent)
        staging_file = str(
            PurePosixPath(staging_job_dir) / f"{relative_path.name}.part-{job_id}"
        )
        pid_file = f"{staging_file}.pid"
        state_file = f"{staging_file}.st"
        root = PurePosixPath(destination_root or node.watch_path)
        final_directory = PurePosixPath(root) / relative_path.parent
        if final_directory.parts[:len(root.parts)] != root.parts:
            raise ValueError("Content destination is outside the configured storage root.")

        self._terminate_remote_process(node, pid_file)
        cleanup_parts = [
            f"rm -f -- {shlex.quote(staging_file)} {shlex.quote(state_file)} "
            f"{shlex.quote(pid_file)}",
            f"rmdir --ignore-fail-on-non-empty {shlex.quote(staging_job_dir)}",
        ]
        if prune_empty_final_directories:
            current = final_directory
            while current != root and current.parts[:len(root.parts)] == root.parts:
                cleanup_parts.append(
                    f"rmdir --ignore-fail-on-non-empty {shlex.quote(str(current))}"
                )
                current = current.parent

        client = self._connect(node)
        try:
            self._run(client, " && ".join(cleanup_parts))
        finally:
            client.close()

    def dispatch(
        self,
        node: Node,
        stream_url: str,
        file_name: str,
        user_agent: str,
        proxy_url: str | None,
        job_id: int,
        destination_root: str | None = None,
        on_progress: Callable[[float], None] | None = None,
        on_verifying: Callable[[], None] | None = None,
        axel_connections: int = settings.default_axel_connections,
        cancel_event: threading.Event | None = None,
        on_transfer_metrics: Callable[[float | None, float | None, int | None], None] | None = None,
        downloader: str = "axel",
        match_existing_series: bool = False,
        series_name: str | None = None,
        series_match_prefixes: str | None = None,
        on_log: Callable[[str], None] | None = None,
        on_command: Callable[[str], None] | None = None,
    ) -> DispatchResult:
        if not 1 <= axel_connections <= 12:
            raise ValueError("Axel connections must be between 1 and 12.")
        if downloader not in ("axel", "wget"):
            raise ValueError("Downloader must be either axel or wget.")
        stream_target = urlsplit(stream_url)
        if (
            stream_target.scheme.lower() not in ("http", "https")
            or not stream_target.hostname
        ):
            raise ValueError("Stream URL must be an absolute HTTP or HTTPS URL.")
        user_agent = user_agent.strip() or "Mozilla"
        proxy_scheme = urlsplit(proxy_url).scheme.lower() if proxy_url else ""
        if downloader == "wget":
            if proxy_scheme in ("socks5", "socks5h"):
                raise RuntimeError(
                    "Wget does not support SOCKS5 proxies. Choose Axel/aria2c or an HTTP(S) proxy."
                )
            if proxy_url and proxy_scheme not in ("http", "https"):
                raise RuntimeError("Wget proxy URL scheme must be http or https.")
        relative_path = PurePosixPath(file_name)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in ("", ".", "..") for part in relative_path.parts)
            or "\\" in file_name
        ):
            raise ValueError("Content destination must be a safe relative POSIX path.")
        final_root = destination_root or node.watch_path
        client = self._connect(node)
        try:
            def run_remote(command: str, *args, **kwargs) -> str:
                kwargs.setdefault("on_output", on_log)
                return self._run(client, command, *args, **kwargs)

            if cancel_event and cancel_event.is_set():
                raise DispatchCancelled("Download cancelled before it started.")
            if match_existing_series:
                if not series_name:
                    raise ValueError("Series name is required for existing-folder matching.")
                if len(relative_path.parts) < 3:
                    raise ValueError("Series destination path is invalid for folder matching.")
                listing = run_remote(
                    f"find {shlex.quote(final_root)} -mindepth 1 -maxdepth 1 "
                    "-type d -print0 | head -z -n 10001",
                    max_output_bytes=3 * 1024 * 1024,
                )
                folder_names = [name for name in listing.split("\0") if name]
                if len(folder_names) > 10000:
                    raise ValueError(
                        "The custom series path contains more than 10,000 folders; "
                        "use a narrower path to avoid ambiguous matches."
                    )
                matched_folder = match_series_directory(
                    series_name,
                    folder_names,
                    series_match_prefixes,
                )
                final_root = str(PurePosixPath(final_root) / matched_folder)
                relative_path = PurePosixPath(*relative_path.parts[-2:])
            staging_job_dir = str(PurePosixPath(node.staging_path) / relative_path.parent)
            staging_file = str(
                PurePosixPath(staging_job_dir) / f"{relative_path.name}.part-{job_id}"
            )
            final_path = str(PurePosixPath(final_root) / relative_path)
            stage_dir = str(PurePosixPath(node.staging_path))
            final_dir = str(PurePosixPath(final_path).parent)
            q_stage = shlex.quote(staging_file)
            q_url = shlex.quote(stream_url)
            q_ua = (
                '"'
                + user_agent.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("$", "\\$")
                .replace("`", "\\`")
                + '"'
            )
            pid_file = f"{staging_file}.pid"
            state_file = f"{staging_file}.st"
            q_state_file = shlex.quote(state_file)
            prepare = (
                f"mkdir -p {shlex.quote(staging_job_dir)} {shlex.quote(final_dir)} "
                f"&& chmod 0755 {shlex.quote(final_dir)} "
                f"&& rm -f -- {q_stage} {q_state_file} {shlex.quote(pid_file)}"
            )
            run_remote(
                prepare,
            )
            if downloader == "wget":
                proxy_env = ""
                use_proxy = "no"
                if proxy_url:
                    safe_proxy = shlex.quote(proxy_url)
                    proxy_env = (
                        f"http_proxy={safe_proxy} https_proxy={safe_proxy} "
                        f"HTTP_PROXY={safe_proxy} HTTPS_PROXY={safe_proxy} "
                    )
                    use_proxy = "yes"
                download = (
                    f"{proxy_env}"
                    f"wget -cO {q_stage} --tries=99 --retry-connrefused "
                    f"--user-agent={q_ua} -e use_proxy={use_proxy} {q_url}"
                )
            elif proxy_scheme == "socks5":
                download = (
                    f"aria2c --quiet=true --split={axel_connections} "
                    f"--max-connection-per-server={axel_connections} --allow-overwrite=true "
                    f"--auto-file-renaming=false --file-allocation=none "
                    f"--all-proxy={shlex.quote(proxy_url)} --user-agent={q_ua} "
                    f"--dir={shlex.quote(staging_job_dir)} "
                    f"--out={shlex.quote(PurePosixPath(staging_file).name)} {q_url}"
                )
            elif proxy_url and proxy_scheme in ("http", "https"):
                safe_proxy = shlex.quote(proxy_url)
                download = (
                    f"http_proxy={safe_proxy} https_proxy={safe_proxy} "
                    f"HTTP_PROXY={safe_proxy} HTTPS_PROXY={safe_proxy} "
                    f"axel -n {axel_connections} -o {q_stage} "
                    f"--user-agent={q_ua} {q_url}"
                )
            elif proxy_url:
                raise RuntimeError("Proxy URL scheme must be http, https, or socks5.")
            else:
                download = (
                    f"axel -n {axel_connections} -o {q_stage} "
                    f"--user-agent={q_ua} {q_url}"
                )
            remote_download = (
                "unset http_proxy https_proxy ftp_proxy all_proxy "
                "HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY no_proxy NO_PROXY; "
                f"setsid sh -c {shlex.quote(download)} & child=$!; "
                f"printf '%s\\n' \"$child\" > {shlex.quote(pid_file)}; "
                "previous_bytes=0; "
                f"while kill -0 \"$child\" 2>/dev/null; do "
                f"current_bytes=$(stat -c %s -- {q_stage} 2>/dev/null || printf '0'); "
                "delta_bytes=$((current_bytes - previous_bytes)); "
                "printf 'MEDIA_TRANSFER speed_bytes_per_second=%s\\n' \"$delta_bytes\"; "
                "previous_bytes=$current_bytes; sleep 1; done; "
                "wait \"$child\"; download_status=$?; "
                f"rm -f -- {q_state_file}; "
                f"if [ \"$download_status\" -eq 0 ]; then "
                f"current_bytes=$(stat -c %s -- {q_stage} 2>/dev/null || printf '0'); "
                "printf 'MEDIA_TRANSFER file_size_bytes=%s\\n' \"$current_bytes\"; fi; "
                "exit \"$download_status\""
            )
            try:
                if on_command:
                    on_command(remote_download)
                run_remote(
                    remote_download,
                    on_progress,
                    cancel_event,
                    lambda: self._terminate_remote_process(node, pid_file),
                    on_transfer_metrics,
                    idle_timeout_seconds=settings.remote_download_idle_timeout_seconds,
                )
                if cancel_event and cancel_event.is_set():
                    raise DispatchCancelled("Download cancelled by request.")
                if on_verifying:
                    on_verifying()
                if cancel_event and cancel_event.is_set():
                    raise DispatchCancelled("Download cancelled by request.")
                probe = (
                    "ffprobe -v error -select_streams v:0 "
                    "-show_entries stream=codec_name "
                    "-of default=noprint_wrappers=1:nokey=1 "
                    f"{q_stage}"
                )
                if not run_remote(probe, cancel_event=cancel_event).strip():
                    raise RuntimeError("ffprobe found no video codec in the downloaded stream.")
                if cancel_event and cancel_event.is_set():
                    raise DispatchCancelled("Download cancelled by request.")
                same_filesystem = (
                    f"test -w {shlex.quote(final_dir)} && "
                    f"test -w {shlex.quote(staging_job_dir)} && "
                    f"[ \"$(stat -c %d {shlex.quote(stage_dir)})\" = "
                    f"\"$(stat -c %d {shlex.quote(final_dir)})\" ]"
                )
                run_remote(same_filesystem)
                move = (
                    f"mv -Tn -- {q_stage} {shlex.quote(final_path)} "
                    f"&& rm -f -- {shlex.quote(pid_file)} && test ! -e {q_stage}"
                )
                run_remote(move)
                run_remote(
                    f"rmdir --ignore-fail-on-non-empty {shlex.quote(staging_job_dir)}"
                )
                return DispatchResult(final_path=final_path)
            except Exception as original_error:
                try:
                    run_remote(
                        f"rm -f -- {q_stage} {q_state_file} {shlex.quote(pid_file)} && "
                        f"rmdir --ignore-fail-on-non-empty {shlex.quote(staging_job_dir)}",
                    )
                except Exception as first_cleanup_error:
                    cleanup_client = None
                    try:
                        cleanup_client = self._connect(node)
                        self._run(
                            cleanup_client,
                            f"rm -f -- {q_stage} {q_state_file} {shlex.quote(pid_file)} && "
                            f"rmdir --ignore-fail-on-non-empty {shlex.quote(staging_job_dir)}",
                        )
                    except Exception as reconnect_cleanup_error:
                        raise RuntimeError(
                            f"{original_error}; staging cleanup failed after reconnect "
                            f"({first_cleanup_error}; {reconnect_cleanup_error})"
                        ) from original_error
                    finally:
                        if cleanup_client:
                            cleanup_client.close()
                raise
        finally:
            client.close()

    def _terminate_remote_process(self, node: Node, pid_file: str) -> None:
        client = self._connect(node)
        try:
            q_pid_file = shlex.quote(pid_file)
            command = (
                "for attempt in 1 2 3 4 5 6 7 8 9 10; do "
                f"if [ -s {q_pid_file} ]; then break; fi; sleep 0.1; done; "
                f"if [ -s {q_pid_file} ]; then pid=$(cat -- {q_pid_file}); "
                "kill -TERM -- \"-$pid\" 2>/dev/null || true; sleep 1; "
                "kill -KILL -- \"-$pid\" 2>/dev/null || true; "
                "if kill -0 -- \"-$pid\" 2>/dev/null; then exit 1; fi; fi"
            )
            self._run(client, command)
        finally:
            client.close()

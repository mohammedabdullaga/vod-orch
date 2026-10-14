import re
import shlex
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

import paramiko

from app.models import Node
from app.security import decrypt_secret


@dataclass(frozen=True)
class DispatchResult:
    final_path: str


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
        except Exception:
            client.close()
            raise
        return client

    def _run(
        self,
        client: paramiko.SSHClient,
        command: str,
        progress: Callable[[float], None] | None = None,
    ) -> str:
        _stdin, stdout, stderr = client.exec_command(command, get_pty=False)
        channel = stdout.channel
        output = bytearray()
        error_output = bytearray()
        started = time.monotonic()
        while not channel.exit_status_ready():
            if time.monotonic() - started > self.command_timeout_seconds:
                channel.close()
                raise TimeoutError(
                    f"Remote command exceeded {self.command_timeout_seconds} seconds."
                )
            if channel.recv_ready():
                output.extend(channel.recv(65536))
                del output[:-65536]
            if channel.recv_stderr_ready():
                error_output.extend(channel.recv_stderr(65536))
                del error_output[:-65536]
            if progress:
                matches = re.findall(rb"(?<!\d)(\d{1,3})%", (output + error_output)[-4096:])
                if matches:
                    progress(min(float(matches[-1]), 100.0))
            time.sleep(0.1)
        while channel.recv_ready():
            output.extend(channel.recv(65536))
        while channel.recv_stderr_ready():
            error_output.extend(channel.recv_stderr(65536))
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
        axel_connections: int = 4,
    ) -> DispatchResult:
        if not 1 <= axel_connections <= 12:
            raise ValueError("Axel connections must be between 1 and 12.")
        relative_path = PurePosixPath(file_name)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in ("", ".", "..") for part in relative_path.parts)
            or "\\" in file_name
        ):
            raise ValueError("Content destination must be a safe relative POSIX path.")
        final_root = destination_root or node.watch_path
        staging_job_dir = str(PurePosixPath(node.staging_path) / relative_path.parent)
        staging_file = str(
            PurePosixPath(staging_job_dir) / f"{relative_path.name}.part-{job_id}"
        )
        final_path = str(PurePosixPath(final_root) / relative_path)
        stage_dir = str(PurePosixPath(node.staging_path))
        final_dir = str(PurePosixPath(final_path).parent)
        q_stage = shlex.quote(staging_file)
        q_url = shlex.quote(stream_url)
        q_ua = shlex.quote(user_agent)
        client = self._connect(node)
        try:
            prepare = (
                f"mkdir -p {shlex.quote(staging_job_dir)} {shlex.quote(final_dir)} "
                f"&& chmod 0755 {shlex.quote(final_dir)} "
                f"&& rm -f -- {q_stage}"
            )
            self._run(
                client,
                prepare,
            )
            proxy_scheme = urlsplit(proxy_url).scheme.lower() if proxy_url else ""
            if proxy_scheme == "socks5":
                download = (
                    "env -u http_proxy -u https_proxy -u ftp_proxy -u all_proxy "
                    "-u HTTP_PROXY -u HTTPS_PROXY -u FTP_PROXY -u ALL_PROXY "
                    "-u no_proxy -u NO_PROXY "
                    f"aria2c --split={axel_connections} "
                    f"--max-connection-per-server={axel_connections} --allow-overwrite=true "
                    f"--auto-file-renaming=false --file-allocation=none --summary-interval=1 "
                    f"--all-proxy={shlex.quote(proxy_url)} --user-agent={q_ua} "
                    f"--dir={shlex.quote(staging_job_dir)} "
                    f"--out={shlex.quote(PurePosixPath(staging_file).name)} {q_url}"
                )
            elif proxy_url and proxy_scheme in ("http", "https"):
                safe_proxy = shlex.quote(proxy_url)
                download = (
                    "env -u ftp_proxy -u all_proxy -u FTP_PROXY -u ALL_PROXY "
                    "-u no_proxy -u NO_PROXY "
                    f"http_proxy={safe_proxy} https_proxy={safe_proxy} "
                    f"HTTP_PROXY={safe_proxy} HTTPS_PROXY={safe_proxy} "
                    f"axel -n {axel_connections} -a -o {q_stage} "
                    f"--user-agent={q_ua} {q_url}"
                )
            elif proxy_url:
                raise RuntimeError("Proxy URL scheme must be http, https, or socks5.")
            else:
                download = (
                    "env -u http_proxy -u https_proxy -u ftp_proxy -u all_proxy "
                    "-u HTTP_PROXY -u HTTPS_PROXY -u FTP_PROXY -u ALL_PROXY "
                    "-u no_proxy -u NO_PROXY "
                    f"axel -n {axel_connections} -a -o {q_stage} "
                    f"--user-agent={q_ua} {q_url}"
                )
            try:
                self._run(client, download, on_progress)
                if on_verifying:
                    on_verifying()
                probe = (
                    "ffprobe -v error -select_streams v:0 "
                    "-show_entries stream=codec_name "
                    "-of default=noprint_wrappers=1:nokey=1 "
                    f"{q_stage}"
                )
                if not self._run(client, probe).strip():
                    raise RuntimeError("ffprobe found no video codec in the downloaded stream.")
                same_filesystem = (
                    f"test -w {shlex.quote(final_dir)} && "
                    f"test -w {shlex.quote(staging_job_dir)} && "
                    f"[ \"$(stat -c %d {shlex.quote(stage_dir)})\" = "
                    f"\"$(stat -c %d {shlex.quote(final_dir)})\" ]"
                )
                self._run(client, same_filesystem)
                move = f"mv -Tn -- {q_stage} {shlex.quote(final_path)} && test ! -e {q_stage}"
                self._run(client, move)
                self._run(client, f"rmdir --ignore-fail-on-non-empty {shlex.quote(staging_job_dir)}")
                return DispatchResult(final_path=final_path)
            except Exception as original_error:
                try:
                    self._run(
                        client,
                        f"rm -f -- {q_stage} && "
                        f"rmdir --ignore-fail-on-non-empty {shlex.quote(staging_job_dir)}",
                    )
                except Exception as first_cleanup_error:
                    cleanup_client = None
                    try:
                        cleanup_client = self._connect(node)
                        self._run(
                            cleanup_client,
                            f"rm -f -- {q_stage} && "
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

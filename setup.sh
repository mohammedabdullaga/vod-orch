#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root (for example: sudo bash setup.sh)." >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y axel aria2 wget ffmpeg ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y axel aria2 wget ffmpeg
elif command -v yum >/dev/null 2>&1; then
  yum install -y axel aria2 wget ffmpeg
else
  echo "Unsupported package manager; install axel, aria2, wget and ffmpeg manually." >&2
  exit 1
fi

command -v axel
command -v aria2c
command -v wget
ffprobe -version | head -n 1

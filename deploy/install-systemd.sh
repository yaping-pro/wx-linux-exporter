#!/usr/bin/env bash
# install-systemd.sh — render the wx-linux-exporter systemd units for the
# current user and enable them.
#
# The units under deploy/systemd/ ship with install-time placeholders:
#   __WXUSER__  -> the login user that owns the vault
#   __WXHOME__  -> that user's real home directory
# We substitute BOTH here so a raw `cp` of the unit can never reach systemd
# with an unsubstituted placeholder (which would make systemd try
# `User=__WXUSER__` and fail to start). This is the robust, single-source
# install path referenced by README.md and AGENT.md.
#
# Usage:
#   sudo ./deploy/install-systemd.sh [--user <login>] [--home <dir>]
# Defaults: current user ($USER / whoami) and $HOME.

set -euo pipefail

WX_USER="${1:-}"
WX_HOME="${2:-}"

if [[ -z "${WX_USER}" ]]; then
  WX_USER="$(whoami)"
fi
if [[ -z "${WX_HOME}" ]]; then
  # Resolve the real home for the target user (handles sudo -u edge cases).
  WX_HOME="$(getent passwd "${WX_USER}" | cut -d: -f6)"
  if [[ -z "${WX_HOME}" || ! -d "${WX_HOME}" ]]; then
    WX_HOME="${HOME}"
  fi
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/systemd" && pwd)"
DEST_DIR="/etc/systemd/system"

echo "== wx-linux-exporter systemd install =="
echo "   user : ${WX_USER}"
echo "   home : ${WX_HOME}"
echo "   src  : ${SRC_DIR}"
echo "   dest : ${DEST_DIR}"

# Copy service + timer units.
sudo cp "${SRC_DIR}"/wechat-*.service "${DEST_DIR}/"
sudo cp "${SRC_DIR}"/wechat-*.timer   "${DEST_DIR}/"

# Render install-time placeholders (BOTH, every occurrence).
sudo sed -i "s|__WXUSER__|${WX_USER}|g; s|__WXHOME__|${WX_HOME}|g" "${DEST_DIR}"/wechat-*.service

# Sanity: fail loudly if any placeholder or systemd specifier leaked through.
if grep -rqE '__WXUSER__|__WXHOME__|%h|%I' "${DEST_DIR}"/wechat-*.service; then
  echo "ERROR: placeholders/%h/%I still present after render — aborting." >&2
  grep -nE '__WXUSER__|__WXHOME__|%h|%I' "${DEST_DIR}"/wechat-*.service >&2
  exit 1
fi

sudo systemctl daemon-reload

# Enable + start (idempotent).
sudo systemctl enable --now wechat-xvfb.service
sudo systemctl enable --now wechat-runner.service
sudo systemctl enable --now wechat-decrypt.timer
sudo systemctl enable --now wechat-export-api.service

echo "== systemd units installed and enabled for user ${WX_USER} =="

#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
escaped_root="$(printf '%s' "${project_root}" | sed 's/[\/&]/\\&/g')"

for unit in oj-rank-display.service oj-rank-window-manager.service oj-rank-novnc.service oj-rank.service; do
    sed "s/__PROJECT_ROOT__/${escaped_root}/g" "${project_root}/systemd/${unit}" > "/etc/systemd/system/${unit}"
    chmod 0644 "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable oj-rank.service
echo "Installed and enabled oj-rank.service from ${project_root}."
echo "Review .env, then start it with: systemctl start oj-rank.service"

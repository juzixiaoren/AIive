#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
# A signal trap can run while stdout is redirected into the database dump.
# Keep status logs on the original journal/console stream.
exec 3>&1
[[ $EUID == 0 ]] || { echo 'Must run as root' >&2; exit 2; }
CONFIG=${AIIVE_BACKUP_CONFIG:-/etc/aiive/backup.conf}
[[ -f $CONFIG && $(stat -c %u "$CONFIG") == 0 && $(stat -c %a "$CONFIG") == 600 ]] || { echo 'Config must be root-owned mode 600' >&2; exit 2; }
source "$CONFIG"
: "${APP_DIR:?}" "${RESTIC_PASSWORD_FILE:?}"
[[ -f $RESTIC_PASSWORD_FILE && $(stat -c %u "$RESTIC_PASSWORD_FILE") == 0 && $(stat -c %a "$RESTIC_PASSWORD_FILE") == 600 ]] || exit 2
BACKUP_ROOT=$(realpath -m "${BACKUP_ROOT:-/var/backups/aiive}")
RESTIC_REPOSITORY="$BACKUP_ROOT/repository"
password_path=$(realpath "$RESTIC_PASSWORD_FILE")
[[ $password_path != "$BACKUP_ROOT" && $password_path != "$BACKUP_ROOT/"* && $password_path != "$APP_DIR/.env" ]] || { echo 'Password must be outside repository and .env' >&2; exit 2; }
[[ $(realpath -m "$RESTIC_REPOSITORY") == "$RESTIC_REPOSITORY" ]] || { echo 'Repository symlink is not allowed' >&2; exit 2; }
export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
KEEP_DAILY=${KEEP_DAILY:-7}
[[ $KEEP_DAILY =~ ^[1-9][0-9]*$ ]] || exit 2
for tool in docker restic python3 flock git sha256sum; do command -v "$tool" >/dev/null; done
[[ $BACKUP_ROOT != /var/lib/docker* && $BACKUP_ROOT != "$APP_DIR"/.data* ]] || exit 2
mkdir -p "$BACKUP_ROOT"
# All operations share a repository lock, including restore and verification.
exec 9>"$BACKUP_ROOT/operation.lock"
flock -n 9 || { echo 'Another backup/restore/verify is running' >&2; exit 3; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$APP_DIR"
COMPOSE_PROJECT=${COMPOSE_PROJECT:-aiive}
c() { docker compose -p "$COMPOSE_PROJECT" -f "$APP_DIR/docker-compose.yml" "$@"; }
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&3; }
space_check() {
 local available
 available=$(df -PB1 "$BACKUP_ROOT" | awk 'NR==2 {print $4}')
 [[ $available -ge $1 ]] || { log 'ERROR insufficient disk space'; return 4; }
}

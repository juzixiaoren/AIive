#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
: "${1:?Usage: verify.sh SNAPSHOT_ID}"
[[ $1 =~ ^[a-f0-9]{8,64}$ ]] || exit 2
stage=$(mktemp -d "$BACKUP_ROOT/verify.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
restic check --read-data
restic restore "$1" --target "$stage" >/dev/null
python3 "$HERE/integrity.py" verify "$stage"
pgimage=$(sed -n 's/^postgres_image=//p' "$stage/metadata.txt")
docker run --rm -i --network none --entrypoint pg_restore "$pgimage" --list < "$stage/database.dump" >/dev/null
log "verified snapshot=$1"

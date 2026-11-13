#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
start=$SECONDS; snapshot=none; commit=$(git rev-parse HEAD); stage=; check=; stopped=0
finish() {
 local rc=$? recovery=0
 set +e
 trap - EXIT ERR
 trap '' INT TERM
 if (( stopped )); then
   c start backend frontend || recovery=1
   c up -d --wait --wait-timeout 180 backend frontend || recovery=1
 fi
 [[ -z $check ]] || rm -rf -- "$check"
 [[ -z $stage ]] || rm -rf -- "$stage" "$stage-result.json"
 if (( recovery )); then log 'ALERT service recovery failed; operator action required'; rc=20; fi
 log "result=$rc snapshot=$snapshot commit=$commit elapsed_seconds=$((SECONDS-start))"
 exit "$rc"
}
trap 'log "ERROR line=$LINENO exit=$?"' ERR
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
backend=$(c ps -q backend); postgres=$(c ps -q postgres); frontend=$(c ps -q frontend)
[[ -n $backend && -n $postgres && -n $frontend ]] || { log 'ERROR required services must be running'; exit 5; }
for container in "$backend" "$postgres" "$frontend"; do
 state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")
 [[ $state == healthy || $state == running ]] || { log "ERROR service not ready container=$container state=$state"; exit 5; }
done
volume=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/.data"}}{{.Name}}{{end}}{{end}}' "$backend")
[[ -n $volume ]] || exit 5
# Reject backup destinations within any Docker volume, including custom data roots.
while read -r mount; do [[ $BACKUP_ROOT != "$mount" && $BACKUP_ROOT != "$mount/"* ]] || exit 5; done < <(docker volume ls -q | xargs -r docker volume inspect -f '{{.Mountpoint}}')
image=$(docker inspect -f '{{.Image}}' "$backend")
frontimage=$(docker inspect -f '{{.Image}}' "$frontend")
pgimage=$(docker inspect -f '{{.Image}}' "$postgres")
bytes=$(docker run --rm --network none -v "$volume:/data:ro" --entrypoint du "$image" -sb /data | awk '{print $1}')
dbbytes=$(c exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT pg_database_size(current_database())"')
space_check "$(( (${bytes}+${dbbytes})*3 + ${MIN_FREE_BYTES:-1073741824} ))"
restic snapshots --json >/dev/null 2>&1 || { [[ ! -e $RESTIC_REPOSITORY/config ]] && restic init >/dev/null; }
restic snapshots --json >/dev/null
stage=$(mktemp -d "$BACKUP_ROOT/stage.XXXXXXXX")
stopped=1
c stop -t 120 frontend backend
[[ $(docker inspect -f '{{.State.Running}}' "$backend") == false ]]
[[ $(docker inspect -f '{{.State.Running}}' "$frontend") == false ]]
c run --rm -T --no-deps --entrypoint python backend - < "$HERE/audit.py" > "$stage/audit.json"
c exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$stage/database.dump"
c exec -T postgres pg_restore --list < "$stage/database.dump" >/dev/null
docker run --rm --network none -v "$volume:/data:ro" --entrypoint tar "$image" -C /data -cpf - . > "$stage/data.tar"
mkdir -p "$stage/config/deploy/pg" "$stage/config/backend"
for f in .env docker-compose.yml deploy/pg/Dockerfile backend/Dockerfile; do cp -- "$APP_DIR/$f" "$stage/config/$f"; done
c exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version()"' > "$stage/database-version.txt"
printf '%s\n' "commit=$commit" "utc=$(date -u +%FT%TZ)" "backend_container=$backend" "postgres_container=$postgres" "backend_image=$image" "postgres_image=$pgimage" "frontend_image=$frontimage" > "$stage/metadata.txt"
python3 "$HERE/integrity.py" create "$stage"
python3 "$HERE/integrity.py" verify "$stage"
# Retention explicitly groups by host/tags because staging paths vary.
(cd "$stage"; restic backup . --tag aiive-v1 --host "$COMPOSE_PROJECT" --json) > "$stage-result.json"
snapshot=$(python3 -c 'import json,sys; print([x["snapshot_id"] for x in map(json.loads,open(sys.argv[1])) if x.get("message_type")=="summary"][0])' "$stage-result.json")
rm -f "$stage-result.json"
# Read all newly stored data before allowing any old snapshots to be removed.
restic check --read-data
check=$(mktemp -d "$BACKUP_ROOT/check.XXXXXXXX")
restic restore "$snapshot" --target "$check" >/dev/null
python3 "$HERE/integrity.py" verify "$check"
rm -rf -- "$check"
restic forget --tag aiive-v1 --host "$COMPOSE_PROJECT" --group-by host,tags --keep-daily "$KEEP_DAILY" --prune
log "backup verified snapshot=$snapshot repository_bytes=$(du -sb "$RESTIC_REPOSITORY" | cut -f1)"

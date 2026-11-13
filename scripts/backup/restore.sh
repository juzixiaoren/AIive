#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
: "${1:?Usage: restore.sh SNAPSHOT_ID NEW_PROJECT ABSOLUTE_TARGET_DIR}"
: "${2:?}" "${3:?}"
snapshot=$1; project=$2; target=$(realpath -m "$3")
[[ $snapshot =~ ^[a-f0-9]{8,64}$ && $project =~ ^aiive-restore-[a-z0-9-]+$ ]] || exit 2
[[ ! -e $target && $target != "$APP_DIR" && $target != "$APP_DIR/"* ]] || { log 'ERROR target must be a new directory'; exit 2; }
[[ -z $(docker ps -aq --filter "label=com.docker.compose.project=$project") ]] || exit 2
for v in "${project}_aiive_data" "${project}_pgdata"; do
 if docker volume inspect "$v" >/dev/null 2>&1; then log "ERROR volume already exists: $v"; exit 2; fi
done
printf 'Restore into NEW project %s, directory %s, volumes %s_aiive_data and %s_pgdata.\n' "$project" "$target" "$project" "$project"
read -r -p "Type RESTORE $project to confirm: " answer
[[ $answer == "RESTORE $project" ]] || exit 2
restic check --read-data
stage=$(mktemp -d "$BACKUP_ROOT/restore.XXXXXXXX")
trap 'rc=$?; rm -rf -- "$stage"; if (( rc )); then echo "RESTORE FAILED: isolated target retained for investigation; do not switch production traffic" >&2; fi' EXIT
restic restore "$snapshot" --target "$stage" >/dev/null
python3 "$HERE/integrity.py" verify "$stage"
pgimage=$(sed -n 's/^postgres_image=//p' "$stage/metadata.txt")
frontimage=$(sed -n 's/^frontend_image=//p' "$stage/metadata.txt")
backendimage=$(sed -n 's/^backend_image=//p' "$stage/metadata.txt")
docker image inspect "$pgimage" "$backendimage" "$frontimage" >/dev/null
docker run --rm -i --network none --entrypoint pg_restore "$pgimage" --list < "$stage/database.dump" >/dev/null
space_check "$(( $(du -sb "$stage" | cut -f1)*3 + ${MIN_FREE_BYTES:-1073741824} ))"
mkdir -p "$target"
cp -a "$stage/config/." "$target/"
# Resolve restored .env using Compose; never print rendered secrets to journald.
docker compose --project-directory "$target" -f "$target/docker-compose.yml" config --format json > "$target/resolved.json"
python3 - "$target" "$project" "$pgimage" "$backendimage" "$frontimage" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); config=json.loads((root/'resolved.json').read_text())
config.pop('name',None)
for name in config.get('networks',{}):
    config['networks'][name]={'name':sys.argv[2]+'_'+name}
# Never reuse host paths or external runtime resources from the source stack.
for service in config['services'].values():
    if service.get('profiles'):
        continue  # Optional services remain disabled until separately reviewed.
    assert not service.get('network_mode'), 'Review network_mode before isolated restore'
    assert not service.get('secrets') and not service.get('configs'), 'Review external secrets/configs'
    for mount in service.get('volumes',[]):
        assert mount.get('type') == 'volume', 'Bind mounts require manual isolation before restore'
for name,service in config['services'].items():
    service.pop('build',None)
    service.pop('ports',None)
    service.pop('container_name',None)
    service.pop('restart',None)
    if name=='postgres': service['image']=sys.argv[3]
    if name=='backend': service['image']=sys.argv[4]
    if name=='frontend': service['image']=sys.argv[5]
for name in ('aiive_data','pgdata'):
    config['volumes'][name]={'name':sys.argv[2]+'_'+name}
# Any other named volumes must also be isolated.
for name in config.get('volumes',{}):
    config['volumes'][name]={'name':sys.argv[2]+'_'+name}
(root/'restore-compose.json').write_text(json.dumps(config))
(root/'resolved.json').unlink()
PY
r() { docker compose -p "$project" -f "$target/restore-compose.json" "$@"; }
docker volume create "${project}_aiive_data" >/dev/null
docker volume create "${project}_pgdata" >/dev/null
docker run --rm -i --network none -v "${project}_aiive_data:/data" --entrypoint tar "$backendimage" -C /data -xpf - < "$stage/data.tar"
r up -d --wait --wait-timeout 180 postgres
r exec -T postgres sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$stage/database.dump"
r run --rm -T --no-deps --entrypoint python backend - < "$HERE/audit.py" > "$target/restored-audit.json"
python3 - "$stage/audit.json" "$target/restored-audit.json" <<'PY'
import json,sys
before,after=[json.load(open(p)) for p in sys.argv[1:]]
assert before==after, 'Database counts/object bytes/auth secret changed during restore'
PY
r up -d --wait --wait-timeout 240 backend
# Application lifespan runs alembic upgrade head before health succeeds.
r exec -T backend python -c 'import urllib.request; assert urllib.request.urlopen("http://localhost:8000/health").status == 200'
r up -d frontend
r exec -T backend python - <<'PY'
import json, time, urllib.request
for attempt in range(60):
    try:
        assert urllib.request.urlopen('http://frontend:8080/', timeout=5).status == 200
        status = json.load(urllib.request.urlopen('http://frontend:8080/api/auth/status', timeout=5))
        assert isinstance(status, dict)
        break
    except Exception:
        if attempt == 59:
            raise
        time.sleep(2)
PY
log "restore complete snapshot=$snapshot project=$project target=$target; ports intentionally unpublished; validate before switching traffic"

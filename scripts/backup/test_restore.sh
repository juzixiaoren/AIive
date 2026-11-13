#!/usr/bin/env bash
# Destructive ONLY to generated aiive-restore-test-* projects. Run on disposable Ubuntu.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || exit 2
: "${AIIVE_TEST_BACKEND_IMAGE:?Build backend image first}"
: "${AIIVE_TEST_PG_IMAGE:?Build PostgreSQL image first}"
: "${AIIVE_TEST_FRONTEND_IMAGE:?Build frontend image first}"
here=$(cd "$(dirname "$0")" && pwd)
root=$(mktemp -d /var/tmp/aiive-backup-test.XXXXXXXX)
project="aiive-restore-test-$(date +%s)-$$"
restored="${project}-new"
cleanup() {
 rc=$?
 docker compose -p "$project" -f "$root/app/docker-compose.yml" logs --no-color > "$root/services.log" 2>&1 || true
 if [[ -f $root/recovered/restore-compose.json ]]; then docker compose -p "$restored" -f "$root/recovered/restore-compose.json" logs --no-color > "$root/restored-services.log" 2>&1 || true; fi
 printf 'exit=%s\nproject=%s\nrestored_project=%s\n' "$rc" "$project" "$restored" > "$root/test-result.txt"
 docker compose -p "$project" -f "$root/app/docker-compose.yml" down -v >/dev/null 2>&1 || true
 if [[ -f $root/recovered/restore-compose.json ]]; then docker compose -p "$restored" -f "$root/recovered/restore-compose.json" down -v >/dev/null 2>&1 || true; fi
 echo "Evidence retained: $root"
}
trap cleanup EXIT
mkdir -p "$root/app/backend" "$root/app/deploy/pg"
cp "$here/../../backend/Dockerfile" "$root/app/backend/"
cp "$here/../../deploy/pg/Dockerfile" "$root/app/deploy/pg/"
: > "$root/app/.env"
python3 - "$root/app/docker-compose.yml" <<'PY'
import json,os,sys
pg=os.environ['AIIVE_TEST_PG_IMAGE']; be=os.environ['AIIVE_TEST_BACKEND_IMAGE']; fe=os.environ['AIIVE_TEST_FRONTEND_IMAGE']
json.dump({'services':{
'postgres':{'image':pg,'environment':{'POSTGRES_USER':'aiive','POSTGRES_PASSWORD':'aiive_dev','POSTGRES_DB':'aiive'},'volumes':['pgdata:/var/lib/postgresql/data'],'healthcheck':{'test':['CMD-SHELL','pg_isready -U aiive'],'interval':'2s','retries':60}},
'backend':{'image':be,'environment':{'DATABASE_URL':'postgresql+psycopg://aiive:aiive_dev@postgres:5432/aiive','AIIVE_DATA_DIR':'/app/.data','AIIVE_MEMORY_BM25_ENABLED':'false'},'volumes':['aiive_data:/app/.data'],'healthcheck':{'test':['CMD','curl','-fsS','http://localhost:8000/health'],'interval':'3s','retries':60}},
'frontend':{'image':fe}},'volumes':{'pgdata':{},'aiive_data':{}}},open(sys.argv[1],'w'))
PY
(cd "$root/app"; git init -q; git add .; git -c user.name=test -c user.email=test@localhost commit -qm fixture)
printf '%s\n' "APP_DIR=$root/app" "COMPOSE_PROJECT=$project" "BACKUP_ROOT=$root/backup" "RESTIC_PASSWORD_FILE=$root/password" 'MIN_FREE_BYTES=1048576' > "$root/config"
openssl rand -base64 48 > "$root/password"
chmod 600 "$root/config" "$root/password"
export AIIVE_BACKUP_CONFIG="$root/config"
c() { docker compose -p "$project" -f "$root/app/docker-compose.yml" "$@"; }
c up -d --wait postgres
c up -d --wait backend frontend
c exec -T backend python - seed < "$here/fixture.py"
bash "$here/backup.sh" | tee "$root/backup.log"
snapshot=$(restic -r "$root/backup/repository" --password-file "$root/password" snapshots --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["id"])')
# Wrong password must fail and must not affect retained snapshots.
printf 'incorrect\n' > "$root/wrong"; chmod 600 "$root/wrong"
if restic -r "$root/backup/repository" --password-file "$root/wrong" snapshots; then exit 1; fi
sed "s|RESTIC_PASSWORD_FILE=.*|RESTIC_PASSWORD_FILE=$root/wrong|" "$root/config" > "$root/wrong-config"
chmod 600 "$root/wrong-config"
if AIIVE_BACKUP_CONFIG="$root/wrong-config" bash "$here/backup.sh"; then exit 1; fi
# Competing operation must fail with the dedicated lock exit code.
flock "$root/backup/operation.lock" sleep 5 &
locker=$!
sleep .2
set +e
bash "$here/backup.sh"
lock_rc=$?
set -e
[[ $lock_rc == 3 ]]
wait "$locker"
# Refusal before downtime on insufficient disk.
printf '\nMIN_FREE_BYTES=1125899906842624\n' >> "$root/config"
if bash "$here/backup.sh"; then exit 1; fi
c exec -T backend curl -fsS http://localhost:8000/health
sed -i '/MIN_FREE_BYTES=1125899906842624/d' "$root/config"
# Interrupt during the dump: no new snapshot, and trap must restore service.
mkdir "$root/bin"
real_docker=$(command -v docker)
cat > "$root/bin/docker" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *pg_dump* ]]; then touch "$root/dumping"; sleep 5; fi
exec "$real_docker" "\$@"
EOF
chmod 700 "$root/bin/docker"
PATH="$root/bin:$PATH" bash "$here/backup.sh" > "$root/interrupted.log" 2>&1 &
pid=$!
for ((i=0;i<600;i++)); do [[ ! -f $root/dumping ]] || break; sleep .1; done
[[ -f $root/dumping ]]
kill -TERM "$pid"
if wait "$pid"; then exit 1; fi
grep -qE '^[0-9TZ:-]+ result=143 ' "$root/interrupted.log"
c exec -T backend curl -fsS http://localhost:8000/health
bash "$here/verify.sh" "$snapshot"
restic -r "$root/backup/repository" --password-file "$root/password" snapshots --json | python3 -c 'import json,sys; assert len(json.load(sys.stdin))==1, "Failure changed snapshot retention"'
# Exercise seven-daily retention using explicit historical test timestamps.
mkdir "$root/retention-fixture"
restic -r "$root/backup/repository" --password-file "$root/password" restore "$snapshot" --target "$root/retention-fixture" >/dev/null
for days in {8..1}; do
 timestamp=$(date -u -d "$days days ago" '+%Y-%m-%d %H:%M:%S')
 (cd "$root/retention-fixture"; restic -r "$root/backup/repository" --password-file "$root/password" backup . --tag aiive-v1 --host "$project" --time "$timestamp" --quiet)
done
bash "$here/backup.sh" | tee "$root/retention.log"
restic -r "$root/backup/repository" --password-file "$root/password" snapshots --json > "$root/snapshots.json"
snapshot=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); assert len(s)==7; print(s[-1]["id"])' "$root/snapshots.json")
# Delete ONLY this disposable project's original volumes before restoring.
c down -v
printf 'RESTORE %s\n' "$restored" | bash "$here/restore.sh" "$snapshot" "$restored" "$root/recovered" | tee "$root/restore.log"
r() { docker compose -p "$restored" -f "$root/recovered/restore-compose.json" "$@"; }
r exec -T backend python - verify < "$here/fixture.py"
if bash "$here/restore.sh" "$snapshot" "$restored" "$root/recovered"; then exit 1; fi
# Corrupt an isolated repository copy; original backup is never mutated.
cp -a "$root/backup/repository" "$root/corrupt"
pack=$(find "$root/corrupt/data" -type f | head -n1)
printf broken | dd of="$pack" conv=notrunc status=none
if restic -r "$root/corrupt" --password-file "$root/password" check --read-data; then exit 1; fi
du -sb "$root/backup/repository" | tee "$root/disk-usage.txt"
echo 'PASS recovery, wrong password, insufficient space, corrupt repository'

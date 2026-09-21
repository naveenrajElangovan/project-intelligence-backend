#!/usr/bin/env bash
set -euo pipefail

backend_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(dirname "$backend_root")"
output="${1:-$workspace_root/project-intelligence-portable.run}"
stage="$(mktemp -d "${TMPDIR:-/tmp}/pi-portable.XXXXXX")"
services_stopped=false

cleanup() {
  rm -rf "$stage"
  if [[ "$services_stopped" == true ]]; then
    (cd "$backend_root" && docker compose up -d >/dev/null)
  fi
}
trap cleanup EXIT INT TERM

for command_name in docker rsync openssl tar shasum; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required." >&2
    exit 1
  }
done
docker info >/dev/null 2>&1 || {
  echo "Start Docker Desktop before building the bundle." >&2
  exit 1
}

password="${PI_PORTABLE_BUNDLE_PASSWORD:-}"
if [[ -z "$password" ]]; then
  read -r -s -p "Bundle password: " password
  echo
  read -r -s -p "Confirm password: " confirmation
  echo
  [[ "$password" == "$confirmation" ]] || {
    echo "Passwords do not match." >&2
    exit 1
  }
fi
[[ ${#password} -ge 16 ]] || {
  echo "Use a bundle password of at least 16 characters." >&2
  exit 1
}

payload_root="$stage/project-intelligence-portable"
mkdir -p "$payload_root/data"

copy_project() {
  local name="$1"
  local source="$workspace_root/$name"
  [[ -d "$source" ]] || {
    echo "Missing project directory: $source" >&2
    exit 1
  }
  rsync -a \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '.models/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'reports/' \
    --exclude 'build/' \
    --exclude '.gradle/' \
    "$source/" "$payload_root/$name/"
}

copy_project project-intelligence-backend
copy_project project-intelligence-ingestion
copy_project project-intelligence-rag
copy_project project-intelligence-atlassian
mkdir -p "$payload_root/project-intelligence-rag/.models"
cp -a \
  "$workspace_root/project-intelligence-rag/.models/multilingual-e5-large" \
  "$workspace_root/project-intelligence-rag/.models/bge-reranker-v2-m3" \
  "$payload_root/project-intelligence-rag/.models/"
# The copied Compose file is a distribution artifact. Reuse the RAG embedding
# model for ingestion so the one-file bundle does not carry a duplicate 2.1 GB.
sed -i.bak \
  's#../project-intelligence-ingestion/.models/multilingual-e5-large#../project-intelligence-rag/.models/multilingual-e5-large#' \
  "$payload_root/project-intelligence-backend/docker-compose.yml"
rm -f "$payload_root/project-intelligence-backend/docker-compose.yml.bak"
cp "$backend_root/scripts/portable_install.sh" "$payload_root/install.sh"
chmod +x "$payload_root/install.sh"

cd "$backend_root"
docker compose stop api atlassian ingestion rag chroma mongodb
services_stopped=true

backup_volume() {
  local volume="$1"
  local archive="$2"
  docker run --rm \
    -v "$volume:/source:ro" \
    -v "$payload_root/data:/backup" \
    alpine:3.21 \
    sh -c "tar -czf /backup/$archive -C /source ."
}

backup_volume project-intelligence-backend_project-intelligence-chroma chroma.tgz
backup_volume project-intelligence-backend_project-intelligence-mongodb-auth mongodb.tgz
backup_volume project-intelligence-backend_provider-secrets provider-secrets.tgz

cat > "$payload_root/MANIFEST.txt" <<EOF
Created: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Source architecture: $(uname -m)
Installer builds application images for the destination Docker architecture.
Includes ChromaDB, MongoDB, provider secrets, control-plane data, models, and service source.
EOF

tar -czf "$stage/payload.tgz" -C "$stage" project-intelligence-portable
export PI_PORTABLE_BUNDLE_PASSWORD="$password"
openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000 \
  -pass env:PI_PORTABLE_BUNDLE_PASSWORD \
  -in "$stage/payload.tgz" \
  -out "$stage/payload.enc"
unset PI_PORTABLE_BUNDLE_PASSWORD password confirmation 2>/dev/null || true

checksum="$(shasum -a 256 "$stage/payload.enc" | awk '{print $1}')"
cat > "$stage/header" <<EOF
#!/usr/bin/env bash
set -euo pipefail
expected_checksum="$checksum"
install_parent="\${PI_PORTABLE_INSTALL_DIR:-\$HOME/ProjectIntelligence}"
command -v docker >/dev/null 2>&1 || { echo "Docker Desktop is required." >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "OpenSSL is required." >&2; exit 1; }
if command -v shasum >/dev/null 2>&1; then
  checksum_command=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then
  checksum_command=(sha256sum)
else
  echo "shasum or sha256sum is required." >&2
  exit 1
fi
marker_line="\$(awk '/^__PI_ENCRYPTED_PAYLOAD_BELOW__\$/ {print NR + 1; exit}' "\$0")"
[[ -n "\$marker_line" ]] || { echo "Invalid portable bundle." >&2; exit 1; }
temporary="\$(mktemp -d "\${TMPDIR:-/tmp}/pi-install.XXXXXX")"
trap 'rm -rf "\$temporary"' EXIT INT TERM
tail -n "+\$marker_line" "\$0" > "\$temporary/payload.enc"
actual_checksum="\$("\${checksum_command[@]}" "\$temporary/payload.enc" | awk '{print \$1}')"
[[ "\$actual_checksum" == "\$expected_checksum" ]] || { echo "Bundle checksum failed." >&2; exit 1; }
read -r -s -p "Bundle password: " bundle_password
echo
export PI_PORTABLE_BUNDLE_PASSWORD="\$bundle_password"
mkdir -p "\$install_parent"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
  -pass env:PI_PORTABLE_BUNDLE_PASSWORD \
  -in "\$temporary/payload.enc" | tar -xzf - -C "\$install_parent"
unset PI_PORTABLE_BUNDLE_PASSWORD bundle_password
bash "\$install_parent/project-intelligence-portable/install.sh"
exit 0
__PI_ENCRYPTED_PAYLOAD_BELOW__
EOF

cat "$stage/header" "$stage/payload.enc" > "$output"
chmod 0700 "$output"
echo "Created encrypted portable installer: $output"
echo "Recipient command: bash $(basename "$output")"

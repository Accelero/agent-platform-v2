#!/bin/sh
# Builds the manager from the mounted source, prepares an empty database and
# runs the API. Safe to run again: every step leaves existing state alone.
set -eu
cd /src/services/hermes-manager

echo "Building the manager. The first build takes several minutes."
cargo build --locked -p hermes-manager
bin="$CARGO_TARGET_DIR/debug/hermes-manager"

state=/state
issuer=https://keycloak.localhost:8443/realms/agents
subject=00000000-0000-4000-8000-0000000000a0
mkdir -p "$state"
[ -f "$state/sealing.key" ] || "$bin" generate-sealing-key \
  --private-key-file "$state/sealing.key" --public-key-file "$state/sealing.pub"
[ -f "$state/session-key" ] || head -c 32 /dev/urandom > "$state/session-key"
echo '{"activeKeyId":"current","keyFiles":{"current":"/state/session-key"}}' > "$state/keyring.json"
echo "postgres://hermes:hermes@postgres:5432/hermes" > "$state/database-url"
printf '%s' "dev-directory-secret" > "$state/directory-client-secret"
echo "{\"issuer\":\"$issuer\",\"subject\":\"$subject\"}" > "$state/identity.json"
cat > "$state/config.json" <<JSON
{"installation":{"id":"00000000-0000-4000-8000-000000000001","issuer":"$issuer"},
 "databaseUrlFile":"/state/database-url","oidcCaFile":"/pki/ca.crt","dataIdentity":{"uid":1000,"gid":1000},
 "storageSources":[
   {"id":"nvme","storageClass":{"name":"local-nvme"}},
   {"id":"host-data","localPath":{"node":"server-1","root":"/srv/hermes-data"}},
   {"id":"nas","nfs":{"server":"nas.internal","root":"/export/homes"}}],
 "administration":{"listen":"0.0.0.0:8446",
  "tls":{"certificateFile":"/pki/admin.crt","privateKeyFile":"/pki/admin.key"},
  "tokens":{"jwksUrl":"$issuer/protocol/openid-connect/certs","audience":"hermes-manager","authorizedClients":["manager-admin"]},
  "secretSealingPublicKeyFile":"/state/sealing.pub",
  "browser":{"databaseUrlFile":"/state/database-url","publicOrigin":"https://hermes-admin.localhost:5173",
   "otherOrigin":"https://hermes.localhost:8445","clientId":"manager-admin","scopes":["openid"],
   "revocationUrl":"$issuer/protocol/openid-connect/revoke","keyringFile":"/state/keyring.json"},
  "directory":{"clientId":"manager-directory-reader","clientSecretFile":"/state/directory-client-secret"}}}
JSON

until pg_isready -q -h postgres -U hermes; do sleep 1; done
# curl treats every *.localhost name as this machine without asking DNS, but
# the manager asks the system resolver, which finds the provider by its alias;
# so the probe is told the address the way the system resolver sees it.
until address=$(getent hosts keycloak.localhost | awk '{print $1; exit}') \
  && [ -n "$address" ] \
  && curl -sf --resolve "keycloak.localhost:8443:$address" --cacert /pki/ca.crt \
    "$issuer/.well-known/openid-configuration" > /dev/null; do
  echo "waiting for the identity provider"; sleep 2
done

"$bin" migrate --config "$state/config.json"
# `initialize` reads a Kubernetes cluster's identity; there is none here, so the
# installation is recorded directly.
psql -q "$(cat "$state/database-url")" -c \
  "INSERT INTO manager.installation (id, issuer, cluster_uid)
   VALUES ('00000000-0000-4000-8000-000000000001', '$issuer', 'dev-no-cluster')
   ON CONFLICT DO NOTHING"
"$bin" grant-administrator --config "$state/config.json" --identity-file "$state/identity.json" \
  || echo "an administrator is already granted"

echo "The manager is ready. Open https://hermes-admin.localhost:5173"
exec "$bin" api --config "$state/config.json"

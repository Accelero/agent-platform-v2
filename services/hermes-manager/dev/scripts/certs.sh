#!/bin/sh
# A throwaway CA and the two server certificates of the development stack,
# written once into the shared volume. The names are the ones the browser and
# the containers both use, so that every URL is the same on both sides.
set -eu
cd /pki
if [ -f ca.crt ] && [ -f admin.crt ] && [ -f keycloak.crt ]; then
  echo "certificates already exist"
  exit 0
fi
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 825 \
  -subj "/CN=Agent Platform development CA" \
  -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
for pair in keycloak:keycloak.localhost admin:hermes-admin.localhost; do
  name=${pair%%:*}
  host=${pair#*:}
  printf 'basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' "$host" > "$name.ext"
  openssl req -new -newkey rsa:2048 -nodes -keyout "$name.key" -out "$name.csr" -subj "/CN=$host"
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$name.crt" -days 825 -extfile "$name.ext"
done
chmod 644 /pki/*
echo "certificates written"

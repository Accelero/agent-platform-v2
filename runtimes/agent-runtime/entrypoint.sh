#!/bin/sh
set -eu
# managed-ssh-v2: the account `worker` must be the UID and GID this container
# runs as. The manager chooses them per backend version (runAsUser, runAsGroup
# and fsGroup), so the account is bound to them at every start instead of
# baking one identity into the image. The root file system is read-only, so the
# account files are rebuilt below /tmp and the user lookup (nss_wrapper) is
# pointed at them, here for sshd and for every session through worker-env.conf.
uid=$(id -u)
gid=$(id -g)
if [ "$uid" != 0 ]; then
 accounts=/tmp/accounts
 mkdir -p "$accounts"
 {
  awk -F: -v uid="$uid" '$1 != "worker" && $3 != uid' /etc/passwd
  echo "worker:*:$uid:$gid:worker:/home/worker:/bin/bash"
 } > "$accounts/passwd"
 {
  awk -F: -v gid="$gid" '$1 != "worker" && $3 != gid' /etc/group
  echo "worker:x:$gid:"
 } > "$accounts/group"
 export LD_PRELOAD=/usr/local/lib/libnss_wrapper.so \
  NSS_WRAPPER_PASSWD="$accounts/passwd" NSS_WRAPPER_GROUP="$accounts/group"
fi
mkdir -p /home/worker/hostkeys
if [ -f /etc/ssh/hostkeys/ssh_host_ed25519_key ]; then
 # managed-ssh-v2: the manager provisions the server identity independently of
 # the client key. Copy the read-only projection into a private 0600 file.
 umask 077
 cp /etc/ssh/hostkeys/ssh_host_ed25519_key /home/worker/hostkeys/ssh_host_ed25519_key
 chmod 600 /home/worker/hostkeys/ssh_host_ed25519_key
elif [ ! -f /home/worker/hostkeys/ssh_host_ed25519_key ]; then
 ssh-keygen -q -t ed25519 -N '' -f /home/worker/hostkeys/ssh_host_ed25519_key
fi
exec /usr/sbin/sshd -D -e

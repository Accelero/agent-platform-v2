FROM ubuntu:26.04@sha256:065aa8626841f16057b78b027b464cd59c4fac36c204c72dcbf48d66f5fa8a96
RUN apt-get -o Acquire::Retries=5 update && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y --no-install-recommends openssh-server libnss-wrapper python3 python3-yaml git curl jq ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/ssh/ssh_host_* /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem \
 && ln -s "$(ls /usr/lib/*/libnss_wrapper.so | head -n 1)" /usr/local/lib/libnss_wrapper.so
RUN userdel -r ubuntu 2>/dev/null || true
# The IDs of the baked `worker` account. The entrypoint binds the account to the
# UID and GID the container runs as, so any identity works without a rebuild;
# these only matter for runs outside the manager.
ARG HOST_UID=1000
ARG HOST_GID=1000
# The password field `*` accepts key logins; a locked field (`!`) would not.
RUN groupadd -g ${HOST_GID} worker && useradd -m -u ${HOST_UID} -g worker -s /bin/bash worker && usermod -p '*' worker && mkdir -p /run/sshd /home/worker/.ssh /workspace && chown -R worker:worker /home/worker /workspace
COPY sshd_config /etc/ssh/sshd_config
COPY ssh/environment-base.conf /etc/ssh/sshd_config.d/worker-env.conf
COPY entrypoint.sh /entrypoint.sh
RUN git config --system user.name 'Hermes Agent' \
 && git config --system user.email 'hermes@localhost' \
 && git config --system init.defaultBranch main
RUN chmod 755 /entrypoint.sh
WORKDIR /workspace
RUN mkdir -p /home/worker/hostkeys /etc/ssh/authorized_keys && chown worker:worker /home/worker/hostkeys
EXPOSE 2222
USER worker
ENTRYPOINT ["/entrypoint.sh"]

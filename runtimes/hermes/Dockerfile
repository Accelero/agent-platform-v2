ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:276d44685c65c0cb2a25f190b63c65c8906f9dc76486fb1c05ae71b5826bda08
FROM ghcr.io/astral-sh/uv@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv
FROM ${HERMES_IMAGE}
USER root
COPY --from=uv /uv /uvx /usr/local/bin/
COPY runtimes/hermes/licenses/ /usr/share/doc/uv/
ENV HERMES_DISABLE_LAZY_INSTALLS=1 \
    HERMES_LAZY_INSTALL_TARGET=/opt/data/lazy-packages \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
# Personal Kubernetes instances run as 1000; OpenSSH requires a passwd entry.
# Preserve the upstream hermes account at UID 10000.
RUN groupadd --gid 1000 hermes-personal \
 && useradd --uid 1000 --gid 1000 --home-dir /opt/data --no-create-home --shell /bin/sh hermes-personal
COPY runtimes/hermes/terminal-policy.py runtimes/hermes/patch-terminal-policy.py /usr/local/lib/
RUN /opt/hermes/.venv/bin/python /usr/local/lib/patch-terminal-policy.py
COPY runtimes/hermes/managed-exec /usr/local/bin/managed-exec
RUN mkdir -p /workspace \
 && chmod 755 /usr/local/bin/managed-exec

# Generic runtime adapters and optional dependencies travel with the image.
COPY runtimes/hermes/requirements.lock /tmp/assistant-requirements.lock
RUN uv --no-config pip install --python /opt/hermes/.venv/bin/python --no-deps -r /tmp/assistant-requirements.lock \
 && HERMES_HOME=/opt/hermes-stack/plugin-home /opt/hermes/.venv/bin/hermes plugins install hindsight --ref f14cc1eb5624b0374cb4d82768eb2e0b9c21ae24 --no-deps --no-enable
# Bundled backend plugins load for every profile, including ones created later.
COPY runtimes/hermes/plugins/managed_execution /opt/hermes/plugins/managed_execution
RUN chmod -R a+rX /opt/hermes/plugins/managed_execution
COPY runtimes/hermes/native_files.py runtimes/hermes/runtime-init.py /usr/local/lib/
COPY runtimes/hermes/personal-init.py /usr/local/lib/personal-init.py
COPY runtimes/hermes/personal-run.py /usr/local/lib/personal-run.py
RUN chmod -R a+rX /opt/hermes-stack

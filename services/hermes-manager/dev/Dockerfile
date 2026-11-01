# Tools for the local development stack: the Rust toolchain the manager is
# built with (the same pinned image as the release build), the PostgreSQL
# client and openssl. It builds nothing itself; the manager is compiled when
# the stack starts, from the mounted source.
FROM rust@sha256:a24e292b4af7bdee351a31d3a6379972a5652f0ea22a1b6d6883324ac4f5097c
RUN apt-get update \
 && apt-get install --yes --no-install-recommends ca-certificates curl openssl postgresql-client \
 && rm -rf /var/lib/apt/lists/*
ENV CARGO_TARGET_DIR=/cache/target

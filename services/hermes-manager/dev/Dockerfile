# The development dashboard uses the release build's Node and npm versions.
FROM node@sha256:e18c3ccdf7d71619990b0e85639f506e2028a95410999cab24cc373cbd10e0ac AS dashboard
RUN npm install --global npm@12.2.2 --ignore-scripts --fetch-timeout=60000 --fetch-retries=2

# Local manager tools; the manager is compiled from mounted source at startup.
FROM rust@sha256:e19840879045c951c905d0eaa9887f3439c62000e429963371b376bac8d38c31 AS manager
RUN apt-get update \
 && apt-get install --yes --no-install-recommends ca-certificates curl openssl postgresql-client \
 && rm -rf /var/lib/apt/lists/*
ENV CARGO_TARGET_DIR=/cache/target

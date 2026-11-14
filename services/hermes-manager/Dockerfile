# Manager (api and controller roles), gateway and dashboard release image.
FROM node@sha256:e18c3ccdf7d71619990b0e85639f506e2028a95410999cab24cc373cbd10e0ac AS ui
WORKDIR /src/services/hermes-manager/ui
COPY services/hermes-manager/ui/package.json services/hermes-manager/ui/package-lock.json services/hermes-manager/ui/.npmrc ./
RUN npm install --global npm@12.2.2 --ignore-scripts --fetch-timeout=60000 --fetch-retries=2 \
 && npm ci --ignore-scripts --fetch-timeout=60000 --fetch-retries=2
COPY contracts/hermes-manager/openapi.yaml /src/contracts/hermes-manager/openapi.yaml
COPY contracts/hermes-manager/router.openapi.yaml /src/contracts/hermes-manager/router.openapi.yaml
COPY contracts/hermes-manager/schemas/ /src/contracts/hermes-manager/schemas/
COPY services/hermes-manager/ui/ ./
RUN npm run generate && npm run check:schemas && npm test && npm run build

# Empty crates with the real manifests: unchanged unless a dependency changes.
FROM rust@sha256:e19840879045c951c905d0eaa9887f3439c62000e429963371b376bac8d38c31 AS rust-manifests
WORKDIR /plan
COPY services/hermes-manager/Cargo.toml services/hermes-manager/Cargo.lock services/hermes-manager/rust-toolchain.toml ./
COPY services/hermes-manager/crates/ ./crates/
RUN find crates -type f ! -name Cargo.toml -delete \
 && for crate in crates/*; do mkdir -p "$crate/src" && touch "$crate/src/lib.rs"; done \
 && rm crates/hermes-manager/src/lib.rs \
 && echo 'fn main() {}' > crates/hermes-manager/src/main.rs \
 && echo 'fn main() {}' > crates/hermes-gateway/src/main.rs

FROM rust@sha256:e19840879045c951c905d0eaa9887f3439c62000e429963371b376bac8d38c31 AS rust
WORKDIR /src/services/hermes-manager
COPY --from=rust-manifests /plan/ ./
# The dependencies alone, kept as a layer; the empty crates' output is discarded.
RUN cargo build --release --locked --bin hermes-manager --bin hermes-gateway \
 && rm -rf target/release/.fingerprint/hermes-* target/release/build/hermes-* \
    target/release/deps/*hermes* target/release/hermes-*
# The server embeds the definition schemas it validates against.
COPY contracts/hermes-manager/schemas/ /src/contracts/hermes-manager/schemas/
COPY services/hermes-manager/crates/ ./crates/
# Only the two binaries; test features such as fault injection stay off.
RUN cargo build --release --locked --bin hermes-manager --bin hermes-gateway \
 && mkdir /out \
 && cp target/release/hermes-manager target/release/hermes-gateway /out/

FROM debian@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ARG VERSION=development
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/Accelero/agent-platform-v2" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$REVISION"
RUN apt-get update \
 && apt-get install --yes --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 manager \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent manager
COPY --from=rust /out/ /usr/local/bin/
COPY --from=ui /src/services/hermes-manager/ui/dist/ /usr/share/hermes-manager/ui/
COPY --from=ui /src/services/hermes-manager/ui/dist/THIRD_PARTY_NOTICES.txt /usr/share/doc/hermes-manager/THIRD_PARTY_NOTICES.txt
COPY --from=ui /src/services/hermes-manager/ui/node_modules/react/LICENSE /usr/share/doc/hermes-manager/react.LICENSE
COPY --from=ui /src/services/hermes-manager/ui/node_modules/react-dom/LICENSE /usr/share/doc/hermes-manager/react-dom.LICENSE
USER 10001:10001
WORKDIR /tmp
EXPOSE 8443 8444 8445
ENTRYPOINT ["hermes-manager"]
CMD ["api", "--config", "/run/hermes/config/config.json"]

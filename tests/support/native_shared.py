"""Disposable multi-user proof on the project image, with real v2 shared homes.

The production native provider, initializer, terminal policy and non-root
supervisor run on an isolated network. The controller's real probe reaches
the instance's private fixture address; no port is published.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from tests.personal_hermes.native_e2e import KEYCLOAK_IMAGE, SUBJECTS, realm
from tests.support.native import (
    PYTHON,
    CommandFailed,
    Layout,
    docker,
    image,
    openssl_ca,
)
from tests.support.ownership import FIXTURE, RUN, Cleanup, new_run, require_fixture

FIXTURE_KIND = "shared-hermes-native"
ISSUER = "https://sso.shared.test:8443/realms/agents"
HOSTS = ("engineering.agents.test", "support.agents.test")
UNREGISTERED = "unregistered.agents.test"
UPSTREAM = (
    "nousresearch/hermes-agent@sha256:"
    "d4da4a40cd7a28aba983775d9fd31d94cbf153eeb0cb9e844d6d0f612b7c24db"
)
PROVIDER_SHA256 = "31b46690fb333fffa1524a63a54225ecc6c8731dcbfdfac95ea7647816cdbf5e"


def test_image(_cleanup: Cleanup):
    """Use the project image directly; its native provider must hash to upstream."""
    dockerfile = Path(__file__).resolve().parents[2] / "runtimes/hermes/Dockerfile"
    assert f"ARG HERMES_IMAGE={UPSTREAM}\n" in dockerfile.read_text()
    return image()


def tls(directory: Path):
    directory.mkdir(mode=0o755)
    authority = directory.parent / "private-ca"
    authority.mkdir(mode=0o700)
    openssl_ca(authority, directory / "ca.crt")
    extension = authority / "server.ext"
    extension.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:sso.shared.test,DNS:*.agents.test\n"
    )
    for arguments in (
        [
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            str(directory / "server.key"),
            "-out",
            str(authority / "server.csr"),
            "-subj",
            "/CN=shared-native-fixture",
        ],
        [
            "x509",
            "-req",
            "-in",
            str(authority / "server.csr"),
            "-CA",
            str(directory / "ca.crt"),
            "-CAkey",
            str(authority / "ca.key"),
            "-set_serial",
            "2",
            "-out",
            str(directory / "server.crt"),
            "-days",
            "1",
            "-extfile",
            str(extension),
        ],
    ):
        subprocess.run(
            ["openssl", *arguments], capture_output=True, check=True, timeout=30
        )
    (directory / "server.key").chmod(0o644)


class SharedNative:
    def __init__(
        self,
        root: Path,
        derivative: str,
        cleanup: Cleanup,
        *,
        rotate=False,
        unregistered=False,
        personal=False,
        seed_providers=False,
        expose_probe=False,
    ):
        self.run = new_run()
        self.cleanup = cleanup
        self.network = f"shared-native-{self.run}"
        self.derivative = derivative
        self.root = root
        self.readiness = "ready_unregistered" if unregistered else "ready"
        root.chmod(0o755)
        tls(root / "tls")
        docker(
            [
                "network",
                "create",
                "--internal",
                "--label",
                f"{FIXTURE}={FIXTURE_KIND}",
                "--label",
                f"{RUN}={self.run}",
                self.network,
            ]
        )
        cleanup.add(self.network, self.remove_network)
        passwords = {
            name: secrets.token_urlsafe(24) for name in [*SUBJECTS, "directory"]
        }
        representation = realm(passwords)
        representation.update(
            accessTokenLifespan=15, revokeRefreshToken=rotate, refreshTokenMaxReuse=0
        )
        for client in representation["clients"]:
            if client["clientId"] == "hermes-native":
                client["redirectUris"] = [
                    f"https://{host}/auth/callback" for host in HOSTS
                ]
            elif client["clientId"] == "hermes-gateway":
                client["redirectUris"] = [
                    f"https://{host}/_platform/oidc/callback" for host in HOSTS
                ]
        (root / "realm").mkdir()
        (root / "realm/agents.json").write_text(json.dumps(representation))
        (root / "people.json").write_text(json.dumps(passwords))
        self.start(
            "keycloak",
            KEYCLOAK_IMAGE,
            [
                "--network-alias",
                "sso.shared.test",
                "--mount",
                f"type=bind,src={root / 'tls'},dst=/fixture/tls,readonly",
                "--mount",
                f"type=bind,src={root / 'realm'},"
                "dst=/opt/keycloak/data/import,readonly",
            ],
            [
                "start-dev",
                "--import-realm",
                "--http-enabled=false",
                "--https-port=8443",
                "--https-certificate-file=/fixture/tls/server.crt",
                "--https-certificate-key-file=/fixture/tls/server.key",
                "--hostname=https://sso.shared.test:8443",
            ],
        )
        self.layouts = []
        hosts = (*HOSTS, UNREGISTERED) if unregistered else HOSTS
        for index, host in enumerate(hosts):
            directory = root / f"instance-{index}"
            directory.mkdir()
            layout = Layout(directory)
            if personal:
                layout.context["owner"] = {"issuer": ISSUER, "subject": SUBJECTS["bob"]}
            else:
                layout.context.pop("owner")
                layout.context["schemaVersion"] = 2
                layout.context["agent"] = {"id": layout.context["instanceId"]}
            layout.managed["dashboard"] = {
                "basic_auth": {
                    "username": "",
                    "password": "",
                    "password_hash": "",
                    "secret": "",
                },
                "oauth": {"client_id": "", "portal_url": ""},
            }
            layout.write_managed()
            (directory / "owner/context.json").write_text(json.dumps(layout.context))
            shutil.copyfile(root / "tls/ca.crt", directory / "trust/oidc-ca.crt")
            # Use the installed initializer, with precisely the production mounts.
            layout.initialize()
            if seed_providers:
                configuration = dict(layout.config)
                configuration["dashboard"] = {
                    "basic_auth": {
                        "username": "local-fixture",
                        "password": "local-fixture-password",
                        "secret": "ab" * 32,
                    },
                    "oauth": {
                        "client_id": "agent:fixture",
                        "portal_url": "https://unreachable.shared.test",
                        "self_hosted": {
                            "issuer": "https://unreachable.shared.test/realm",
                            "client_id": "wrong-fixture-client",
                            "client_secret": "wrong-fixture-secret",
                        },
                    },
                }
                (directory / "home/config.yaml").write_text(json.dumps(configuration))
                (directory / "home/.env").write_text(
                    "HERMES_DASHBOARD_OIDC_ISSUER=https://unreachable.shared.test/realm\n"
                    "HERMES_DASHBOARD_OIDC_CLIENT_ID=wrong-fixture-client\n"
                    "HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:fixture\n"
                    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=local-fixture\n"
                    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=local-fixture-password\n"
                )
            common = layout.common()
            # Replace the networkless default with this isolated network.
            del common[:2]
            name = self.start(
                f"native-{index}",
                derivative,
                [
                    *common,
                    "--network-alias",
                    f"native-{index}",
                    "--env",
                    f"HERMES_DASHBOARD_OIDC_ISSUER={ISSUER}",
                    "--env",
                    f"HERMES_PLATFORM_OIDC_ISSUER={ISSUER}",
                    "--env",
                    "HERMES_DASHBOARD_OIDC_CLIENT_ID=hermes-native",
                    "--env",
                    f"HERMES_DASHBOARD_PUBLIC_URL=https://{host}",
                    "--env",
                    "HERMES_PERSONAL_OIDC_CA_FILE=/run/hermes/execution-ca/oidc-ca.crt",
                    "--entrypoint",
                    PYTHON,
                ],
                ["/usr/local/lib/personal-run.py"],
            )
            if expose_probe and index == 0:
                self.probe_container = name
            self.layouts.append(layout)
        self.driver = self.start(
            "driver",
            derivative,
            [
                "--read-only",
                "--user",
                "1000:1000",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:mode=1777",
                *[part for host in hosts for part in ("--network-alias", host)],
                "--mount",
                f"type=bind,src={root / 'people.json'},"
                "dst=/fixture/people.json,readonly",
                "--mount",
                f"type=bind,src={root / 'tls'},dst=/fixture/tls,readonly",
                "--mount",
                f"type=bind,src={Path(__file__).with_name('native_shared_client.py')},dst=/client.py,readonly",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                PYTHON,
            ],
            ["/client.py", "proxy"],
        )
        self.wait_ready()
        if expose_probe:
            networks = json.loads(
                docker(
                    [
                        "inspect",
                        "--format",
                        "{{json .NetworkSettings.Networks}}",
                        self.probe_container,
                    ]
                )
            )
            self.probe_address = networks[self.network]["IPAddress"]

    def remove_network(self):
        labels = json.loads(
            docker(["network", "inspect", "--format", "{{json .Labels}}", self.network])
        )
        assert labels.get(FIXTURE) == FIXTURE_KIND and labels.get(RUN) == self.run
        docker(["network", "rm", self.network])

    def start(self, suffix, container_image, options, command):
        name = f"shared-native-{self.run}-{suffix}"
        docker(
            [
                "run",
                "-d",
                "--name",
                name,
                "--network",
                self.network,
                "--label",
                f"{FIXTURE}={FIXTURE_KIND}",
                "--label",
                f"{RUN}={self.run}",
                *options,
                container_image,
                *command,
            ]
        )

        def remove():
            require_fixture(name, FIXTURE_KIND, self.run)
            docker(["rm", "--force", "--volumes", name])

        self.cleanup.add(name, remove)
        return name

    def check_probe(self):
        """Run the controller's unmodified probe against the real instance."""
        manager = Path(__file__).resolve().parents[2] / "services/hermes-manager"
        subprocess.run(
            [
                "cargo",
                "run",
                "--quiet",
                "--locked",
                "-p",
                "hermes-native",
                "--example",
                "probe_fixture",
                "--",
                f"http://{self.probe_address}:9119/",
            ],
            cwd=manager,
            capture_output=True,
            check=True,
            timeout=180,
        )

    def check(self, case, *, timeout=90):
        result = subprocess.run(
            ["docker", "exec", self.driver, PYTHON, "/client.py", case],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            # Only the client's fixed location/type diagnostic can escape.
            diagnostic = result.stderr.decode(errors="replace").strip()
            if not re.fullmatch(
                r"native shared assertion failed: [\w:/]+ \(\w+\)", diagnostic
            ):
                diagnostic = "native shared client failed"
            raise CommandFailed(diagnostic)
        return result.stdout

    def wait_ready(self):
        deadline = time.monotonic() + 120
        while True:
            if (
                docker(
                    ["inspect", "--format", "{{.State.Running}}", self.driver]
                ).strip()
                != b"true"
            ):
                raise CommandFailed("shared native driver exited before readiness")
            try:
                self.check(self.readiness, timeout=10)
                return
            except CommandFailed:
                if time.monotonic() >= deadline:
                    raise CommandFailed(
                        "shared native fixture readiness deadline"
                    ) from None
                time.sleep(1)

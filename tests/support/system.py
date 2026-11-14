"""The installed platform's building blocks for the end-to-end cases.

Installing the chart (manager, gateway and operator Jobs), the Keycloak realm,
the in-cluster fake model, commissioning the published catalog, and the
clients the cases drive it with: the administration API, a Hermes browser
session and the directory. The cases themselves live in `tests/e2e/`.

Every credential (TLS keys, sealing keys, session keys, passwords) is generated
for the run and stays in its temporary directory. Nothing secret is printed.
"""

import base64
import hashlib
import itertools
import json
import re
import secrets
import socket
import ssl
import subprocess
import time
import uuid
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import requests
import websocket
import yaml
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from tests.support.k3s import (
    ROOT,
    command,
)

NAMESPACE = "agents"
PUBLIC = "hermes.e2e.test"
SHARED_DOMAIN = "agents.e2e.test"
SHARED_LABELS = ("engineering", "research")
ADMIN = "hermes-admin.e2e.test"
KEYCLOAK = "keycloak.keycloak.svc.cluster.local"
ISSUER = f"https://{KEYCLOAK}:8443/realms/agents"
GATEWAY_IDENTITY = "spiffe://e2e.test/hermes-gateway"
KEYCLOAK_IMAGE = (
    "quay.io/keycloak/keycloak@sha256:"
    "c68e286cacd87b942ac1939a0a1481461109d6acd3a57dc496969a319a76bbf0"
)
NODE_PORTS = {"keycloak": 30843, "gateway": 30443, "admin": 30444}
# Exact Keycloak subjects, fixed in the imported realm.
SUBJECTS = {
    "alice": "00000000-0000-4000-8000-0000000000a1",
    "bob": "00000000-0000-4000-8000-0000000000b0",
    "carol": "00000000-0000-4000-8000-0000000000c0",
    "mallory": "00000000-0000-4000-8000-0000000000d0",
    "erin": "00000000-0000-4000-8000-0000000000e0",
    "frank": "00000000-0000-4000-8000-0000000000f0",
}
READY_TIMEOUT = 900
# Instance and worker requests equal their limits, so these fix how many fit on
# a CI runner's node: 2 CPUs and about 7 GiB for the whole platform. Two owners
# run throughout, and a case may add three instances and four workers.
# A real instance stays near 400 MiB.
HERMES_RESOURCES = {"cpuMillis": 250, "memoryMib": 768}
WORKER_RESOURCES = {"cpuMillis": 100, "memoryMib": 256, "ephemeralGib": 1}
# The manager, controller and gateway have no CPU limit; a small request leaves
# the node's CPUs to the instances without slowing them.
PLATFORM_REQUESTS = {"cpu": "50m", "memory": "128Mi"}
HERE = Path(__file__).resolve().parent
# The in-cluster fake model (fake_model.py) every assistant instance uses.
MODEL_URL = "http://fake-model.model.svc.cluster.local:8080/v1"


def kubectl(identifier, *args, data=None, timeout=120, check=True, quiet=False):
    """kubectl inside the fixture cluster. A failure shows kubectl's error,
    except with `quiet`, for commands whose input or output holds secrets."""
    result = subprocess.run(
        ["docker", "exec", "-i", identifier, "kubectl", *args],
        input=data,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = "" if quiet else ": " + result.stderr.decode(errors="replace")[:800]
        raise AssertionError(f"kubectl {args[0]} failed{detail}")
    return result.stdout.decode()


def apply(identifier, documents):
    has_secret = any(document.get("kind") == "Secret" for document in documents)
    kubectl(
        identifier,
        "apply",
        "-f",
        "-",
        data=yaml.safe_dump_all(documents).encode(),
        quiet=has_secret,
    )


def b64(value):
    return base64.b64encode(
        value if isinstance(value, bytes) else value.encode()
    ).decode()


def secret(name, namespace, files):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "data": {key: b64(value) for key, value in files.items()},
    }


def openssl(*args):
    command(["openssl", *args], timeout=60)


def certificates(directory, shared_domain=None):
    """A throwaway CA and the fixture's server and client certificates."""
    path = Path(directory)
    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(path / "ca.key"),
        "-out",
        str(path / "ca.crt"),
        "-days",
        "2",
        "-subj",
        "/CN=hermes-e2e-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    leaves = {
        "api": (
            f"DNS:hermes-manager-api.{NAMESPACE}.svc,DNS:{ADMIN}",
            "serverAuth",
        ),
        "gateway": (
            f"DNS:{PUBLIC},DNS:hermes-gateway.{NAMESPACE}.svc"
            + (f",DNS:*.{shared_domain}" if shared_domain else ""),
            "serverAuth",
        ),
        "gateway-client": (f"URI:{GATEWAY_IDENTITY}", "clientAuth"),
        "keycloak": (f"DNS:{KEYCLOAK},DNS:keycloak.keycloak.svc", "serverAuth"),
    }
    for serial, (name, (san, usage)) in enumerate(leaves.items(), start=2):
        (path / f"{name}.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            f"subjectAltName={san}\n"
            f"extendedKeyUsage={usage}\n"
            "keyUsage=digitalSignature,keyEncipherment\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n"
        )
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(path / f"{name}.key"),
            "-out",
            str(path / f"{name}.csr"),
            "-subj",
            f"/CN={name}",
        )
        openssl(
            "x509",
            "-req",
            "-in",
            str(path / f"{name}.csr"),
            "-CA",
            str(path / "ca.crt"),
            "-CAkey",
            str(path / "ca.key"),
            "-set_serial",
            str(serial),
            "-out",
            str(path / f"{name}.crt"),
            "-days",
            "2",
            "-extfile",
            str(path / f"{name}.ext"),
        )
    return {
        name: (path / name).read_bytes()
        for name in [p.name for p in path.iterdir() if p.suffix in (".crt", ".key")]
    }


def realm(passwords, shared_hosts=()):
    """The `agents` realm: the fixture people, the three OIDC clients and a
    service account that manages users, standing in for the provider's own
    administration."""

    def audience(name):
        return {
            "name": f"audience-{name}",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": name,
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        }

    def client(client_id, callback, audience_name, password_grant=False):
        return {
            "clientId": client_id,
            "enabled": True,
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": password_grant,
            "redirectUris": [callback]
            + [
                f"https://{host}/_platform/oidc/callback"
                if client_id == "hermes-gateway"
                else f"https://{host}/auth/callback"
                for host in shared_hosts
                if client_id in ("hermes-gateway", "hermes-native")
            ],
            "webOrigins": ["+"],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "protocolMappers": [audience(audience_name)],
        }

    return {
        "realm": "agents",
        "enabled": True,
        "sslRequired": "all",
        "users": [
            {
                "id": subject,
                "username": name,
                "enabled": True,
                "email": f"{name}@e2e.test",
                "emailVerified": True,
                "firstName": name.title(),
                "lastName": "Fixture",
                "credentials": [
                    {"type": "password", "value": passwords[name], "temporary": False}
                ],
            }
            for name, subject in SUBJECTS.items()
        ]
        + [
            {
                "username": "service-account-e2e-directory",
                "enabled": True,
                "serviceAccountClientId": "e2e-directory",
                "clientRoles": {
                    "realm-management": ["manage-users", "view-users", "manage-realm"]
                },
            }
        ],
        "clients": [
            {
                "clientId": "e2e-directory",
                "defaultClientScopes": ["roles", "kc-admin-api"],
                "enabled": True,
                "publicClient": False,
                "secret": passwords["directory"],
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
            },
            client(
                "manager-admin",
                f"https://{ADMIN}/auth/oidc/callback",
                "hermes-manager",
                password_grant=True,
            ),
            client(
                "hermes-gateway",
                f"https://{PUBLIC}/_platform/oidc/callback",
                "hermes-gateway",
            ),
            client("hermes-native", f"https://{PUBLIC}/auth/callback", "hermes-native"),
        ],
    }


def keycloak(identifier, pinned, tls, passwords, shared_hosts=()):
    labels = {"app": "keycloak"}
    apply(
        identifier,
        [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "keycloak"}},
            secret(
                "keycloak-tls",
                "keycloak",
                {"tls.crt": tls["keycloak.crt"], "tls.key": tls["keycloak.key"]},
            ),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "keycloak-realm", "namespace": "keycloak"},
                "data": {"agents.json": json.dumps(realm(passwords, shared_hosts))},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "keycloak", "namespace": "keycloak"},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "containers": [
                                {
                                    "name": "keycloak",
                                    "image": pinned,
                                    "args": [
                                        "start-dev",
                                        "--import-realm",
                                        "--http-enabled=false",
                                        "--https-port=8443",
                                        "--https-certificate-file=/etc/keycloak/tls/tls.crt",
                                        "--https-certificate-key-file=/etc/keycloak/tls/tls.key",
                                        f"--hostname=https://{KEYCLOAK}:8443",
                                    ],
                                    "ports": [{"containerPort": 8443}],
                                    "readinessProbe": {
                                        "tcpSocket": {"port": 8443},
                                        "periodSeconds": 3,
                                    },
                                    "resources": {"limits": {"memory": "1536Mi"}},
                                    "volumeMounts": [
                                        {
                                            "name": "tls",
                                            "mountPath": "/etc/keycloak/tls",
                                        },
                                        {
                                            "name": "realm",
                                            "mountPath": "/opt/keycloak/data/import",
                                        },
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "tls",
                                    "secret": {"secretName": "keycloak-tls"},
                                },
                                {
                                    "name": "realm",
                                    "configMap": {"name": "keycloak-realm"},
                                },
                            ],
                        },
                    },
                },
            },
            node_port_service(
                "keycloak", "keycloak", labels, 8443, NODE_PORTS["keycloak"]
            ),
        ],
    )


def fake_model(identifier, image):
    """The OpenAI-compatible fake model in namespace `model`, run by the
    Hermes image's Python."""
    labels = {"app": "fake-model"}
    apply(
        identifier,
        [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "model"}},
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "fake-model", "namespace": "model"},
                "data": {"fake_model.py": (HERE / "fake_model.py").read_text()},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "fake-model", "namespace": "model"},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                            },
                            "containers": [
                                {
                                    "name": "model",
                                    "image": image,
                                    "command": [
                                        "/opt/hermes/.venv/bin/python",
                                        "/fake/fake_model.py",
                                    ],
                                    "ports": [{"containerPort": 8080}],
                                    "readinessProbe": {
                                        "tcpSocket": {"port": 8080},
                                        "periodSeconds": 2,
                                    },
                                    "volumeMounts": [
                                        {"name": "fake", "mountPath": "/fake"}
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "fake", "configMap": {"name": "fake-model"}}
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "fake-model", "namespace": "model"},
                "spec": {"selector": labels, "ports": [{"port": 8080}]},
            },
        ],
    )


def node_port_service(name, namespace, selector, port, node_port):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "type": "NodePort",
            "selector": selector,
            "ports": [{"port": port, "targetPort": port, "nodePort": node_port}],
        },
    }


# Every installed platform's routes, the active one last. Platforms use the same
# hostnames, so one process resolves them for one platform at a time.
ROUTES = []
SYSTEM_GETADDRINFO = socket.getaddrinfo


def resolve(host, port, *args, **kwargs):
    if ROUTES:
        node_ip, targets = ROUTES[-1]["node"], ROUTES[-1]["targets"]
        target = targets.get((host, int(port) if port is not None else None))
        if target is not None:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (node_ip, target),
                )
            ]
    return SYSTEM_GETADDRINFO(host, port, *args, **kwargs)


@contextmanager
def routed(node_ip, shared_hosts=()):
    """Resolves the fixture's hostnames to this node's ports, for this process,
    until it ends or another platform's route is made active; yields the
    route, which `activate` makes active again."""
    targets = {
        (KEYCLOAK, 8443): NODE_PORTS["keycloak"],
        (PUBLIC, 443): NODE_PORTS["gateway"],
        (ADMIN, 443): NODE_PORTS["admin"],
    }
    targets.update({(host, 443): NODE_PORTS["gateway"] for host in shared_hosts})
    route = {"node": node_ip, "targets": targets}
    ROUTES.append(route)
    socket.getaddrinfo = resolve
    try:
        yield route
    finally:
        ROUTES[:] = [other for other in ROUTES if other is not route]
        if not ROUTES:
            socket.getaddrinfo = SYSTEM_GETADDRINFO


def activate(route):
    """Makes an installed platform's route the one its hostnames resolve to."""
    if not any(other is route for other in ROUTES):
        raise RuntimeError("the platform's route has ended")
    ROUTES[:] = [other for other in ROUTES if other is not route] + [route]


def wait(condition, timeout, message, interval=3.0):
    """Polls until `condition` is truthy; the failure names the last error type."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = condition()
        except (requests.RequestException, OSError) as error:
            value, last = None, type(error).__name__
        if value:
            return value
        time.sleep(interval)
    text = message() if callable(message) else message
    raise AssertionError(f"{text} (last error: {last})" if last else text)


def container_ip(container, network=None):
    networks = json.loads(command(["docker", "inspect", container]))[0][
        "NetworkSettings"
    ]["Networks"]
    if network is not None:
        return networks[network]["IPAddress"]
    return next(iter(networks.values()))["IPAddress"]


def shared_network(first, second):
    def names(container):
        return set(
            json.loads(command(["docker", "inspect", container]))[0]["NetworkSettings"][
                "Networks"
            ]
        )

    shared = sorted(names(first) & names(second))
    assert shared, "PostgreSQL and the cluster share no network"
    return shared[0]


def resilient(session, methods=Retry.DEFAULT_ALLOWED_METHODS):
    """Retries a request on a kept-alive connection the server has just
    closed, as browsers do; only for `methods`."""
    retry = Retry(total=2, connect=2, read=2, status=0, allowed_methods=methods)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class Admin:
    """The administration API as Alice, with a bearer token."""

    def __init__(self, ca, password):
        # Every command carries its idempotency key, so a resent one is safe.
        self.session = resilient(requests.Session(), methods=None)
        self.session.verify = ca
        self.password = password
        self.token = None
        self.issued = 0.0

    def headers(self, etag=None):
        if time.monotonic() - self.issued > 60:
            reply = self.session.post(
                f"{ISSUER}/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "manager-admin",
                    "username": "alice",
                    "password": self.password,
                    "scope": "openid",
                },
                timeout=20,
            )
            assert reply.status_code == 200, "Keycloak refused the administrator"
            self.token = reply.json()["access_token"]
            self.issued = time.monotonic()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": uuid.uuid4().hex,
        }
        if etag is not None:
            headers["If-Match"] = etag
        return headers

    def call(self, method, path, body=None, etag=None, expected=(200, 201, 202)):
        reply = self.session.request(
            method,
            f"https://{ADMIN}{path}",
            json=body,
            headers=self.headers(etag),
            timeout=30,
        )
        assert reply.status_code in expected, (
            f"{method} {path}: {reply.status_code} {reply.text[:500]}"
        )
        return reply

    def publish(self, kind, catalog_id, definition):
        draft = self.call(
            "POST", f"/api/v1/{kind}/{catalog_id}/versions", {"definition": definition}
        ).json()
        self.call(
            "POST",
            f"/api/v1/{kind}/{catalog_id}/versions/{draft['version']}/publish",
            etag=f'"{draft["revision"]}"',
        )
        return draft["version"]

    def assignment(self, assignment_id):
        reply = self.call("GET", f"/api/v1/assignments/{assignment_id}")
        return reply.json(), reply.headers["ETag"]

    def set_access(self, assignment_id, access):
        _, etag = self.assignment(assignment_id)
        return self.call(
            "PATCH",
            f"/api/v1/assignments/{assignment_id}",
            {"access": access},
            etag=etag,
        ).json()

    def phase(self, instance_id):
        return self.call("GET", f"/api/v1/instances/{instance_id}").json()

    def release(self, receipt):
        """Stops a finished scenario's instance, returning its CPU and memory."""
        self.set_access(receipt["assignmentId"], "suspended")
        wait(
            lambda: self.phase(receipt["instanceId"])["phase"] == "retained",
            300,
            "a finished scenario's instance did not stop",
            interval=2,
        )


def keycloak_login(session, page, username, password):
    """Submits Keycloak's login form from `page`; follows to the end."""
    match = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
    assert match, f"no Keycloak login form for {username}"
    return session.post(
        unescape(match.group(1)),
        data={"username": username, "password": password, "credentialId": ""},
        # As a browser submits a form; the redirects after it are page loads.
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=30,
    )


class Browser:
    """One person's browser: platform session, native session, cookies."""

    def __init__(self, ca, name, password, host=PUBLIC):
        self.session = resilient(requests.Session())
        self.session.verify = ca
        self.ca = ca
        self.name = name
        self.password = password
        self.host = host

    def get(self, path, **kwargs):
        kwargs.setdefault("timeout", 30)
        return self.session.get(f"https://{self.host}{path}", **kwargs)

    def post(self, path, **kwargs):
        kwargs.setdefault("timeout", 30)
        headers = {"Origin": f"https://{self.host}", **kwargs.pop("headers", {})}
        return self.session.post(
            f"https://{self.host}{path}", headers=headers, **kwargs
        )

    def sign_in(self):
        """Platform login from a protected page, through Keycloak."""
        page = self.get("/", headers={"Accept": "text/html"})
        assert KEYCLOAK in page.url, f"{self.name} was not sent to Keycloak"
        return keycloak_login(self.session, page, self.name, self.password)

    def native_sign_in(self):
        """Native Hermes's own OIDC login, which reuses the Keycloak session."""
        providers = self.get("/api/auth/providers").json()
        listed = providers.get("providers", providers)
        name = next(item["name"] if isinstance(item, dict) else item for item in listed)
        reply = self.get("/auth/login", params={"provider": name, "next": "/"})
        assert reply.status_code == 200, (
            f"native login for {self.name}: {reply.status_code}"
        )
        me = self.get("/api/auth/me")
        assert me.status_code == 200, (
            f"native session for {self.name}: {me.status_code}"
        )
        return me.json()

    def cookie_header(self):
        return "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in self.session.cookies
            if cookie.domain.lstrip(".") == self.host
        )

    def websocket(self):
        ticket = self.post("/api/auth/ws-ticket")
        assert ticket.status_code == 200, f"ws ticket: {ticket.status_code}"
        connection = websocket.create_connection(
            f"wss://{self.host}/api/events?channel=e2e&ticket={ticket.json()['ticket']}",
            header=[f"Cookie: {self.cookie_header()}"],
            origin=f"https://{self.host}",
            sslopt={"ca_certs": self.ca, "cert_reqs": ssl.CERT_REQUIRED},
            timeout=20,
        )
        return connection

    def put(self, path, **kwargs):
        kwargs.setdefault("timeout", 30)
        headers = {"Origin": f"https://{self.host}", **kwargs.pop("headers", {})}
        return self.session.put(f"https://{self.host}{path}", headers=headers, **kwargs)

    def upload(self, path, data):
        """Native Hermes's managed file upload."""
        encoded = base64.b64encode(data).decode()
        return self.post(
            "/api/files/upload",
            json={
                "path": path,
                "data_url": f"data:text/plain;base64,{encoded}",
                "overwrite": True,
            },
        )

    def rpc(self):
        """Native chat's JSON-RPC WebSocket, through the gateway."""
        ticket = self.post("/api/auth/ws-ticket")
        assert ticket.status_code == 200, f"ws ticket: {ticket.status_code}"
        return websocket.create_connection(
            f"wss://{self.host}/api/ws?ticket={ticket.json()['ticket']}",
            header=[f"Cookie: {self.cookie_header()}"],
            origin=f"https://{self.host}",
            sslopt={"ca_certs": self.ca, "cert_reqs": ssl.CERT_REQUIRED},
            timeout=60,
        )

    def chat(self, text, timeout=240):
        """One turn in a new native session; returns the session and the
        turn's final text."""
        connection = self.rpc()
        try:
            return converse(connection, text, timeout)
        finally:
            connection.close()


def converse(connection, text, timeout):
    """`session.create`, then `prompt.submit`, then the session's
    `message.complete` event, over newline-delimited JSON-RPC."""
    deadline = time.monotonic() + timeout
    held = []

    def frames():
        while True:
            connection.settimeout(max(1.0, deadline - time.monotonic()))
            for line in connection.recv().splitlines():
                if line.strip():
                    yield json.loads(line)

    stream = frames()

    def call(ident, method, params):
        connection.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
            )
        )
        for frame in stream:
            if frame.get("id") == ident:
                assert "error" not in frame, f"{method}: {frame['error']}"
                return frame["result"]
            held.append(frame)
        raise AssertionError(f"{method}: no reply")

    session = call(1, "session.create", {})
    call(2, "prompt.submit", {"session_id": session["session_id"], "text": text})
    for frame in itertools.chain(held, stream):
        params = frame.get("params") or {}
        if (
            frame.get("method") == "event"
            and params.get("type") == "message.complete"
            and params.get("session_id") == session["session_id"]
        ):
            return session, str((params.get("payload") or {}).get("text", ""))
    raise AssertionError("the turn never completed")


class Directory:
    """The provider's user administration (Keycloak's admin API), as its own
    administrator would change people."""

    def __init__(self, ca, secret):
        self.session = requests.Session()
        self.session.verify = ca
        self.secret = secret

    def call(self, method, path, body=None, expected=(200, 201, 204)):
        token = self.session.post(
            f"{ISSUER}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "e2e-directory",
                "client_secret": self.secret,
            },
            timeout=20,
        )
        assert token.status_code == 200, "Keycloak refused the directory client"
        reply = self.session.request(
            method,
            f"https://{KEYCLOAK}:8443/admin/realms/agents{path}",
            json=body,
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            timeout=20,
        )
        assert reply.status_code in expected, f"{method} {path}: {reply.status_code}"
        return reply

    def update(self, subject, **changes):
        user = self.call("GET", f"/users/{subject}").json()
        user.update(changes)
        self.call("PUT", f"/users/{subject}", user)

    def create(self, username, email, password):
        """A new person; returns their new subject."""
        reply = self.call(
            "POST",
            "/users",
            {
                "username": username,
                "email": email,
                "emailVerified": True,
                "enabled": True,
                "firstName": username.title(),
                "lastName": "Fixture",
                "credentials": [
                    {"type": "password", "value": password, "temporary": False}
                ],
            },
            expected=(201,),
        )
        return reply.headers["Location"].rsplit("/", 1)[1]


def closed_within(connection, seconds):
    """Seconds until the server closes `connection`."""
    started = time.monotonic()
    connection.settimeout(seconds)
    try:
        while True:
            opcode, _data = connection.recv_data()
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
    except websocket.WebSocketTimeoutException:
        return time.monotonic() - started
    except websocket.WebSocketException, OSError:
        pass
    # The client still reports `connected` after a close frame or a reset.
    connection.shutdown()
    return time.monotonic() - started


def installation():
    """The installation's ID and the controller's sealing key pair. A restore
    keeps both: materials and admin secrets are sealed to the key."""
    sealing = X25519PrivateKey.generate()
    return {
        "id": str(uuid.uuid4()),
        "private": sealing.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        ),
        "public": sealing.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    }


FRESH = [
    ("migrate", "hermes-manager-migrate"),
    ("initialize", "hermes-manager-initialize"),
    ("grantAdministrator", "hermes-manager-grant-administrator"),
]


def install(
    identifier,
    kubeconfig,
    temporary,
    images,
    database_url,
    tls,
    node,
    owned,
    steps,
    shared_domain=None,
):
    """Secrets, then the chart with each operator step, then the roles."""
    private, public = owned["private"], owned["public"]
    identity = json.dumps({"issuer": ISSUER, "subject": SUBJECTS["alice"]})
    apply(
        identifier,
        [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
            secret(
                "hermes-manager-api",
                NAMESPACE,
                {
                    "database-url": database_url,
                    "admin-tls.crt": tls["api.crt"],
                    "admin-tls.key": tls["api.key"],
                    "execution-tls.crt": tls["api.crt"],
                    "execution-tls.key": tls["api.key"],
                    "resolver-tls.crt": tls["api.crt"],
                    "resolver-tls.key": tls["api.key"],
                    "resolver-client-ca.crt": tls["ca.crt"],
                    "secret-sealing.pub": public,
                    "session-database-url": database_url,
                    "session-key-k1": secrets.token_bytes(32),
                    "oidc-ca.crt": tls["ca.crt"],
                },
            ),
            secret(
                "hermes-manager-controller",
                NAMESPACE,
                {
                    "database-url": database_url,
                    "secret-sealing.key": private,
                    "execution-ca.crt": tls["ca.crt"],
                    "oidc-ca.crt": tls["ca.crt"],
                },
            ),
            secret(
                "hermes-gateway",
                NAMESPACE,
                {
                    "database-url": database_url,
                    "tls.crt": tls["gateway.crt"],
                    "tls.key": tls["gateway.key"],
                    "session-key-g1": secrets.token_bytes(32),
                    "resolver-ca.crt": tls["ca.crt"],
                    "resolver-client.crt": tls["gateway-client.crt"],
                    "resolver-client.key": tls["gateway-client.key"],
                    "oidc-ca.crt": tls["ca.crt"],
                },
            ),
            secret("hermes-manager-schema", NAMESPACE, {"database-url": database_url}),
            secret(
                "hermes-first-administrator",
                NAMESPACE,
                {"database-url": database_url, "identity.json": identity},
            ),
        ],
    )
    jwks = f"{ISSUER}/protocol/openid-connect/certs"
    everyone = [{"ipBlock": {"cidr": "0.0.0.0/0"}}]
    values = {
        "images": {"hermes-manager": images["manager"]},
        "hermesManager": {
            "enabled": True,
            "resources": {"requests": PLATFORM_REQUESTS},
            "installation": {"id": owned["id"], "issuer": ISSUER},
            "storageSources": [
                {"id": "local", "storageClass": {"name": "local-path"}},
                {"id": "host", "localPath": {"node": node, "root": "/srv/hermes-data"}},
            ],
            "publicOrigin": f"https://{PUBLIC}",
            "adminOrigin": f"https://{ADMIN}",
            "oidcPrivateCa": True,
            "publicHttps": False,
            "serviceEgress": [
                {"namespace": "keycloak", "labels": {"app": "keycloak"}, "port": 8443},
                {"namespace": "model", "labels": {"app": "fake-model"}, "port": 8080},
            ],
            "api": {
                "existingSecret": "hermes-manager-api",
                "tokens": {
                    "jwksUrl": jwks,
                    "audience": "hermes-manager",
                    "authorizedClients": ["manager-admin"],
                },
                "browser": {"sessionKeys": {"active": "k1", "ids": ["k1"]}},
                "gatewayIdentity": GATEWAY_IDENTITY,
            },
            "controller": {"existingSecret": "hermes-manager-controller"},
            "gateway": {
                "existingSecret": "hermes-gateway",
                "oidc": {
                    "clientId": "hermes-gateway",
                    "jwksUrl": jwks,
                    "audience": "hermes-gateway",
                },
                "sessionKeys": {"active": "g1", "ids": ["g1"]},
            },
            "ingress": {"admin": everyone, "gateway": everyone},
        },
    }
    if shared_domain:
        values["hermesManager"]["agentDomain"] = shared_domain

    def helm(operations):
        values["hermesManager"]["operations"] = operations
        path = Path(temporary) / "values.yaml"
        path.write_text(yaml.safe_dump(values))
        command(
            [
                "helm",
                "upgrade",
                "--install",
                "agents",
                str(ROOT / "charts/agent-platform"),
                "--namespace",
                NAMESPACE,
                "--kubeconfig",
                str(kubeconfig),
                "--values",
                str(path),
            ],
            timeout=120,
        )

    off = {
        "migrate": {"enabled": False, "existingSecret": "hermes-manager-schema"},
        "initialize": {"enabled": False},
        "rebindCluster": {"enabled": False},
        "grantAdministrator": {
            "enabled": False,
            "existingSecret": "hermes-first-administrator",
            "recover": False,
        },
    }
    for step, job in steps:
        operations = json.loads(json.dumps(off))
        operations[step]["enabled"] = True
        helm(operations)
        revision = json.loads(
            command(
                [
                    "helm",
                    "history",
                    "agents",
                    "--namespace",
                    NAMESPACE,
                    "--kubeconfig",
                    str(kubeconfig),
                    "--output",
                    "json",
                ]
            )
        )[-1]["revision"]
        kubectl(
            identifier,
            "wait",
            "--namespace",
            NAMESPACE,
            "--for=condition=complete",
            f"job/{job}-{revision}",
            "--timeout=300s",
            timeout=320,
        )
    helm(off)
    for name in ("hermes-manager-api", "hermes-manager-controller", "hermes-gateway"):
        kubectl(
            identifier,
            "rollout",
            "status",
            "--namespace",
            NAMESPACE,
            f"deployment/{name}",
            "--timeout=300s",
            timeout=320,
        )
    apply(
        identifier,
        [
            node_port_service(
                "hermes-gateway-e2e",
                NAMESPACE,
                {"app.kubernetes.io/name": "hermes-gateway"},
                8443,
                NODE_PORTS["gateway"],
            ),
            node_port_service(
                "hermes-manager-admin-e2e",
                NAMESPACE,
                {"app.kubernetes.io/name": "hermes-manager-api"},
                8443,
                NODE_PORTS["admin"],
            ),
        ],
    )


def instance_pod_exec(identifier, namespace, script):
    return kubectl(
        identifier,
        "exec",
        "--namespace",
        namespace,
        "deployment/hermes",
        "-c",
        "hermes",
        "--",
        "sh",
        "-c",
        script,
    )


def commission(identifier, images, ca, passwords):
    """Publishes the catalog and commissions Bob's and Carol's instances through
    the administration API, and waits until both are ready. Everything a
    scenario needs is on the returned state; nothing is torn down."""
    admin = Admin(ca, passwords["alice"])
    wait(
        lambda: (
            admin.session.get(f"https://{ADMIN}/healthz", timeout=5).status_code == 200
        ),
        120,
        "the administration API is unreachable",
    )
    # The model key: an admin secret only the assistant template references.
    admin.call(
        "PUT",
        "/api/v1/secrets/llm-key",
        {"kind": "env", "value": passwords["llm"]},
    )
    base = {
        "schemaVersion": 2,
        "image": images["hermes"],
        "resources": HERMES_RESOURCES,
        "home": {"source": "local", "sizeGib": 1},
        "mounts": {"workspace": {"mountPath": "/workspace"}},
        "defaultVolumes": {"workspace": {"source": "local", "sizeGib": 1}},
    }
    template = admin.publish("templates", "personal", base)
    assistant = admin.publish(
        "templates",
        "assistant",
        {
            **base,
            "adminConfig": {
                "model": {
                    "provider": "custom",
                    "base_url": MODEL_URL,
                    "default": "fake-model",
                    "api_key": "${OPENAI_API_KEY}",
                }
            },
            "secrets": [{"name": "llm-key", "env": "OPENAI_API_KEY"}],
        },
    )
    worker = {
        "image": images["worker"],
        "sshContract": "managed-ssh-v2",
        "uid": 1000,
        "gid": 1000,
        "resources": WORKER_RESOURCES,
    }
    backend = admin.publish(
        "terminal-backends",
        "shell",
        {
            **worker,
            "limits": {
                "idleSeconds": 300,
                "maxLeaseSeconds": 900,
                "maxEnvironments": 4,
            },
            "mounts": {"workspace": {"mountPath": "/workspace"}},
        },
    )
    # Bob's second backend also mounts his network home and idles out fast.
    tools = admin.publish(
        "terminal-backends",
        "tools",
        {
            **worker,
            "limits": {"idleSeconds": 60, "maxLeaseSeconds": 900, "maxEnvironments": 2},
            "mounts": {
                "workspace": {"mountPath": "/workspace"},
                "home": {"mountPath": "/home/worker/home"},
            },
        },
    )
    command(
        [
            "docker",
            "exec",
            identifier,
            "sh",
            "-c",
            "mkdir -p /srv/hermes-data/homes/bob"
            " && chown -R 1000:1000 /srv/hermes-data/homes",
        ]
    )
    terminal = {"default": "shell", "allowed": {"shell": backend}}
    assignments = {}
    for name, spec in [
        (
            "bob",
            {
                "template": {"id": "assistant", "version": assistant},
                "volumes": {
                    "workspace": {"source": "local", "sizeGib": 1},
                    "home": {"source": "host", "path": "homes/bob"},
                },
                "terminal": {
                    "default": "shell",
                    "allowed": {"shell": backend, "tools": tools},
                },
                # Layered over the template's settings.
                "adminConfig": {"display": {"compact": True}},
            },
        ),
        (
            "carol",
            {
                "template": {"id": "personal", "version": template},
                "volumes": {"workspace": {"source": "host", "path": "carol"}},
                "terminal": terminal,
            },
        ),
    ]:
        assignments[name] = admin.call(
            "POST",
            "/api/v1/assignments",
            {
                "identity": {"issuer": ISSUER, "subject": SUBJECTS[name]},
                "spec": {"access": "enabled", **spec},
            },
        ).json()
    for name, receipt in assignments.items():
        wait(
            lambda receipt=receipt: (
                admin.phase(receipt["instanceId"])["phase"] == "ready"
            ),
            READY_TIMEOUT,
            lambda receipt=receipt, name=name: (
                f"{name}'s instance never became ready: "
                f"{admin.phase(receipt['instanceId'])}"
            ),
            interval=5,
        )
    versions = {"template": template, "shell": backend, "tools": tools}
    return SimpleNamespace(
        admin=admin,
        assignments=assignments,
        template=template,
        terminal=terminal,
        versions=versions,
        worker=worker,
    )


PLUGIN_PRELUDE = """
import json, sys, time
from pathlib import Path
sys.path[:0] = ["/opt/hermes", "/opt/hermes/plugins"]
import managed_execution

class Context:
    def __init__(self, runtime_id):
        self.runtime_id = runtime_id

    def get_config(self, key, default=""):
        return self.runtime_id if key == "runtime_id" else default

def environment(runtime_id, task, timeout=120):
    provider = managed_execution.Provider(Context(runtime_id))
    return provider.create_environment(cwd="/workspace", timeout=timeout, task_id=task)

def refused(runtime_id, task):
    try:
        environment(runtime_id, task, timeout=60).execute("true")
    except Exception as error:
        return str(error)
    return None
"""


def hermes_python(identifier, namespace, script, timeout=400):
    """Runs `script` with the Hermes Python in the owner's running instance;
    returns the JSON object its last line prints."""
    output = kubectl(
        identifier,
        "exec",
        "-i",
        "--namespace",
        namespace,
        "deployment/hermes",
        "-c",
        "hermes",
        "--",
        "/opt/hermes/.venv/bin/python",
        "-",
        data=script.encode(),
        timeout=timeout,
    )
    return json.loads(output.strip().splitlines()[-1])


def hermes_python_started(identifier, namespace, script):
    """The same in the background; read its output lines as they come."""
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            identifier,
            "kubectl",
            "exec",
            "-i",
            "--namespace",
            namespace,
            "deployment/hermes",
            "-c",
            "hermes",
            "--",
            "/opt/hermes/.venv/bin/python",
            "-u",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    process.stdin.write(script)
    process.stdin.close()
    return process


def environments(admin, instance_id):
    return admin.call("GET", f"/api/v1/instances/{instance_id}/environments").json()[
        "items"
    ]


def namespaces(identifier):
    return set(
        kubectl(
            identifier, "get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}"
        ).split()
    )


def secret_digests(identifier, namespace, name):
    """SHA-256 of each value of a Secret; the values are never printed."""
    data = json.loads(
        kubectl(
            identifier,
            "get",
            "secret",
            name,
            "--namespace",
            namespace,
            "-o",
            "json",
            quiet=True,
        )
    ).get("data", {})
    return {
        key: hashlib.sha256(base64.b64decode(value)).hexdigest()
        for key, value in data.items()
    }


def signed_in(ca, name, passwords):
    """A browser with platform and native sessions."""
    browser = Browser(ca, name, passwords[name])
    landed = browser.sign_in()
    assert landed.status_code == 200, f"{name}: {landed.status_code}"
    browser.native_sign_in()
    return browser


def hermes_pod(identifier, namespace):
    """The UID of the instance's Hermes Pod (worker Pods are `hw-…`)."""
    pods = kubectl(
        identifier,
        "get",
        "pods",
        "--namespace",
        namespace,
        "-o",
        'jsonpath={range .items[*]}{.metadata.name} {.metadata.uid}{"\\n"}{end}',
    )
    uids = [
        line.split()[1]
        for line in pods.splitlines()
        if line.strip() and not line.startswith("hw-")
    ]
    assert len(uids) == 1, f"Hermes Pods: {pods}"
    return uids[0]


def change_terminal(admin, assignment_id, change):
    """Applies `change(terminal)` to an assignment's terminal grant."""
    current, etag = admin.assignment(assignment_id)
    terminal = json.loads(json.dumps(current["spec"]["terminal"]))
    change(terminal)
    return admin.call(
        "PATCH",
        f"/api/v1/assignments/{assignment_id}",
        {"terminal": terminal},
        etag=etag,
    ).json()


def scale(identifier, namespace, deployment, replicas):
    kubectl(
        identifier,
        "scale",
        "--namespace",
        namespace,
        f"deployment/{deployment}",
        f"--replicas={replicas}",
    )
    if replicas:
        kubectl(
            identifier,
            "rollout",
            "status",
            "--namespace",
            namespace,
            f"deployment/{deployment}",
            "--timeout=300s",
            timeout=320,
        )
    else:
        wait(
            lambda: (
                not kubectl(
                    identifier,
                    "get",
                    "pods",
                    "--namespace",
                    namespace,
                    "--selector",
                    f"app.kubernetes.io/name={deployment}"
                    if namespace == NAMESPACE
                    else f"app={deployment}",
                    "-o",
                    "name",
                ).strip()
            ),
            180,
            f"{deployment} did not stop",
            interval=1,
        )

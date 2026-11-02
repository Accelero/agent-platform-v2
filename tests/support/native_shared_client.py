"""Runs inside the isolated native fixture; outputs no identities or tokens.

The relay terminates TLS and copies HTTP/1 bytes (including WebSocket upgrades)
to a fixed native destination. This is a fixture, not the platform gateway.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import ssl
import sys
import time
from contextlib import ExitStack
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from websockets.sync.client import connect

HOSTS = ("engineering.agents.test", "support.agents.test")
UNREGISTERED = "unregistered.agents.test"
ISSUER = "https://sso.shared.test:8443/realms/agents"
CA = "/fixture/tls/ca.crt"
BOB = "00000000-0000-4000-8000-0000000000b0"
CAROL = "00000000-0000-4000-8000-0000000000c0"


async def proxy():
    async def serve(reader, writer):
        upstream = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            host = next(
                line.split(b":", 1)[1].strip().decode().lower()
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"host:")
            )
            if host not in (*HOSTS, UNREGISTERED):
                return
            index = (*HOSTS, UNREGISTERED).index(host)
            remote, upstream = await asyncio.open_connection(f"native-{index}", 9119)
            header = (
                header[:-2]
                + f"X-Forwarded-Host: {host}\r\n".encode()
                + b"X-Forwarded-Proto: https\r\n\r\n"
            )
            upstream.write(header)
            await upstream.drain()

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            tasks = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(remote, writer)),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as error:
            print(f"native relay failed: {type(error).__name__}", flush=True)
        finally:
            writer.close()
            if upstream:
                upstream.close()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/fixture/tls/server.crt", "/fixture/tls/server.key")
    server = await asyncio.start_server(serve, "0.0.0.0", 443, ssl=context)
    async with server:
        await server.serve_forever()


class Browser:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = CA

    def close(self):
        self.session.close()

    def request(self, method, host, path, **kwargs):
        return self.session.request(
            method,
            f"https://{host}{path}",
            headers={"Origin": f"https://{host}"},
            timeout=20,
            **kwargs,
        )

    def login(self, host, person=None):
        reply = self.request(
            "GET",
            host,
            "/auth/login?provider=self-hosted&next=/",
            allow_redirects=False,
        )
        assert reply.status_code == 302, "native login did not redirect"
        target = reply.headers["Location"]
        assert parse_qs(urlsplit(target).query)["redirect_uri"] == [
            f"https://{host}/auth/callback"
        ]
        reply = self.session.get(target, timeout=20)
        match = re.search(
            r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', reply.text
        )
        if person is None:
            assert match is None, "SSO showed a second login form"
        else:
            assert match is not None, "first login did not show the provider form"
            passwords = json.loads(Path("/fixture/people.json").read_text())
            reply = self.session.post(
                unescape(match.group(1)),
                data={
                    "username": person,
                    "password": passwords[person],
                    "credentialId": "",
                },
                timeout=20,
            )
        assert reply.status_code == 200, "native login failed"

    def me(self, host, subject):
        reply = self.request("GET", host, "/api/auth/me")
        assert reply.status_code == 200, "native session not authenticated"
        assert reply.json()["user_id"] == subject, "wrong native subject"

    def gateway_client(self, host):
        """Join the existing SSO session as the gateway's real Keycloak client.

        This exercises the provider's client/session lifetime. It creates no
        platform session and does not stand in for the gateway's own tests.
        """
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(24)
        callback = f"https://{host}/_platform/oidc/callback"
        authorization = self.session.get(
            f"{ISSUER}/protocol/openid-connect/auth",
            params={
                "client_id": "hermes-gateway",
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": callback,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            allow_redirects=False,
            timeout=20,
        )
        assert authorization.status_code == 302, "gateway client SSO failed"
        redirect = urlsplit(authorization.headers["Location"])
        expected = urlsplit(callback)
        assert (redirect.scheme, redirect.netloc, redirect.path) == (
            expected.scheme,
            expected.netloc,
            expected.path,
        ), "wrong gateway client callback"
        query = parse_qs(redirect.query)
        assert query["state"] == [state], "wrong gateway client state"
        token = self.session.post(
            f"{ISSUER}/protocol/openid-connect/token",
            data={
                "client_id": "hermes-gateway",
                "grant_type": "authorization_code",
                "redirect_uri": callback,
                "code_verifier": verifier,
                "code": query["code"][0],
            },
            timeout=20,
        )
        assert token.status_code == 200, "gateway client code exchange failed"
        return token.json()["refresh_token"]

    def provider_refresh(self, client, token):
        return self.session.post(
            f"{ISSUER}/protocol/openid-connect/token",
            data={
                "client_id": client,
                "grant_type": "refresh_token",
                "refresh_token": token,
            },
            timeout=20,
        )

    def ws(self, host):
        reply = self.request("POST", host, "/api/auth/ws-ticket")
        assert reply.status_code == 200, "ticket mint failed"
        cookies = "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in self.session.cookies
            if cookie.domain.lstrip(".") == host
        )
        return connect(
            f"wss://{host}/api/ws?ticket={reply.json()['ticket']}",
            additional_headers={"Cookie": cookies},
            origin=f"https://{host}",
            ssl=ssl.create_default_context(cafile=CA),
            proxy=None,
            open_timeout=20,
            close_timeout=2,
        )

    def expires(self, host):
        token = next(
            cookie.value
            for cookie in self.session.cookies
            if cookie.domain.lstrip(".") == host
            and cookie.name.endswith("hermes_session_at")
        )
        payload = token.split(".")[1]
        return json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )["exp"]


def ready(*, unregistered=False):
    with requests.Session() as client:
        client.trust_env = False
        client.verify = CA
        assert (
            client.get(
                f"{ISSUER}/.well-known/openid-configuration", timeout=2
            ).status_code
            == 200
        )
        hosts = (*HOSTS, UNREGISTERED) if unregistered else HOSTS
        for host in hosts:
            reply = client.get(f"https://{host}/api/auth/providers", timeout=2)
            assert reply.status_code == 200
            assert [item["name"] for item in reply.json()["providers"]] == [
                "self-hosted"
            ]


def two_users():
    with ExitStack() as stack:
        clients = [Browser(), Browser()]
        for client, person, subject in zip(
            clients, ("bob", "carol"), (BOB, CAROL), strict=True
        ):
            stack.callback(client.close)
            client.login(HOSTS[0], person)
            client.me(HOSTS[0], subject)
        sockets = [client.ws(HOSTS[0]) for client in clients]
        for connection in sockets:
            stack.callback(connection.close)
        # A protocol ping/pong proves both upgrades remain live concurrently.
        for connection in sockets:
            assert connection.ping(b"shared-native").wait(timeout=10), "no native pong"


def providers():
    with ExitStack() as stack:
        browser = Browser()
        stack.callback(browser.close)
        response = browser.request("GET", HOSTS[0], "/api/auth/providers")
        assert [p["name"] for p in response.json()["providers"]] == ["self-hosted"]
        refused = browser.request(
            "POST",
            HOSTS[0],
            "/auth/password-login",
            json={"username": "local-fixture", "password": "local-fixture-password"},
            allow_redirects=False,
        )
        assert not 200 <= refused.status_code < 300
        assert browser.request("GET", HOSTS[0], "/api/auth/me").status_code == 401
        browser.login(HOSTS[0], "bob")
        browser.me(HOSTS[0], BOB)


def source():
    import plugins.dashboard_auth.self_hosted as provider

    print(hashlib.sha256(Path(provider.__file__).read_bytes()).hexdigest())


def selector():
    browser = Browser()
    try:
        browser.login(HOSTS[0], "bob")
        reply = browser.request("GET", HOSTS[0], "/api/config/schema")
        assert reply.status_code == 200, "native settings schema unavailable"
        assert "terminal.runtime_id" in reply.json()["fields"], (
            "terminal selector missing"
        )
    finally:
        browser.close()


def parallel():
    with ExitStack() as stack:
        bob, carol = Browser(), Browser()
        stack.callback(bob.close)
        stack.callback(carol.close)
        for browser, person in ((bob, "bob"), (carol, "carol")):
            browser.login(HOSTS[0], person)
            browser.login(HOSTS[1])
        for browser, subject in ((bob, BOB), (carol, CAROL)):
            for host in HOSTS:
                browser.me(host, subject)
        for _ in range(2):
            expiry = max(
                browser.expires(host) for browser in (bob, carol) for host in HOSTS
            )
            time.sleep(max(0, expiry - time.time()) + 1)
            for browser, subject in ((bob, BOB), (carol, CAROL)):
                for host in HOSTS:
                    browser.me(host, subject)
                    assert browser.expires(host) > expiry, (
                        "native token did not refresh"
                    )
        # Logging out revokes the native client across this person's provider
        # session. It leaves another person's native client and SSO untouched.
        # With no other active client, the provider ends Bob's SSO session.
        reply = bob.request("POST", HOSTS[0], "/auth/logout")
        assert reply.status_code == 200
        expiry = max(bob.expires(HOSTS[1]), *(carol.expires(host) for host in HOSTS))
        time.sleep(max(0, expiry - time.time()) + 1)
        reply = bob.request("GET", HOSTS[1], "/api/auth/me", allow_redirects=False)
        assert reply.status_code == 401, "other host refreshed after native logout"
        for host in HOSTS:
            carol.me(host, CAROL)
        for index, host in enumerate(HOSTS):
            bob.login(host, "bob" if index == 0 else None)
            bob.me(host, BOB)


def provider_sessions(mode):
    """Reproduce the last-client finding without weakening the SH-N04 gate."""
    browser = Browser()
    try:
        browser.login(HOSTS[0], "bob")
        browser.login(HOSTS[1])
        for host in HOSTS:
            browser.me(host, BOB)
        native_refresh = next(
            cookie.value
            for cookie in browser.session.cookies
            if cookie.domain.lstrip(".") == HOSTS[1]
            and cookie.name.endswith("hermes_session_rt")
        )
        gateway_refresh = None
        if mode != "native-only":
            gateway_refresh = browser.gateway_client(HOSTS[0])
        logout = browser.request(
            "POST", HOSTS[0], "/auth/logout", allow_redirects=False
        )
        assert logout.status_code == 302, "native logout failed"
        if mode == "both-revoked":
            revoked = browser.session.post(
                f"{ISSUER}/protocol/openid-connect/revoke",
                data={
                    "client_id": "hermes-gateway",
                    "token_type_hint": "refresh_token",
                    "token": gateway_refresh,
                },
                timeout=20,
            )
            assert revoked.status_code == 200, "gateway client revocation failed"
        native = browser.provider_refresh("hermes-native", native_refresh)
        assert native.status_code == 400, "native client still refreshed after logout"
        assert native.json()["error"] == "invalid_grant", "wrong native refresh refusal"
        if gateway_refresh:
            gateway = browser.provider_refresh("hermes-gateway", gateway_refresh)
            expected = 200 if mode == "gateway-retained" else 400
            assert gateway.status_code == expected, "wrong gateway client lifetime"
            if mode == "both-revoked":
                assert gateway.json()["error"] == "invalid_grant", (
                    "wrong gateway refresh refusal"
                )
        # An active client retains Keycloak SSO. With the last client revoked,
        # the same browser must enter its credentials again on the native host.
        person = None if mode == "gateway-retained" else "bob"
        browser.login(HOSTS[0], person)
        browser.me(HOSTS[0], BOB)
    finally:
        browser.close()


def callback():
    browser = Browser()
    try:
        browser.login(HOSTS[0], "bob")
        browser.login(HOSTS[1])
        browser.me(HOSTS[1], BOB)
        for path in ("/api/sessions", "/api/config", "/api/cron/jobs"):
            assert browser.request("GET", HOSTS[1], path).status_code == 200, (
                "native API failed"
            )
        uploaded = browser.request(
            "POST",
            HOSTS[1],
            "/api/files/upload",
            json={
                "path": "uploads/shared.txt",
                "data_url": "data:text/plain;base64,c2hhcmVk",
                "overwrite": True,
            },
        )
        assert uploaded.status_code == 200, "native upload failed"
        listing = browser.request("GET", HOSTS[1], "/api/files?path=uploads")
        assert listing.status_code == 200 and "shared.txt" in listing.text
        # This third instance renders an unregistered hostname as its actual
        # public URL. The provider must reject it before issuing a code.
        reply = browser.request(
            "GET",
            UNREGISTERED,
            "/auth/login?provider=self-hosted",
            allow_redirects=False,
        )
        assert reply.status_code == 302
        target = reply.headers["Location"]
        assert parse_qs(urlsplit(target).query)["redirect_uri"] == [
            f"https://{UNREGISTERED}/auth/callback"
        ]
        refused = browser.session.get(target, timeout=20)
        assert refused.status_code == 400 and "redirect_uri" in refused.text
        with requests.Session() as stranger:
            stranger.trust_env = False
            reply = stranger.get(
                f"https://{UNREGISTERED}/api/auth/me",
                verify=CA,
                allow_redirects=False,
                timeout=10,
            )
            assert reply.status_code == 401
    finally:
        browser.close()


if __name__ == "__main__":
    if sys.argv[1] == "proxy":
        asyncio.run(proxy())
    else:
        try:
            {
                "ready": ready,
                "ready_unregistered": lambda: ready(unregistered=True),
                "two_users": two_users,
                "source": source,
                "providers": providers,
                "selector": selector,
                "parallel": parallel,
                "provider_native_only": lambda: provider_sessions("native-only"),
                "provider_gateway_retained": lambda: provider_sessions(
                    "gateway-retained"
                ),
                "provider_both_revoked": lambda: provider_sessions("both-revoked"),
                "callback": callback,
            }[sys.argv[1]]()
        except Exception as error:
            # Auth URLs, token responses and request objects never enter test
            # reports. Diagnose locally inside this disposable fixture.
            import traceback

            locations = "/".join(
                f"{re.sub(r'\W', '_', frame.name)}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)
                if frame.filename == __file__ and frame.name != "<module>"
            )
            raise SystemExit(
                f"native shared assertion failed: {locations} ({type(error).__name__})"
            ) from None

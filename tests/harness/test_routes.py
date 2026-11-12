"""Platforms share hostnames: the process resolves them to the active one."""

import socket

import pytest

from tests.support import system

pytestmark = pytest.mark.unit


def address(host=system.ADMIN):
    return socket.getaddrinfo(host, 443)[0][4]


def test_the_latest_route_answers_until_another_is_activated():
    with system.routed("10.0.0.1") as first, system.routed("10.0.0.2"):
        assert address() == ("10.0.0.2", system.NODE_PORTS["admin"])
        system.activate(first)
        assert address() == ("10.0.0.1", system.NODE_PORTS["admin"])
    assert socket.getaddrinfo is system.SYSTEM_GETADDRINFO


def test_a_route_ends_for_itself_in_any_order():
    first = system.routed("10.0.0.1")
    second = system.routed("10.0.0.2")
    first.__enter__()
    route = second.__enter__()
    first.__exit__(None, None, None)
    assert address() == ("10.0.0.2", system.NODE_PORTS["admin"])
    second.__exit__(None, None, None)
    assert socket.getaddrinfo is system.SYSTEM_GETADDRINFO
    with pytest.raises(RuntimeError):
        system.activate(route)


def test_other_hosts_resolve_as_usual():
    with system.routed("10.0.0.1"):
        assert address("localhost")[0] in {"127.0.0.1", "::1"}

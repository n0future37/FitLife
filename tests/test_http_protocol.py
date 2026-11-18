import socket
from types import SimpleNamespace

from uvicorn.protocols.http.h11_impl import H11Protocol

from fitlife.http_protocol import NoDelayH11Protocol


def test_inherited_proto_zero_socket_has_nodelay(monkeypatch):
    """Reproduce asyncio's skipped setting on a TCP socket reporting proto=0."""
    seen = []
    monkeypatch.setattr(
        H11Protocol, "connection_made", lambda self, transport: seen.append(transport)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0) as sock:
        assert sock.proto == 0
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 0
        transport = SimpleNamespace(get_extra_info=lambda name: sock)
        protocol = object.__new__(NoDelayH11Protocol)
        protocol.connection_made(transport)
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
        assert seen == [transport]


def test_missing_transport_socket_still_calls_base(monkeypatch):
    seen = []
    monkeypatch.setattr(
        H11Protocol, "connection_made", lambda self, transport: seen.append(transport)
    )
    transport = SimpleNamespace(get_extra_info=lambda name: None)
    object.__new__(NoDelayH11Protocol).connection_made(transport)
    assert seen == [transport]


def test_non_tcp_transport_is_not_given_tcp_options(monkeypatch):
    monkeypatch.setattr(H11Protocol, "connection_made", lambda *args: None)
    sock = SimpleNamespace(family=socket.AF_UNSPEC)
    transport = SimpleNamespace(get_extra_info=lambda name: sock)
    object.__new__(NoDelayH11Protocol).connection_made(transport)

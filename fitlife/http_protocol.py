"""Explicit TCP_NODELAY for HTTP sockets, including inherited worker sockets."""

import socket

from uvicorn.protocols.http.h11_impl import H11Protocol


class NoDelayH11Protocol(H11Protocol):
    def connection_made(self, transport):
        sock = transport.get_extra_info("socket")
        if sock is not None and sock.family in {socket.AF_INET, socket.AF_INET6}:
            # Python 3.12 asyncio skips its automatic setting when an inherited
            # Uvicorn socket has proto=0. Apply to the accepted TCP socket directly.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().connection_made(transport)

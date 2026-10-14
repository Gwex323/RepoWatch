"""Trusted, bounded TCP forwarder copied as code into a separate preview container."""

SCRIPT = r"""
import select
import socket
import socketserver
import sys
import threading

slots = threading.BoundedSemaphore(32)
target = (sys.argv[1], int(sys.argv[2]))

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not slots.acquire(blocking=False):
            return
        try:
            with socket.create_connection(target, timeout=5) as upstream:
                self.request.settimeout(5)
                upstream.settimeout(5)
                sockets = (self.request, upstream)
                while True:
                    ready, _, _ = select.select(sockets, [], [], 60)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        other = upstream if source is self.request else self.request
                        other.sendall(data)
        except OSError:
            pass
        finally:
            slots.release()

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def handle_error(self, request, address):
        pass

Server(("0.0.0.0", 8000), Handler).serve_forever()
"""

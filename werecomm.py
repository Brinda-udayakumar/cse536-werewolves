"""
werecomm.py - Communications library for Werewolves

Primitive: UNIX domain stream sockets (socket.AF_UNIX, SOCK_STREAM),
not named pipes. Each connection runs a dedicated background reader
thread that pulls framed messages off the wire and drops them into a
per-connection queue, so the rest of the game code can do simple
blocking recv() calls without worrying about partial reads or TCP/UNIX
stream framing itself.

Wire format (per message):
    4 bytes  -> big-endian length of the JSON payload that follows
    N bytes  -> UTF-8 JSON payload: {"type": str, "payload": any,
                                      "sender": str}

Public API
----------
Server (moderator side):
    Server(socket_path)
        .start()                                  bind + listen, spawn accept thread
        .wait_for_players(n, timeout=None) -> list[str]
                                                   block until n clients have
                                                   completed the handshake
        .send(player_name, msg_type, payload)      unicast to one player
        .broadcast(msg_type, payload, exclude=())  send to every connected player
        .recv(player_name, timeout=None) -> (msg_type, payload)
                                                   blocking read from one
                                                   player's inbox
        .recv_any(player_names, timeout=None) -> (player_name, msg_type, payload)
                                                   blocking read from the
                                                   first of several players
                                                   to have something waiting
        .close()                                  shut everything down

Client (player side):
    Client(socket_path, name)
        .connect()
        .send(msg_type, payload)
        .recv(timeout=None) -> (msg_type, payload)
        .close()

Both Server.send/broadcast and Client.send accept any JSON-serializable
payload (dicts, lists, strings, numbers). msg_type is a short string tag
the game logic uses to route messages (e.g. "role", "prompt", "chat",
"vote", "info").
"""

import socket
import struct
import threading
import queue
import json
import os


# ---------------------------------------------------------------- framing --

def _send_framed(sock, obj):
    data = json.dumps(obj).encode("utf-8")
    header = struct.pack(">I", len(data))
    sock.sendall(header + data)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_framed(sock):
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    data = _recv_exact(sock, length)
    if data is None:
        return None
    return json.loads(data.decode("utf-8"))


# -------------------------------------------------------------- Connection --

class _Connection:
    """One live socket + its background reader thread + inbox queue."""

    def __init__(self, sock, name):
        self.sock = sock
        self.name = name
        self.inbox = queue.Queue()
        self.alive = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def _reader_loop(self):
        while self.alive:
            try:
                msg = _recv_framed(self.sock)
            except OSError:
                msg = None
            if msg is None:
                self.alive = False
                self.inbox.put(("__disconnect__", None, self.name))
                return
            self.inbox.put((msg.get("type"), msg.get("payload"), msg.get("sender")))

    def send(self, msg_type, payload, sender):
        with self._lock:
            _send_framed(self.sock, {"type": msg_type, "payload": payload, "sender": sender})

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ Server --

class Server:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self._listener = None
        self._accept_thread = None
        self.connections = {}          # name -> _Connection
        self._connections_lock = threading.Lock()
        self._running = False

    def start(self):
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(16)
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, _ = self._listener.accept()
            except OSError:
                return
            # Handshake: first frame the client sends must be its name.
            try:
                hello = _recv_framed(client_sock)
            except OSError:
                client_sock.close()
                continue
            if not hello or hello.get("type") != "hello":
                client_sock.close()
                continue
            name = hello["payload"]
            conn = _Connection(client_sock, name)
            with self._connections_lock:
                self.connections[name] = conn

    def wait_for_players(self, n, timeout=None):
        """Block until n distinct players have connected. Returns their names."""
        import time
        start = time.time()
        while True:
            with self._connections_lock:
                names = list(self.connections.keys())
            if len(names) >= n:
                return names
            if timeout is not None and time.time() - start > timeout:
                return names
            time.sleep(0.1)

    def send(self, player_name, msg_type, payload):
        conn = self.connections.get(player_name)
        if conn and conn.alive:
            conn.send(msg_type, payload, sender="moderator")

    def broadcast(self, msg_type, payload, exclude=()):
        with self._connections_lock:
            targets = [c for n, c in self.connections.items() if n not in exclude]
        for conn in targets:
            if conn.alive:
                conn.send(msg_type, payload, sender="moderator")

    def recv(self, player_name, timeout=None):
        conn = self.connections.get(player_name)
        if conn is None:
            return (None, None)
        try:
            msg_type, payload, _sender = conn.inbox.get(timeout=timeout)
        except queue.Empty:
            return (None, None)
        return (msg_type, payload)

    def recv_any(self, player_names, timeout=None):
        """Poll several players' inboxes; return the first message that shows up."""
        import time
        start = time.time()
        while True:
            for name in player_names:
                conn = self.connections.get(name)
                if conn is None:
                    continue
                try:
                    msg_type, payload, _sender = conn.inbox.get_nowait()
                    return (name, msg_type, payload)
                except queue.Empty:
                    continue
            if timeout is not None and time.time() - start > timeout:
                return (None, None, None)
            time.sleep(0.05)

    def close(self):
        self._running = False
        with self._connections_lock:
            for conn in self.connections.values():
                conn.close()
        if self._listener:
            self._listener.close()
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)


# ------------------------------------------------------------------ Client --

class Client:
    def __init__(self, socket_path, name):
        self.socket_path = socket_path
        self.name = name
        self.sock = None
        self._conn = None

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)
        _send_framed(self.sock, {"type": "hello", "payload": self.name, "sender": self.name})
        self._conn = _Connection(self.sock, self.name)

    def send(self, msg_type, payload):
        _send_framed(self.sock, {"type": msg_type, "payload": payload, "sender": self.name})

    def recv(self, timeout=None):
        try:
            msg_type, payload, _sender = self._conn.inbox.get(timeout=timeout)
        except queue.Empty:
            return (None, None)
        return (msg_type, payload)

    def close(self):
        if self._conn:
            self._conn.close()

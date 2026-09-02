"""
lg_soundbar.py

Client for LG's local soundbar control protocol.

LG's newer soundbars (SK/SN/S-series, including the S80TR) expose a local
TCP service on port 9741 -- the same channel the LG ThinQ app uses when
it's on the same network. Local control plane, not a cloud API.

Wire format:
    - Plain TCP socket to <soundbar-ip>:9741
    - Every message (both directions) is JSON, AES-128-CBC encrypted with a
      fixed key/IV shared by every soundbar of this generation
    - Each encrypted frame has a small header:
          [0x10][0x00 0x00 0x00][1-byte length][ciphertext...]
    - Requests: {"cmd": "get"|"set", "data": {...}, "msg": "<TOPIC>"}
    - Responses arrive asynchronously on the same socket, so a background
      thread listens continuously and calls back into your code.
"""

import json
import socket
import struct
import threading
import time

from Crypto.Cipher import AES

# Fixed key/IV used by this generation of LG soundbars for local control.
_AES_KEY = b"T^&*J%^7tr~4^%^&I(o%^!jIJ__+a0 k"
_AES_IV = b"'%^Ur7gy$~t+f)%@"

# Topic names ("msg" field) used by get/set requests.
MSG_SETTINGS = "SETTING_VIEW_INFO"   # woofer/rear/top/center levels, night mode, etc.
MSG_SPK_LIST = "SPK_LIST_VIEW_INFO"  # master volume + mute
MSG_EQ = "EQ_VIEW_INFO"              # sound mode / equalizer
MSG_FUNC = "FUNC_VIEW_INFO"          # input source
MSG_PRODUCT_INFO = "PRODUCT_INFO"


class LGSoundbar:
    """A persistent connection to one LG soundbar."""

    def __init__(self, host, port=9741, on_update=None, timeout=5):
        """
        host:      IP address of the soundbar on your LAN
        on_update: optional callback(dict) fired whenever the soundbar
                   pushes a status update (in response to a command, or
                   spontaneously e.g. someone changed volume on the remote)
        """
        self.host = host
        self.port = port
        self.on_update = on_update
        self.timeout = timeout
        self._sock = None
        self._lock = threading.Lock()
        self._latest = {}  # last known state, keyed by msg topic
        self._connect()
        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener_thread.start()

    # ---------- low-level plumbing ----------

    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect((self.host, self.port))
        sock.settimeout(None)
        self._sock = sock

    def _encrypt(self, payload: dict) -> bytes:
        raw = json.dumps(payload).encode("utf-8")
        pad_len = 16 - (len(raw) % 16)
        raw += bytes([pad_len]) * pad_len
        cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
        body = cipher.encrypt(raw)
        header = bytes([0x10, 0x00, 0x00, 0x00, len(body)])
        return header + body

    def _decrypt(self, body: bytes) -> dict:
        cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
        raw = cipher.decrypt(body)
        pad_len = raw[-1]
        raw = raw[:-pad_len]
        return json.loads(raw.decode("utf-8"))

    def _send(self, payload: dict):
        frame = self._encrypt(payload)
        with self._lock:
            try:
                self._sock.sendall(frame)
            except OSError:
                self._connect()
                self._sock.sendall(frame)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("soundbar closed the connection")
            buf += chunk
        return buf

    def _listen_loop(self):
        while True:
            try:
                tag = self._recv_exact(1)
                if tag[0] != 0x10:
                    continue
                length_bytes = self._recv_exact(4)
                length = int.from_bytes(length_bytes, "big")
                body = self._recv_exact(length)
                msg = self._decrypt(body)
                topic = msg.get("msg")
                if topic:
                    # merge, don't replace -- a "set" confirmation often
                    # only carries the one field that changed
                    self._latest.setdefault(topic, {})
                    self._latest[topic].update(msg.get("data", {}))
                if self.on_update:
                    self.on_update(msg)
            except (ConnectionError, OSError):
                time.sleep(1)
                try:
                    self._connect()
                except OSError:
                    time.sleep(2)
            except Exception as e:
                print(f"[lg_soundbar] error parsing a frame: {e!r}")

    def latest(self, topic):
        """Last known data dict for a topic (may be stale / empty until a
        get_* call has round-tripped at least once)."""
        return self._latest.get(topic, {})

    # ---------- getters ----------

    def refresh_all(self):
        """Ask the soundbar to (re)send every piece of state we care about."""
        self._send({"cmd": "get", "msg": MSG_SPK_LIST})
        self._send({"cmd": "get", "msg": MSG_SETTINGS})
        self._send({"cmd": "get", "msg": MSG_EQ})
        self._send({"cmd": "get", "msg": MSG_FUNC})

    # ---------- setters: the channel-level controls ----------

    def set_master_volume(self, value: int):
        """Overall/front volume, matches the main volume in ThinQ."""
        self._send({"cmd": "set", "data": {"i_vol": value}, "msg": MSG_SPK_LIST})

    def set_mute(self, enable: bool):
        self._send({"cmd": "set", "data": {"b_mute": enable}, "msg": MSG_SPK_LIST})

    def set_subwoofer_level(self, value: int):
        """Woofer/subwoofer trim, roughly -15..+6 depending on model."""
        self._send({"cmd": "set", "data": {"i_woofer_level": value}, "msg": MSG_SETTINGS})

    def set_rear_level(self, value: int):
        """Rear surround speaker trim."""
        self._send({"cmd": "set", "data": {"i_rear_level": value}, "msg": MSG_SETTINGS})

    def set_rear_enabled(self, enable: bool):
        """Turn the wireless rear speakers on/off entirely."""
        self._send({"cmd": "set", "data": {"b_rear": enable}, "msg": MSG_SETTINGS})

    def set_center_level(self, value: int):
        """Center channel trim (dialogue clarity)."""
        self._send({"cmd": "set", "data": {"i_center_level": value}, "msg": MSG_SETTINGS})

    def set_top_level(self, value: int):
        """Height/top (Atmos) speaker trim."""
        self._send({"cmd": "set", "data": {"i_top_level": value}, "msg": MSG_SETTINGS})

    def set_dialog_level(self, value: int):
        """Dialogue enhancer level (a separate boost on top of center)."""
        self._send({"cmd": "set", "data": {"i_dialog_level": value}, "msg": MSG_SETTINGS})
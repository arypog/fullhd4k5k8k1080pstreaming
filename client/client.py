import json
import socket
import subprocess
import threading
import time
import uuid
import random
import string
import tomllib


class MPVPlayer:
    """Class to manage an MPV subprocess with IPC enabled."""

    def __init__(self, url: str):
        """
        Initialize MPV player.

        Args:
            url (str): Video URL.
        """
        self.url = url
        self.socket_path = self._random_socket_name()
        self.proc = self._start_mpv_subprocess()

    @staticmethod
    def _random_socket_name():
        """Generate a random IPC socket path."""
        rand_str = "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        return f"/tmp/mpv-socket-{rand_str}"

    def _start_mpv_subprocess(self):
        """Start the MPV subprocess with the specified IPC socket."""
        cmd = [
            "mpv",
            self.url,
            f"--input-ipc-server={self.socket_path}",
            "--idle=yes",
            "--pause",
            "--force-seekable=yes",
        ]
        return subprocess.Popen(cmd)

    def stop(self):
        """Terminate the MPV subprocess."""
        if self.proc:
            self.proc.terminate()
            self.proc.wait()


class SyncClient:
    """Client to synchronize MPV playback with a remote server."""

    def __init__(
        self, mpv_socket_path: str, server_host: str, server_port: int, debug: bool
    ):
        """
        Initialize the synchronization client.

        Args:
            mpv_socket_path (str): Path to MPV IPC socket.
            server_host (str): Sync server IP.
            server_port (int): Sync server port.
            debug (bool): Enable debug logging.
        """
        self.client_id = str(uuid.uuid4())
        self.mpv_socket_path = mpv_socket_path
        self.server_host = server_host
        self.server_port = server_port
        self.debug = debug

        self.mpv_sock = None
        self.server_sock = None

    def _log(self, msg: str):
        """Print debug messages if debug is enabled."""
        if self.debug:
            print(msg)

    def connect(self):
        """Connect to MPV IPC socket and the sync server."""
        # Connect to MPV IPC
        self.mpv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 5
        while True:
            try:
                self.mpv_sock.connect(self.mpv_socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.time() > deadline:
                    raise TimeoutError(
                        f"Could not connect to mpv IPC socket: {self.mpv_socket_path}"
                    )
                time.sleep(0.1)
        self._log(f"[CONNECTED] MPV at {self.mpv_socket_path}")

        # Connect to sync server
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.connect((self.server_host, self.server_port))
        self._log(f"[CONNECTED] Server at {self.server_host}:{self.server_port}")

    def run(self):
        """Start observing MPV events and listening to server messages."""
        threading.Thread(target=self._observe_mpv, daemon=True).start()
        threading.Thread(target=self._listen_server, daemon=True).start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self._log("[EXIT] Shutting down")

    def _send_to_mpv(self, command: list):
        """Send a command to the MPV IPC socket."""
        msg = {"command": command}
        self.mpv_sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def _send_to_server(self, event: dict):
        """Send an event to the sync server."""
        event["source"] = self.client_id
        self.server_sock.sendall((json.dumps(event) + "\n").encode("utf-8"))

    def _observe_mpv(self):
        """Observe MPV properties (pause, time-pos) and send updates to server."""
        self._send_to_mpv(["observe_property", 1, "pause"])
        self._send_to_mpv(["observe_property", 2, "time-pos"])

        buffer = ""
        while True:
            data = self.mpv_sock.recv(4096)
            if not data:
                break
            buffer += data.decode("utf-8")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue

                msg = json.loads(line)
                if msg.get("event") == "property-change":
                    name = msg.get("name")
                    value = msg.get("data")

                    if name == "pause":
                        self._log(f"[MPV] Pause: {value}")
                        self._send_to_server({"event": "pause", "paused": value})

                    elif name == "time-pos":
                        if value is not None:
                            self._log(f"[MPV] Time: {value:.2f}")
                            self._send_to_server({"event": "time", "raw_time": value})
                        else:
                            self._log("[MPV] Time: None (no playback yet)")

                elif msg.get("event") == "seek":
                    self._log("[MPV] Seek detected")

    def _listen_server(self):
        """Listen for events from the sync server and apply them to MPV."""
        buffer = ""
        while True:
            data = self.server_sock.recv(4096)
            if not data:
                break
            buffer += data.decode("utf-8")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue

                msg = json.loads(line)
                if msg.get("source") == self.client_id:
                    continue

                event = msg.get("event")
                if event == "pause":
                    self._log(f"[SYNC] Apply pause={msg['paused']}")
                    self._send_to_mpv(["set_property", "pause", msg["paused"]])
                elif event in ("seek", "time"):
                    self._log(f"[SYNC] Apply time={msg['raw_time']:.2f}")
                    self._send_to_mpv(["set_property", "time-pos", msg["raw_time"]])


def main():
    """Load configuration, start MPV player and sync client."""
    with open("config.toml", "rb") as f:
        config = tomllib.load(f)

    server_host = config["syncserver"]["host"]
    server_port = config["syncserver"]["port"]
    debug = config["syncserver"].get("debug", False)
    url = config["mpv"]["url"]

    player = MPVPlayer(url)
    if debug:
        print(f"[INFO] mpv started with IPC socket {player.socket_path}")

    client = SyncClient(player.socket_path, server_host, server_port, debug)
    client.connect()
    client.run()


if __name__ == "__main__":
    main()

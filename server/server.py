from flask import Flask, request, send_file, Response
import os
import socket
import threading
import json
import functools
import tomllib
from typing import Any, Dict
import tkinter as tk
from tkinter import filedialog


class SyncServer:
    """Server to synchronize playback among multiple clients."""

    def __init__(self, time_tolerance: float, debug: bool = False):
        self.time_tolerance = time_tolerance
        self.debug = debug
        self.clients: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.current_pause = False
        self.current_time = 0.0

    def _log(self, msg: str):
        """Print debug messages if debug mode is enabled."""
        if self.debug:
            print(msg)

    def _handle_errors(self, func):
        """Decorator to catch and log exceptions in threaded functions."""

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                print(f"[ERROR] Exception in {func.__name__}: {e}")

        return wrapper

    def broadcast_to_others(self, message: Dict[str, Any], sender_conn: socket.socket):
        """Broadcast a message to all connected clients except the sender."""
        raw = (json.dumps(message) + "\n").encode("utf-8")
        with self.lock:
            for client in self.clients.copy():
                if client != sender_conn:
                    try:
                        client.sendall(raw)
                    except Exception as e:
                        self._log(f"[ERROR] Failed to send to client: {e}")

    def start(self, host: str, port: int):
        """Start the sync server and accept client connections."""
        print(f"[SYNC SERVER] Listening on {host}:{port}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind((host, port))
            server.listen()
            while True:
                conn, addr = server.accept()
                threading.Thread(
                    target=self._handle_errors(self.handle_client),
                    args=(conn, addr),
                    daemon=True,
                ).start()

    def handle_client(self, conn: socket.socket, addr: Any):
        """Handle messages from a single client."""
        print(f"[CONNECT] {addr} connected")
        with self.lock:
            self.clients.add(conn)

        buffer = ""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue

                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        self._log(f"[WARN] Invalid JSON: {line}")
                        continue

                    self._handle_message(msg, conn)
        finally:
            print(f"[DISCONNECT] {addr}")
            with self.lock:
                self.clients.discard(conn)
            conn.close()

    def _handle_message(self, msg: Dict[str, Any], sender_conn: socket.socket):
        """Process a message from a client and broadcast updates if necessary."""
        event = msg.get("event")
        if event == "pause":
            new_pause = msg.get("paused")
            if new_pause != self.current_pause:
                self.current_pause = new_pause
                self._log(f"[UPDATE] Pause: {new_pause}")
                self.broadcast_to_others(msg, sender_conn)

        elif event in ("seek", "time"):
            new_time = msg.get("raw_time")
            if new_time is None:
                return
            drift = abs(new_time - self.current_time)
            if drift > self.time_tolerance:
                self.current_time = new_time
                self._log(f"[UPDATE] Time change: {new_time:.2f}s (drift={drift:.2f}s)")
                self.broadcast_to_others(msg, sender_conn)
            else:
                self._log(f"[SKIP] Drift {drift:.2f}s within tolerance")


class MkvServer:
    """HTTP server to serve MKV video files with range requests."""

    def __init__(self, path: str):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"MKV file not found: {path}")
        self.mkv_path = path
        self.app = Flask(__name__)
        self.app.route("/video")(self.serve_video)

    def serve_video(self):
        """Serve the MKV file, supporting HTTP range requests."""
        try:
            file_size = os.path.getsize(self.mkv_path)
            range_header = request.headers.get("Range")

            if range_header:
                start, end = self._parse_range(range_header, file_size)
                length = end - start + 1
                with open(self.mkv_path, "rb") as f:
                    f.seek(start)
                    data = f.read(length)

                resp = Response(data, mimetype="video/x-matroska", status=206)
                resp.headers.update(
                    {
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Length": str(length),
                    }
                )
                return resp
            else:
                return send_file(self.mkv_path, mimetype="video/x-matroska")

        except FileNotFoundError:
            return Response("File not found", status=404)
        except Exception as e:
            return Response(f"An error occurred: {e}", status=500)

    @staticmethod
    def _parse_range(range_header: str, file_size: int):
        """Parse a Range header into start and end bytes."""
        byte_range = range_header.replace("bytes=", "").split("-")
        start = int(byte_range[0])
        end = int(byte_range[1]) if byte_range[1] else file_size - 1
        if not (0 <= start <= end < file_size):
            raise ValueError("Requested Range Not Satisfiable")
        return start, end


def select_file() -> str:
    """Open a Tkinter file dialog to select an MKV file."""
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(
        title="Select MKV file",
        filetypes=[("MKV files", "*.mkv"), ("All files", "*.*")],
    )
    if not file_path:
        raise ValueError("No file selected")
    return file_path


def main():
    """Load config and start MKV and sync servers."""
    with open("config.toml", "rb") as f:
        config = tomllib.load(f)

    host = config["server"].get("host", "0.0.0.0")
    port = config["server"].get("port", 8000)
    tolerance = config["timesync"].get("tolerance", 2.5)
    debug = config["server"].get("debug", False)

    file_path = config["server"].get("file_path")
    if not file_path or not os.path.isfile(file_path):
        print("[INFO] No valid file in config. Opening file dialog...")
        file_path = select_file()

    mkv_server = MkvServer(path=file_path)
    sync_server = SyncServer(time_tolerance=tolerance, debug=debug)

    def run_mkv():
        mkv_server.app.run(debug=False, host=host, port=port, threaded=True)

    def run_sync():
        sync_server.start(host=host, port=port + 1)

    try:
        threading.Thread(target=run_mkv, daemon=True).start()
        threading.Thread(target=run_sync, daemon=True).start()

        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")


if __name__ == "__main__":
    main()

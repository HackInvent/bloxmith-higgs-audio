"""Disposable official-shape HTTP fixture. It emits synthetic tones, never model speech."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import math
from pathlib import Path
import select
import socket
import ssl
import struct
import subprocess
import threading
import time
import wave

SECRET = "synthetic-higgs-test-token-do-not-log"


def wav_bytes(frames=6000, rate=24000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", int(10000*math.sin(index/30))) for index in range(frames)))
    return output.getvalue()


def wait(predicate, timeout=8):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("Fixture observation timed out")


@contextmanager
def provider(root, tls=False):
    state = {"requests": [], "closed": [], "stop": threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                text = body["input"]
                state["requests"].append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
                if text.startswith("fixture:hold"):
                    self.connection.settimeout(.1)
                    while not state["stop"].is_set():
                        try:
                            if self.connection.recv(1) == b"":
                                state["closed"].append(text); return
                        except (TimeoutError, socket.timeout):
                            continue
                    return
                body_bytes = wav_bytes()
                code = {"fixture:auth":401,"fixture:busy":429,"fixture:server":500,"fixture:redirect":307}.get(text,200)
                if code != 200:
                    self.send_response(code)
                    self.send_header("Location", state["url"]+"/do-not-follow")
                    self.send_header("Content-Length", str(len(SECRET)))
                    self.end_headers(); self.wfile.write(SECRET.encode()); return
                if text == "fixture:wrong-rate": body_bytes = wav_bytes(rate=16000)
                if text == "fixture:duration": body_bytes = wav_bytes(frames=48001)
                if text == "fixture:extra": body_bytes += b"extra"
                if text == "fixture:bad-wav": body_bytes = b"INVALID AUDIO"
                length = len(body_bytes)
                if text == "fixture:truncated": body_bytes = body_bytes[:-40]
                self.send_response(200)
                if text != "fixture:no-finish":
                    self.send_header("X-Finish-Reason", "length" if text == "fixture:length" else "unknown" if text == "fixture:unknown" else "stop")
                if text == "fixture:duplicate": self.send_header("X-Finish-Reason", "stop")
                self.send_header("Content-Type", "application/json" if text == "fixture:mime" else "audio/wav")
                self.send_header("X-Request-Id", SECRET)  # Must not be reflected into workflow state.
                if text == "fixture:compressed": self.send_header("Content-Encoding", "gzip")
                if text == "fixture:chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                elif text != "fixture:no-length":
                    self.send_header("Content-Length", str(999999999 if text == "fixture:oversize" else length))
                self.send_header("Connection", "close"); self.end_headers()
                if text == "fixture:chunked":
                    for index in range(0,len(body_bytes),101):
                        chunk = body_bytes[index:index+101]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode()+chunk+b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(body_bytes)
            except (BrokenPipeError, ConnectionError, OSError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    server.daemon_threads = True
    cert = None
    if tls:
        root = Path(root); cert=root/"test-ca.pem"; key=root/"test-key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
            "-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",str(key),"-out",str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    state["url"] = ("https" if tls else "http")+"://127.0.0.1:"+str(server.server_port)
    state["ca"] = str(cert) if cert else ""
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield state
    finally:
        state["stop"].set(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def settings(state):
    return {"enabled":True,"base_url":state["url"],"api_key_ref":"higgs_fixture","ca_bundle":state["ca"],
        "format":"ogg_opus","timeout_sec":15,"max_pending":1,"max_audio_sec":2}


def check_file(metadata):
    path=Path(metadata["path"]); data=path.read_bytes()
    assert len(data)==metadata["bytes"] and hashlib.sha256(data).hexdigest()==metadata["sha256"]
    assert metadata["complete"] is True and metadata["finish_reason"]=="stop"
    assert metadata["server_revision_verified"] is False and SECRET not in str(metadata)
    probe=json.loads(subprocess.check_output(["ffprobe","-v","error","-show_streams","-of","json",str(path)]))
    audio=probe["streams"][0]
    assert audio["channels"]==1
    assert audio["codec_name"]==("opus" if metadata["format"]=="ogg_opus" else "pcm_s16le")
    assert int(audio["sample_rate"])==metadata["sample_rate"]
    return path

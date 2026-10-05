"""Owned killable process supervision. No model imports or framework internals."""
from contextlib import suppress
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

ERRORS = {
    "platform": "Higgs TTS 3 currently requires Linux for owned-process supervision.",
    "input": "Send bounded valid text or an explicit interrupt command.",
    "config": "Check the Higgs server settings and limits.",
    "disabled": "Enable access to your configured Higgs server before synthesis.",
    "credentials": "Unlock the wallet and provide a valid server token reference.",
    "dependency": "FFmpeg is required for Ogg/Opus output.",
    "provider_failed": "The speech request failed. Check your server; no automatic retry was made.",
    "authentication": "The configured speech server rejected authentication.",
    "unavailable": "The speech server is unavailable or busy; this request was not retried.",
    "protocol": "The server response does not match the required complete-speech contract.",
    "audio_limit": "Audio or generation limits were reached; no partial file was published.",
    "audio_invalid": "The returned WAV is empty, incomplete or incompatible with Higgs 24 kHz mono PCM.",
    "process_output": "An owned process exceeded its bounded diagnostic output.",
    "timeout": "The request deadline expired; remote cancellation is not confirmed.",
    "cancelled": "Local request cancelled; no partial file was published. Remote cancellation is not confirmed.",
    "storage": "Check the block storage and output directories.",
    "queue_full": "The local pending queue is full; this request was not accepted.",
    "listener": "The synthesis listener is unavailable; Stop and reload the Run.",
}

class HiggsError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "provider_failed"
        super().__init__(ERRORS[self.code])

class Cancelled(HiggsError):
    def __init__(self):
        super().__init__("cancelled")

def check(deadline, cancel):
    if cancel():
        raise Cancelled()
    if time.monotonic() >= deadline:
        raise HiggsError("timeout")

def run_process(args, *, directory, deadline, cancel, limit, stdin=None):
    """Bound every pipe; fence children on cancellation and abrupt framework-host death."""
    check(deadline, cancel)
    if sys.platform != "linux":
        raise HiggsError("platform")
    envelope = stdin or b""
    if len(envelope) > 32768:
        raise HiggsError("input")
    environment = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL"}}
    environment.update(DO_NOT_TRACK="1", PYTHONDONTWRITEBYTECODE="1",
        XDG_CACHE_HOME=str(directory / "cache"), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    command = [sys.executable, "-E", "-B", str(Path(__file__).with_name("process_guard.py")), str(os.getpid()), *args]
    process = subprocess.Popen(command, cwd=directory, env=environment, start_new_session=True,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
    selector, output, errors, offset = selectors.DefaultSelector(), bytearray(), bytearray(), 0
    try:
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            if envelope:
                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
            else:
                process.stdin.close()
        for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            check(deadline, cancel)
            for key, _ in selector.select(timeout=0.03):
                if key.data == "in":
                    try:
                        offset += os.write(key.fd, envelope[offset:offset + 4096])
                    except BrokenPipeError:
                        offset = len(envelope)
                    if offset == len(envelope):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target, maximum = (output, limit) if key.data == "out" else (errors, 262144)
                if len(target) + len(chunk) > maximum:
                    raise HiggsError("process_output")
                target.extend(chunk)
        while process.poll() is None:
            check(deadline, cancel)
            time.sleep(0.02)
        check(deadline, cancel)
        if process.returncode:
            try:
                detail = json.loads(output).get("error_code")
            except (ValueError, AttributeError):
                detail = None
            raise HiggsError(detail if detail in ERRORS else "provider_failed")
        return bytes(output)
    finally:
        selector.close()
        # The guardian owns this group. Fence descendants even after normal exit.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

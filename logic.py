"""Bounded self-hosted synthesis; only complete validated audio may be published."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
from .process import HiggsError, Cancelled, check, run_process

DEFAULTS = {
    "enabled": False, "base_url": "", "api_key_ref": "", "ca_bundle": "",
    "model_id": "bosonai/higgs-tts-3-4b", "voice": "default",
    "output_directory": "", "format": "ogg_opus", "temperature": 0.8,
    "top_k": 50, "max_new_tokens": 2048, "max_audio_sec": 90,
    "max_text_chars": 2000, "timeout_sec": 120, "max_pending": 4,
}
CONTRACT_REVISION = "353cf3535f8a67a020facc80aef93eba9f8ed5bc"


def endpoint(value):
    """Accept one explicit server root; plain HTTP is limited to numeric loopback."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if parsed.query or parsed.fragment or "?" in value or "#" in value or parsed.port == 0:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9:/._\[\]-]+", value) or ".." in parsed.path or "//" in parsed.path:
            raise ValueError()
        if parsed.scheme == "http" and not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError()
        return value.rstrip("/") + "/v1/audio/speech"
    except (ValueError, TypeError, UnicodeError):
        raise HiggsError("config") from None


def configuration(raw):
    """Validate persisted settings without resolving secrets, opening files or networking."""
    if not isinstance(raw, Mapping) or set(raw)-set(DEFAULTS)-{"execution", "position", "runtime_path", "runtime_path_label"}:
        raise HiggsError("config")
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    if type(cfg["enabled"]) is not bool:
        raise HiggsError("config")
    for key in ("base_url", "api_key_ref", "ca_bundle", "model_id", "voice", "output_directory", "format"):
        value = cfg[key]
        try:
            if not isinstance(value, str) or len(value.encode("utf-8")) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise HiggsError("config")
        except UnicodeError:
            raise HiggsError("config") from None
    if cfg["base_url"]:
        endpoint(cfg["base_url"])
    elif cfg["enabled"]:
        raise HiggsError("config")
    secret_name = r"[A-Za-z0-9_.-]{1,128}"
    if cfg["api_key_ref"] and not re.fullmatch(rf"(?:{secret_name}|secret://workspace/{secret_name}|secret://project/{secret_name}/{secret_name})", cfg["api_key_ref"]):
        raise HiggsError("config")
    for key in ("model_id", "voice"):
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", cfg[key]):
            raise HiggsError("config")
    if cfg["format"] not in {"wav", "ogg_opus"}:
        raise HiggsError("config")
    for key, low, high in (("top_k", 1, 500), ("max_new_tokens", 16, 4096), ("max_audio_sec", 1, 180),
            ("max_text_chars", 1, 4000), ("timeout_sec", 1, 240), ("max_pending", 0, 16)):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise HiggsError("config")
    if type(cfg["temperature"]) not in (int, float) or not math.isfinite(cfg["temperature"]) or not .1 <= cfg["temperature"] <= 2:
        raise HiggsError("config")
    return cfg


def json_value(raw, limit):
    """Decode bounded JSON, refusing duplicate keys and non-finite constants."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise HiggsError("input")
            out[key] = value
        return out
    if not isinstance(raw, str):
        return raw
    try:
        if len(raw.encode("utf-8")) > limit:
            raise ValueError()
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise HiggsError("input") from None


def interrupt(raw):
    """Commands are explicit and never inferred from words to synthesize."""
    value = json_value(raw, 1024)
    if not isinstance(value, Mapping) or dict(value) != {"action": "interrupt"}:
        raise HiggsError("input")
    return {"action": "interrupt"}


def request(raw, cfg):
    """Accept plain speech or a strict correlated request supplied with JSON MIME."""
    correlation = uuid4().hex
    if isinstance(raw, Mapping):
        if set(raw) != {"text", "request_id"}:
            raise HiggsError("input")
        correlation, raw = raw["request_id"], raw["text"]
        if not isinstance(correlation, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", correlation):
            raise HiggsError("input")
    try:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > cfg["max_text_chars"] or len(raw.encode("utf-8")) > 8192 or "\0" in raw:
            raise HiggsError("input")
    except UnicodeError:
        raise HiggsError("input") from None
    return {"text": raw.strip(), "request_id": correlation}


def bearer_token(resolver, reference):
    """Read only the explicit optional wallet reference, once for this request."""
    if not reference:
        return ""
    try:
        canonical = reference if reference.startswith("secret://") else "secret://workspace/" + reference
        raw = resolver(canonical)
        if not isinstance(raw, str) or not re.fullmatch(r"[!-~]{1,4096}", raw):
            raise ValueError()
        return raw
    except Exception:
        raise HiggsError("credentials") from None


def resolve_path(root, value):
    """Resolve administrator-authored paths against the application root."""
    path = Path(value).expanduser()
    return (Path(root)/path).absolute() if not path.is_absolute() else path


@dataclass
class PreparedAudio:
    """Owned unpublished result; the listener alone decides whether it may escape."""
    owner: object
    path: Path
    output_directory: Path
    metadata: dict
    deadline: float

    def close(self):
        self.owner.cleanup()


def audio_info(path, cfg):
    """Validate the canonical PCM WAV emitted by the pinned official server encoder."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            size = os.fstat(source.fileno())
            if not stat.S_ISREG(size.st_mode) or not 44 < size.st_size <= 44 + 48000*cfg["max_audio_sec"]:
                raise ValueError()
            header = source.read(44)
            riff, length, wave, fmt, fmtlen, codec, channels, rate, byte_rate, align, bits, data, count = struct.unpack("<4sI4s4sIHHIIHH4sI", header)
            if (riff, wave, fmt, fmtlen, codec, channels, rate, byte_rate, align, bits, data) != (b"RIFF", b"WAVE", b"fmt ", 16, 1, 1, 24000, 48000, 2, 16, b"data"):
                raise ValueError()
            if length+8 != size.st_size or count+44 != size.st_size or count % 2:
                raise ValueError()
        return {"duration_sec": count/48000, "source_frames": count//2, "source_sample_rate": 24000, "channels": 1}
    except (OSError, ValueError, struct.error):
        raise HiggsError("audio_invalid") from None


def generate(item, cfg, root, storage, resolver, cancel=lambda: False):
    """Execute one cancellable request and codec job; do not publish or retry implicitly."""
    cfg = configuration(cfg); item = request(item, cfg)
    if not cfg["enabled"]:
        raise HiggsError("disabled")
    started = time.monotonic(); deadline = started + cfg["timeout_sec"]
    check(deadline, cancel)
    storage = Path(storage)
    if not storage.is_absolute() or storage.is_symlink() or not storage.is_dir():
        raise HiggsError("storage")
    target = resolve_path(root, cfg["output_directory"]) if cfg["output_directory"] else storage/"audio"
    if target.is_symlink() or target.exists() and not target.is_dir():
        raise HiggsError("storage")
    if cfg["format"] == "ogg_opus" and not shutil.which("ffmpeg"):
        raise HiggsError("dependency")
    ca = resolve_path(root, cfg["ca_bundle"]) if cfg["ca_bundle"] else None
    if ca is not None and (not ca.is_file() or ca.is_symlink() or ca.stat().st_size > 1048576):
        raise HiggsError("config")
    token = bearer_token(resolver, cfg["api_key_ref"])
    owner = tempfile.TemporaryDirectory(prefix="higgs-job-", dir=storage); job = Path(owner.name)
    try:
        envelope = {"url": endpoint(cfg["base_url"]), "token": token, "ca_bundle": str(ca) if ca else None,
            "timeout_sec": cfg["timeout_sec"], "max_bytes": 44 + 48000*cfg["max_audio_sec"],
            "payload": {"model": cfg["model_id"], "voice": cfg["voice"], "input": item["text"],
                "stream": False, "response_format": "wav", "speed": 1.0,
                "temperature": cfg["temperature"], "top_k": cfg["top_k"], "max_new_tokens": cfg["max_new_tokens"]}}
        raw = run_process([sys.executable, "-E", "-B", str(Path(__file__).with_name("worker.py"))], directory=job,
            deadline=deadline, cancel=cancel, limit=8192, stdin=json.dumps(envelope, ensure_ascii=True).encode())
        del envelope, token
        if json_value(raw.decode("utf-8"), 8192) != {"finish_reason": "stop"}:
            raise HiggsError("protocol")
        wav = job/"generated.wav"; info = audio_info(wav, cfg); output = wav
        if cfg["format"] == "ogg_opus":
            output = job/"generated.ogg"
            run_process(["ffmpeg", "-v", "error", "-nostdin", "-n", "-threads", "1", "-protocol_whitelist", "file,pipe",
                "-format_whitelist", "wav", "-i", str(wav), "-map_metadata", "-1", "-ac", "1", "-ar", "48000",
                "-c:a", "libopus", "-b:a", "96000", "-f", "ogg", str(output)], directory=job,
                deadline=deadline, cancel=cancel, limit=1024)
        check(deadline, cancel)
        info.update(request_id=item["request_id"], format=cfg["format"], complete=True, finish_reason="stop",
            sample_rate=48000 if cfg["format"] == "ogg_opus" else 24000, elapsed_sec=round(time.monotonic()-started, 4),
            engine="self_hosted_sglang_omni", requested_model=cfg["model_id"], server_revision_verified=False,
            adapter_contract_revision=CONTRACT_REVISION,
            settings={k: cfg[k] for k in ("voice", "temperature", "top_k", "max_new_tokens", "max_audio_sec")})
        return PreparedAudio(owner, output, target, info, deadline)
    except BaseException:
        owner.cleanup()
        raise


def publish(prepared, cancel=lambda: False):
    """Atomically expose a complete unique file after pending interrupts were drained."""
    check(prepared.deadline, cancel)
    target = prepared.output_directory
    if target.is_symlink():
        raise HiggsError("storage")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    suffix = ".ogg" if prepared.metadata["format"] == "ogg_opus" else ".wav"
    final = target/("higgs-" + uuid4().hex + suffix); temporary = None
    try:
        fd = os.open(prepared.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            size = os.fstat(source.fileno())
            if not stat.S_ISREG(size.st_mode) or not 0 < size.st_size <= 33554432:
                raise HiggsError("audio_invalid")
            descriptor, temporary = tempfile.mkstemp(prefix=".higgs-publish-", dir=target)
            digest = hashlib.sha256(); total = 0
            with os.fdopen(descriptor, "wb") as output:
                while chunk := source.read(65536):
                    check(prepared.deadline, cancel); total += len(chunk)
                    if total > 33554432:
                        raise HiggsError("audio_invalid")
                    output.write(chunk); digest.update(chunk)
                output.flush(); os.fsync(output.fileno())
            if total != size.st_size:
                raise HiggsError("audio_invalid")
        check(prepared.deadline, cancel)
        os.link(temporary, final, follow_symlinks=False)
        return {**prepared.metadata, "path": str(final), "bytes": total, "sha256": digest.hexdigest(),
            "content_type": "audio/ogg" if suffix == ".ogg" else "audio/wav"}
    except OSError:
        raise HiggsError("storage") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)

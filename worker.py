"""Killable single HTTP request; no redirects, proxies, ambient credentials or retries."""

import http.client
import json
import os
from pathlib import Path
import ssl
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


class Failure(ValueError):
    """A safe owned error code; provider response bodies never become diagnostics."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("protocol")


def one_header(headers, key, required=True):
    """Reject ambiguous duplicate response fields rather than picking one."""
    values = headers.get_all(key, [])
    if not values and not required:
        return None
    if len(values) != 1:
        raise Failure("protocol")
    return values[0].strip()


def main():
    """Fetch bounded canonical WAV into this job directory, then attest terminal success."""
    raw = sys.stdin.buffer.read(32769)
    if len(raw) > 32768:
        raise Failure("input")
    job = json.loads(raw)
    context = ssl.create_default_context(cafile=job["ca_bundle"])
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
    headers = {"Content-Type": "application/json", "Accept": "audio/wav", "Accept-Encoding": "identity",
        "User-Agent": "BloxSmith-Higgs-TTS/0.1.0"}
    if job["token"]:
        headers["Authorization"] = "Bearer " + job["token"]
    request = Request(job["url"], data=json.dumps(job["payload"], ensure_ascii=True).encode(), headers=headers, method="POST")
    try:
        with opener.open(request, timeout=job["timeout_sec"]) as response:
            if response.status != 200:
                raise Failure("protocol")
            reason = one_header(response.headers, "X-Finish-Reason")
            if reason == "length":
                raise Failure("audio_limit")
            if reason != "stop":
                raise Failure("protocol")
            if one_header(response.headers, "Content-Type").lower() != "audio/wav":
                raise Failure("protocol")
            if one_header(response.headers, "Content-Encoding", False) not in {None, "identity"}:
                raise Failure("protocol")
            length = one_header(response.headers, "Content-Length", False)
            transfer = one_header(response.headers, "Transfer-Encoding", False)
            if transfer not in {None, "chunked"} or transfer is not None and length is not None:
                raise Failure("protocol")
            if length is not None:
                if not length.isascii() or not length.isdigit() or len(length) > 10:
                    raise Failure("protocol")
                length = int(length)
                if length > job["max_bytes"]:
                    raise Failure("audio_limit")
            total = 0
            with open(Path.cwd()/"generated.wav", "xb") as output:
                os.chmod(output.name, 0o600)
                while chunk := response.read(min(65536, job["max_bytes"]+1-total)):
                    total += len(chunk)
                    if total > job["max_bytes"]:
                        raise Failure("audio_limit")
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            if length is not None and total != length:
                raise Failure("protocol")
    except HTTPError as error:
        code = error.code
        error.close()
        raise Failure("authentication" if code in {401, 403} else "unavailable" if code in {429, 502, 503, 504} else "provider_failed") from None
    except (http.client.HTTPException, UnicodeError):
        raise Failure("protocol") from None
    print(json.dumps({"finish_reason": "stop"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_code": str(error) if isinstance(error, Failure) else "provider_failed"}))
        raise SystemExit(1) from None

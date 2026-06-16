import gzip
import hmac
import json
import zlib
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


def _extract_token(request):
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return request.META.get("HTTP_X_SANTA_TOKEN", "").strip()


def _strip_nuls(value):
    """Recursively strip NUL bytes (\\u0000) from strings in a decoded JSON value.

    Why: Postgres' jsonb type rejects \\u0000 in string values, and CharField inserts
    fail the same way. Santa clients occasionally surface NULs in event fields
    (e.g. signing-chain certificate blobs that include binary padding).
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: _strip_nuls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nuls(v) for v in value]
    return value


def _decompress(body, encoding):
    """Decompress a request body per Content-Encoding.

    Santa's sync client sends compressed payloads with Content-Encoding either
    "deflate" (the new default) or "zlib" (the legacy Upvote-compatibility mode).
    Both carry a zlib-wrapped deflate stream. We also accept gzip.
    """
    if not body:
        return body
    enc = (encoding or "").lower().strip()
    if enc == "gzip":
        return gzip.decompress(body)
    if enc in ("deflate", "zlib"):
        try:
            return zlib.decompress(body)
        except zlib.error:
            # Some clients send raw deflate without the zlib wrapper.
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def json_endpoint(view):
    @csrf_exempt
    @require_POST
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        expected = settings.SANTA_SYNC_TOKEN
        if not expected:
            return JsonResponse(
                {"error": "server misconfigured: SANTA_SYNC_TOKEN not set"},
                status=500,
            )
        supplied = _extract_token(request)
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JsonResponse({"error": "unauthorized"}, status=401)
        raw = request.body or b""
        try:
            raw = _decompress(raw, request.META.get("HTTP_CONTENT_ENCODING", ""))
        except (OSError, zlib.error) as e:
            return JsonResponse({"error": f"decompression failed: {e}"}, status=400)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return JsonResponse({"error": "expected JSON object"}, status=400)
        return view(request, _strip_nuls(body), *args, **kwargs)

    return wrapper

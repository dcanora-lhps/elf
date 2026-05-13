import hmac
import json
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
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return JsonResponse({"error": "expected JSON object"}, status=400)
        return view(request, body, *args, **kwargs)

    return wrapper

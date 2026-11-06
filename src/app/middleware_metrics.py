"""Prometheus instrumentation using only the standard library.

Exposes ``http_request_duration_seconds`` (histogram) at ``/metrics``. State is
kept per process, which is enough because gunicorn runs a single worker; a
recycled worker (``max_requests``) restarts the counters and ``rate()`` absorbs
the reset.
"""
import logging
import threading
import time

from django.http import HttpResponse

logger = logging.getLogger(__name__)

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
)
EXCLUDED_PATHS = frozenset({"/metrics", "/health", "/health/"})
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
NAME = "http_request_duration_seconds"

_lock = threading.Lock()
# (method, route, status) -> [per-bucket counts (+Inf last), sum, count]
_series = {}


def reset():
    with _lock:
        _series.clear()


def observe(method, route, status, seconds):
    if method not in METHODS:
        method = "OTHER"
    key = (method, route, str(status))
    idx = len(BUCKETS)
    for i, upper in enumerate(BUCKETS):
        if seconds <= upper:
            idx = i
            break
    with _lock:
        entry = _series.get(key)
        if entry is None:
            entry = [[0] * (len(BUCKETS) + 1), 0.0, 0]
            _series[key] = entry
        entry[0][idx] += 1
        entry[1] += seconds
        entry[2] += 1


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics():
    with _lock:
        snapshot = [(k, list(v[0]), v[1], v[2]) for k, v in _series.items()]
    lines = [
        f"# HELP {NAME} HTTP request duration in seconds.",
        f"# TYPE {NAME} histogram",
    ]
    for (method, route, status), counts, total, count in sorted(snapshot):
        labels = (
            f'method="{_escape(method)}",route="{_escape(route)}",'
            f'status="{_escape(status)}"'
        )
        cumulative = 0
        for upper, n in zip(BUCKETS, counts, strict=False):
            cumulative += n
            lines.append(f'{NAME}_bucket{{{labels},le="{upper:g}"}} {cumulative}')
        lines.append(f'{NAME}_bucket{{{labels},le="+Inf"}} {count}')
        lines.append(f"{NAME}_sum{{{labels}}} {total!r}")
        lines.append(f"{NAME}_count{{{labels}}} {count}")
    return "\n".join(lines) + "\n"


class PrometheusMiddleware:
    """Serve /metrics and time every other request (first in MIDDLEWARE)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path == "/metrics":
            return HttpResponse(render_metrics(), content_type=CONTENT_TYPE)
        if path in EXCLUDED_PATHS:
            return self.get_response(request)

        start = time.perf_counter()
        try:
            response = self.get_response(request)
        except Exception:
            self._record(request, 500, start)
            raise
        self._record(request, response.status_code, start)
        return response

    @staticmethod
    def _record(request, status, start):
        try:
            match = getattr(request, "resolver_match", None)
            route = "/" + match.route if match and match.route else "unmatched"
            observe(request.method, route, status, time.perf_counter() - start)
        except Exception:
            logger.exception("failed to record request metrics")

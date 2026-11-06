import re

from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path

from app import middleware_metrics
from app.middleware_metrics import BUCKETS, render_metrics
from config.urls import urlpatterns as project_urlpatterns


@login_not_required
def boom(request):
    raise RuntimeError("boom")


@login_not_required
def ok(request, item_id):
    return HttpResponse("ok")


urlpatterns = [
    path("boom/", boom),
    path("item/<int:item_id>/", ok),
    *project_urlpatterns,
]


def series(text, name):
    return [ln for ln in text.splitlines() if ln.startswith(name)]


class MetricsTests(TestCase):
    def setUp(self):
        middleware_metrics.reset()

    def test_metrics_returns_prometheus_text(self):
        self.client.get("/medialist/anime")
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp["Content-Type"].startswith("text/plain; version=0.0.4"),
        )
        self.assertIn(
            "http_request_duration_seconds_bucket",
            resp.content.decode(),
        )

    def test_metrics_needs_no_login(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)

    def test_uses_route_template(self):
        self.client.get("/medialist/anime")
        text = render_metrics()
        self.assertIn('route="/medialist/<media_type:media_type>"', text)
        self.assertNotIn('route="/medialist/anime"', text)

    @override_settings(ROOT_URLCONF="app.tests.test_middleware_metrics")
    def test_parameter_value_never_in_route(self):
        self.client.get("/item/12345/")
        text = render_metrics()
        self.assertIn('route="/item/<int:item_id>/"', text)
        self.assertNotIn("12345", text)

    def test_unknown_paths_collapse_to_unmatched(self):
        for p in ("/wp-admin", "/.env", "/x/98765"):
            self.client.get(p)
        routes = set(re.findall(r'route="([^"]*)"', render_metrics()))
        self.assertEqual(routes, {"unmatched"})

    def test_unknown_method_collapses_to_other(self):
        self.client.generic("FOOBAR", "/nope")
        self.assertIn('method="OTHER"', render_metrics())

    @override_settings(ROOT_URLCONF="app.tests.test_middleware_metrics")
    def test_handler_error_keeps_response_and_counts_500(self):
        self.client.raise_request_exception = False
        resp = self.client.get("/boom/")
        self.assertEqual(resp.status_code, 500)
        text = render_metrics()
        self.assertIn('route="/boom/"', text)
        self.assertIn('status="500"', text)

    def test_excludes_metrics_and_health(self):
        self.client.get("/metrics")
        self.client.get("/health/")
        self.assertNotIn("http_request_duration_seconds_bucket{", render_metrics())

    def test_exposition_format_is_valid(self):
        for _ in range(3):
            self.client.get("/medialist/anime")
        self.client.get("/nope")
        text = render_metrics()
        self.assertIn("# HELP http_request_duration_seconds", text)
        self.assertIn("# TYPE http_request_duration_seconds histogram", text)
        les = [f'le="{b:g}"' for b in BUCKETS]
        self.assertEqual(
            les,
            ['le="0.01"', 'le="0.025"', 'le="0.05"', 'le="0.1"', 'le="0.25"',
             'le="0.5"', 'le="1"', 'le="2.5"', 'le="5"', 'le="10"'],
        )
        route = "/medialist/<media_type:media_type>"
        pre = f'method="GET",route="{route}",status="302"'
        buckets = [
            int(float(ln.rsplit(" ", 1)[1]))
            for ln in series(text, "http_request_duration_seconds_bucket")
            if pre in ln
        ]
        self.assertEqual(len(buckets), len(BUCKETS) + 1)
        self.assertEqual(buckets, sorted(buckets))
        count = [
            ln for ln in series(text, "http_request_duration_seconds_count")
            if pre in ln
        ]
        self.assertEqual(buckets[-1], int(count[0].rsplit(" ", 1)[1]))
        self.assertEqual(buckets[-1], 3)
        self.assertTrue(
            any(pre in ln and 'le="+Inf"' in ln for ln in series(text, "http_request_duration_seconds_bucket")),
        )
        self.assertTrue(series(text, "http_request_duration_seconds_sum"))


class DirectTests(SimpleTestCase):
    def setUp(self):
        middleware_metrics.reset()

    def test_exception_from_get_response_counts_500_and_reraises(self):
        def failing(request):
            raise RuntimeError("x")

        mw = middleware_metrics.PrometheusMiddleware(failing)
        with self.assertRaises(RuntimeError):
            mw(RequestFactory().get("/anything"))
        text = render_metrics()
        self.assertIn('route="unmatched"', text)
        self.assertIn('status="500"', text)


class EscapeTests(SimpleTestCase):
    def test_label_escaping(self):
        middleware_metrics.reset()
        middleware_metrics.observe('G"E\\T', 'a"b\\c\nd', 200, 0.01)
        text = render_metrics()
        self.assertIn(r'route="a\"b\\c\nd"', text)
        middleware_metrics.reset()

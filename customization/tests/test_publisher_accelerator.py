import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request

from release import publisher


class Response(io.BytesIO):
    status = 200
    headers = {}


class AssetAcceleratorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.payload = b"verified asset bytes"
        self.asset = {
            "name": "fixture.exe",
            "url": "https://api.github.com/repos/owner/repo/releases/assets/123",
            "browser_download_url": "https://github.com/owner/repo/releases/download/1.5.3/fixture.exe",
            "size": len(self.payload),
            "digest": "sha256:" + hashlib.sha256(self.payload).hexdigest(),
        }
        self.accelerators = publisher.accelerator_prefixes({
            "github_asset_accelerators": ["https://first.example/", "https://next.example"]
        })
        self.requests = []
        self.headers = {"Authorization": "Bearer gh-fixture"}

    def run_download(self, receive, *, prefer_accelerators=True, route_state=None):
        def open_request(req, *, timeout):
            self.requests.append((req, timeout))
            return receive(req)

        opener = mock.Mock()
        opener.open.side_effect = open_request
        with mock.patch.object(publisher.urllib.request, "build_opener", return_value=opener):
            return publisher.download(
                self.asset, self.cache, self.headers, self.accelerators, prefer_accelerators, route_state
            )

    def test_accelerator_first_default_downloads_via_mirror_without_auth_and_caches(self):
        result = self.run_download(lambda _: Response(self.payload))
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(
            [req.full_url for req, _ in self.requests],
            [self.accelerators[0] + self.asset["browser_download_url"]],
        )
        self.assertIsNone(self.requests[0][0].get_header("Authorization"))
        self.assertEqual(self.run_download(lambda _: self.fail("Cache must avoid the network")), result)
        self.assertEqual(len(self.requests), 1)

    def test_accelerator_first_falls_back_to_next_mirror_then_official_with_auth(self):
        def receive(req):
            if req.host == "first.example":
                return Response(b"x" * len(self.payload))
            if req.host == "next.example":
                raise error.URLError("Mirror down")
            return Response(self.payload)

        result = self.run_download(receive)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(
            [req.host for req, _ in self.requests],
            ["first.example", "next.example", "api.github.com"],
        )
        self.assertTrue(all(not req.get_header("Authorization") for req, _ in self.requests[:2]))
        self.assertEqual(self.requests[2][0].get_header("Authorization"), "Bearer gh-fixture")

    def test_route_state_promotes_healthy_mirror_for_subsequent_assets(self):
        route_state = {}

        def receive(req):
            if req.host == "first.example":
                raise error.URLError("Timeout")
            return Response(self.payload)

        first = self.run_download(receive, route_state=route_state)
        self.assertEqual(first.read_bytes(), self.payload)
        self.assertEqual([req.host for req, _ in self.requests], ["first.example", "next.example"])
        self.assertEqual(route_state["preferred"], self.accelerators[1])
        self.assertIn(self.accelerators[0], route_state["failed"])

        self.requests.clear()
        payload_two = b"second verified asset bytes"
        asset_two = {
            "name": "fixture.deb",
            "url": "https://api.github.com/repos/owner/repo/releases/assets/456",
            "browser_download_url": "https://github.com/owner/repo/releases/download/1.5.3/fixture.deb",
            "size": len(payload_two),
            "digest": "sha256:" + hashlib.sha256(payload_two).hexdigest(),
        }
        opener = mock.Mock()

        def open_second(req, *, timeout):
            self.requests.append((req, timeout))
            return Response(payload_two)

        opener.open.side_effect = open_second
        with mock.patch.object(publisher.urllib.request, "build_opener", return_value=opener):
            second = publisher.download(
                asset_two, self.cache, self.headers, self.accelerators, True, route_state
            )
        self.assertEqual(second.read_bytes(), payload_two)
        self.assertEqual([req.host for req, _ in self.requests], ["next.example"])

    def test_official_first_success_uses_verified_cache_without_a_mirror(self):
        result = self.run_download(lambda _: Response(self.payload), prefer_accelerators=False)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual([req.full_url for req, _ in self.requests], [self.asset["url"]])
        self.assertEqual(self.requests[0][0].get_header("Authorization"), "Bearer gh-fixture")
        self.assertEqual(
            self.run_download(lambda _: self.fail("Cache must avoid the network"), prefer_accelerators=False),
            result,
        )
        self.assertEqual(len(self.requests), 1)

    def test_official_first_slow_stream_switches_without_carrying_over_partial_bytes(self):
        clock = [0.0]

        class SlowResponse(Response):
            def read1(self, size):
                clock[0] += 9
                return super().read(1)

        def receive(req):
            if req.host == "api.github.com":
                return SlowResponse(self.payload)
            return Response(self.payload)

        with mock.patch.object(publisher.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.run_download(receive, prefer_accelerators=False)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(clock[0], 27.0)
        self.assertEqual(self.requests[1][0].full_url, self.accelerators[0] + self.asset["browser_download_url"])
        self.assertIsNone(self.requests[1][0].get_header("Authorization"))
        self.assertEqual(list(self.cache.iterdir()), [result])

    def test_official_first_http_failure_and_corrupt_first_accelerator_try_the_second(self):
        def receive(req):
            if req.host == "api.github.com":
                raise error.HTTPError(req.full_url, 503, "Service unavailable", {}, None)
            if req.host == "first.example":
                return Response(b"x" * len(self.payload))
            return Response(self.payload)

        result = self.run_download(receive, prefer_accelerators=False)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual([req.host for req, _ in self.requests], ["api.github.com", "first.example", "next.example"])
        self.assertTrue(all(not req.get_header("Authorization") for req, _ in self.requests[1:]))

    def test_failed_routes_leave_no_cache_entry(self):
        with self.assertRaisesRegex(RuntimeError, "Could not download and verify"):
            self.run_download(lambda req: Response(self.payload + b"x"))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_mirror_ranges_have_no_credentials(self):
        self.payload = b"x" * (publisher.RANGE_CHUNK_BYTES + 32)
        self.asset["size"] = len(self.payload)
        self.asset["digest"] = "sha256:" + hashlib.sha256(self.payload).hexdigest()

        def receive(req):
            if req.host == "api.github.com":
                raise error.URLError("Unavailable")
            span = req.get_header("Range").removeprefix("bytes=")
            start, end = map(int, span.split("-"))
            response = Response(self.payload[start:end + 1])
            response.status = 206
            response.headers = {"Content-Range": f"bytes {start}-{end}/{len(self.payload)}"}
            response.geturl = lambda: req.full_url
            return response

        result = self.run_download(receive, prefer_accelerators=False)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(all(req.host == "first.example" and not req.headers.get("Authorization")
                            for req, _ in self.requests[1:]))

    def test_default_accelerators_apply_when_omitted_and_empty_list_disables(self):
        self.assertEqual(publisher.accelerator_prefixes({}), publisher.DEFAULT_ASSET_ACCELERATORS)
        self.assertEqual(publisher.accelerator_prefixes({"github_asset_accelerators": []}), ())
        self.assertTrue(publisher.prefer_accelerators({}))
        self.assertFalse(publisher.prefer_accelerators({"github_prefer_accelerators": False}))
        with self.assertRaises(ValueError):
            publisher.prefer_accelerators({"github_prefer_accelerators": "yes"})
        with self.assertRaises(ValueError):
            publisher.accelerator_prefixes({
                "github_asset_accelerators": [f"https://m{i}.example" for i in range(6)]
            })

    def test_redirect_allows_accelerator_subdomain_and_rejects_unrelated_hosts(self):
        for value in ("http://mirror.example", "https://user@mirror.example/", "https://mirror.example/?q=",
                      "https://mirror.example:9999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publisher.accelerator_prefixes({"github_asset_accelerators": [value]})
        handler = publisher.DownloadRedirect("first.example")
        req = request.Request(self.asset["url"], headers=self.headers)
        for allowed_url in ("https://first.example/file", "https://cdn.first.example/file"):
            approved = handler.redirect_request(req, None, 302, "", {}, allowed_url)
            self.assertIsNone(approved.get_header("Authorization"))
        for target in (
            "http://first.example/file",
            "https://other.example/file",
            "https://notfirst.example/file",
            "https://user@first.example/",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, "", {}, target)

    def test_request_json_retries_transient_errors_and_propagates_304(self):
        ok_resp = mock.MagicMock()
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.read.return_value = b'{"tag_name": "1.5.3"}'
        ok_resp.headers = {"ETag": '"v1"'}

        calls = []

        def fake_urlopen(req, timeout=40):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise error.URLError("reset")
            if len(calls) == 2:
                raise error.HTTPError(req.full_url, 502, "Bad gateway", {}, None)
            return ok_resp

        with mock.patch.object(publisher.urllib.request, "urlopen", side_effect=fake_urlopen), mock.patch.object(
            publisher.time, "sleep"
        ) as sleep:
            data, etag = publisher.request_json("https://api.github.com/repos/o/r/releases/latest")
        self.assertEqual(data, {"tag_name": "1.5.3"})
        self.assertEqual(etag, '"v1"')
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

        calls.clear()

        def return_304(req, timeout=40):
            calls.append(req.full_url)
            raise error.HTTPError(req.full_url, 304, "Not modified", {}, None)

        with mock.patch.object(publisher.urllib.request, "urlopen", side_effect=return_304), mock.patch.object(
            publisher.time, "sleep"
        ) as sleep:
            with self.assertRaises(error.HTTPError) as ctx:
                publisher.request_json("https://api.github.com/repos/o/r/releases/latest")
            ctx.exception.close()
        self.assertEqual(ctx.exception.code, 304)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

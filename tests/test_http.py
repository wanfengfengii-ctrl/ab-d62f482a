"""End-to-end tests against the real HTTP server."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.main import Handler


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(base, body, raw=False, ctype="application/json"):
    data = body if raw else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/cold-chain/exposure",
        data=data,
        headers={"Content-Type": ctype},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


PAYLOAD = {
    "start_at": "2026-10-05T08:00:00Z",
    "end_at": "2026-10-05T08:20:00Z",
    "temperature_threshold": 8,
    "max_interval_seconds": 1200,
    "max_exposure_seconds": 900,
    "degree_minutes_budget": 100,
    "readings": [
        {"timestamp": "2026-10-05T08:00:00Z", "temperature": 10},
        {"timestamp": "2026-10-05T08:01:00Z", "temperature": 10},
        {"timestamp": "2026-10-05T08:02:00Z", "temperature": 6},
        {"timestamp": "2026-10-05T08:20:00Z", "temperature": 6},
    ],
}


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = serve()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health(self):
        with urllib.request.urlopen(self.base + "/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read()), {"status": "ok"})

    def test_success_shape(self):
        status, body = post(self.base, PAYLOAD)
        self.assertEqual(status, 200)
        self.assertEqual(body["verdict"], "pass")
        self.assertTrue(body["pass"])
        self.assertEqual(len(body["exposures"]), 1)
        self.assertEqual(
            body["exposures"][0]["start"], "2026-10-05T08:00:00Z"
        )
        self.assertEqual(
            body["exposures"][0]["end"], "2026-10-05T08:01:30Z"
        )
        self.assertEqual(
            float(body["exposures"][0]["duration_seconds"]), 90.0
        )
        self.assertIn("total_degree_minutes", body)
        self.assertIn("coverage_gaps", body)
        self.assertIn("reasons", body)

    def test_gap_failure_with_reason(self):
        payload = json.loads(json.dumps(PAYLOAD))
        payload["max_interval_seconds"] = 600
        payload["readings"][2] = {
            "timestamp": "2026-10-05T08:15:00Z", "temperature": 6
        }
        status, body = post(self.base, payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["pass"])
        self.assertEqual(body["verdict"], "fail")
        self.assertEqual(len(body["coverage_gaps"]), 1)
        codes = {r["code"] for r in body["reasons"]}
        self.assertIn("coverage_gap", codes)

    def test_422_duplicate_timestamp(self):
        payload = json.loads(json.dumps(PAYLOAD))
        payload["readings"][1]["timestamp"] = payload["readings"][0]["timestamp"]
        status, body = post(self.base, payload)
        self.assertEqual(status, 422)
        locs = [e["loc"] for e in body["errors"]]
        self.assertIn("readings[1].timestamp", locs)

    def test_422_bad_timestamp(self):
        payload = json.loads(json.dumps(PAYLOAD))
        payload["readings"][1]["timestamp"] = "08:01:00"
        status, body = post(self.base, payload)
        self.assertEqual(status, 422)
        locs = [e["loc"] for e in body["errors"]]
        self.assertIn("readings[1].timestamp", locs)

    def test_422_nan_token(self):
        # A raw NaN token (some clients emit it); must not crash the server.
        raw = json.dumps(PAYLOAD).replace(
            '"temperature": 10', '"temperature": NaN', 1
        )
        status, body = post(self.base, raw.encode("utf-8"), raw=True)
        self.assertEqual(status, 422)
        locs = [e["loc"] for e in body["errors"]]
        self.assertIn("readings[0].temperature", locs)

    def test_422_malformed_json(self):
        status, body = post(self.base, b"{not json", raw=True)
        self.assertEqual(status, 422)
        self.assertEqual(body["errors"][0]["loc"], "body")

    def test_404(self):
        req = urllib.request.Request(self.base + "/nope")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()

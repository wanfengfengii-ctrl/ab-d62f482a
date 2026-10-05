"""Business smoke request.

Submits a shipment that contains BOTH:
  * an exact threshold intersection (temperature segment crossing the
    threshold between two readings), and
  * a coverage gap (two consecutive readings more than max interval apart).

Exits 0 only if the API answers 200, reports at least one exposure interval
whose boundary is an interpolated intersection (not a reading timestamp),
reports at least one coverage gap, and returns a coherent fail verdict.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
URL = API_BASE.rstrip("/") + "/api/cold-chain/exposure"

PAYLOAD = {
    "start_at": "2026-10-05T08:00:00Z",
    "end_at": "2026-10-05T08:25:00Z",
    "temperature_threshold": 8,
    "max_interval_seconds": 600,        # 10 minutes
    "max_exposure_seconds": 60,
    "degree_minutes_budget": 100,
    "readings": [
        # 08:00 -> 08:02: above 8, segment 10 -> 6 crosses 8 at 08:01:30
        {"timestamp": "2026-10-05T08:00:00Z", "temperature": 10},
        {"timestamp": "2026-10-05T08:01:00Z", "temperature": 10},
        {"timestamp": "2026-10-05T08:02:00Z", "temperature": 6},
        # 18-minute jump -> coverage gap (1080 s > 600 s), no interpolation
        {"timestamp": "2026-10-05T08:20:00Z", "temperature": 10},
        # 10 -> 6 over 5 minutes crosses 8 again at 08:22:30
        {"timestamp": "2026-10-05T08:25:00Z", "temperature": 6},
    ],
}


def wait_for_api(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                API_BASE.rstrip("/") + "/health", timeout=2
            ) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last = exc
        time.sleep(1)
    raise SystemExit(f"API 在 {timeout} 秒内未就绪: {last}")


def main() -> int:
    wait_for_api()
    req = urllib.request.Request(
        URL,
        data=json.dumps(PAYLOAD).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            print(f"FAIL: 期望 200，实际 {resp.status}")
            return 1
        body = json.loads(resp.read())

    print(json.dumps(body, ensure_ascii=False, indent=2))

    failures = []

    gaps = body.get("coverage_gaps", [])
    if not gaps:
        failures.append("响应缺少覆盖缺口")
    elif gaps[0].get("start") != "2026-10-05T08:02:00Z" or gaps[0].get(
        "end"
    ) != "2026-10-05T08:20:00Z":
        failures.append(f"覆盖缺口边界不正确: {gaps[0]}")

    exposures = body.get("exposures", [])
    if len(exposures) < 2:
        failures.append(f"期望缺口两侧各有超温区间，实际 {len(exposures)} 个")
    else:
        expected_boundaries = {
            ("2026-10-05T08:00:00Z", "2026-10-05T08:01:30Z"),
            ("2026-10-05T08:20:00Z", "2026-10-05T08:22:30Z"),
        }
        actual = {(iv["start"], iv["end"]) for iv in exposures}
        for pair in expected_boundaries:
            if pair not in actual:
                failures.append(f"缺少精确阈值交点区间 {pair}，实际 {actual}")

    if body.get("verdict") != "fail" or body.get("pass") is not False:
        failures.append("存在缺口与超时长区间，裁决应为 fail")

    codes = {r.get("code") for r in body.get("reasons", [])}
    if "coverage_gap" not in codes:
        failures.append("reasons 缺少 coverage_gap")
    if "single_exposure_exceeded" not in codes:
        failures.append("reasons 缺少 single_exposure_exceeded")

    if float(body.get("total_degree_minutes", 0)) <= 0:
        failures.append("总度分钟应为正数")

    if failures:
        for item in failures:
            print(f"FAIL: {item}")
        return 1

    print("SMOKE OK: 阈值交点、覆盖缺口、裁决与原因均符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Core adjudication logic for cold-chain thermal exposure.

The input is a series of timestamped temperature readings for one shipment.
Between two consecutive readings whose spacing does not exceed the maximum
allowed sampling interval, temperature is assumed to vary *linearly* with
time. Threshold crossings are solved exactly (decimal arithmetic, no binary
floating point), and only temperatures strictly greater than the threshold
count as exposure (equality does not). A spacing larger than the maximum
interval forms a coverage gap across which no interpolation is allowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from typing import Any

getcontext().prec = 60

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_Q = Decimal("0.000000001")  # display quantization: 9 fractional digits

_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?(Z|z|[+-]\d{2}:\d{2})$"
)


class NonFinite:
    """Sentinel produced when JSON contains NaN / Infinity / -Infinity."""

    def __init__(self, token: str) -> None:
        self.token = token


@dataclass(frozen=True)
class FieldError:
    loc: str
    message: str


class ValidationError(Exception):
    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.loc}: {e.message}" for e in errors))


def parse_rfc3339(value: str) -> Decimal:
    """Parse an RFC 3339 timestamp into exact decimal seconds since the epoch.

    Fractional seconds are preserved verbatim as a :class:`Decimal` so that
    arithmetic on intersections stays exact.
    """
    m = _RFC3339.match(value)
    if not m:
        raise ValueError("时刻必须是带时区的 RFC 3339 时间，例如 2026-10-05T08:00:00Z")
    year, mon, day, hour, minute, second, frac, zone = m.groups()
    try:
        dt = datetime(
            int(year), int(mon), int(day),
            int(hour), int(minute), int(second),
        )
    except ValueError as exc:
        raise ValueError(f"非法的日历时刻: {exc}") from exc

    if zone in ("Z", "z"):
        offset = timedelta(0)
    else:
        sign = 1 if zone[0] == "+" else -1
        offset = sign * timedelta(
            hours=int(zone[1:3]), minutes=int(zone[4:6])
        )
    dt = dt.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)

    delta = dt - _EPOCH
    whole = delta.days * 86400 + delta.seconds
    result = Decimal(whole)
    if frac:
        result += Decimal("0." + frac)
    return result


def format_epoch(value: Decimal) -> str:
    """Render an exact decimal epoch-second value as RFC 3339 UTC.

    Quantized to nanoseconds for display so that timestamps and the derived
    second durations never disagree at the last shown digit.
    """
    value = value.quantize(_Q)
    whole = int(value)  # truncates toward zero; epoch values are positive
    frac = value - Decimal(whole)
    dt = datetime.fromtimestamp(whole, tz=timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if frac:
        digits = str(frac).split(".", 1)[1].rstrip("0")
        if digits:
            text += "." + digits
    return text + "Z"


def _as_decimal(value: Any, loc: str, errors: list[FieldError]) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        errors.append(FieldError(loc, "必须是有限数值"))
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            errors.append(FieldError(loc, "必须是有限数值，不能为 NaN 或无穷"))
            return None
        return Decimal(str(value))
    return Decimal(value)


def _validate(payload: Any) -> dict[str, Any]:
    errors: list[FieldError] = []
    if not isinstance(payload, dict):
        raise ValidationError([FieldError("body", "请求体必须是 JSON 对象")])

    scalar_fields = (
        "start_at",
        "end_at",
        "temperature_threshold",
        "max_interval_seconds",
        "max_exposure_seconds",
        "degree_minutes_budget",
    )
    for key in scalar_fields:
        if key not in payload:
            errors.append(FieldError(key, "缺少必填字段"))
    if errors:
        raise ValidationError(errors)

    start = end = None
    start_raw = payload["start_at"]
    end_raw = payload["end_at"]
    if not isinstance(start_raw, str):
        errors.append(FieldError("start_at", "必须是 RFC 3339 时间字符串"))
    else:
        try:
            start = parse_rfc3339(start_raw)
        except ValueError as exc:
            errors.append(FieldError("start_at", str(exc)))
    if not isinstance(end_raw, str):
        errors.append(FieldError("end_at", "必须是 RFC 3339 时间字符串"))
    else:
        try:
            end = parse_rfc3339(end_raw)
        except ValueError as exc:
            errors.append(FieldError("end_at", str(exc)))
    if start is not None and end is not None and start >= end:
        errors.append(FieldError("end_at", "结束时刻必须严格晚于起始时刻"))

    threshold = _as_decimal(
        payload["temperature_threshold"], "temperature_threshold", errors
    )
    max_interval = _as_decimal(
        payload["max_interval_seconds"], "max_interval_seconds", errors
    )
    max_exposure = _as_decimal(
        payload["max_exposure_seconds"], "max_exposure_seconds", errors
    )
    budget = _as_decimal(
        payload["degree_minutes_budget"], "degree_minutes_budget", errors
    )
    if max_interval is not None and max_interval <= 0:
        errors.append(FieldError("max_interval_seconds", "必须大于 0"))
    if max_exposure is not None and max_exposure < 0:
        errors.append(FieldError("max_exposure_seconds", "不能为负数"))
    if budget is not None and budget < 0:
        errors.append(FieldError("degree_minutes_budget", "不能为负数"))

    readings_raw = payload.get("readings")
    if not isinstance(readings_raw, list):
        errors.append(FieldError("readings", "必须是读数数组"))
        raise ValidationError(errors)
    if not 2 <= len(readings_raw) <= 500:
        errors.append(
            FieldError("readings", "读数数量必须在 2 到 500 条之间（含边界）")
        )

    readings: list[dict[str, Any]] = []
    for i, item in enumerate(readings_raw):
        if isinstance(item, NonFinite) or not isinstance(item, dict):
            errors.append(FieldError(f"readings[{i}]", "读数必须是对象"))
            continue

        epoch = None
        ts_value = item.get("timestamp")
        if isinstance(ts_value, NonFinite) or not isinstance(ts_value, str):
            errors.append(
                FieldError(f"readings[{i}].timestamp", "必须是 RFC 3339 时间字符串")
            )
        else:
            try:
                epoch = parse_rfc3339(ts_value)
            except ValueError as exc:
                errors.append(FieldError(f"readings[{i}].timestamp", str(exc)))
            else:
                if start is not None and end is not None:
                    if epoch < start or epoch > end:
                        errors.append(
                            FieldError(
                                f"readings[{i}].timestamp",
                                "读数时刻超出运输起止区间",
                            )
                        )

        temperature = None
        temp_value = item.get("temperature")
        if isinstance(temp_value, NonFinite):
            errors.append(
                FieldError(
                    f"readings[{i}].temperature",
                    f"必须是有限数值，不能为 {temp_value.token}",
                )
            )
        elif isinstance(temp_value, bool) or not isinstance(
            temp_value, (int, float, Decimal)
        ):
            errors.append(
                FieldError(f"readings[{i}].temperature", "必须是有限数值")
            )
        elif isinstance(temp_value, float) and not (
            temp_value == temp_value
            and temp_value != float("inf")
            and temp_value != float("-inf")
        ):
            errors.append(
                FieldError(
                    f"readings[{i}].temperature", "必须是有限数值，不能为 NaN 或无穷"
                )
            )
        elif isinstance(temp_value, float):
            temperature = Decimal(str(temp_value))
        else:
            temperature = Decimal(temp_value)

        if epoch is not None and temperature is not None:
            readings.append(
                {"epoch": epoch, "temperature": temperature, "idx": i}
            )

    # Duplicate timestamps (reported against request indices, before sorting).
    seen: dict[Decimal, int] = {}
    for r in readings:
        epoch = r["epoch"]
        if epoch in seen:
            errors.append(
                FieldError(
                    f"readings[{r['idx']}].timestamp",
                    f"时刻重复，首次出现在 readings[{seen[epoch]}]",
                )
            )
        else:
            seen[epoch] = r["idx"]

    if errors:
        raise ValidationError(errors)

    readings.sort(key=lambda r: r["epoch"])
    if readings[0]["epoch"] != start or readings[-1]["epoch"] != end:
        raise ValidationError(
            [
                FieldError(
                    "readings",
                    "排序后首条读数必须恰好位于运输起始时刻、末条读数必须恰好位于运输结束时刻",
                )
            ]
        )

    return {
        "start": start,
        "end": end,
        "threshold": threshold,
        "max_interval": max_interval,
        "max_exposure": max_exposure,
        "budget": budget,
        "readings": readings,
    }


def _exposed_portion(
    t1: Decimal, t2: Decimal, d1: Decimal, d2: Decimal
) -> tuple[Decimal, Decimal] | None:
    """Return (a, b) of the segment portion strictly above the threshold.

    Endpoints that sit exactly on the threshold are included; they carry zero
    excess and therefore add neither duration nor degree-minutes by themselves
    (inclusion only matters for merging adjacent intervals).
    """
    pos1, pos2 = d1 > 0, d2 > 0

    if pos1 and pos2:
        return t1, t2
    if pos1 and d2 == 0:
        return t1, t2
    if d1 == 0 and pos2:
        return t1, t2
    if pos1 and d2 < 0:
        x = t1 + (-d1) * (t2 - t1) / (d2 - d1)
        return t1, x
    if d1 < 0 and pos2:
        x = t1 + (-d1) * (t2 - t1) / (d2 - d1)
        return x, t2
    # below/below, a single equality touch (0,- / -,0 / 0,0), never strictly >
    return None


def evaluate(payload: Any) -> dict[str, Any]:
    """Validate payload and adjudicate thermal exposure."""
    data = _validate(payload)
    threshold: Decimal = data["threshold"]
    max_interval: Decimal = data["max_interval"]
    readings = data["readings"]

    gaps: list[dict[str, Any]] = []
    cur_start = cur_end = None
    cur_seconds = Decimal(0)
    cur_dm = Decimal(0)
    intervals: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_seconds, cur_dm
        if cur_start is not None:
            intervals.append(
                {
                    "start": cur_start,
                    "end": cur_end,
                    "seconds": cur_seconds,
                    "degree_minutes": cur_dm,
                }
            )
            cur_start = cur_end = None
            cur_seconds = Decimal(0)
            cur_dm = Decimal(0)

    for prev, nxt in zip(readings, readings[1:]):
        t1, t2 = prev["epoch"], nxt["epoch"]
        dt = t2 - t1
        if dt > max_interval:
            # Coverage gap: no interpolation, and exposure cannot cross it.
            flush()
            gaps.append({"start": t1, "end": t2, "seconds": dt})
            continue

        d1 = prev["temperature"] - threshold
        d2 = nxt["temperature"] - threshold
        portion = _exposed_portion(t1, t2, d1, d2)
        if portion is None:
            flush()
            continue

        a, b = portion
        # Excess above threshold at the portion endpoints (linear profile).
        da = d1 + (d2 - d1) * (a - t1) / dt
        db = d1 + (d2 - d1) * (b - t1) / dt
        length = b - a
        dm = (da + db) / 2 * length / 60  # degree-minutes (exact)

        if cur_start is None or a != cur_end:
            flush()
            cur_start, cur_end = a, b
        else:
            cur_end = b
        cur_seconds += length
        cur_dm += dm

    flush()

    total_dm = sum((iv["degree_minutes"] for iv in intervals), Decimal(0))

    reasons: list[dict[str, str]] = []
    if gaps:
        reasons.append(
            {
                "code": "coverage_gap",
                "message": (
                    f"存在 {len(gaps)} 个覆盖缺口：相邻读数间隔超过最大采样间隔 "
                    f"{max_interval} 秒，缺口区间不做线性插值"
                ),
            }
        )
    for i, iv in enumerate(intervals):
        if iv["seconds"] > data["max_exposure"]:
            reasons.append(
                {
                    "code": "single_exposure_exceeded",
                    "message": (
                        f"第 {i + 1} 个超温区间持续 {iv['seconds']} 秒，"
                        f"超过单次上限 {data['max_exposure']} 秒"
                    ),
                }
            )
    if total_dm > data["budget"]:
        reasons.append(
            {
                "code": "degree_minutes_budget_exceeded",
                "message": f"累计度分钟 {total_dm} 超过预算 {data['budget']}",
            }
        )

    verdict = "pass" if not reasons else "fail"

    def qnum(value: Decimal) -> Decimal:
        q = value.quantize(_Q)
        return Decimal(0) if q == 0 else q

    return {
        "verdict": verdict,
        "pass": verdict == "pass",
        "exposures": [
            {
                "start": format_epoch(iv["start"]),
                "end": format_epoch(iv["end"]),
                "duration_seconds": qnum(iv["seconds"]),
                "degree_minutes": qnum(iv["degree_minutes"]),
            }
            for iv in intervals
        ],
        "total_degree_minutes": qnum(total_dm),
        "coverage_gaps": [
            {
                "start": format_epoch(g["start"]),
                "end": format_epoch(g["end"]),
                "duration_seconds": qnum(g["seconds"]),
            }
            for g in gaps
        ],
        "reasons": reasons,
    }

"""Reproducible, close-only three-index CZSC research. No order execution."""
import argparse
import calendar
from datetime import date, datetime, time, timedelta
import fcntl
import json
import os
from pathlib import Path
import sys
import uuid

import numpy as np
import pandas as pd
import requests

from czsc import CZSC, Freq, format_standard_kline, get_zs_seq
from czsc._native.signals import cxt
from czsc.utils.plotting.lightweight import plot_czsc
from market import SHANGHAI, DAILY_URL, QUOTE_URL, parse_daily, parse_quote

BASE = Path(__file__).resolve().parent
ROOT = BASE / "output" / "chan"
SYMBOLS = (("sz399006", "创业板指"), ("sh000001", "上证指数"), ("sh000688", "科创50"))
CALENDAR_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
SIGNALS = {"first_buy": "cxt_first_buy_V221126", "first_sell": "cxt_first_sell_V221126",
           "third_buy": "cxt_third_buy_V230228"}


def now_cn():
    return datetime.now(SHANGHAI)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def validate_calendar(body, year, month):
    records = body.get("data")
    if not isinstance(records, list) or not records:
        raise ValueError("交易所尚未发布完整月历")
    days = {}
    for r in records:
        stamp = date.fromisoformat(str(r.get("jyrq", "")))
        flag = str(r.get("jybz"))
        if (stamp.year, stamp.month) != (year, month) or flag not in ("0", "1") or str(stamp) in days:
            raise ValueError("交易所日历日期或开市标志异常")
        days[str(stamp)] = flag == "1"
    expected = {f"{year}-{month:02d}-{d:02d}" for d in range(1, calendar.monthrange(year, month)[1] + 1)}
    if set(days) != expected:
        raise ValueError("交易所日历缺日，停止运行；不会把工作日当作交易日")
    return days


def get_calendar(session, year, month, root=ROOT):
    cache = root / "calendar" / f"{year}-{month:02d}.json"
    # Refresh daily; a previously validated full month remains a usable explicit calendar.
    old = read_json(cache)
    if old and old["fetched_at"][:10] == str(now_cn().date()):
        return validate_calendar(old["raw"], year, month)
    try:
        response = session.get(CALENDAR_URL, params={"month": f"{year}-{month}"},
                               headers={"Referer": "https://www.szse.cn/"}, timeout=(5, 12))
        response.raise_for_status()
        raw = response.json()
        days = validate_calendar(raw, year, month)
        atomic_json(cache, {"source": CALENDAR_URL, "fetched_at": now_cn().isoformat(), "raw": raw})
        return days
    except (ValueError, requests.RequestException):
        if old:
            return validate_calendar(old["raw"], year, month)
        raise


def expected_session(session, now, scheduled=False, root=ROOT):
    days = get_calendar(session, now.year, now.month, root)
    today = str(now.date())
    if scheduled and not days[today]:
        return None
    if scheduled and now.time() < time(16, 30):
        raise ValueError("定时运行尚未到上海时间16:30")
    cutoff = now.date() if now.time() >= time(15, 10) else now.date() - timedelta(days=1)
    candidates = [d for d, opened in days.items() if opened and date.fromisoformat(d) <= cutoff]
    if not candidates:
        previous = now.date().replace(day=1) - timedelta(days=1)
        days = get_calendar(session, previous.year, previous.month, root)
        candidates = [d for d, opened in days.items() if opened and date.fromisoformat(d) <= cutoff]
    if not candidates:
        raise ValueError("交易日历中没有已收盘交易日")
    return max(candidates)


def parse_m30(body, symbol, expected):
    if body.get("code") != 0:
        raise ValueError("腾讯30分钟数据请求失败")
    rows = body.get("data", {}).get(symbol, {}).get("m30")
    if not rows or any(len(r) < 6 for r in rows):
        raise ValueError("腾讯30分钟数据为空或字段不全")
    frame = pd.DataFrame([dict(symbol=symbol, dt=r[0], open=r[1], close=r[2], high=r[3], low=r[4], vol=r[5]) for r in rows])
    frame["dt"] = pd.to_datetime(frame["dt"], format="%Y%m%d%H%M", errors="raise").astype("datetime64[ns]")
    cols = ["open", "close", "high", "low", "vol"]
    frame[cols] = frame[cols].apply(pd.to_numeric, errors="raise").astype(float)
    if not np.isfinite(frame[cols].to_numpy()).all() or frame["dt"].duplicated().any() or not frame["dt"].is_monotonic_increasing:
        raise ValueError("30分钟数据缺失、重复或乱序")
    if ((frame[["open", "close", "high", "low"]] <= 0).any().any()
            or (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()
            or (frame["vol"] < 0).any()):
        raise ValueError("30分钟价格或成交量不合理")
    frame = frame.loc[frame["dt"].dt.strftime("%Y-%m-%d") <= expected].copy().reset_index(drop=True)
    if len(frame) < 100 or frame.iloc[-1]["dt"].strftime("%Y-%m-%d %H:%M") != expected + " 15:00":
        raise ValueError(f"30分钟数据尚未完整更新到 {expected} 收盘")
    last = frame.loc[frame["dt"].dt.strftime("%Y-%m-%d") == expected]
    if list(last["dt"].dt.strftime("%H:%M")) != ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]:
        raise ValueError("最新交易日30分钟K线不完整")
    frame["vol"] *= 100
    # Tencent m30 has turnover rate, NOT amount. Keep missing amount explicit.
    frame["amount"] = np.nan
    return frame


def signal_values(c):
    return {key: [{"key": s.key, "value": s.value} for s in cxt.call_signal(fn, c, {"di": 1})]
            for key, fn in SIGNALS.items()}


def direction(b):
    return "Up" if b.direction.value == "向上" else "Down"


def anchor(c):
    if not c.bi_list:
        return None
    b = c.bi_list[-1]
    return dict(start=str(b.sdt), end=str(b.edt), direction=direction(b), low=b.low, high=b.high)


def structure(c):
    centres = [dict(start=str(z.sdt), end=str(z.edt), zd=z.zd, zg=z.zg, pen_count=len(z.bis))
               for z in get_zs_seq(c.bi_list) if len(z.bis) >= 3 and z.is_valid()]
    return dict(pen_count=len(c.bi_list), last_completed_pen=anchor(c), centres=centres,
                unfinished_direction=str(c.ubi["direction"]) if c.ubi else None,
                signals=signal_values(c))


def confirmation_window(bars, freq, limit):
    """Prefix replay: each observation uses ONLY bars available at that time."""
    records = []
    for end in range(max(1, len(bars) - limit + 1), len(bars) + 1):
        c = CZSC(bars[:end])
        records.append(dict(as_of=str(bars[end - 1].dt), close=bars[end - 1].close,
                            anchor=anchor(c), signals=signal_values(c)))
    return records


def active_signals(c, timeline):
    current = signal_values(c)
    pen = anchor(c)
    found = []
    for kind, values in current.items():
        value = values[0]["value"]
        if value.split("_")[0] == "其他" or not pen:
            continue
        buy = kind != "first_sell"
        price = pen["low"] if buy else pen["high"]
        tail = []
        for r in reversed(timeline):
            if r["anchor"] != pen or r["signals"][kind][0]["value"] != value:
                break
            tail.append(r)
        first = tail[-1] if tail else None
        found.append(dict(kind=kind, label=value.split("_")[0] + "辅助", detail=value,
                          price=price, structural_time=pen["end"],
                          observed_at=first["as_of"] if first else None,
                          confirmation_close=first["close"] if first else None,
                          exact_first_seen=bool(first and len(tail) < len(timeline)),
                          invalidation=price))
    return found


def levels(daily, minute, close):
    candidates = []
    for c, label in ((minute, "30分钟"), (daily, "日线")):
        for f in c.fx_list:
            is_low = str(f.mark) in ("底分型", "D") or f.mark.value == "底分型"
            price = f.low if is_low else f.high
            candidates.append(dict(price=price, date=str(f.dt), source=label + ("底分型" if is_low else "顶分型"),
                                   side="support" if is_low else "resistance"))
        for b in c.bi_list:
            down = direction(b) == "Down"
            candidates.append(dict(price=b.low if down else b.high, date=str(b.edt),
                                   source=label + ("下行笔低点" if down else "上行笔高点"),
                                   side="support" if down else "resistance"))
        centres = [z for z in get_zs_seq(c.bi_list) if len(z.bis) >= 3 and z.is_valid()]
        if centres:
            for price, edge in ((centres[-1].zd, "下沿"), (centres[-1].zg, "上沿")):
                candidates.append(dict(price=price, date=str(centres[-1].edt), source=label + "最近笔中枢" + edge,
                                       side="support" if price <= close else "resistance"))
    result = {}
    for side in ("support", "resistance"):
        eligible = [x for x in candidates if x["side"] == side and
                    (x["price"] <= close if side == "support" else x["price"] > close)]
        eligible.sort(key=lambda x: (abs(x["price"] - close), x["date"]))
        unique = []
        for item in eligible:
            if not any(abs(x["price"] - item["price"]) < 0.01 for x in unique):
                unique.append(item)
        result[side] = unique[:4]
    return result


def analyze_symbol(session, symbol, name, expected, now, output):
    output.mkdir(parents=True)
    def get(url, **params):
        response = session.get(url, params=params, timeout=(5, 12))
        response.raise_for_status()
        return response
    response = get(QUOTE_URL + symbol, _r=now.timestamp())
    response.encoding = "gbk"
    raw_quote = response.text
    quote = parse_quote(raw_quote, symbol, now)
    daily_raw = get(DAILY_URL, param=f"{symbol},day,,,640,").json()
    minute_raw = get(MINUTE_URL, param=f"{symbol},m30,,640").json()
    daily, excluded = parse_daily(daily_raw, symbol, now, 640, "none")
    daily = daily.loc[daily["dt"].dt.strftime("%Y-%m-%d") <= expected].copy().reset_index(drop=True)
    if len(daily) < 100 or str(daily.iloc[-1]["dt"].date()) != expected:
        raise ValueError(f"{name}日线尚未更新到 {expected}")
    minute = parse_m30(minute_raw, symbol, expected)
    latest_m30 = minute.loc[minute["dt"].dt.strftime("%Y-%m-%d") == expected]
    last = daily.iloc[-1]
    for field in ("open", "close", "high", "low"):
        actual = {"open": latest_m30.iloc[0]["open"], "close": latest_m30.iloc[-1]["close"],
                  "high": latest_m30["high"].max(), "low": latest_m30["low"].min()}[field]
        if abs(actual - last[field]) > 0.021:
            raise ValueError(f"{name}日线与30分钟{field}不一致")
    if abs(latest_m30["vol"].sum() - last["vol"]) > max(100, last["vol"] * 0.0001):
        raise ValueError(f"{name}日线与30分钟成交量不一致")
    quote_date = quote["quote_time"][:10]
    if quote_date < expected or (quote_date == expected and abs(quote["price"] - last["close"]) > 0.021):
        raise ValueError(f"{name}报价陈旧或与收盘价不一致")
    contexts, summaries, timelines, triggered = {}, {}, {}, {}
    for key, frame, freq, lookback in (("daily", daily, Freq.D, 12), ("m30", minute, Freq.F30, 24)):
        bars = format_standard_kline(frame, freq)
        c = contexts[key] = CZSC(bars)
        summaries[key] = structure(c)
        timelines[key] = confirmation_window(bars, freq, lookback)
        triggered[key] = active_signals(c, timelines[key])
        chart = "chart.html" if key == "daily" else "m30_chart.html"
        plot_czsc(c, path=output / chart, tail_bars=400, theme="dark",
                  title=f"{name} · {'日线' if key == 'daily' else '30分钟'} · {expected}收盘 · 腾讯真实行情 · 不复权 · 点")
        frame.to_csv(output / ("bars.csv" if key == "daily" else "m30_bars.csv"), index=False)
    close = float(last["close"])
    result = dict(symbol=symbol, name=name, close=close, session=expected,
                  change_pct=round((close / float(daily.iloc[-2]["close"]) - 1) * 100, 2),
                  quote=quote, daily=summaries["daily"], m30=summaries["m30"],
                  signals=triggered, levels=levels(contexts["daily"], contexts["m30"], close),
                  current_low=float(last["low"]), current_high=float(last["high"]),
                  bars={"daily": len(daily), "m30": len(minute)}, excluded_incomplete_dates=excluded)
    for filename, data in (("raw_daily.json", daily_raw), ("m30_raw.json", minute_raw),
                           ("analysis.json", result), ("signal_timeline.json", timelines)):
        atomic_json(output / filename, data)
    (output / "raw_quote.txt").write_text(raw_quote, encoding="utf-8")
    atomic_json(output / "quality.json", {"status": "PASS", "session": expected, "daily_m30_ohlcv_match": True,
        "source": "tencent", "price_unit": "index_points", "volume_unit": "shares", "daily_amount_unit": "CNY",
        "m30_amount": "MISSING; NOT used by selected signals; no estimated or zero-filled amounts",
        "coverage": "Provider rolling window; earlier history completeness not verified"})
    return result


def run(scheduled=False, root=ROOT):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有分析任务正在运行，本次不重复启动。")
            return 2
        now = now_cn()
        state = dict(status="running", started_at=now.isoformat(), pid=os.getpid(),
                     source="scheduled" if scheduled else "manual", message="正在核对交易日历")
        atomic_json(root / "state.json", state)
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
                expected = expected_session(session, now, scheduled, root)
                if expected is None:
                    state.update(status="skipped", finished_at=now_cn().isoformat(), message="交易所日历：今日休市，已跳过")
                    atomic_json(root / "state.json", state)
                    print(state["message"])
                    return 0
                run_id = now.strftime("%Y%m%d_%H%M%S_%f")
                output = root / run_id
                output.mkdir()
                indices = []
                for symbol, name in SYMBOLS:
                    state["message"] = f"正在更新{name} · 日线和30分钟"
                    atomic_json(root / "state.json", state)
                    indices.append(analyze_symbol(session, symbol, name, expected, now, output / symbol))
            report = dict(schema_version=1, generated_at=now_cn().isoformat(), session=expected, run_id=run_id,
                          source="腾讯行情", calendar_source=CALENDAR_URL, indices=indices,
                          method="CZSC 1.0.2 原生笔级一买/一卖/三买辅助；默认笔参数；日线+30分钟；640根窗口",
                          limitations=["辅助信号不等于严格递归缠论买卖点；未实现二买、二卖和三卖信号。",
                            "支撑/压力按已确认分型、笔端点、最近有效笔中枢边界距收盘价由近及远列出；压力位不等于已触发卖点。",
                            "未完成笔会重画，当前信号后续可能消失；结构极值的时间早于信号可见时间。",
                            "分钟成交额缺失；所选信号只用价格、成交量和笔长度，不用成交额。",
                            "盘中手动运行使用上一个已收盘交易日，不输出当日未收盘信号。指数单位为点。"])
            atomic_json(output / "report.json", report)
            # Single atomic pointer is the publication boundary for all three indices.
            atomic_json(root / "latest.json", dict(path=str(output), run_id=run_id, session=expected, generated_at=report["generated_at"]))
            state.update(status="success", finished_at=now_cn().isoformat(), message=f"三指数更新完成 · {expected}收盘", run_id=run_id)
            atomic_json(root / "state.json", state)
            print(json.dumps(dict(status="success", session=expected, path=str(output)), ensure_ascii=False))
            return 0
        except Exception as exc:
            state.update(status="error", finished_at=now_cn().isoformat(), message=f"更新失败：{type(exc).__name__}: {exc}；保留上次成功结果")
            atomic_json(root / "state.json", state)
            print(state["message"], file=sys.stderr)
            return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true", help="仅在交易所确认的交易日16:30之后运行")
    args = parser.parse_args()
    raise SystemExit(run(args.scheduled))

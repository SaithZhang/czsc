"""Fetch dated Tencent quotes and daily bars, then run CZSC's public APIs."""

import argparse
from datetime import datetime, time
from html import escape
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from czsc import CZSC, Freq, format_standard_kline
from czsc.utils.plotting.lightweight import plot_czsc

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="


def normalize_symbol(value):
    value = value.strip().lower()
    suffix = re.fullmatch(r"(\d{6})\.(sh|sz)", value)
    if suffix:
        value = suffix[2] + suffix[1]
    if re.fullmatch(r"\d{6}", value):
        if value.startswith(("5", "6")):
            value = "sh" + value
        elif value.startswith(("0", "1", "3")):
            value = "sz" + value
    if not re.fullmatch(r"(?:sh|sz)\d{6}", value):
        raise ValueError("请输入沪深代码，如 600519、sh600519 或 600519.SH；指数请带交易所前缀。")
    return value


def parse_quote(text, symbol, now):
    match = re.search(r'v_' + re.escape(symbol) + r'="([^"]+)"', text)
    if not match:
        raise ValueError("腾讯未返回该标的报价")
    fields = match[1].split("~")
    if len(fields) < 38 or fields[2] != symbol[2:]:
        raise ValueError("报价字段不全或标的代码不一致")
    stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
    if (stamp - now).total_seconds() > 120:
        raise ValueError("报价时间超出当前时间")
    result = dict(source="tencent", symbol=symbol, name=fields[1],
                  price=float(fields[3]), previous_close=float(fields[4]),
                  volume_shares=float(fields[6]) * 100,
                  amount_cny=float(fields[37]) * 10000,
                  quote_time=stamp.isoformat(), fetched_at=now.isoformat())
    if not all(np.isfinite(result[k]) and result[k] > 0 for k in ("price", "previous_close")):
        raise ValueError("报价价格无效")
    if not all(np.isfinite(result[k]) and result[k] >= 0 for k in ("volume_shares", "amount_cny")):
        raise ValueError("报价成交量或成交额无效")
    active = now.weekday() < 5 and (time(9, 30) <= now.time() <= time(11, 30)
                                      or time(13) <= now.time() <= time(15))
    result["age_seconds"] = max(0, int((now - stamp).total_seconds()))
    result["status"] = "DATED_SNAPSHOT"
    if stamp.date() < now.date() or (active and result["age_seconds"] > 300):
        result["status"] = "STALE_SNAPSHOT"
    if result["volume_shares"] == 0:
        result["status"] = "NO_TRADES_SNAPSHOT"
    return result


def parse_daily(body, symbol, now, count, adjustment):
    if body.get("code") != 0:
        raise ValueError(f"腾讯日线请求失败：{body.get('msg')}")
    key = "qfqday" if adjustment == "qfq" else "day"
    rows = body.get("data", {}).get(symbol, {}).get(key)
    if not rows:
        raise ValueError(f"腾讯未提供 {key} 数据；不会自动替换复权口径")
    if any(len(row) < 9 for row in rows):
        raise ValueError("日线缺少成交额字段")
    # Tencent: OHLC are yuan, volume is lots of 100 shares, row[8] is CNY 10,000.
    frame = pd.DataFrame([dict(symbol=symbol, dt=row[0], open=row[1], close=row[2],
                               high=row[3], low=row[4], vol=row[5], amount=row[8]) for row in rows])
    frame["symbol"] = frame["symbol"].astype(object)
    frame["dt"] = pd.to_datetime(frame["dt"], errors="raise").astype("datetime64[ns]")
    cols = ["open", "close", "high", "low", "vol", "amount"]
    frame[cols] = frame[cols].apply(pd.to_numeric, errors="raise").astype(float)
    if not np.isfinite(frame[cols].to_numpy()).all():
        raise ValueError("日线存在缺失或非有限数值")
    if not frame["dt"].is_monotonic_increasing or frame["dt"].duplicated().any():
        raise ValueError("日线日期乱序或重复")
    if (frame["dt"].dt.date > now.date()).any():
        raise ValueError("日线包含未来日期")
    if ((frame[["open", "close", "high", "low"]] <= 0).any().any()
            or (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()
            or (frame[["vol", "amount"]] < 0).any().any()):
        raise ValueError("日线价格、成交量或成交额不合理")
    frame["vol"] *= 100
    frame["amount"] *= 10000
    incomplete = (frame["dt"].dt.date == now.date()) & (now.time() < time(15, 10))
    excluded = frame.loc[incomplete, "dt"].dt.strftime("%Y-%m-%d").tolist()
    frame = frame.loc[~incomplete].tail(count).reset_index(drop=True)
    if frame.empty:
        raise ValueError("没有可用于分析的已收盘日线")
    return frame, excluded


def main():
    parser = argparse.ArgumentParser(description="腾讯真实行情 → CZSC 日线图表（手动刷新）")
    parser.add_argument("--symbol", default="sh000001", help="默认上证指数 sh000001；也支持 600519.SH、sz000001 等股票")
    parser.add_argument("--count", type=int, default=640, help="最近日线条数，100 至 640")
    parser.add_argument("--adjustment", choices=["qfq", "none"], default=None, help="指数默认不复权；股票默认前复权")
    args = parser.parse_args()
    if not 100 <= args.count <= 640:
        parser.error("--count 必须在 100 至 640 之间")
    symbol = normalize_symbol(args.symbol)
    is_index = symbol == "sh000001"
    args.adjustment = args.adjustment or ("none" if is_index else "qfq")
    if is_index and args.adjustment != "none":
        parser.error("上证指数不适用复权，请使用 --adjustment none")
    price_unit = "点" if is_index else "元"
    now = datetime.now(SHANGHAI)
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        response = session.get(QUOTE_URL + symbol, params={"_r": now.timestamp()}, timeout=(5, 12))
        response.raise_for_status()
        response.encoding = "gbk"
        quote_text = response.text
        quote = parse_quote(quote_text, symbol, now)
        response = session.get(DAILY_URL, params={"param": f"{symbol},day,,,{args.count},{'qfq' if args.adjustment == 'qfq' else ''}"}, timeout=(5, 12))
        response.raise_for_status()
        raw = response.json()
    frame, excluded = parse_daily(raw, symbol, now, args.count, args.adjustment)
    c = CZSC(format_standard_kline(frame, freq=Freq.D))
    last_date = frame.iloc[-1]["dt"].date()
    quote_date = datetime.fromisoformat(quote["quote_time"]).date()
    history_status = "DATED_HISTORY"
    if last_date > quote_date:
        raise ValueError("历史日线比报价日期更新，请重试以获得一致的行情快照")
    if quote_date == now.date() and ((now.time() >= time(15, 10) and last_date < now.date())
                                    or (now.time() < time(15, 10) and abs(frame.iloc[-1]["close"] - quote["previous_close"]) > 0.02)):
        history_status = "NEEDS_REVIEW"
    metadata = dict(source="tencent", symbol=symbol, name=quote["name"], frequency="日线",
                    adjustment=args.adjustment, fetched_at=now.isoformat(), requested_bars=args.count,
                    actual_bars=len(frame), first_date=str(frame.iloc[0]["dt"].date()),
                    last_date=str(last_date), excluded_incomplete_dates=excluded, history_status=history_status,
                    quote=quote, units={"price": "index_points" if is_index else "CNY", "vol": "shares", "amount": "CNY"},
                    coverage="Returned provider window; trading-calendar completeness not verified",
                    qfq_note="Latest-basis forward adjustment; historical prices can change after corporate actions" if args.adjustment == "qfq" else None)
    label = "前复权" if args.adjustment == "qfq" else "不复权"
    title = (f"{escape(quote['name'])} · 日线 · {label} · 日线截至 {last_date}；"
             f"最新报价 {quote['price']:.2f} {price_unit}（{quote['quote_time']}，{quote['status']}）；腾讯行情")
    if history_status == "NEEDS_REVIEW":
        title += "；历史日线与报价衔接待核查"
    root = Path(__file__).resolve().parent / "output" / "market"
    output = root / f"{now:%Y%m%d_%H%M%S_%f}_{symbol}"
    output.mkdir(parents=True)
    frame.to_csv(output / "bars.csv", index=False)
    (output / "quote_raw.txt").write_text(quote_text)
    (output / "daily_raw.json").write_text(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False))
    (output / "analysis.json").write_text(json.dumps(dict(symbol=symbol, input_bars=len(frame),
        retained_bars=len(c.bars_raw), fractals=len(c.fx_list), bis=len(c.bi_list)), ensure_ascii=False, indent=2))
    plot_czsc(c, path=output / "chart.html", title=title, tail_bars=400)
    # Publish only after a complete successful run; failures leave the previous chart intact.
    latest = root / "latest.html.tmp"
    latest.write_text((output / "chart.html").read_text())
    latest.replace(root / "latest.html")
    pointer = root / "latest.json.tmp"
    pointer.write_text(json.dumps(dict(path=str(output), **metadata), ensure_ascii=False, indent=2))
    pointer.replace(root / "latest.json")
    print(f"{quote['name']} {symbol}：{quote['price']:.2f} {price_unit}，报价时间 {quote['quote_time']}（{quote['status']}）")
    print(f"日线：{len(frame)} 根，{metadata['first_date']} 至 {last_date}，{label}，识别 {len(c.bi_list)} 笔。")
    print(f"未收盘日线排除：{excluded or '无'}；历史状态：{history_status}")
    print(f"图表：{output / 'chart.html'}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, requests.RequestException) as exc:
        raise SystemExit(f"行情更新失败：{exc}。保留上次成功结果；不会回退到模拟数据。") from exc

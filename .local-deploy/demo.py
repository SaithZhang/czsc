"""Run the project's public analysis and chart APIs with reproducible mock data."""

from pathlib import Path

import czsc
from czsc import CZSC, BarGenerator, CzscTrader, Freq, format_standard_kline
from czsc.mock import generate_symbol_kines
from czsc.utils.plotting.lightweight import plot_czsc, plot_czsc_trader


def main():
    output = Path(__file__).resolve().parent / "output"
    output.mkdir(exist_ok=True)
    df = generate_symbol_kines("DEMO", "30分钟", "20230101", "20240301", seed=42)
    df.to_csv(output / "demo_bars.csv", index=False)
    bars = format_standard_kline(df, freq=Freq.F30)
    analysis = CZSC(bars)
    plot_czsc(
        analysis,
        path=output / "single.html",
        title="CZSC 单周期演示 · 模拟数据（非真实行情）",
        tail_bars=400,
    )
    generator = BarGenerator(base_freq="30分钟", freqs=["60分钟", "日线"], max_count=5000)
    for bar in bars:
        generator.update(bar)
    trader = CzscTrader(generator, positions=[], signals_config=[])
    plot_czsc_trader(
        trader,
        path=output / "multi.html",
        title="CZSC 多周期演示 · 模拟数据（非真实行情）",
        tail_bars=400,
    )
    print(f"CZSC {czsc.__version__}：输入 {len(bars)} 根模拟 K 线，识别 {len(analysis.bi_list)} 笔。")
    print(f"图表已生成：{output / 'multi.html'}")


if __name__ == "__main__":
    main()

#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
export PATH="$PWD/.venv/bin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
  print '本地运行环境不存在，请先按 .local-deploy/README.md 安装。'
  exit 1
fi
if (( $# == 0 )); then
  print '打开三指数缠论界面：http://127.0.0.1:8793'
  launchctl kickstart "gui/$(id -u)/local.czsc.dashboard"
  open 'http://127.0.0.1:8793'
  exit 0
fi
mode=market
show_chart=1
args=()
for arg in "$@"; do
  case "$arg" in
    --demo) mode=demo ;;
    --no-open) show_chart=0 ;;
    *) args+=("$arg") ;;
  esac
done
if [[ "$mode" == demo ]]; then
  print '正在生成 CZSC 演示图表（使用模拟数据）…'
  .venv/bin/python .local-deploy/demo.py
  chart=.local-deploy/output/multi.html
else
  print '正在获取腾讯真实行情（默认上证指数；--symbol 可更换代码）…'
  .venv/bin/python .local-deploy/market.py "${args[@]}"
  chart=.local-deploy/output/market/latest.html
fi
if (( show_chart )); then
  open "$chart"
fi

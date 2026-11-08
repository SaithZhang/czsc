# CZSC 本机部署

部署日期：2026-10-22（北京时间）

- 仓库：<https://github.com/SaithZhang/czsc>
- 源码提交：`5d350a943d6b1935561765fed5467c234973d121`
- 源码版本：1.0.2
- 环境：macOS Apple Silicon、Python 3.12、项目内 `.venv`

## 三指数界面与每日运行（当前入口）

双击项目根目录的 `CZSC.command`，打开 <http://127.0.0.1:8793>。页面显示创业板指 `sz399006`、上证指数 `sh000001`、科创50 `sh000688` 的最新成功结果，并提供“手动重跑三个指数”按钮和六张日线/30分钟交互图。页面每两秒读取任务状态；每次运行固定三个指数，互斥执行。

已通过 Codex 原生自动化登记“三指数缠论收盘更新”（heartbeat，ID `automation`）：上海时间周一至周五16:30触发，运行前核对深交所完整月历；交易所确认休市时跳过，不用工作日推断开市。电脑需要保持开机、唤醒、联网，Codex 桌面应用需要保持运行；没有配置系统自动唤醒。未来定时触发尚需在实际时点观察，已完成同一 `--scheduled` 运行入口的实跑验收。

手动运行在上海时间15:10前使用上一已收盘交易日，之后使用最近已收盘交易日。要求三个指数的日线和30分钟线均更新到目标交易日，且最新一天OHLC和成交量对齐，才原子发布 `output/chan/latest.json`。失败保留上次成功结果并在页面显示失败和数据截至日期。

算法沿用 CZSC 1.0.2 默认笔参数和原生 `cxt_first_buy_V221126`、`cxt_first_sell_V221126`、`cxt_third_buy_V230228`。这是笔级辅助信号，尚未实现二买、二卖、三卖以及严格递归走势类型确认。支撑/压力从已确认分型、笔端点和最近有效笔中枢边界中，按距收盘价由近及远列出，带来源和结构时间。压力不等于已触发卖点。最近24根分钟线、12根日线按前缀回放确定信号首次可见时间；窗口第一根就存在的信号仅标“不晚于”，不会把结构极值当成事先已知的成交价。

腾讯分钟接口没有成交额字段，保留为缺失；不把换手率当作成交额，不估算或填零。所选信号仅用价格、成交量和笔长度。数据均不复权，指数单位为点；提供方滚动窗口不代表全历史。

本地界面仅监听 `127.0.0.1:8793`，由用户级 `~/Library/LaunchAgents/local.czsc.dashboard.plist` 登录启动、退出恢复。界面使用 Python 标准库，不新增运行依赖；原有8792、8787等服务未改动。手动按钮只接受本机同源请求和随机令牌，不能传入代码、命令或任意标的。

入口和运维文件：

- `.local-deploy/chan_runner.py`：三指数批量运行；加 `--scheduled` 启用交易日和16:30门禁。
- `.local-deploy/dashboard.py`、`dashboard.html`：本地服务与界面。
- `.local-deploy/output/chan/latest.json`：唯一发布指针；所指目录含 `report.json`、原始响应、CSV、图表、数据校验和信号回放。
- `.local-deploy/output/chan/state.json`、`runner.log`、`dashboard*.log`：当前状态、手动运行和界面日志。
- `.local-deploy/schedule.json`：本次原生自动化登记信息快照；实际启停以 Codex 自动化卡片为准。

命令行手动重跑：`.venv/bin/python .local-deploy/chan_runner.py`。定时口径：`.venv/bin/python .local-deploy/chan_runner.py --scheduled`。15项离线边界/回归测试：`.venv/bin/python -m unittest discover -s .local-deploy -p 'test_*.py' -v`。

暂停每日运行请在 Codex 自动化卡片中暂停“三指数缠论收盘更新”。停止本地界面可执行 `launchctl bootout gui/$(id -u)/local.czsc.dashboard`；重新加载使用 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.czsc.dashboard.plist`。

## 保留的单指数真实行情命令

向 `CZSC.command` 传入下列参数仍使用原单指数入口：默认拉取上证指数 `sh000001` 的腾讯最新报价及最近640根日线。该入口运行结束即退出，不负责三指数定时报告。

更换股票或口径：

```sh
./CZSC.command --symbol sz000001
./CZSC.command --symbol 600519.SH --count 500
./CZSC.command --symbol sh600519 --adjustment none
./CZSC.command --no-open
```

上证指数默认且仅使用不复权（`none`），股票默认使用前复权（`qfq`），也可明确选择不复权。两种口径不会自动替换，也不会在请求失败时换成模拟数据。沪深代码支持数字、前缀或 `.SH` / `.SZ` 后缀格式；上证指数需显式带 `sh` 前缀以区别平安银行 `sz000001`，北交所暂不支持。

真实结果保存在 `.local-deploy/output/market/`：

- `latest.html`：最近一次成功更新的日线图表，显示行情时间和复权口径。
- `latest.json`：最近成功结果的路径、来源、时间、范围和状态。
- 每次成功生成一个独立结果目录，含 `bars.csv`、`metadata.json`、`analysis.json`、`chart.html` 和腾讯原始响应。

日线采用“已收盘”口径：北京时间 15:10 前排除当天日线，图表中的最新报价仍单独显示原始时间。陈旧或无成交的报价有状态标记，不视为新鲜实时成交。未核验完整交易日历，不把提供方返回窗口视为全历史覆盖。成交量从手换算为股，成交额从万元换算为元；不填补缺失数据。

前复权采用获取时的最新复权基准，除权后历史价格可能变化；历史研究应固定本次保存的原始响应和 CSV。最新报价是不复权快照，独立展示，不拼接到前复权日线中。

图表数据存储在 HTML 中，图表脚本与可选字体由公网 CDN 加载，需要浏览器能够联网。

## 保留的模拟演示

执行 `./CZSC.command --demo` 可打开原来的多周期模拟演示。结果保存在 `.local-deploy/output/`：

- `multi.html`：日线、60 分钟、30 分钟切换。
- `single.html`：30 分钟单周期图表。
- `demo_bars.csv`：演示输入，明确为模拟数据。

这些演示文件为模拟数据，不代表真实行情或策略有效性。

## 使用自己的数据

上游项目本身是分析库和命令行工具；本次本地界面位于独立的 `.local-deploy/` 中。

在本目录执行：

```sh
.venv/bin/czsc --help
.venv/bin/czsc analyze /绝对路径/bars.csv --freq 日线
.venv/bin/czsc plot czsc /绝对路径/bars.csv --freq 日线 -o .local-deploy/output/my-chart.html
```

标准数据列：`symbol, dt, open, close, high, low, vol, amount`。每次分析应使用同一标的、同一周期，按时间排序。腾讯数据连接无需凭证；也可使用上述自行准备的数据文件。

## 重建运行环境

```sh
uv venv --python /opt/homebrew/bin/python3.12 .venv
DEVELOPER_DIR=/Library/Developer/CommandLineTools SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk uv pip install --python .venv/bin/python --index-url https://pypi.org/simple -e .
./CZSC.command --no-open
```

构建工具自动将所需 Rust 工具链安装在用户缓存中，不修改 shell 配置。这里显式使用已有 Command Line Tools 和 macOS 15.4 SDK，绕开系统当前所选 Xcode 的许可证状态，以及默认 27.0 SDK 与现有链接器不兼容的问题。这些变量仅作用于该次安装，不改变系统开发工具设置。

2026-11-08 checkpoint 更新：部署代码、启动器、锁文件与说明纳入 Git；行情输出、日志和本机运行记录由 `.local-deploy/.gitignore` 排除。未修改上游业务源码、依赖声明或锁文件。当前登录启动服务和定时配置见本文顶部；下方为分阶段历史验收记录。

## 初始部署验收

- 源码构建安装成功，`import czsc` 返回版本 1.0.2。
- 原模拟入口运行成功：4260 根模拟 K 线，识别 49 笔（现通过 `--demo` 调用）。
- 命令行帮助、分析及质量检查均退出成功；39 个运行依赖兼容性检查通过。
- 单周期和多周期 HTML 均已生成并解析验证，分别包含 1 和 3 个周期面板，每面板 400 根可视 K 线。
- 当前 pandas 的字符串类型为 `str`，上游质量检查描述仍期望旧式 `object` 类型，因此存在类型描述提示；缺失值、时间顺序、价格与重复记录检查未报异常。分析和图表生成未受影响，未修改上游校验规则。
- 自动浏览器的安全策略禁止打开 `file://`，因此未执行浏览器视觉验收，也未验证公网 CDN 在浏览器中的加载。请双击入口或直接打开生成的 HTML 查看。
- 项目源码 Git 状态干净。运行版本记录见 `.local-deploy/requirements.lock`，安装记录见 `.local-deploy/install.log`。

## 真实行情接入验收（2026-10-22）

- 双击入口已切换到真实行情，实跑贵州茅台，获取 640 根已收盘日线（2024-02-29 至 2026-10-21），识别 45 笔。
- 2026-10-22 未收盘日线已排除，最新报价另有准确时间戳。
- 实际 CSV 通过项目原有质量检查，价格、成交量、成交额、缺失值、日期顺序、重复记录和类型检查未报异常；该结果目录中另存 `quality.json`。
- 通过当日原始日线与最新报价核对手/股和万元/元换算；生成的真实图表包含正确标的和 400 根展示 K 线。
- 7 项离线边界测试通过，覆盖单位换算、未收盘过滤、复权口径缺失、数据缺失、重复/未来日期、非法价格、代码格式和陈旧报价。
- 仍未进行浏览器视觉验收（本地文件访问策略限制）；行情范围先限定为沪深单标的日线和报价快照，不含分钟行情或持续推送。

## 默认切换为大盘（2026-10-22）

- 默认上证指数 `sh000001`，使用不复权指数点位；实跑 640 根日线，截至 2026-10-22，识别 48 笔。
- 当日收盘 3658.03 点。图表 MACD(12,26,9) 与完整输入序列计算一致：DIF -56.3053、DEA -43.3622、柱值 -25.8862。
- “钝化消失/结构形成”的周期、参考峰谷和确认规则尚待用户确认，不把缠论的笔或 MACD 拐头直接当作同一个信号。

# Weather99

上海浦东机场最高温 × Polymarket 温度市场研究项目。现有 FMZ 策略规格保留在 `doc/`，PolyWeather 作为固定版本的数据与分析底座放在 `vendor/PolyWeather` 子模块中。

已实现四模型采集、60 天滚动偏差校准、540 天岭回归、正态整数温度桶、组合概率/成本/优势筛选、分层门槛、账户风控、连续确认与 SQLite 审计。研究信号计算和历史回放共用纯函数，原始数据、模型和配置都有版本记录。

详细公式、模块边界、文档事实与研究假设见 [模型实现与程序设计](doc/模型实现与程序设计.md)。

## 启动

需要 Python 3.11+ 和 Git；采集入口使用标准库，岭回归使用 NumPy。从项目根目录运行：

```powershell
git submodule update --init --recursive
python -m pip install -e .
python -m weather99 collect --date 2026-11-18
python -m weather99 demo --database runtime/demo.sqlite3
python -m unittest discover -s tests -v
```

默认日期按北京时间计算。Open-Meteo 当前请求覆盖当地今天起三天；指定历史或更远日期时，预报会明确缺失，不以当前预报冒充历史预报。快照写入 `runtime/snapshots/`（Git 忽略），每次运行生成唯一文件。退出码 0 表示本轮采集完整，2 表示数据缺失或外部请求失败，仍保留快照。`data_ready` 只是采集完整性标记，未判断观测新鲜度、市场结算规则、深度或策略入场条件。

网络请求读取系统/环境代理设置（`HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`），使用证书校验、20 秒超时和有限重试。HTTP 451、403 等错误会记录并停止该源重试。

## 和风天气

已接入和风天气的[当前天气](https://dev.qweather.com/en/docs/api/weather/weather-current/)，使用专属 API Host 与请求头认证。环境变量模板见 `.env.example`；程序读取启动进程的环境变量，不自动加载 `.env` 文件。

在同一个 PowerShell 窗口中配置后运行（以下内容是占位符）：

```powershell
$env:QWEATHER_API_HOST = "你的专属域名.qweatherapi.com"
$env:QWEATHER_API_KEY = "你的APIKey"
python -m weather99 qweather
python -m weather99 collect --date 2026-11-18 --with-qweather
```

也支持 JWT：设置 `QWEATHER_JWT`，并清除 `QWEATHER_API_KEY`，二者只配置一个。已配置的本机可直接读取 Git 忽略的 `runtime/credentials/qweather.json` 和 Ed25519 私钥，每次请求自动生成 15 分钟有效的 JWT；显式环境变量优先。其他机器使用本地签名功能时安装 `python -m pip install -e ".[qweather-jwt]"`。[认证说明](https://dev.qweather.com/en/docs/configuration/authentication/)、[API Host 获取方法](https://dev.qweather.com/en/docs/configuration/api-host/)。凭据不放进 URL 或快照；带认证的请求拒绝跟随重定向。

默认位置固定为**上海浦东国际机场 ZSPD**，坐标为纬度 **31.1443**、经度 **121.8083**（沿用项目策略规格）。位置统一定义在 `weather99/locations.py`，主采集与和风查询共同使用。快照中的 `location` 记录机场身份；和风输出用 `reference_station=ZSPD` 表示查询坐标所属机场，`data_type=coordinate_grid_weather` 表示该数据仍为网格天气。

`qweather` 命令默认查询机场区域 **5 个采样点**的当前温度，点位定义在 `weather99/locations.py`。这是一组围绕机场参考坐标的固定采样点，不是 5 个官方观测站：

| 点位 | 纬度 | 经度 |
|---|---|---|
| 中心 | 31.1443 | 121.8083 |
| 北侧 | 31.1543 | 121.8083 |
| 南侧 | 31.1343 | 121.8083 |
| 东侧 | 31.1443 | 121.8183 |
| 西侧 | 31.1443 | 121.7983 |

每次多点采集共请求 5 次当前天气接口，最多并发 2 个。`points` 保留各点的独立结果和原始响应，`summary` 给出当前温度的空间范围、均值及温差。只有当前天气有效时间一致，才计算这些汇总值；时间不同则汇总值为 null。点位缺失时保留其他点的数据，明确显示有效点数及错误，整体 `complete=false`。相邻网格数据属于同一提供方，不作为 5 个独立模型参与融合，也不替代 ZSPD METAR。

加 `--single-point` 可只查中心点；同时指定 `--latitude`、`--longitude` 可查询一个自定义坐标。自定义坐标不匹配机场中心时不会挂浦东机场标识；数据时间使用接口返回的有效时间。所有采样点的当前温度均有效时退出 0，否则退出 2。未配置凭据时不会发起请求。

配置完整后，主 `collect` 命令自动附带和风天气的这 5 个点，结果和原始响应保存在 `auxiliary_weather.qweather`，多点格式标记为 `weather99.qweather.airport.v2`；未配置时维持原有采集。显式 `--with-qweather` 会要求配置存在，并在和风数据不完整时返回退出码 2。辅助源错误保存在其自身 `errors` 字段，原有 `data_ready` 只描述四模型/机场实测/盘口主链路。控制台省略各点的原始响应，主采集快照完整保留。

和风数据不填充四模型向量，不冒充机场 METAR 或最终结算值，不改变岭回归特征。主采集和模拟盘另查询机场**中心一个点**的三天逐日预报（每轮额外 1 次请求），保留最高/最低温、昼夜天气、提供方有效区间和采集时间；不把采集时间冒充预报发布时间。预报失败独立标记，不影响已成功的当前天气。来源署名保存在 `attributions`，展示数据时同时展示。当前天气始终标记 `station_verified=false`。

当日评估增加 `intraday_residual_v1` 研究校正：用 ZSPD 常规 METAR（优先 T 段）和完整、同有效时间的和风五点均温，分别减去同一时刻四模型小时温度的等权均值（整点间线性插值）。仅使用本日、不晚于快照且不超过 60 分钟的观测；缺少任一小时模型则该源不参与。中心调整为 `clip(0.5 × (0.75 × METAR偏差 + 0.25 × 和风偏差), -2, 2)`，缺失源贡献为零，之后预测中心不低于当日已观测最高温。误差尺度保持原值。这些权重、增益和限幅是**未经回测的研究假设**，并非已验证的精度提升；不把当天偏差带到明天。原始中心、每源时间与偏差、校正后中心及参数写入评估记录，校正版本参与连续确认；旧记录不会追溯改写。和风逐日预报单独展示，不自动加入该残差校正。

## 市场结算依据

每次 `collect` 根据当前 Gamma 事件的实际 `resolutionSource` 和规则识别结算口径。当前上海市场指定 AviationWeather 的 ZSPD 常规 METAR：采集覆盖上海自然日零点至当前时刻的报文，优先读取 `RMK Txxxxxxxx` 中的十分之一度温度；无有效 T 段时读取正文温度，排除 `SPECI`，保留最新更正报文。最高值按最近整数取整，恰好半度向上（包括 -8.5 → -8）。不能以 JSON 的整度 `temp` 代替 T 段。

快照 `settlement` 保存规则全文、来源链接、有效报文、当日最高温、取整温度及状态，`sources.settlement` 保留原始响应。网页“市场结算依据”展示这些数据；未来日期显示等待目标日，未支持的规则暂停新增入场。当天策略的观测下界使用结算口径最高温，缺失或过期时暂停。日内数据为暂定值，观察到次日首份常规报文也不等于市场已裁定；模拟账本仍仅按 Polymarket 最终 resolved 结果支付。

规则中的 Weather Underground 兜底暂未自动接入（本机页面返回 HTTP 403），不会用和风、预报或其他站点补值。超过 AviationWeather 可查询范围的历史日期可能无数据；当前模块不提供最终结算历史回填。此前“METAR 仅作辅助观测”的说明不适用于这批已明确指定 METAR 为主结算源的市场。旧快照不会被补写成已采集。

## 训练与研究信号

准备带预报发布时间和正式结算可用时间的四模型历史样本；格式见 `examples/history.sample.json`。该文件的数值仅用于展示格式，`examples/risk.sample.json` 也是虚构账户状态，均不应当作真实数据输入。默认要求最近 60 个日历日有 60 条完整、已结算样本；540 日窗口最多使用 540 天历史，各时间层分别训练。

```powershell
python -m weather99 train --history runtime/history.json --as-of 2026-11-17T08:00:00+08:00 --layer 24h --output runtime/models/20261117-24h.json
python -m weather99 validate --history runtime/history.json --days 180 --output runtime/validation.json
python -m weather99 analyze --snapshot runtime/snapshots/CAPTURE.json --model runtime/models/20261117-24h.json --layer 24h --risk runtime/risk.json --database runtime/weather99.sqlite3
```

`analyze` 可省略 `--risk` 查看计算，但会收到 `risk_state_missing` 否决。模型训练时间不能晚于快照，日内与隔日模型不能混用。源错误、未知桶结构和不完整分区会拒绝进入评估。配置在 `config/strategy.json`，新增假设集中在 `research_assumptions`。

`demo` 不需要网络或历史训练集，直接回放文档里的中心与误差尺度，查看各组合的拒绝原因。它不声称已复刻未公开的 FMZ 数学公式。完整评估、输入、模型、配置及事务事件保存在 SQLite；控制台只输出摘要。重复回放不会增加连续确认次数；需从头按时间回测时使用独立数据库。

命令执行成功退出 0；采集不完整或输入无效退出 2。研究信号被策略或风险拒绝属于正常计算结果，`analyze` 仍退出 0，应读取 `status` 和 `reasons`。

## 持续模拟盘

### 网页监控

双击 `start.bat`，选 **4 打开网页监控**，会启动本地服务并打开 <http://127.0.0.1:8765>；也可执行 `start.bat web`。采集进程仍由菜单 **1** 启动、**3** 停止；网页每 10 秒读取本地记录，采集每 3 分钟运行，页面保留各数据源的真实时间。也可在终端运行 `python -m weather99 web --port 8765`。

界面参考仓库内保存的 FMZ 页面规格，包含运行概览、模拟账户权益曲线、逐日入场评估、浦东五点天气、四模型预测、真实订单簿、模拟持仓和历史日志。天气、盘口来自实际采集；模型概率属于估计；资金、成交属于本地模拟账本。未采集、无报价、无成交均显示为空，不制造样例数据。历史曲线按账本现金流和当时持仓估值还原，仅展示最近 240 个采集时间点。

网页服务仅监听本机 `127.0.0.1`，接口只读，不提供实盘下单或凭据查询。停止采集后网页保留最后数据并显示离线状态。关闭网页不会停止后台采集或网页服务。

Windows 下只需双击根目录的 `start.bat`，在统一菜单中选择 **1 启动、2 查看状态、3 停止、0 退出菜单**。每次操作后会返回菜单；退出菜单不会停止后台进程。脚本使用自身所在目录，不要求事先打开终端；优先使用 `.venv` 中的 Python，其次使用系统 Python（要求 3.11 及以上），缺少依赖时自动安装。需预先配置和风凭据并开启代理。重复启动会检查账户锁，已有进程时只显示状态，不重复运行。启动窗口关闭后模拟进程继续运行，重启电脑后需要重新启动。当前启动的是模拟盘采集程序，尚无网页监控界面。

后台输出和错误日志保存在 `runtime/paper/runner-*.log`。命令行可用 `start.bat start`、`start.bat status`、`start.bat stop` 直接执行对应操作并退出，失败时返回非零退出码。底层 PowerShell 实现位于 `scripts/paper.ps1`。

```powershell
python -m weather99 paper run
python -m weather99 paper status
python -m weather99 paper status --human
python -m weather99 paper stop
```

`paper run` 是持续前台进程，也可由后台进程启动。它使用程序专用代理 `runtime/network.json`（本机已配置 `http://127.0.0.1:20808`），不修改系统代理；设置 `WEATHER99_PROXY_URL` 可覆盖这个地址。代理请求失败时记录阻塞，禁止以直连或虚构盘口替代。

默认参数在 `config/paper.json`：初始模拟现金 50 USD，每个温度桶 5 份、每组投入含预留最多 5 USD、最多 2 个未结算市场、每 180 秒（3 分钟）扫描今天和明天。持仓未结算时持续跟踪对应旧日期。每轮和风只采集机场 5 点一次，共 5 次当前天气请求。今天和明天的市场快照共享同轮采样。原始五点采样另存 `runtime/paper/weather/`。

状态中的 `latest_weather.valid_times` 保留接口实际有效时间，`observation_time_changed` 只在这些时间发生变化时为 true；轮询成功不表示提供方发布了新观测。每轮仍保存快照，便于检查网络和数据更新时间。

目前没有自有可用的历史训练模型，因此运行模式明确为 `equal_weight_baseline`：使用当前四模型等权中心，误差尺度为 `sqrt(1.5² + 0.072 × 分歧²)`，**未做历史偏差校准或岭回归训练**。它用于先验证模拟流程，不等同于文档增强模型的准确率。若要使用自有训练制品，将 `model_mode` 改为 `trained`，在 `trained_models` 中按 24h/12h/6h/3h 提供制品路径；该模式缺少对应模型时阻塞，不静默退回基线。

模拟调度按事件 API 返回的 `endDate` 剩余时间选择 3h/6h/12h/24h 门槛（超过 12h 使用 24h），这是本项目的调度假设。执行核心沿用 `evaluate()`，账户风控来自模拟账本。两次合格扫描须同配置、模型与组合；缺失/无效扫描会重置连续确认。

通过连续确认后，按真实卖盘深度模拟购买等量 Yes 份额；检查最小订单份数、总预算、可用现金和深度调整后的净优势。采用每组每份 0.015 USD 的固定费用/滑点预留，入场时计入已实现费用。同一市场日最多入场一次，不做自动加仓。原始深度不够时不成交，不假设无限卖一深度。这里只模拟静态快照撮合，没有模拟延迟、排队和其他参与者的盘口变化。

持仓按可卖买盘深度估值；缺失、过期或不足的买盘按未知部分零价值保守记账，并标记 `valuation_complete=false`、暂停新增仓位。只有事件全部温度档已 closed、`umaResolutionStatus=resolved`、各 Yes 价格为 0/1 且恰有一个胜出档，才按胜出结果模拟结算。未最终结算、取消或退款型结果不会猜测支付。

资金使用整数微美元记账，入场扣款和建仓同事务提交，结算幂等；进程重启保留账户与持仓，不能靠修改初始金额重置既有账户。单实例文件锁避免重复运行同一个模拟账户。

运行状态：`runtime/paper/status.json`；模拟资金/持仓/估值/扫描日志：`runtime/paper/paper.sqlite3`；信号与配置审计：`runtime/paper/audit.sqlite3`；原始快照：`runtime/paper/snapshots/`。`stop` 发出停止请求，当前请求完成后退出；主机休眠或进程结束时扫描不会继续，本阶段不安装系统自启动服务。

所有交易行为仅落本地模拟账本，外部接口只执行 GET 数据读取。`execution_enabled=false` 表示实盘执行关闭。此阶段不自动调参以制造入场；门槛不满足时维持现金。

## 底座与边界

- 上游：[yangyuan-zhen/PolyWeather](https://github.com/yangyuan-zhen/PolyWeather)，固定提交 `d3a02039208f88860cbe02e0846a8a23783b220a`。目前直接读取其城市注册表中的 ZSPD 站点；上游完整应用、DEB 引擎及运行说明保留在子模块，尚未接入本项目策略。
- 上海坐标、四模型与小时要素取自 `doc/strategy_680665_config.json`。其中的校准参数、持仓和绩效是历史快照，不作为实时输入。
- [Open-Meteo API](https://open-meteo.com/en/docs) 返回各模型逐小时气象要素及日最高温，四模型齐全才计算等权中心。
- [Polymarket Gamma API](https://docs.polymarket.com/api-reference/events/get-event-by-slug) 按上海日期 slug 获取事件，保留事件结算源和完整规则；按 outcomes 映射 Yes token，向 CLOB 请求真实盘口，取最高 bid / 最低 ask，不依赖盘口排序，不把 Gamma 价格当成交价。
- METAR 来自 [AviationWeather API](https://aviationweather.gov/data/api/)，只作为观测数据保存，不能直接冒充最终结算最高温。
- 已实现可审计的独立研究模型。FMZ 的精确概率族、回归特征、融合公式、权重求解和完整成本模型未公开，替代实现逐项写在设计文档中；尚未接入 PolyWeather DEB 核心。
- 历史 forecast vintage / 正式结算数据需导入文件，尚未自动回填；已支持本地模拟账本与按事件截止时间分层扫描，未启用实盘下单。`execution_enabled` 始终为 `false`。

## 本机验证（2026-11-17，北京时间）

Open-Meteo 四模型和 ZSPD METAR 已返回；四模型最高温分别为 17.7 / 16.9 / 17.3 / 17.1°C，等权中心 17.25°C。本机 Gamma 请求发生 TLS 中断；CLOB 连接探测返回 HTTP 451。因此当前环境尚未取得实时盘口，采集结果正确标记为不完整。连接恢复后可直接重新运行采集命令。

## 许可证与来源

本项目代码采用 AGPL-3.0-only，全文见 `LICENSE`。PolyWeather 保留在独立子模块，版权及其附带说明归上游作者所有；来源与版本记录见 `NOTICE.md`。上游品牌、域名、生产服务、私有模型及数据库不包含在本项目中。

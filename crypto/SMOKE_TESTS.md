# 冒烟脚本索引

本文件是仓库内所有 `_smoke_*.py` 脚本的统一索引。**下次做任务前，先查这里**——
避免重复造脚本、避免误跑会写库/发信/调真实 API 的脚本。

> 技术文档 `crypto/task/定时任务量化交易系统技术文档.md` 第 532 行已把
> `_smoke_rg2.py` / `_smoke_dual_position.py` / `_smoke_fix_regression.py` 列为
> 「改动交易链路后应全部重跑」的常驻回归脚本。本索引补全其余 28 个。

## 安全等级

| 等级 | 含义 | 能否随便跑 |
|---|---|---|
| 🔒 纯离线 | 不连 DB、不调 API、不发邮件（内存沙箱 / 临时文件 / 日志文件） | ✅ 随时 |
| 🛡️ 写库 + 还原 | 写 DB，但 finally 无条件恢复快照（或自清理插入的行） | ✅ 可随时（生产调度器并发可能干扰断言） |
| ⚠️ 写库 + 租约 | 写 DB/KV，需取维护租约暂停生产巡检 | ⚠️ 生产环境跑要避开巡检高峰 |
| 🌐 连真实 API | 调 OKX 接口 | ❌ 实盘时段慎跑 |

## 🔒 纯离线（18 个）

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `_smoke_strategy_gate.py`（根目录） | 策略计算锁（`crypto/task/strategy_gate.py`）—— 两种导入身份共用同一把锁、两种降级方式 | 改 `strategy_gate.py` 或 `builtins` 传递方式 |
| `crypto/_smoke_config_save_flag.py` | 配置写库失败如实上报（问题#7）—— `save_config` 返回 DB 主存结果、`last_save_db_ok()` 一次性消费、失败时文件仍落盘 | 改 `real_strategy_adapter.save_config` 或 `app._coin_cfg_warn()` |
| `crypto/_smoke_longtermism.py` | 长期主义 v1.1（`plan_routes._calc_presence_days`）—— 在场天数去重、最佳连续 | 改 `plan_routes.py` 在场/连续天数逻辑 |
| `crypto/_smoke_sysmon.py` | 内存自监控 + 系统监控（`memory_watchdog` / `system_monitor`）—— RSS 读取、JSONL 写入、状态字段 | 改 `memory_watchdog.py` 或 `system_monitor.py` |
| `crypto/_smoke_web_auth.py` | Web 访问闸门（问题#1）—— 41 场景覆盖判定矩阵（未配口令=只放行回环、配口令=回环也要过口令）、cookie 存 HMAC 而非口令原文、口令来源优先级与文件回退、`safe_next` 挡开放重定向，并用自建极小 Flask app 验真接线（403/401 JSON与登录页、不发/发 cookie、HttpOnly+SameSite=Lax、`?token=` 引导后抹掉地址栏口令、Bearer、登出、静态资源免鉴权） | 改 `crypto/web_auth.py` 任何判定分支或 `init_app` 里的钩子；**注意它不导入 `crypto.app`**，闸门在真 app 上的接线要手工在线验（启服务后跑一轮 HTTP，见《闸门改动的在线验证步骤》） |
| `crypto/task/_smoke_cl_ord_id.py` | 下单幂等打标与「结果未知」反查（问题#4）—— 29 场景覆盖 clOrdId 格式/打标全覆盖/异常后 found 不重发、absent 可重发、unknown 停手需人工核实 | 改 `trade_executor.py` 的 `gen_cl_ord_id` / `probe_*_by_*_id` / `execute_trade` / `execute_reduce_only_order` / `execute_chase_limit_order`，或新增下单出口时 |
| `crypto/task/_smoke_dir_lock.py` | 人工方向锁定语义（2026-09-01 事故修复）—— 8 个场景覆盖 dual/single/页面锁定 | 改 `trend_range_trader.py` 方向锁定分支 |
| `crypto/task/_smoke_dual_position.py` | 双仓位架构离线（**常驻**）—— 趋势/区间/减仓/对账/配置校验 | 改 `position_order_manager.py` 或 `trend_range_trader.py` 挂单/账本链路 |
| `crypto/task/_smoke_fix_regression.py` | 审计修复回归（**常驻**）—— fx01~fx16 撤单竞态/仓位上限/人工强平/粉尘清零 | 改挂单管理器或交易器任何方法 |
| `crypto/task/_smoke_leverage_guard.py` | 杠杆交易所回核（2026-09-01）—— 8 个场景覆盖缓存/回核/重设/cross-isolated 分键 | 改 `_set_leverage_if_needed` |
| `crypto/task/_smoke_manual_close_detect.py` | 手动平仓检测 `_detect_external_close`（临时） | 改 `trend_range_trader._detect_external_close` |
| `crypto/task/_smoke_manual_override_regression.py` | 人工干预三问题回归 —— 幽灵挂单/强制方向持续期/加仓吸收/对账缩减 | 改挂单管理器 `reconcile` 或 `_reconcile_slot` |
| `crypto/task/_smoke_okx_ratelimit.py` | 只读接口限频/退避（问题#8）—— 41 场景覆盖开关直通、令牌桶、50011 退避、网络抖动重试、本地 bug 不重试、等不到令牌抛 RateLimited、环境变量覆盖，并静态核对 10 个文件（1 个自身除外）“只读已接线 / 下单未被包” | 改 `task/utils/okx_ratelimit.py`，或改 `trade_executor` / `api_routes` / `market_scanner` / `star_market` / `batch_trend_updater` / `instrument_spec` / `alert_monitor` / `app.py` / `plan_routes` 里 OKX 接口调用方式（尤其新增只读接口、或给下单加包层时）；**新增调用 OKX 的模块必须加进 `_TARGETS`** |
| `crypto/task/_smoke_order_step.py` | 合约下单步长三档（2026-09-01 实盘故障）—— XRP/NEAR/POL 三档规格 | 改 `InstrumentSpecCache.steps()`/`quantize()` 或挂单 `_q()` |
| `crypto/task/_smoke_order_ttl.py` | 挂单 TTL 到期撤销（临时）—— 超龄撤单/未超龄保留/部分成交入账 | 改 `poll_fills` 或 TTL 分支 |
| `crypto/task/_smoke_rg2.py` | 反向持仓风控强平（临时）—— 成功/失败分支、通知计数 | 改反向持仓风控分支 |
| `crypto/task/_smoke_smart_reduce.py` | 智能减仓 —— `classify_reason` 优先级、0.5U 折算、`_close_bucket_smart` 分支 | 改 `_close_bucket_smart` 或 `classify_reason` |
| `crypto/task/_smoke_trading_runtime.py` | 实盘调度器启停竞态（#6）+ 重启自愈记账（#5）—— 17 场景覆盖收尾窗口拒启、finishing 三态、开关×期望状态组合 | 改 `scheduler.py` 的 start/stop/get_trading_status/schedule_auto_resume，或改 `trading_runtime_repo.py`（桩靠 `sys.modules` 顶掉 `trend_range_trader`，一旦改成模块级 import 桩会失效） |

## 🛡️ 写库 + 还原（10 个）

所有脚本都遵循「快照→清场→finally 无条件恢复」的隔离策略。前置条件：`CRYPTO_DB_URL` 环境变量。

| 脚本 | 覆盖模块 | 何时重跑 |
|---|---|---|
| `crypto/_smoke_analysis_record.py` | 实盘分析记录（批次10）—— 建表 + CRUD + 复盘回填 + 统计 | 改 `analysis_record_repo.py` 或 `TaskAnalysisRecord` 模型 |
| `crypto/_smoke_balance.py` | 账户余额历史（批次4）—— `balance_repo` + `api_routes` 快照/回溯辅助函数 | 改 `balance_repo.py` 或 `BalanceHistory` 模型 |
| `crypto/_smoke_calorie.py` | 热量模块 HTTP API —— `calorie_bp` 全部路由 | 改 `calorie_routes.py` 或 `CalorieFood/Record/MealItem/Config` 模型 |
| `crypto/_smoke_config_store.py` | 策略配置/币种自选 kv_store（批次7a）—— round-trip/缺省/损坏/覆盖/删除 + 切库兜底 | 改 `config_store_repo.py` 或 `real_strategy_adapter` 切库逻辑 |
| `crypto/_smoke_journal.py` | 随笔复盘 HTTP API —— `journal_bp` 全部路由 | 改 `journal_routes.py` 或 `JournalTag/Note/NoteTag` 模型 |
| `crypto/_smoke_kv_cache.py` | 缓存类 kv_store（批次8）—— `instrument_spec_cache` / `market_scan_cache` 切库 + 双写 | 改 `config_store_repo.py` 缓存键、`instrument_spec.py`、`market_scanner.py` |
| `crypto/_smoke_market_csv.py` | 行情 CSV（批次7b）—— `crypto_coins` / `star_market` 整表/排序/中文键/文件兜底 | 改 `market_data_repo.py` 或 `CryptoCoin/StarMarketRow` 模型 |
| `crypto/_smoke_plan.py` | 任务计划 HTTP API —— `plan_bp` 全部路由 | 改 `plan_routes.py` 或 `PlanPlan/Card/Slot` 模型 |
| `crypto/_smoke_trade_journal.py` | 结构化成交流水（批次6）—— `record_fill` / `read_journal` / `classify_reason` | 改 `trade_journal_repo.py` 或 `TradeJournal` 模型 |
| `crypto/_smoke_trader_state.py` | 交易运行时状态（批次5）—— `trader_state_repo` + `DualPositionOrderManager` / `TakeProfitEngine` 持久化链路 | 改 `trader_state_repo.py` 或 8 张状态表模型 |

## ⚠️ 写库 + 租约（3 个）

| 脚本 | 覆盖模块 | 何时重跑 | 关键约束 |
|---|---|---|---|
| `crypto/_smoke_discipline.py` | 分析纪律（批次11）—— DB 增量迁移 + 配置读写 + 闸门判定 + 看板只读 | 改 `discipline_repo.py` 或 `task_analysis_records` 表结构 | 唯一写 kv_store 配置，测完还原 |
| `crypto/_smoke_discipline_http.py` | 分析纪律 HTTP 全部蓝图路由 | 改 `discipline_routes.py` 或闸门/台账/豁免逻辑 | 必须 `CRYPTO_NO_BACKGROUND=1`（后台线程会真发信）；取巡检维护租约 |
| `crypto/_smoke_discipline_mail.py` | 分析纪律邮件链路 —— 缺口信/断档汇总/日报/策略行情表 | 改 `task/monitor/analysis_discipline.py` 或邮件渲染 | 接管发信出口落盘到 `data/mail_preview/`，**脚本内无真发分支**；取巡检租约 |

## 🌐 连真实 API（1 个）

| 脚本 | 覆盖模块 | 何时重跑 | 备注 |
|---|---|---|---|
| `crypto/_smoke_pos_history.py` | 主账号持仓盈亏查询（OKX 官方接口） | 改 `ApiUtils/account_query_utils.py` 或分页逻辑 | **查询工具，非回归测试**；调真实 OKX API |

## 🔒 脚本的四类假失败（一次性修掉，别再犯）

2026-09-09 排查，四个 🔒 脚本因历史迁移而长期红/静默失效，均为**脚本自身**问题，
生产代码零改动：

- `crypto/_smoke_longtermism.py`：用 `spec_from_file_location` 顶层加载 `plan_routes`，
  而它内部是 `from .database import ...` 包相对导入 → 必 ImportError。改 `import crypto.plan_routes`。
- `crypto/task/_smoke_order_ttl.py`：`M.__new__(M)` 手工桩漏了 `spec_cache`，被测函数
  访 `self.spec_cache` 直接 AttributeError。桩的接口面要跟着被测代码的依赖清单走。
- `crypto/_smoke_sysmon.py`：读/写真实告警冷却状态文件 —— 30 分钟内真发过告警则「应发
  邮件」必假失败，更严重的是脚本 stub 会写脏真文件、把消费端真告警压掉 30 分钟。改指
  临时路径；并在顶部 reconfigure stdout 为 UTF-8（告警主题带 🚨，GBK 控制台直接崩）。
- `crypto/task/_smoke_smart_reduce.py`：拿本地 `config_trend_range.json` 的内容做断言，
  但策略配置已迁 MySQL kv_store，快照文件早不是真相 → 改验代码层缺省行为。另一坑：
  `FakeSpecCache` 只实现了 `get_spec`/`usd_to_contracts`，而生产代码走 `steps()/quantize()`，
  缺方法被 `except` 吞成兜底 (0.1, 0.1) —— 只要被测函数用 try 包着，桩必须实现**真实接口**
 而不是“够用”，否则测的是兜底路径而不是目标分支。

> 通则：这些脚本都用了「首次失败即 `sys.exit(1)`」的 `ok()`，一旦前面卡住，后面的
> 用例**从未跑到**——修好第一个坏点后必须复跑全量，才会暴露被掩盖的第二个。

### 还有一类更危险：假完备（绿灯不等于验全了）

`_smoke_okx_ratelimit.py` 的静态核对报 41/41 全绿，事后发现 `alert_monitor.py` 与
`crypto/app.py` 共四处只读接口**完全没接线**：因为 `_TARGETS` 只列了 7 个文件，而且
`_CLIENT` 只认变量名（`account_api.get_positions(`），认不出链式直调
（`MarketData.MarketAPI(flag=flag).get_ticker(`、`get_account_api(a).get_positions(`）。
2026-09-10 补了清单与正则，并用一个临时脚本做**反向验证**（把六种裸调用形状喂给
正则，确认逐个能咬、四种已包形式不误报）才收工——静态扫到 0 命中与根本没扫到，
在输出上长得一模一样。

## 闸门改动的在线验证步骤（改 `web_auth.py` 后补做一遍）

`_smoke_web_auth.py` 故意不导入 `crypto.app`（那会拉起调度器与 DB 预热），它只能证明
闸门自身逻辑对，证明不了“挂在真 app 上确实生效”。两边都验才算改完：

```powershell
$env:CRYPTO_NO_BACKGROUND='1'; $env:CRYPTO_WEB_PORT='5111'; python app.py   # 后台起临时实例
```

然后至少打四个点：未登录 `GET /` → 401 登录页；`POST /auth/gate` 带错口令 → 401 且不发
cookie；带对口令 → 302 到 `next` 且 `Set-Cookie` 含 HttpOnly；登录后 `GET /` 与
`/plan`、`/api-console`、`/api/task/status` 都不再是 401/403（验蓝图路由与模板/静态资源
没被误伤）。2026-09-10 就是这么跑过一轮 11 项全绿的。

## 已知缺口（待办）

- [ ] 公共断言 helper `ck()` / `_title()` / `FAILS` 在至少 4 个文件里各自复制
  （`_smoke_discipline_mail.py`、`_smoke_discipline_http.py`、`_smoke_strategy_gate.py`、
  `_smoke_discipline.py`），可考虑抽到 `crypto/_smoke_util.py`
- [ ] 没有统一 runner；想跑全部 🔒 脚本得手敲 18 条命令
- [ ] `_smoke_pos_history.py` 不是严格意义的冒烟测试，是查询工具，可考虑挪到 `demo/` 或重命名

## 维护约定

- 新增 `_smoke_*.py` 时，**必须**在对应分组表里加一行，至少写清"覆盖模块"和"何时重跑"
- 如果脚本会写库/发信/调真实 API，**必须**在 docstring 顶部写明，并在本索引放到对应分组
- docstring 里建议记录"曾经踩过的坑"（参考 `_smoke_discipline_mail.py` 第 3-21 行的事故记忆，
  以及 `_smoke_discipline_http.py` 第 22-24 行关于 `CRYPTO_NO_BACKGROUND` 的由来）

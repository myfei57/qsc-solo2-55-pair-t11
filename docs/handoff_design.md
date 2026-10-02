# 冷热盐罐平滑切换设计

## 1. 背景与问题

旧切换顺序是"先停冷泵、等它真正停稳，再启热泵"。冷泵从基线流量（58 kg/s）
按泵升速斜率退到 0、热泵再从 0 升上来，中间有数十秒母管流量整段下掉，高负荷
时母管压力跟着塌，低流量联锁逼近，安全阀与干烧保护都没有余量。

## 2. 过渡方式（定死，不可选项）

**按流量斜率（flow-share 交叉），时长窗口只作看门狗，不用时间推动状态机。**

切换期间两台泵短时重叠，出泵（冷泵）流量目标按份额 `s` 下降、进泵（热泵）按
份额上升，两者互补：

```
Q_out_target = Q_base · (1 - s)
Q_in_target  = Q_base · s
Q_header     = Q_in + Q_out ≈ Q_base   （实测始终夹在基线带 ±handoff_flow_band_kg_s 内）
s(t)         = s(t-dt) + r_mode · dt   （r_mode 是模式斜率，份额/s）
```

- 交叉**完成判据看实测流量份额**：进泵实测份额 ≥ `handoff_takeover_share`
  （默认 0.9）且出泵实测流量 ≤ 基线的 5%，才允许停出泵。没有"时间到就切"。
- 模式看门狗窗口内条件未达成 → 记 `crossover_window_exceeded`，按**部分失败**
  退回安全位，而不是硬切。
- 协调重叠只发生在切换编排内部，水力模型与母管模型都带 `coordinated` 标志：
  协调重叠不打"两泵对顶"折扣、不叠顶压倍数、不累计 `opposed_steps`；手动并泵
  仍然按对顶模型拒绝/记账，联锁语义不变。

## 3. 模式由罐况裁决，不由操作员选

用罐位余量折算"还能安全对灌多久"：

```
冷罐可抽 = 冷罐存量 - 罐容·(冷罐下限% + 保护带%)
热罐可灌 = 罐容·(热罐上限% - 保护带%) - 热罐存量
t_avail  = min(冷罐可抽, 热罐可灌) / Q_base
```

| 条件 | 模式 | 斜率/窗口 | 含义 |
|---|---|---|---|
| `t_avail ≥ handoff_slow_window_s` | `slow` | 缓斜率 0.02/s，窗口 120 s | 罐况允许，延长窗口平缓过渡 |
| `handoff_fast_window_s ≤ t_avail < slow_window` | `fast` | 陡斜率 0.06/s，窗口 60 s | 罐况紧，快速切换保安全 |
| `t_avail < fast_window`，或冷罐已到下限／热罐已到上限 | `inhibited` | — | 禁止硬切，走别的走法 |

高负荷只在记录里标注（压力更脆），不改变模式归属——**模式严格按罐况**，题目
要求的"按罐况定"落在这一个函数（`cspplant/salt/mode.py::select_mode`）里，
操作员无法指定。

### 极端工况的"别的走法"

`begin` 阶段预检到 `inhibited` 直接返回 `handoff_inhibited_by_tank_margin`
违约，设备一步不动。此时有显式可用的安全走法：

1. **再循环旁路保持**：打开再循环回流阀（`RecirculationValve`，安全位=开，
   失气/失电打开），母管盐量经再循环回路不断流、吸热器不干烧，同时停止
   冷罐→热罐倒灌，为降负荷/等罐位恢复争取时间；
2. **降负荷后重试**：负荷降下来、罐位约束缓解后重新裁决；
3. **保持原状**：冷泵继续运行，不做切换。

## 4. 时序（写死在状态机，任何调用不能跳步）

```
precheck（模式裁决+联锁）
  → valve_closing（回流阀全关，回读确认到位）
  → incoming_starting（进泵零流量并网，份额即开始爬升）
  → crossing（流量斜率交叉，进泵实测份额≥接管份额）  ← 提交点在进泵稳定建流
  → outgoing_stopping（停出泵，回读确认流量归零）
  → settling（流量在基线带、压力在目标带，持续 handoff_confirm_s）
  → done
```

每一步进入下一状态都以**回读条件成立**为准，且顺序在代码结构上不可能颠倒：
没有"先启热泵后关阀"或"未交叉先停冷泵"的路径。手动泵操作在切换进行中一律
被联锁拒绝（`切换正在进行，…由切换编排接管`）。

## 5. 部分失败与安全位退回

回读两类故障：

- **传感器掉线**：回读返回 `None`（`inject_*_fault` 演练）；
- **执行器卡死**：指令继续推进、回读冻结，偏差超过 `pump_ramp·stale_s`／
  `valve_travel·stale_s` 判 `readback_stuck`。

退回方向以**提交点**（进泵实测流量 ≥ 基线 10%，已稳定出流）为界：

| 故障时机 | 处置 | 结局 |
|---|---|---|
| 提交点之前（阀关失败、进泵建流失败、进泵回读丢） | 出泵拉回基线、进泵解列、回流阀打开 | `rolled_back`（退回原状） |
| 提交点之后（交叉／停出泵／确认阶段回读丢） | 出泵解列、进泵拉基线单泵运行，走确认 | 收敛为 `completed`；进泵本身不可信则升级 |
| 进泵回读丢失/冻结（任何阶段） | 两泵全停 + 回流阀打开 | `safe_hold` |
| 退回原状失败（出泵拉不回） | 升级两泵全停 + 回流阀打开 | `safe_hold` |
| 罐在切换中触限、流量跌破低流量联锁门限、窗口超时 | 同上，按提交点方向 | `rolled_back`/`safe_hold` |

回流阀任何时刻回读失败都按其安全位（开）处理。终态必须满足：出泵绝不继续
压在母管上；流量要么由进泵单泵维持在带内，要么两泵全停由回流阀保住循环。

## 6. 过冲记录（每回切换留痕，方便回头比）

`HandoffRecord` 每次切换（含被拒绝、退回、保持）都生成同构一条：

- 流量：`flow_min/max_kg_s`、`flow_overshoot_kg_s`（高于基线）、
  `flow_undershoot_kg_s`（低于基线，旧方案这个值接近整段基线）；
- 压力：`pressure_min/max_mpa`、`pressure_overshoot/undershoot_mpa`；
- 模式与窗口：`mode`、`window_s`、`available_s`、`load_mw`、`duration_s`；
- 过程：5 s 间隔的 `trace`（两泵流量、总流量、压力、回流阀、份额、双罐液位）；
- 违约：`violations` 与 `outcome`（completed/rolled_back/safe_hold/rejected）。

## 7. 台账与审计一致

- 切换事件全部经事件总线发布（`handoff_begin`、阶段推进、
  `handoff_partial_failure`、`changeover_complete`、
  `changeover_safe_position`、`handoff_rejected`），审计
  `AuditLog` 作为全量订阅者统一落盘——切换代码不直接写审计，避免两套口径。
- 台账 `HandoffJournal` 是只增不改的 JSONL（`salt/handoffs.jsonl`，fsync），
  存的是**同一份** `HandoffRecord`。每次切换以 `op_id` 作为 `handoff_id`
  贯穿审计与台账。
- 对账本就放在查询面上：`GET /api/handoffs/verify` 收集审计流水中全部
  `handoff_id`，调 `HandoffJournal.verify_ids` 逐条核对，任何台账缺失直接报
  `missing_in_journal`。
- 掉电恢复：台账追加行原子 fsync；恢复时只允许跳过末尾半截残行，前面记录
  一条不丢。

## 8. 控制台展示

- 熔盐页（`/salt`）新增"切换过程"面板：编号、进行中/阶段、模式及裁决依据、
  看门狗窗口、罐况允许对灌时长、负荷、四项过冲实时值、违约项；进行中用警示
  色，失败用告警色。
- "切换轨迹"多列表格按 5 s 采样回放交叉过程；"切换过冲台账"列最近 12 次的
  流量/压力上下偏离量。
- 异步观察切换：`POST /api/changeover/begin` 发起，之后控制周期
  `POST /api/changeover/step`（或平台 `cycle`）逐周期推进，控制台每一周期都能
  读到中间阶段；`POST /api/changeover/abort` 人工中止退回安全位。同步入口
  `POST /api/changeover` 保留（脚本/演示用）。
- 回流阀操作 `POST /api/recirc`；故障演练 `POST /api/salt/fault`。

## 9. 配置（`limits.salt`，均可现场调，逻辑不改代码）

| 字段 | 默认 | 说明 |
|---|---|---|
| `handoff_flow_band_kg_s` | 4.0 | 切换期总流量允许偏离基线的带宽 |
| `handoff_share_ramp_per_s` | 0.02 | 平缓模式份额斜率（1/s） |
| `handoff_fast_share_ramp_per_s` | 0.06 | 快速模式份额斜率（必须大于平缓） |
| `handoff_slow_window_s` | 120.0 | 平缓看门狗窗口 |
| `handoff_fast_window_s` | 60.0 | 快速看门狗窗口（不得长于平缓） |
| `handoff_takeover_share` | 0.9 | 进泵实测份额接管门限 |
| `handoff_level_guard_pct` | 3.0 | 罐位保护带（模式裁决用） |
| `handoff_readback_stale_s` | 3.0 | 回读冻结/掉线判故时长 |
| `recirc_valve_travel_pct_s` | 10.0 | 回流阀行程速度 |

装配期对全部新字段做存在性/取值/相互关系校验（陡斜率>缓斜率、快窗口≤慢窗口、
接管份额≤1、保护带不和上下限矛盾），半份配置不允许启动。

## 10. 代码地图

| 文件 | 职责 |
|---|---|
| `cspplant/salt/mode.py` | 罐况→模式裁决（slow/fast/inhibited） |
| `cspplant/salt/handoff.py` | 流量斜率交叉状态机、时序、回读部分失败、过冲统计 |
| `cspplant/salt/recirc.py` | 再循环回流阀（安全位开、回读、故障注入） |
| `cspplant/salt/record.py` | 切换记录（过冲/轨迹/结局） |
| `cspplant/salt/handoff_journal.py` | 切换台账 JSONL + 审计对账 |
| `cspplant/salt/pump.py` | 泵运行中改流量目标、流量回读与故障注入 |
| `cspplant/salt/hydraulic.py` | 增加 `coordinated` 协调叠加（不打对顶折扣） |
| `receiver/header.py` | 协调重叠不叠顶压倍数 |
| `cspplant/salt/loop.py` | 回路装配、同步/异步切换入口、切换期 step 委托、对账 |
| `cspplant/app.py` | op_id 贯穿、审计发布、模式/过冲上送 |
| `cspplant/console/{api,pages}.py` | 切换接口、实时面板、过冲台账、对账目 |
| `tests/test_handoff.py` | 平过渡、模式裁决、极端拒绝、回读退回、窗口看门狗、过冲、台账审计一致 |

## 11. 验证结论

默认配置下平缓切换（仿真步长 1 s）：母管总流量全程保持 58.0 kg/s
（下掉/过冲 ≈ 0），压力稳定在 2.5 MPa（峰 2.5、无下塌），两泵在交叉阶段
短时重叠但无对顶记账；旧方案的整段流量断档消除。极端罐况（冷罐见底/热罐
将满）切换被拒绝并给出再循环旁路走法；提交点前后回读故障分别验证退回原状
与推进安全位；窗口超时触发退回而非时间到硬切；台账与审计按 `handoff_id`
逐条对账一致。全部 114 个用例通过。

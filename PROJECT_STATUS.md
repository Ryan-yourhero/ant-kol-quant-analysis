# 蚂蚁财富大V交易采集 - 项目开发进度

> 更新日期：2026-08-04

---

## 1. 项目目标

通过 ADB 控制真实安卓手机，从 **蚂蚁财富 App → 理财盘友圈** 页面自动采集大V交易记录，支持单页解析与自动滚屏遍历，提取结果保存为 JSON/CSV 供后续使用。

---

## 2. 已实现功能总览

| 模块 | 功能 | 完成度 |
|------|------|:------:|
| **config/settings** | 全局配置：ADB参数、关键词、金额/时间正则、输出目录 | ✅ |
| **core/adb_controller** | 设备连接/状态检查、uiautomator dump + pull、滑动/点击/截图/启动App | ✅ |
| **core/xml_parser** | uiautomator XML 解析、按行分组、位置排序、文本提取 | ✅ |
| **core/data_extractor** | 4 种交易类型识别 + 去重 + CANCEL 抑制 + operation_text 快照 | ✅ |
| **core/scroll_manager** | 自动滚屏采集、到底自动停止、唯一键防重复、JSON 落盘 | ✅ |
| **scripts/test_4_ops** | 6 种场景离线测试（无需手机） | ✅ |
| **main.py** | 调试入口：无参自动滚屏；有参单页/离线/CSV | ✅ |

---

## 3. 4 种交易类型支持

已支持 **买入 / 卖出 / 转换 / 撤销** 四种交易识别，并针对撤销识别撤销子类型。

| 类型 | 识别关键词 | 关键字段 |
|------|-----------|----------|
| **BUY 买入** | 买入确认中、买入金额(元)、买入/申购/加仓等 | fund_name, amount, amount_unit(元) |
| **SELL 卖出** | 卖出确认中、卖出份额(份)、卖出/赎回/减仓等 | fund_name, amount, amount_unit(份) |
| **TRANSFER 转换** | 转换确认中、基金转换、转换中、转换成功 | source_fund + source_amount（份）→ target_fund + target_amount（元） |
| **CANCEL 撤销** | 撤销、已撤销、撤单、撤销申请、交易撤销 | cancel_type：BUY_CANCEL / SELL_CANCEL / TRANSFER_CANCEL / UNKNOWN，字段跟随对应类型 |

### 3.1 撤销类型识别规则

- 上下文含「转换」 → **TRANSFER_CANCEL**（同时填 source/target 基金）
- 上下文含「买入/申购/定投」→ **BUY_CANCEL**
- 上下文含「卖出/赎回/减仓」→ **SELL_CANCEL**
- 都没有 → **UNKNOWN**

---

## 4. 数据结构与数据库字段建议

建议新增/使用以下数据库字段（对应 `KolOperation` 模型）：

| 通用字段 | 说明 | 示例 |
|----------|------|------|
| kol_name | 大V昵称 | 稳健老王 |
| timestamp | 时间戳 | 2026-08-04 11:00 |
| action_type | 动作类型枚举 | BUY / SELL / TRANSFER / CANCEL |
| confidence | 提取置信度（0-1） | 0.85 |
| operation_text | 原始多行快照（解析错不用重爬） | 见下一节 |

| BUY/SELL 字段 | 说明 |
|--------------|------|
| fund_name | 基金名 |
| amount | 原始金额字符串 |
| amount_value | 数值化金额（float） |
| amount_unit | 单位：元 / 份 |

| TRANSFER 字段 | 说明 |
|--------------|------|
| source_fund / source_amount / source_amount_value | 源基金 + 卖出份额 |
| target_fund / target_amount / target_amount_value | 目标基金 + 买入金额 |

| CANCEL 字段 | 说明 |
|------------|------|
| cancel_type | BUY_CANCEL / SELL_CANCEL / TRANSFER_CANCEL / UNKNOWN |
| 其余字段 | 根据 cancel_type 复用 BUY/SELL/TRANSFER 字段 |

> **operation_text 强建议保留**：保存人类可读的多行原始操作快照（锚点 + 基金 + 金额），即使解析逻辑后续调整，也可以不重新爬取直接从快照重算。

### 4.1 四种类型 JSON 示例

```jsonc
// BUY 买入
{
  "action_type": "BUY",
  "fund": "朱雀企业优胜股票C",
  "amount": "2,000.00元",
  "amount_value": 2000.0,
  "unit": "元"
}

// SELL 卖出
{
  "action_type": "SELL",
  "fund": "富国中证沪港深创新药ETF",
  "amount": "20,158.54份",
  "amount_value": 20158.54,
  "unit": "份"
}

// TRANSFER 转换（一条记录：源→目标）
{
  "action_type": "TRANSFER",
  "source_fund": "富国中证消费电子主题ETF",
  "source_amount": "2,700份",
  "source_amount_value": 2700.0,
  "target_fund": "广发沪港深精选混合C",
  "target_amount": "4,831.49元",
  "target_amount_value": 4831.49
}

// CANCEL 撤销（示例：转换撤销）
{
  "action_type": "CANCEL",
  "cancel_type": "TRANSFER_CANCEL",
  "source_fund": "景顺长城新兴成长混合",
  "target_fund": "易方达蓝筹精选混合"
}
```

---

## 5. operation_text 快照示例

`operation_text` 字段保存为多行文本，与页面展示对齐：

```
# TRANSFER 示例（完全对应页面展示）
转换确认中
富国中证消费电子主题ETF...
卖出2,700份
转换至广发沪港深精选混合C
买入4,831.49元

# BUY_CANCEL 示例
撤销
华夏沪深300ETF联接C
买入撤销500.00元

# TRANSFER_CANCEL 示例
撤销
景顺长城新兴成长混合
转换撤销 卖出1,200份
转换至易方达蓝筹精选混合
转换撤销 买入5,000元
```

---

## 6. 关键机制

### 6.1 交易唯一键（去重）

对每条记录生成唯一 hash，相同记录不重复保存：

| 类型 | 唯一键组成 |
|------|-----------|
| BUY/SELL | kol_name + timestamp + action_type + fund + amount_value + unit |
| TRANSFER | kol_name + timestamp + TRANSFER + source_fund+source_amount + target_fund+target_amount |
| CANCEL | kol_name + timestamp + CANCEL + cancel_type + fund + source/target + amount |

### 6.2 三层去重 + CANCEL 抑制链路

1. **节点级消费**：TRANSFER / CANCEL 识别成功后，把上下文的「买入金额」「卖出份额」等标签节点标记为 consumed，避免同一个转换又额外生成 BUY + SELL 两条冗余记录。
2. **业务 key 级去重**：extract() 返回前按 6.1 的 key 去重，相同 fund+amount+unit 只保留置信度最高的一条。
3. **CANCEL 抑制**：如果同时存在 BUY 与 BUY_CANCEL 两条记录且基金名匹配，则丢弃原始 BUY（只保留 CANCEL 记录，代表该笔已被撤销）；SELL/TRANSFER 同理。

### 6.3 自动滚屏 + 到底停止判定

单一停止条件（用户要求：滑到底自动停止）：

- **连续 2 次 uiautomator dump 的 XML MD5 完全一致** → 判定页面无法再向上滑动 → **立即停止，不再多滑**
- 兜底：滑动次数 ≥ 50（可用 `--max` 调整）→ 停止

主循环顺序严格为：**collect_page → is_finished 判断是否到底 → 没到底才 scroll_up**，避免到底后多滑一次空滑。

### 6.4 基金打分修正

修复了带数字基金名（如「华夏沪深300ETF联接C」）因 _looks_like_amount 匹配数字 300 导致负分、无法识别的 bug。逻辑：**文本含基金后缀（ETF/联接/混合等）或「基金」字时，即使匹配到数字也不当作金额扣分**。

---

## 7. 目录结构

```
KOL-RICH/
├── config/
│   └── settings.py              # 全局配置：ADB、关键词、金额/时间正则
├── core/
│   ├── adb_controller.py        # ADB 设备控制（连接/dump/滑动/点击/截图）
│   ├── xml_parser.py            # uiautomator XML 解析 / 按行分组
│   ├── data_extractor.py        # 4 种类型提取 / 去重 / CANCEL 抑制 / 快照生成
│   └── scroll_manager.py        # 自动滚屏 / 到底停止 / JSON 落盘
├── scripts/
│   └── test_4_ops.py            # 6 场景离线测试（无需手机）
├── output/                      # 采集结果 JSON（自动创建）
├── dumps/                       # uiautomator XML 备份（可选）
├── logs/                        # 运行日志（可选）
├── main.py                      # 调试入口
├── requirements.txt
└── PROJECT_STATUS.md            # 本文档
```

---

## 8. 运行方式

### 8.1 自动滚屏采集（最常用）

打开蚂蚁财富 App → 进入某大V主页 → 电脑终端运行：

```bash
# 无参：自动跑 ScrollManager，连续2次XML相同即停止，最多50滑
python main.py
```

### 8.2 ScrollManager 自定义参数

```bash
# 自定义：最多100滑 + 慢手机等待1.5s + 连续3次XML相同判到底
python core/scroll_manager.py --max 100 --wait 1.5 --stable 3

# 指定设备序列号
python core/scroll_manager.py -s ABC123DEF456
```

### 8.3 单页采集 / 离线调试

```bash
# 单页采集（不滚屏）
python main.py

# 从本地 XML 离线解析（无需手机，调试 XML 解析/提取逻辑）
python main.py --xml-file dumps/window_dump.xml

# 输出 CSV 格式
python main.py --format csv -o result.csv

# 启动App + 滑3次 + 截图 + 保存CSV
python main.py --launch --scroll 3 --screenshot --format csv
```

### 8.4 离线跑 4 种类型验证（无设备环境）

```bash
python scripts/test_4_ops.py
```

覆盖 6 个场景：`BUY / SELL / TRANSFER / BUY_CANCEL / SELL_CANCEL / TRANSFER_CANCEL`，每个场景都会打印结构化字段 + operation_text 快照。

---

## 9. 测试覆盖结果

运行 `scripts/test_4_ops.py` 的最终输出验证：

| 场景 | action_type | cancel_type | 基金识别 | 金额识别 | operation_text 快照 | 冗余记录被抑制 |
|------|:-----------:|:-----------:|:--------:|:--------:|:-------------------:|:--------------:|
| 1. 买入 | BUY | - | ✅ 朱雀企业优胜股票C | ✅ 2000元 | ✅ 3行 | ✅ 去重后剩1条 |
| 2. 卖出 | SELL | - | ✅ 富国沪港深创新药ETF | ✅ 20158.54份 | ✅ 3行 | ✅ 去重后剩1条 |
| 3. 转换 | TRANSFER | - | ✅ 源+目标 | ✅ 源2700份/目标4831.49元 | ✅ 5行（完全匹配页面格式） | ✅ 无额外BUY/SELL |
| 4. 买入撤销 | CANCEL | BUY_CANCEL | ✅ 华夏沪深300ETF联接C | ✅ 500元 | ✅ 3行 | ✅ 原始BUY被抑制 |
| 5. 卖出撤销 | CANCEL | SELL_CANCEL | ✅ 汇添富全球医疗混合 | ✅ 888份 | ✅ 3行 | ✅ 原始SELL被抑制 |
| 6. 转换撤销 | CANCEL | TRANSFER_CANCEL | ✅ 源+目标基金 | ✅ 源1200份/目标5000元 | ✅ 5行 | ✅ 原始TRANSFER被抑制 |

---

## 10. 下一步建议

- **真实设备联调**：用 ScrollManager 真实手机跑，看 XML 页面布局与离线 mock 的差异（节点行数、标签是否含全角括号、时间戳位置等），微调 data_extractor 的 context 搜索范围。
- **数据库持久化**：按第 4 节的字段建表（SQLite / MySQL 均可），把 `KolOperation.to_dict()` 的 JSON 直接写库。
- **operation_text 复核面板**：做一个简单的前端/脚本，对 confidence<0.5 的记录展示 operation_text 快照，支持人工点选修正 fund/amount。
- **大V列表循环采集**：增加一个大V主页 URL / 昵称列表，外层循环自动切换大V主页后复用 ScrollManager 跑单人大V采集。

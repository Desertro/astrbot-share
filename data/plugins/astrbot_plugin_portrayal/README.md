
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_portrayal?name=astrbot_plugin_portrayal&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_portrayal

_✨ [astrbot](https://github.com/AstrBotDevs/AstrBot) 画像插件 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

## 💡 介绍

根据群友的聊天记录，调用 LLM 分析群友画像，并支持基于画像生成群名片（`取名`）。

## 📦 安装

- 可以直接在astrbot的插件市场搜索astrbot_plugin_portrayal，点击安装即可  

- 或者可以直接克隆源码到插件文件夹：

```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/Zhalslar/astrbot_plugin_portrayal

# 控制台重启AstrBot
```

## ⚙️ 配置

请在astrbot面板配置，插件管理 -> astrbot_plugin_portrayal -> 操作 -> 插件配置

## ⌨️ 使用说明

## ⌨️ 指令表

|     指令      |                    说明                    |
|:-------------:|:-----------------------------------------------:|
| `画像 @群友 [轮数]` | 分析目标用户画像（不指定则默认分析发送者） |
| `取名 @群友 [轮数]` | 先生成画像，再生成新群名片并尝试设置群名片 |

## 🆕 最近更新（2026-02）

### 1) 上下文抓取算法升级

- 从“命中目标后固定前后窗口”改为“目标发言锚点 + 相关性打分筛选”。
- 新增“两阶段抓取”：先尽量凑够目标本人有效发言，再构建参与式上下文；上下文不足时会用目标本人直出语料补齐。
- 新增“mem_chat目标直查优先”：先按 `group_id + /qq:` 在 `memory_embeddings` 中计数与提取；若已达到目标下限，直接返回目标语料，避免在线翻页卡住。
- 自动保留目标回复前 N 条消息（默认 3 条），并控制每块消息上限，减少噪音。
- 结构化 TURN 增加 `qq`/`type`/`reply` 字段，降低 LLM 说话人错归因。
- 目标条数改为“参与式上下文约 400 条（默认允许 ±10）”。

### 2) 检索架构升级（索引优先 + 在线补齐）

- 检索顺序：`mem_chat语义历史 -> 本地消息索引 -> OneBot在线回溯补齐`。
- `mem_chat` 历史优先可直接利用图片转写文本，减少“有发言但抓不到文本”的情况。
- 插件新增本地 SQLite 消息索引（默认 `portrayal_msg_index.db`）。
- 每次在线回溯会自动把消息写入索引；后续画像/取名优先从索引召回，速度更稳定。
- 当索引仍不足时，再自动向前在线回溯补齐。
- 回溯轮数采用“默认轮数 + 硬上限”机制，可覆盖 400+ 轮的长历史场景。

### 3) 画像与取名都支持“当前消息 + 历史消息”

- 画像提示词新增 `【当前消息】` 分段，和 `【历史对话】` 一起输入。
- 取名阶段（ID 生成）会同时参考：
  - 当前消息摘录
  - 历史消息摘录
  - 画像文本

### 4) 取名避免沿用旧群名片

- 取名时优先使用 QQ 昵称（不是当前群名片）作为人物名。
- ID 提示词新增硬约束：忽略当前群名片，不做同字/同结构改写。

### 5) 文转图稳定性增强

- 画像/取名合并卡片渲染前会进行文本清洗与长度截断，降低空白图概率。
- 渲染失败时自动回退为纯文本回复，避免无反馈。

### 6) 图片摘要上下文支持

- 可读取群消息中的图片 `summary`（如 vision_media 已产出的描述）并注入上下文。
- 默认忽略 `[动画表情]` 这类低语义噪音。

### 7) 接入 mem_chat 人物印象基准

- 画像/取名提示词会优先注入 `【mem_chat人物印象基准】`，包含：
  - `impression_profiles_v3` 的摘要与标签
  - `affinity_state` 的关系倾向（affinity/trust/bias）
  - `user_profiles` 的 canonical_text（兜底补充）
- 注入顺序：`mem_chat基准 -> 当前消息 -> 历史对话`，当前消息优先于历史语料。
- 当基准信息与当前消息冲突时，提示词要求模型标注“近期变化”。

## 🔧 关键配置（新增/推荐）

- `context_target_count`: 参与式上下文目标数量（默认 `400`）
- `context_target_tolerance`: 参与式上下文目标浮动（默认 `10`，即 390~410 视为达标）
- `max_query_rounds`: 默认在线回溯轮数（默认 `200`）
- `max_query_rounds_hard_cap`: 在线回溯硬上限（默认 `600`）
- `target_msg_scan_limit`: 目标用户消息预扫描上限（默认 `2400`）
- `memchat_target_only_first`: 是否优先使用 mem_chat 目标用户直出语料（默认 `true`）
- `prefer_memchat_history`: 是否优先使用 mem_chat 语义历史（默认 `true`）
- `memchat_history_scan_limit`: mem_chat 历史扫描上限（默认 `12000`）
- `enable_msg_index`: 是否启用本地消息索引（默认 `true`）
- `msg_index_db_path`: 本地消息索引 DB 路径（留空使用默认）
- `index_query_batch_size`: 索引单轮读取条数（默认 `1200`）
- `index_query_max_rounds`: 索引读取最大轮数（默认 `120`）
- `context_force_prev_msgs`: 每个目标锚点强制保留前文条数（默认 `3`）
- `context_relevance_min_score`: 群友消息相关性阈值（默认 `6`）
- `current_context_scan_count`: 当前消息扫描条数（默认 `40`）
- `current_context_line_limit`: 注入当前消息行数（默认 `12`）
- `include_image_summary`: 是否注入图片摘要（默认 `true`）
- `image_summary_max_len`: 单条图片摘要最大长度（默认 `200`）
- `render_text_max_chars`: 文转图最大字符数（默认 `2200`）
- `portrait_output_max_chars`: 画像正文最大字符数（默认 `1500`，超出后按完整段落压缩）
- `provider_id`: 画像/取名固定使用的 Provider ID（默认 `deepseek_v4_flash`；留空使用当前默认 provider）
- `llm_model`: 画像/取名使用的 LLM 模型（默认 `deepseek-flash`，会覆盖 provider 默认模型）
- `use_memchat_impression`: 是否启用 mem_chat 人物印象基准注入（默认 `true`）
- `memchat_db_path`: mem_chat 数据库路径（留空使用默认路径）
- `memchat_impression_max_chars`: mem_chat 基准注入最大字符数（默认 `1000`）

## 🧪 排错建议

- 若“目标 400 条”长期达不到：先看群里该用户历史本身是否足够，再适当上调 `max_query_rounds` 或 `max_query_rounds_hard_cap`。
- 若在线回溯过慢：保持 `enable_msg_index=true`，让索引逐步热起来，后续请求会明显变快。
- 若出现空白图：先下调 `render_text_max_chars`（如 `1600~1800`）。
- 若上下文不相关：提高 `context_relevance_min_score`，或降低 `current_context_line_limit`。
- 若取名仍像旧名片：检查是否启用了最新版本并确认 `取名` 分支已重载。

## 效果图

![download](https://github.com/user-attachments/assets/988e7cc1-92d1-48c9-8d95-cf83e802bfc9)

## 🤝 TODO

- [ ] 改用astrbot自带数据库，增强对其他平台的兼容性

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）

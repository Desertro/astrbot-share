# astrbot_plugin_colg_knowledge

COLG 论坛抓取与知识库同步插件，对应包为 `colg_knowledge`。该插件通常作为 AstrBot 的保留插件（Reserved Plugin），专门为 `mem_chat` 知识库提供持续的数据补充。

## 功能特性

- 自动抓取：按配置周期自动抓取 COLG 论坛（默认 DNF 综合讨论区）的新帖。
- 知识提取：使用 `readability` 和 `BeautifulSoup` 提取帖子正文，去除 HTML 杂质。
- 质量过滤：按标题黑名单、回复数和正文长度过滤水帖，自动剔除“游客，如果您要查看本帖隐藏内容请”等隐藏内容提示，尽量保留有价值内容。
  - 额外硬过滤：抓取阶段直接丢弃网盘/提取码/联系方式引流、编辑痕迹、隐藏内容占位、纯灌水问句、招募广告等低价值内容。
  - 内容去重：同一轮抓取会按正文归一化指纹去重，减少多页/重复转帖进入知识库。
- 知识库同步：
  - 优先调用 `mem_chat` 插件接口进行向量化入库；
  - 若 `mem_chat` 未安装，则降级写入本地 Markdown 文件 `data/plugins/astrbot_plugin_mem_chat/knowledge/colg_hourly.md`。
- 每日摘要：每天在配置时间点生成当日论坛动态摘要（可选使用 LLM 生成）。
- 检索工具：向 LLM 注册 `colg_search` 工具，支持在已抓取的 COLG 知识中进行字符串匹配检索。
- 群聊推送：支持配置群聊白名单与推送周期，定期向目标群推送精选 COLG 帖子。
- 人格化推送：在安装并启用 `mem_chat` 插件时，群聊推送会优先通过其统一会话与人格系统，以机器人人格口吻对 COLG 精选内容进行转述与播报；若 `mem_chat` 不存在，则回退为直接发送原始整理文案。
- 去重过滤：对同一天已抓取过的主题按 thread_id 去重，减少版头公告重复抓取。
- 快照模式：支持 `mshots` 与 `browser` 两种模式，`browser` 模式可更稳定地获取帖子正文区域。

## 配置说明

插件运行时会从 WebUI 以及对应的配置文件中读取配置（通常位于 AstrBot 的 `data/config` 目录），典型字段包括：

| 参数 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `forum_id` | COLG 论坛版块 ID | `171` (DNF 综合) |
| `pages_per_hour` | 每次抓取的页数 | `2` |
| `max_threads_per_hour` | 每次抓取的帖子数量上限 | `50` |
| `rate_limit_rps` | 抓取频率限制 (请求/秒) | `0.5` |
| `embedding_rate_limit_rps` | 嵌入入库频率限制 (请求/秒) | `0.5` |
| `daily_digest_hour` | 每日摘要生成的整点时间 | `3` |
| `crawl_interval_sec` | 抓取周期（秒） | `3600` |
| `push_enabled` | 是否开启群聊自动推送 | `false` |
| `push_interval_sec` | 群聊推送周期（秒） | `3600` |
| `push_snapshot_enabled` | 推送时附带网页快照 | `false` |
| `snapshot_width` | 网页快照宽度（像素，200-1200） | `800` |
| `snapshot_mode` | 网页快照模式（mshots/browser） | `mshots` |
| `snapshot_height` | 浏览器快照高度（像素） | `900` |
| `snapshot_wait_sec` | 浏览器快照额外等待秒数 | `1.5` |
| `snapshot_timeout_sec` | 浏览器快照超时秒数 | `12` |
| `snapshot_mshots_poll_attempts` | mshots 快照未生成时的轮询次数 | `6` |
| `snapshot_mshots_poll_interval_sec` | mshots 快照轮询间隔秒数 | `5` |
| `snapshot_full_page` | 浏览器快照是否截取整页 | `true` |
| `snapshot_user_agent` | 浏览器快照使用的 User-Agent | 默认内置 UA |
| `group_whitelist` | 接收推送的群聊白名单（Group ID 列表） | `[]` |

> 其中 WebUI 配置项来源于插件目录下的 `_conf_schema.json`，修改后会在下次插件加载时生效。

## 与 `mem_chat` 的集成

- 知识库同步：插件在抓取和清洗帖子内容后，会优先调用 `mem_chat` 暴露的知识入库接口，将内容写入其向量知识库，用于后续对话检索与回答。
- 群聊人格化推送：对于配置在 `group_whitelist` 中的群聊，本插件会为每个群维护独立的会话标识，并通过 `mem_chat` 提供的统一推送接口，将 COLG 精选帖子内容注入到机器人的提示词中，再由机器人以当前人格生成自然语言播报。
- 兼容降级：如果 `mem_chat` 未安装或推送接口不可用，群聊推送会自动降级为由 `colg_knowledge` 直接在群里发送整理后的帖子内容，不影响基础功能。
- 网页快照附图：开启 `push_snapshot_enabled` 后，推送会额外发送一张帖子网页快照图片，不依赖 `mem_chat` 的图片接口。`mshots` 模式会先在本地轮询并校验快照，避免把生成中的 loading 占位图发到群里。

## 指令列表

- `colg status`：查看插件运行状态、配置信息及日志路径（在群聊中使用可顺带记录当前群的会话标识，用于后续推送）。
- `colg ingest <pages> <max_threads> <token>`：手动触发一次抓取任务（需要确认口令 `{{AB_COLG_KNOWLEDGE_CONFIRM_TOKEN}}`）。

## 日志

- 插件日志文件：`data/logs/colg_knowledge.log`。
- 同时也会输出到 AstrBot 的系统标准日志中。

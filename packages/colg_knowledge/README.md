# COLG Knowledge Service (colg_knowledge)

COLG 论坛抓取与知识库同步服务。该插件作为 AstrBot 的保留插件（Reserved Plugin），专门为 `mem_chat` 知识库提供持续的数据补充。

## 功能特性

- **自动抓取**：每小时自动抓取 COLG 论坛（默认 DNF 综合讨论区）的新帖。
- **知识提取**：使用 `readability` 和 `BeautifulSoup` 自动提取帖子正文，去除 HTML 杂质。
- **基础过滤**：自动跳过含有禁止转载、回复可见下载链接、联系方式广告、“游客，如果您要查看本帖隐藏内容请”等噪声内容的帖子，过滤标题含“COLG运营小妹”的公告帖，并忽略只有标题没有正文内容的页面。
- **知识库同步**：
    - 优先尝试调用 `mem_chat` 插件的接口进行向量化入库。
    - 如果 `mem_chat` 未安装，则自动降级保存为本地 Markdown 文件 (`data/plugins/astrbot_plugin_mem_chat/knowledge/colg_hourly.md`)。
- **每日摘要**：每天凌晨自动生成当日论坛动态摘要。
- **检索工具**：为 LLM 提供 `colg_search` 工具，使其能够检索已抓取的 COLG 知识。

## 配置说明

配置文件位于 `data/config/colg_knowledge_config.json`。

| 参数 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `forum_id` | COLG 论坛版块 ID | `171` (DNF 综合) |
| `pages_per_hour` | 每小时抓取的页数 | `2` |
| `max_threads_per_hour` | 每小时最大抓取帖子数 | `50` |
| `rate_limit_rps` | 抓取频率限制 (请求/秒) | `0.5` |
| `embedding_rate_limit_rps` | 嵌入入库频率限制 (请求/秒) | `0.5` |
| `daily_digest_hour` | 每日摘要生成的整点时间 | `3` |

## 指令列表

- `/colg status`: 查看插件运行状态、配置信息及日志路径。
- `/colg ingest <pages> <max_threads> <token>`: 手动触发一次抓取任务。
- `/colg clean_spam`: 清理 mem_chat 中已入库的 COLG 垃圾知识条目，并同步清理本地 Markdown 备份。

## 日志

日志文件路径：`data/logs/colg_knowledge.log`。
同时也会输出到 AstrBot 的系统标准日志中。

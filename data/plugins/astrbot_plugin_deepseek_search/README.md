# DeepSeek 搜索与网页抓取

调用官方 Search / Fetch API，向 AstrBot 注册 `deepseek_web_search` 和
`deepseek_web_fetch`。在插件配置中指定已配置的官方 DeepSeek `provider_id`。

搜索返回标题、链接、发布日期和摘要；抓取最多返回 8000 字符。请求最多等待
25 秒，瞬时 HTTP 错误重试一次；每个会话的结果缓存 120 秒，最多保留 128 条，
最多同时执行 3 个请求。网页内容作为外部资料，回答应引用来源链接。

官方接口和计费说明：https://api-docs.deepseek.com/zh-cn/guides/search/

本地 mem_chat 已有按会话排队和记忆模块。本次参考 DSH QQ adapter 的会话工具
白名单思路，将联网能力接入现有工具循环，并预留最终回答轮次。相关参考：
https://github.com/sindo-s/dsh-qq-bot

示例：在 QQ 中 @机器人说“搜一下 DeepSeek 搜索 API 的最新文档，附来源”，
或“读一下 https://api-docs.deepseek.com/zh-cn/guides/search/ 并总结”。

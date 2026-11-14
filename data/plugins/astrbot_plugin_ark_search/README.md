# astrbot_plugin_ark_search

把火山方舟（Volces ark）`/api/v3/responses` 接口 + `web_search` 工具包装成一个 LLM Function Tool。

## 效果

聊天 LLM（如 DeepSeek v4）原本不会联网，配置后它在需要时会调用 `ark_web_search(query)` —— 内部由豆包 `doubao-seed-2-0-mini` 触发 `web_search` 拿到最新信息，把总结好的中文答案回传给聊天 LLM。

## 依赖

- AstrBot v4.5+（支持 `@llm_tool` 装饰器）
- 火山方舟 ark API Key

## 配置

在 WebUI / 配置文件里填 `api_key` 即可，其它项保留默认。

```json
{
  "api_key": "ark-xxxxx",
  "model": "doubao-seed-2-0-mini-260215",
  "enable_web_search": true
}
```

## 与 mem_chat 的协作

`mem_chat` 的 `_setup_tools` 默认会按 `websearch_provider` 切换搜索工具白名单。需要在 `_setup_tools` 内把 `ark_web_search` 视为搜索类工具，且在 `llm_tools_allowed_patterns` 里增加 `ark` 或 `search` 匹配，详见上层项目 PR。

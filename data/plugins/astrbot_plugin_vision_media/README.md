# vision_media

提供视觉媒体能力的服务型插件，包含：

- 从消息事件中抽取图片 URL / 文件 token
- 图片压缩与 GIF 抽帧（预算控制）
- 调用视觉 LLM 生成图片内容描述（输出形如 `[图片内容: ...]`）
- 图片描述缓存（SQLite）
- 可选：视觉 JSON 打标与自动群精华（白名单）
- 可选：黄暴判定与自动撤回（违规时描述替换为“黄暴图片”）

## 使用方式

该插件主要作为其它插件的依赖使用。其它插件可通过 `context.get_registered_star("vision_media")` 获取插件实例并调用：

- `extract_images(event)`
- `render_text_with_captions(event)`
- `prepare_llm_image_contents(image_urls)`

## 配置

在 WebUI 中配置本插件：

- `vision_llm.*`：视觉 LLM 参数
- `vision_llm.api_key_source=mem_chat`：仅当目标为 `{{AB_CMD_PROVIDER_SOURCES_2_API_BASE}}` 时，复用 mem_chat 的 DeepSeek Key；Key 只在内存中注入，不复制到本插件配置。
- `vision_llm.api_key_source=shoubanhua`：仅当目标为 `https://api.yezi.help` 时，复用手办化插件的 Yezi Key；Key 只在内存中注入，不复制到本插件配置。
- `vision_llm.format=anthropic`：使用 Anthropic Messages API（`/v1/messages`）发送图片，适用于 Claude 兼容服务。
- `vision_llm.bypass_proxy=true`：让视觉 API 直连，不读取系统代理；适合局域网或专线 API。
- `vision_llm.timeout_sec` / `queue_timeout_sec` / `max_concurrency`：限制单次等待、并发排队与 API 并发，避免识图拖住群聊流水线。
- `vision_llm.circuit_breaker_failures` / `circuit_breaker_cooldown_sec`：连续失败后短暂熔断，冷却期间直接向 mem_chat 注入失败提示。
- `vision_llm.failure_fallback_text`：识图超时、熔断或返回异常时使用的提示；建议明确要求聊天模型不要猜图。
- `vision_fast_llm.*` / `vision_route_*`：低成本识图路由。普通账单/网页截图/现实照片等先走 fast-router；只有二次元、角色识别意图或不确定图片再升级到 `vision_llm` 的复杂识图与 web_search。
- `vision_image_cache_*`：图片描述缓存参数
- `vision_image_simhash_*`：感知哈希近似去重参数（应对压缩图/轻微变体）
- `vision_image_simhash_ui_strict_enable` / `vision_image_simhash_ui_max_hamming`：对游戏/UI截图启用更严格阈值，减少“新截图误命中旧描述”。
- `auto_ingest_group_images`：自动接收并处理群内所有图片消息（包含机器人自身发图）
- `mirror_bot_image_msgs_to_mem_chat`：机器人发图时自动把视觉描述镜像写入 mem_chat 记忆
- `mem_chat_llm_image_*`：图片压缩与 GIF 抽帧预算参数（命名沿用以保持兼容）
- `vision_llm_response_mode`：视觉输出模式（text/json）
- `vision_essence_enable / vision_essence_group_whitelist`：自动群精华（仅 OneBot v11 场景）
- `vision_essence_score_threshold`：自动群精华阈值分数（0-100）
- `vision_nsfw_*`：黄暴判定与撤回相关配置（仅 JSON 模式生效）

## 视觉 JSON 与自动精华

当 `vision_llm_response_mode=json` 时，会要求视觉模型只输出 JSON：`{"desc":"...","score":0-100}`。

- `desc` 会被注入为 `[图片内容: ...]` 给 mem_chat 理解。
- 当 `score >= vision_essence_score_threshold` 时，会额外注入标志符（默认形如 `[vision_essence=1]`），并在白名单群聊内尝试把“触发这张图的原消息”设为群精华。
- 设精华参考 `qqadmin` 的实现方式：`event.bot.set_essence_msg(message_id=...)`，因此仅在 OneBot v11 / aiocqhttp 且协议端支持该 action、机器人有权限时生效；其它平台会自动跳过。
- JSON 解析会自动清理代码块/杂质文本/注释，支持多 JSON 块与 response 包裹提取，并在响应不是标准 JSON 时回退为文本解析。

## 智能识图路由

开启 `vision_route_enable` 后，未命中缓存的图片会先按上下文判断：

- 明显来自二次元图库、消息文本包含“这是谁/角色/二次元/画师/图源/VTuber/cos”等识别意图时，直接走 `vision_llm`，避免二次元图额外多一次 fast 调用。
- 普通来源图片先用 `vision_fast_llm` 低成本识图，图片会压到 `vision_route_image_max_dim` / `vision_route_image_max_kb` 的预算。
- fast 返回 `route=simple` 且置信度足够时，直接把 `desc` 写成 `[图片内容: ...]` 并缓存，跳过复杂识图和 web_search。
- fast 返回 `route=anime` / `route=uncertain`、`anime_score` 超阈值、JSON 解析失败、描述为空或 NSFW 分数超阈值时，会保守升级到 `vision_llm`。

如果使用豆包 Seed 2.0 mini 作为 fast-router，建议 `vision_route_max_output_tokens` 保持 800 左右；过低时模型可能把输出预算消耗在 reasoning 上，导致 JSON 为空并触发兜底 heavy。

## 黄暴判定与撤回

当 `vision_llm_response_mode=json` 且 `vision_nsfw_enable=true` 时，会要求视觉模型输出黄暴相关字段：

- `nsfw_score`：黄暴评分（0-100）。
- `violation`：违规标记（违规时为“黄暴图片”）。
- `reason`：违规原因。

命中违规后会：

- 尝试撤回触发图片的原消息（依赖平台支持 delete_msg/recall_msg action）。
- 将图片描述替换为 “黄暴图片”，并写入缓存，供 mem_chat 记录识别。

## 配置迁移

如果检测到本插件配置仍为默认值且存在 `mem_chat` 的配置文件，本插件会自动从 `mem_chat` 迁移以下配置并写入自身配置文件：

- `vision_llm.*`
- `chat_model_supports_vision`
- `vision_image_cache_*`
- `mem_chat_llm_image_*`

## 图片去重与缓存键

图片描述缓存会把“同一张图”归一化成稳定的 `image_key`，用于复用 `[图片内容: ...]` 与 `essence` 标记，减少重复视觉 LLM 调用。

## 自动接收群图

- 开启 `auto_ingest_group_images` 后，插件会监听群内所有图片消息并进行缓存预热。
- 这包含其它插件发出的机器人图片（如手办化、抽老婆等），可减少后续“复读同图时不认识”的问题。
- 开启 `mirror_bot_image_msgs_to_mem_chat` 后，机器人自己发图会额外写入 mem_chat 记忆库（文本含 `[图片内容: ...]`），便于后续对话直接召回。

- 本地文件（`file:///...` 或本地绝对路径）：读取文件内容计算 SHA1，key 形如 `contentsha1:<sha1>`。
- QQ 图片 token（非本地路径的 `file` 字段）：key 形如 `qqfile:<lower(token)>`（依赖平台提供的 token 稳定性）。
- URL：对 URL 做归一化后计算 SHA1，key 形如 `urlsha1:<sha1>`。
  - 对 `multimedia.nt.qq.com.cn`（QQ 多媒体域名）会忽略所有 query 参数（例如 `rkey`），仅使用 `scheme://host/path` 参与哈希，以避免同图不同参数导致重复。

此外，开启 `vision_image_simhash_enable` 后，会对图片首帧计算 `dHash` 并进行近似匹配（汉明距离阈值可配），可显著降低“QQ 压缩后同图被当新图”的问题。

`vision_llm` 可配置 `fallback_enabled`、`fallback_base_url`、`fallback_api_key`、`fallback_model`：主站失败时在原超时预算内尝试兼容 OpenAI 格式的备用站。需要统一使用独立识图模型时，将 `chat_model_supports_vision` 设为 false，确保 @ 和私聊也经过该识图服务。

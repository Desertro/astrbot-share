# 配置说明

配置向导只会写当前分享目录，不会访问原作者的 AstrBot。不要在已有机器人目录中覆盖安装；请在新目录启动。

## 建议顺序

1. 运行 `bash start.sh`，填必需的 QQ / 群号、面板登录信息和模型密钥。
2. 在 NapCat 连接 6199 端口，使用向导生成的 `AB_ONEBOT_TOKEN`。
3. 登录 6185 管理面板，检查模型是否能正常调用。
4. 根据需要填写其他 API Key、Cookie 和服务地址。改变 `.env` 后用 `python tools/setup.py --overwrite` 生成配置；这会用模板重新覆盖配置，所以请先备份 WebUI 中的自定义修改。

生成器保留模型、人设、阈值、功能开关及模板中的对应关系。QQ / 群号只填自己的号码；未填写的可选身份会被移除，而不会继续指向原作者的账号。已有数据库不会被重置。

## 密钥填写规则

- `.env` 的变量值用 JSON 字符串表示，例如 `AB_ONEBOT_TOKEN="自己设置的Token"`。不要把示例文字当成真实密钥。
- Cookie 可整体填入对应字段；JSON 转义规则适用。也可通过插件 WebUI 填写生成后的配置。
- 同一个变量对应多个字段，说明原配置共享同一个凭据。只填一次即可。
- 各平台的 Key 通常不通用。模型名称、API 格式和服务地址需与自己的账号匹配。
- 向导不会检测凭据余额或调用计费 API。`--check` 只检查本地配置与占位符。
- OneBot Token 与模型 API Key 是不同用途。首次向导会生成 OneBot Token，NapCat 需要填同样的值。

## 多群与权限

主群 `AB_QQ_2` 会写入聊天、任务调度、推送等共享主群配置。可选群号沿用各自的频率 / 人设覆盖关系。`AB_QQ_3` 对应额外的人设分组；其他角色见下表。不要把不同角色的群全部填成同一个号码，否则字典中的同名群配置会互相覆盖。

自开发、群管理、外部命令等能力保留原有开关与权限结构，请仅把自己的账号填入管理员项。对于不需要的插件，可以在 WebUI 停用。

## 外部服务

| 功能 | 使用者需要提供 |
| --- | --- |
| 记忆聊天 / 自主聊天 | `.env` 中 mem_chat 的 LLM Key / URL，保持对应的模型和接口格式 |
| 向量记忆 | embedding Key / URL，对应的 embedding 模型 |
| 识图 / 视觉上下文 | vision_media / mem_chat 视觉模型对应 Key |
| 手办化等 API 绘图 | 插件对应服务商 API Key；不包含本地 ComfyUI 部署 |
| 图片搜索 | SearchAPI / SauceNAO 等所选检索服务的 Key |
| 视频 / 图片解析 | 根据站点填写 douyin_ck、pixiv_ck 等自己的 Cookie；ffmpeg / Chromium 由启动过程准备 |
| 图床 | 所选图床的 Key、Secret；没有账号可在 WebUI 选择自己使用的方式 |
| RSS | 可访问的 RSSHub 地址；原 localhost 地址需要自己运行对应服务 |
| 自主命令代理 | 自己安装并登录 Codex / DSH，配置可执行文件与工作目录 |
| QQ 平台 | 自己安装并登录 NapCat，配置 OneBot 反向 WebSocket |
| 语音、GScore、本地绘图 | 本次暂不部署；保留插件及接口配置 |

## 变量与具体字段

表内只包含变量用途，未包含原作者的任何密钥或号码。空白可选项的对应服务需要自行补齐后使用。

| 变量 | 必填 | 类型 | 配置字段 |
| --- | --- | --- | --- |
| `AB_CMD_PROVIDER_SOURCES_0_KEY_0` | 是 | secret | data/cmd_config.json.provider_sources.0.key.0 |
| `AB_CMD_PROVIDER_SOURCES_0_API_BASE` | 可选 | service | data/cmd_config.json.provider_sources.0.api_base |
| `AB_CMD_PROVIDER_SOURCES_1_KEY_0` | 是 | secret | data/cmd_config.json.provider_sources.1.key.0 |
| `AB_CMD_PROVIDER_SOURCES_1_API_BASE` | 可选 | service | data/cmd_config.json.provider_sources.1.api_base |
| `AB_CMD_PROVIDER_SOURCES_2_KEY_0` | 是 | secret | data/cmd_config.json.provider_sources.2.key.0<br>data/config/astrbot_plugin_mem_chat_config.json.llm_fallback.api_key |
| `AB_CMD_PROVIDER_SOURCES_2_API_BASE` | 可选 | service | data/cmd_config.json.provider_sources.2.api_base<br>data/config/astrbot_plugin_mem_chat_config.json.llm_fallback.base_url |
| `AB_CMD_PROVIDER_2_API_BASE` | 可选 | service | data/cmd_config.json.provider.2.api_base |
| `AB_CMD_PROVIDER_2_GPT_WEIGHTS_PATH` | 可选 | path | data/cmd_config.json.provider.2.gpt_weights_path |
| `AB_CMD_PROVIDER_2_SOVITS_WEIGHTS_PATH` | 可选 | path | data/cmd_config.json.provider.2.sovits_weights_path |
| `AB_CMD_PROVIDER_2_GSV_DEFAULT_PARMS_GSV_REF_AUDIO_PATH` | 可选 | path | data/cmd_config.json.provider.2.gsv_default_parms.gsv_ref_audio_path |
| `AB_CMD_PROVIDER_SETTINGS_WEBSEARCH_TAVILY_KEY_0` | 可选 | secret | data/cmd_config.json.provider_settings.websearch_tavily_key.0 |
| `AB_QQ_1` | 是 | basic | data/cmd_config.json.admins_id.2<br>data/config/astrbot_plugin_capability_agent_config.json.admin_uids.0<br>data/config/astrbot_plugin_mem_chat_config.json.private_chat_uids.0<br>data/config/astrbot_plugin_mem_chat_config.json.adult_private_uids.0<br>data/config/astrbot_plugin_mem_chat_config.json.codex_allowed_uids.0<br>data/config/astrbot_plugin_meme_kb_config.json.admin_uids.0<br>data/config/astrbot_plugin_self_agent_config.json.admin_uids.0<br>data/config/astrbot_plugin_self_dev_config.json.admin_uids.0<br>identity.value |
| `AB_CMD_LOG_FILE_PATH` | 可选 | path | data/cmd_config.json.log_file_path |
| `AB_ARK_SEARCH_API_KEY` | 可选 | secret | data/config/astrbot_plugin_ark_search_config.json.api_key |
| `AB_ARK_SEARCH_BASE_URL` | 可选 | service | data/config/astrbot_plugin_ark_search_config.json.base_url |
| `AB_CAPABILITY_AGENT_ANIMA_COMFY_URL` | 可选 | service | data/config/astrbot_plugin_capability_agent_config.json.anima_comfy_url |
| `AB_COLG_KNOWLEDGE_CONFIRM_TOKEN` | 可选 | secret | data/config/astrbot_plugin_colg_knowledge_config.json.confirm_token |
| `AB_QQ_2` | 是 | identity | data/config/astrbot_plugin_colg_knowledge_config.json.group_whitelist.0<br>data/config/astrbot_plugin_dnf_gold_config.json.broadcast_groups.0<br>data/config/astrbot_plugin_dogdiary_config.json.dogdiary_auto_send_groups.0<br>data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.0<br>data/config/astrbot_plugin_mem_chat_config.json.vision_chat_groups.1<br>data/config/astrbot_plugin_mem_chat_config.json.group_frequency_overrides.{{AB_QQ_2}}<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.0.target_umo<br>data/config/astrbot_plugin_rss_config.json.rsshub_whitelist_sessions.2<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.0<br>data/config/astrbot_plugin_vision_media_config.json.vision_essence_group_whitelist.0<br>data/config/astrbot_plugin_vision_media_config.json.vision_essence_cleanup_group_whitelist.0<br>data/config/astrbot_plugin_x_anime_push_test_config.json.target_umo<br>identity.value<br>identity.map_key |
| `AB_FIGURINE_WORKSHOP_API_BASE_URL` | 可选 | service | data/config/astrbot_plugin_figurine_workshop_config.json.api_base_url<br>data/config/astrbot_plugin_gemini2point5image_openrouter_config.json.custom_api_base |
| `AB_IMG_REV_SEARCHER_PROXIES` | 可选 | service | data/config/astrbot_plugin_img_rev_searcher_config.json.proxies<br>data/config/astrbot_plugin_shoubanhua_config.json.proxy_url |
| `AB_IMG_REV_SEARCHER_DEFAULT_PARAMS_SAUCENAO_API_KEY` | 可选 | secret | data/config/astrbot_plugin_img_rev_searcher_config.json.default_params.saucenao.api_key |
| `AB_IMGEXPLORATION_NETWORK_PROXY_URL` | 可选 | service | data/config/astrbot_plugin_imgexploration_config.json.network.proxy_url |
| `AB_IMGEXPLORATION_API_KEYS_SEARCHAPI_KEYS_0` | 可选 | secret | data/config/astrbot_plugin_imgexploration_config.json.api_keys.searchapi_keys.0 |
| `AB_QQ_3` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_persona_overrides.{{AB_QQ_3}}<br>data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.9<br>data/config/astrbot_plugin_mem_chat_config.json.adult_group_whitelist.2<br>identity.map_key<br>identity.value |
| `AB_MEM_CHAT_CUSTOM_LLM_BASE_URL` | 可选 | service | data/config/astrbot_plugin_mem_chat_config.json.custom_llm_base_url |
| `AB_MEM_CHAT_CUSTOM_LLM_API_KEY` | 是 | secret | data/config/astrbot_plugin_mem_chat_config.json.custom_llm_api_key |
| `AB_MEM_CHAT_LLM_BASE_URL` | 可选 | service | data/config/astrbot_plugin_mem_chat_config.json.llm_base_url<br>data/config/astrbot_plugin_mem_chat_config.json.meme_vision.base_url |
| `AB_MEM_CHAT_LLM_API_KEY` | 是 | secret | data/config/astrbot_plugin_mem_chat_config.json.llm_api_key<br>data/config/astrbot_plugin_mem_chat_config.json.meme_vision.api_key<br>data/config/astrbot_plugin_mem_chat_config.json.meme_vision.fallback_api_key<br>data/config/astrbot_plugin_vision_media_config.json.vision_llm.api_key |
| `AB_QQ_4` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.1<br>data/config/astrbot_plugin_mem_chat_config.json.group_frequency_overrides.{{AB_QQ_4}}<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.1.target_umo<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.4<br>identity.value<br>identity.map_key |
| `AB_QQ_5` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.2<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.2.target_umo<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.2<br>identity.value |
| `AB_QQ_6` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.3<br>data/config/astrbot_plugin_mem_chat_config.json.adult_group_whitelist.0<br>data/config/astrbot_plugin_mem_chat_config.json.vision_chat_groups.2<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.3.target_umo<br>data/config/astrbot_plugin_rss_config.json.rsshub_whitelist_sessions.1<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.1<br>identity.value |
| `AB_QQ_7` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.4<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.4.target_umo<br>data/config/astrbot_plugin_rss_config.json.rsshub_whitelist_sessions.3<br>identity.value |
| `AB_QQ_8` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.5<br>data/config/astrbot_plugin_mem_chat_config.json.adult_group_whitelist.1<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.5.target_umo<br>data/config/astrbot_plugin_rss_config.json.rsshub_whitelist_sessions.0<br>data/config/astrbot_plugin_self_agent_config.json.idle_chat_groups.0<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.3<br>data/config/astrbot_plugin_vision_media_config.json.vision_essence_group_whitelist.1<br>identity.value |
| `AB_QQ_9` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.6<br>data/config/astrbot_plugin_mem_chat_config.json.group_frequency_overrides.{{AB_QQ_9}}<br>data/config/astrbot_plugin_mem_scheduler_config.json.seed_jobs.6.target_umo<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.5<br>identity.value<br>identity.map_key |
| `AB_QQ_10` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.7<br>data/config/astrbot_plugin_mem_chat_config.json.vision_chat_groups.0<br>data/config/astrbot_plugin_mem_chat_config.json.group_frequency_overrides.{{AB_QQ_10}}<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.6<br>identity.value<br>identity.map_key |
| `AB_QQ_11` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.group_whitelist.8<br>data/config/astrbot_plugin_shoubanhua_config.json.group_whitelist.7<br>identity.value |
| `AB_QQ_12` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.private_chat_uids.1<br>data/config/astrbot_plugin_mem_chat_config.json.adult_private_uids.1<br>data/config/astrbot_plugin_mem_chat_config.json.codex_allowed_uids.1<br>data/config/astrbot_plugin_meme_kb_config.json.admin_uids.1<br>data/config/astrbot_plugin_napcat_debug_config.json.test_qq_account<br>data/config/astrbot_plugin_self_agent_config.json.admin_uids.1<br>data/config/astrbot_plugin_self_dev_config.json.admin_uids.1<br>identity.value |
| `AB_QQ_13` | 可选 | identity | data/config/astrbot_plugin_mem_chat_config.json.private_chat_uids.2<br>data/config/astrbot_plugin_mem_chat_config.json.adult_private_uids.2<br>identity.value |
| `AB_MEM_CHAT_EMBEDDING_OPENAI_BASE_URL` | 可选 | service | data/config/astrbot_plugin_mem_chat_config.json.embedding_openai_base_url |
| `AB_MEM_CHAT_EMBEDDING_OPENAI_API_KEY` | 是 | secret | data/config/astrbot_plugin_mem_chat_config.json.embedding_openai_api_key |
| `AB_MEM_CHAT_MEME_VISION_FALLBACK_BASE_URL` | 可选 | service | data/config/astrbot_plugin_mem_chat_config.json.meme_vision.fallback_base_url<br>data/config/astrbot_plugin_vision_media_config.json.vision_llm.base_url |
| `AB_MEME_MANAGER_IMAGE_HOST_CONFIG_STARDOTS_KEY` | 可选 | secret | data/config/astrbot_plugin_meme_manager_config.json.image_host_config.stardots.key |
| `AB_MEME_MANAGER_IMAGE_HOST_CONFIG_STARDOTS_SECRET` | 可选 | secret | data/config/astrbot_plugin_meme_manager_config.json.image_host_config.stardots.secret |
| `AB_MNEMOSYNE_ADDRESS` | 可选 | service | data/config/astrbot_plugin_mnemosyne_config.json.address |
| `AB_PARSER_DATA_DIR` | 可选 | path | data/config/astrbot_plugin_parser_config.json.data_dir |
| `AB_PARSER_CACHE_DIR` | 可选 | path | data/config/astrbot_plugin_parser_config.json.cache_dir |
| `AB_RSS_DEFAULT_RSSHUB_ENDPOINT` | 可选 | service | data/config/astrbot_plugin_rss_config.json.default_rsshub_endpoint |
| `AB_SELF_AGENT_DSH_CMD` | 可选 | path | data/config/astrbot_plugin_self_agent_config.json.dsh_cmd |
| `AB_SELF_AGENT_CODEX_CMD` | 可选 | path | data/config/astrbot_plugin_self_agent_config.json.codex_cmd |
| `AB_SELF_AGENT_CODEX_HOME` | 可选 | path | data/config/astrbot_plugin_self_agent_config.json.codex_home |
| `AB_SELF_AGENT_NODE_BIN_DIR` | 可选 | path | data/config/astrbot_plugin_self_agent_config.json.node_bin_dir |
| `AB_SHOUBANHUA_API_KEYS_0` | 可选 | secret | data/config/astrbot_plugin_shoubanhua_config.json.api_keys.0 |
| `AB_SHOUBANHUA_BACKUP_API_KEYS_0` | 可选 | secret | data/config/astrbot_plugin_shoubanhua_config.json.backup_api_keys.0<br>data/config/astrbot_plugin_vision_media_config.json.vision_llm.fallback_api_key |
| `AB_VISION_MEDIA_VISION_LLM_FALLBACK_BASE_URL` | 可选 | service | data/config/astrbot_plugin_vision_media_config.json.vision_llm.fallback_base_url |
| `AB_ZIMAGE_LOCAL_LOCAL_ZIMAGE_URL` | 可选 | service | data/config/astrbot_plugin_zimage_local_config.json.local_zimage_url |
| `AB_DASHBOARD_USERNAME` | 是 | basic | dashboard.username |
| `AB_DASHBOARD_PASSWORD` | 是 | basic | dashboard.password (generated hash) |
| `AB_ONEBOT_TOKEN` | 是 | basic | platform.ws_reverse_token |
| `AB_GEMINI2POINT5IMAGE_OPENROUTER_GEMINI_API_KEYS_0` | 可选 | secret | data/config/astrbot_plugin_gemini2point5image_openrouter_config.json.gemini_api_keys.0 |
| `AB_GEMINI2POINT5IMAGE_OPENROUTER_GEMINI_API_KEYS_1` | 可选 | secret | data/config/astrbot_plugin_gemini2point5image_openrouter_config.json.gemini_api_keys.1 |
| `AB_PARSER_DOUYIN_CK` | 可选 | secret | data/config/astrbot_plugin_parser_config.json.douyin_ck |
| `AB_PARSER_PIXIV_CK` | 可选 | secret | data/config/astrbot_plugin_parser_config.json.pixiv_ck |

## 补充的可移植路径

| 变量 | 必填 | 类型 | 字段 |
| --- | --- | --- | --- |
| `AB_MEM_CHAT_CODEX_CLI_PATH` | 可选 | path | data/config/astrbot_plugin_mem_chat_config.json.codex_cli_path |
| `AB_MEM_CHAT_CODEX_WORKDIR` | 可选 | path | data/config/astrbot_plugin_mem_chat_config.json.codex_workdir |
| `AB_CAPABILITY_AGENT_ANIMA_WINDOWS_PYTHON` | 可选 | path | data/config/astrbot_plugin_capability_agent_config.json.anima_windows_python |
| `AB_CAPABILITY_AGENT_ANIMA_WORKFLOW` | 可选 | path | data/config/astrbot_plugin_capability_agent_config.json.anima_workflow |

## 常见问题

- **模型 401 / 403**：检查该服务对应的 Key、API 地址和账号权限；向导不会将其他服务商 Key 自动混用。
- **机器人不回消息**：检查 NapCat 已连接、管理员 / 群号填写正确、mem_chat 相关模型密钥已配置；原配置主要由 mem_chat 驱动聊天，AstrBot 内置 LLM 总开关沿用原设置。
- **首次依赖安装失败**：使用 Python 3.12，检查网络 / PyPI 访问；Linux 上 ffmpeg、Node.js、Chromium 系统依赖需要可安装。Docker 镜像包含这些依赖。
- **代理连不上**：将 `.env` 中相应代理地址改成自己可用的地址，或清空后在 WebUI 关闭该插件的代理开关。
- **记忆与好感度为空**：新安装从空数据库开始，这是预期行为；人物设定和知识素材已经保留。
- **修改 .env 后没有变化**：运行 `python tools/setup.py --overwrite`，然后重启。此操作重建模板对应配置，已有数据库保留。
- **想保留 WebUI 的所有手动修改**：直接在 WebUI 更新凭据，或先将变化合并回模板，再使用生成器。

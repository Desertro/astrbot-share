# astrbot_plugin_mem_scheduler

`mem_chat` 的独立定时编排插件。

## 功能
- 定时任务持久化（`jobs.json`）。
- 支持 `cron` / `interval` 两种触发。
- 当前内置任务类型：
  - `reminder`：定时提醒/播报文本。
  - `nickname_refresh`：定时评估群名片是否需要更新（支持 `dry_run`）。
- 提供命令与 `llm_tool`，可让 `mem_chat` 在对话中创建任务。

## 任务数据目录
- `data/plugin_data/astrbot_plugin_mem_scheduler/jobs.json`
- `data/plugin_data/astrbot_plugin_mem_scheduler/state.json`

## 命令
- `/mcs jobs`：查看任务列表。
- `/mcs run <job_id>`：立即执行一次。
  - 手动执行会跳过 `nickname_refresh` 的冷却/日限拦截，便于调试改名链路。
- `/mcs enable <job_id>`：启用任务。
- `/mcs disable <job_id>`：停用任务。
- `/mcs rm <job_id>`：删除任务。
- `/mcs dryrun on|off`：设置全局 dry_run。
- `/mcs history [n]`：查看最近执行记录。
- `/mcs add_reminder <cron_expr> <text>`：当前会话新增 reminder 任务。
- `/mcs add_nickname <cron_expr> [topic]`：当前会话新增 nickname_refresh 任务。
- `enable_command_api=false` 时，`/mcs` 命令入口全部禁用，仅保留语义工具触发。

## llm_tool
- `llm_schedule_task(...)`
  - 创建任务，支持 `reminder` / `nickname_refresh`
  - 支持 `cron_expression` 或 `interval_sec`
  - `nickname_refresh` 额外支持 `random_daily_once` / `random_weekly_once`、`window_start` / `window_end`、`must_change`、`bypass_limits`
- `llm_list_scheduled_tasks(...)`
  - 查看当前任务列表，可按 `target_umo` 过滤
- `llm_run_scheduled_task(...)`
  - 立即手动执行一次任务
- `llm_set_scheduled_task_enabled(...)`
  - 启用或停用指定任务
- `llm_remove_scheduled_task(...)`
  - 删除指定任务
- `llm_get_schedule_history(...)`
  - 查看最近执行历史

## 说明
- `nickname_refresh` 默认只做建议（`dry_run=true`）。
- `nickname_refresh` 优先向 `mem_chat.submit_nickname_refresh_task(...)` 投递一次“改名任务”（与常规推送同链路）。
- `mem_chat.submit_nickname_refresh_task(...)` 现在会返回结构化结果，至少区分：
  - `no_change`
  - `updated_via_mem_chat`
  - `mem_chat_card_failed`
- 若上一步未执行成功，再回退到 `mem_chat.suggest_self_group_card(...)` 决策（使用 mem_chat 人格与上下文）。
- 若 `mem_chat` 未提供该接口，才会回退到 `mem_scheduler` 内置 LLM 决策。
- 若关闭 `dry_run`，插件会调用 OneBot `set_group_card` 更新机器人在群内名片。
- 执行改名片需要插件近期收到过该群消息以缓存运行态（bot/self_id）。
- 自动触发遇到 `cooldown/day_limit` 时默认静默跳过（`notify_skip_in_group=false`），防止刷屏。
- `history` 现在会额外记录 `outcome/details`，区分 `updated`、`dry_run`、`no_change`、`delegated_unverified`、`skipped_*` 等结果，不再只有粗粒度的成功/失败。
- `nickname_refresh` 支持 `payload.random_daily_once=true`：
  - 仅每天执行一次；
  - 在 `window_start`~`window_end`（`HH:MM`）内随机选时间点触发。
- `nickname_refresh` 支持 `payload.random_weekly_once=true`：
  - 每周仅执行一次；
  - 在本周内随机选一天、在 `window_start`~`window_end` 内随机选时间点触发。
- `nickname_refresh` 支持测试/验链模式：
  - `payload.force_direct_set=true`：跳过委托 `mem_chat`，直接由 `mem_scheduler` 写群名片并做回读验证。
  - `payload.force_card_template`：按模板生成测试名片，支持 `{run}`、`{count}`、`{max_runs}`、`{job_id}`。
  - `payload.max_success_runs` / `payload.max_runs`：累计成功次数达到阈值后自动停用任务。
  - `payload.bypass_limits=true`：跳过 cooldown/day_limit 拦截，适合短周期验链。
  - `payload.notify_every_success=true`：每次成功后在群里播报一次，便于观察验链过程。

# astrbot_plugin_zimage_local

本地 `Z-Image Turbo` 插件，仅提供两个能力：
- 文生图
- 图生图

## 指令
- `z画 <提示词>`：文生图
- `z改 <提示词>`：图生图（发送图/引用图/@用户头像）
- `z文生图 <提示词>`：文生图别名
- `z图生图 <提示词>`：图生图别名
- `z图帮助`：查看帮助

## 依赖
先启动本地服务：
- `POST {{AB_ZIMAGE_LOCAL_LOCAL_ZIMAGE_URL}}`

## 限流与保护
插件内置：
- 用户 RPM 限制（`user_rpm`）
- 全局 RPM 限制（`global_rpm`）
- 用户冷却秒数（`user_cooldown_seconds`）
- 并发限制（`max_concurrency`）

默认值偏保守，适合本地部署防止显存打满。

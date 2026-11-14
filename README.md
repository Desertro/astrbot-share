# AstrBot 个人配置整合包

这是从实际运行的 AstrBot **4.28.1** 整理出的独立分享版本，包含 **33 个插件**、本地功能改动、人物设定、提示词、表情包和兼容的管理面板。快照日期：2026-11-14。

安装者填写自己的 API Key、Cookie、QQ / 群号即可生成配置。同一凭据会自动写入所有使用它的插件。功能开关、模型选择、超时、记忆策略、群聊频率等沿用快照中的设置；未填写凭据的付费服务需要补齐后才可使用。

本包不带原作者的聊天记录、群成员资料、记忆数据库、Cookie、Token、面板密码或登录会话。人物设定和人工整理的知识素材保留，运行产生的记忆从空白开始。

## 快速开始：Linux / WSL（推荐）

安装 Git、ffmpeg、Node.js、中文字体、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。使用 ZIP 的人可以直接解压后进入目录。

```bash
git clone https://github.com/Desertro/astrbot-share.git
cd astrbot-share
bash start.sh
```

首次启动自动安装锁定的依赖，提示填写管理员 QQ、主群、模型密钥和面板账号。面板密码可回车自动生成，随后从本机 `.env` 查看。OneBot Token 也会自动生成。首次安装会下载浏览器依赖；Linux 环境可能需要系统包管理权限。

如果希望一次填完所有选用服务（包括识图、图床、解析 Cookie 等）：

```bash
uv run --no-sync python tools/setup.py --all --overwrite
```

已有配置会先备份到 `data/setup-backups/`。已有数据库保留。后续直接 `bash start.sh`，不会重新覆盖你在 WebUI 中修改的配置。

## Windows

优先使用 WSL；也提供 PowerShell 启动脚本：

```powershell
.\start.ps1
```

Windows 直接运行需要安装 Python 3.12+、uv、Git、Node.js 和 ffmpeg。部分插件包含 Linux 相关功能，Windows 原生环境没有完整实机验证。

## 连接 QQ

本包提供 AstrBot 的 OneBot v11 接口，NapCat / QQ 客户端由使用者自行安装和登录。在 NapCat 中新增反向 WebSocket：

- 地址：`ws://AstrBot所在主机:6199/ws`（同一主机可填 `ws://127.0.0.1:6199/ws`）
- Token：填写本机 `.env` 的 `AB_ONEBOT_TOKEN`
- 管理面板：`http://localhost:6185`
- 面板账号和密码：本机 `.env` 的 `AB_DASHBOARD_USERNAME` / `AB_DASHBOARD_PASSWORD`

`AB_QQ_1` 是主要管理员 QQ，`AB_QQ_2` 是主群号。其他群 / 私聊账号可选填；空着的身份条目会从生成配置中去掉。多群的人设覆盖和频率差异保留在模板中，可按 [配置说明](CONFIGURATION.md) 映射到自己的群。

## Docker（Linux / WSL）

本机先用 Python 生成配置，再构建容器：

```bash
python3 tools/setup.py --docker
docker compose up -d --build
docker compose logs -f astrbot
```

镜像内安装 AstrBot、插件依赖和 Chromium。`compose.yml` 使用 host 网络，以便保持本地服务地址的语义；Windows / macOS Docker Desktop 需要启用 host 网络，或者自行改成端口映射与可访问的服务地址。`.env` 作为只读文件挂载，不会打入镜像。

## 配置入口

所有分享版凭据集中在本机 `.env`，可参考 [`.env.example`](.env.example)。支持 JSON 引号、特殊符号和转义换行，不执行 shell 表达式。

```bash
python3 tools/setup.py --list           # 看每个变量对应哪些插件字段
python3 tools/setup.py --check          # 检查，不显示密钥
python3 tools/setup.py --non-interactive # 从 .env / 环境变量生成
```

可用环境变量覆盖 `.env`。默认 API 地址和模型选择延续原配置；使用其他服务商时，应在 `.env` 对应的服务地址项和 WebUI 的模型项一起调整。

详细配置、凭据用途、可选依赖及故障处理见 [CONFIGURATION.md](CONFIGURATION.md)，插件清单见 [PLUGINS.md](PLUGINS.md)。

## 这次暂不部署的服务

语音 / GPT-SoVITS、GScore / gsuid_core、ComfyUI / Z-Image 的插件和配置入口保留。本包不提供这些服务的部署、模型权重或运行环境；需要使用时再自行接入。AstrBot 的基础聊天、记忆、识图等使用各自的模型接口。

RSSHub、Codex / DSH 等额外服务也使用安装者自己的实例或登录。具体哪些插件需要它们，见插件说明与配置表。

## 更新与隐私

`.env`、生成的配置、数据库、日志和备份由 `.gitignore` 排除。不要强制上传这些文件。`uv.lock` 锁定核心与常用插件依赖；`requirements-plugins.txt` 补齐各插件声明与实际使用的额外依赖，版本取自原运行环境。

为了保持本地修改，更新前应备份自己的 `.env` 和 `data/`；不要直接在这套定制源码上使用上游覆盖更新。定制模块的变更需要与上游合并后再升级。

本仓库以 AstrBot 的 AGPL-3.0 许可为基础，保留各插件与素材自身的 LICENSE 和来源声明。第三方组件的许可仍由各自文件定义，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 已完成的验证

- 新虚拟环境完成 `uv sync --frozen` 与插件补充依赖安装。
- 33 / 33 插件在禁止网络连接的隔离进程中成功导入。
- 配置向导测试覆盖空白安装、带特殊字符的 Cookie / 密码、可选群号移除、重复生成保护和已有数据库保留。
- 所有 Python 源文件通过语法编译；分享目录经过密钥扫描，未检出安装凭据。上游 UI 公开群邀请链接有明确的单项扫描豁免。
- 原运行目录的配置未改动；分享模板逐项核对保留功能参数。

旧版 `meme-generator==0.1.14` 的包元数据要求 Pillow < 11，而当前 AstrBot 使用 Pillow 11.3。本包沿用原运行环境中的明确覆盖配置；因此 `uv pip check` 会报告这一条已知元数据冲突。安装命令使用 `--override local-dependency-overrides.txt` 保留兼容方案。

未使用原作者的真实凭据做付费 API、QQ 消息收发或全功能联网测试。当前环境没有 Docker Compose 插件，Docker 部署方案未完成实际镜像构建验证；推荐先使用已验证依赖安装的 Linux / WSL 原生启动方式。

维护者可运行：

```bash
uv run --no-sync python -m pytest tests/test_setup.py -q
uv run --no-sync ruff check tools/setup.py share_runtime.py tests/test_setup.py
```

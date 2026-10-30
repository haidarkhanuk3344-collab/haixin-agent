# 本机配置

本项目连接既有 OpenClaw，不自动覆盖其配置、模型或工作区。先确保自己的 OpenClaw 安装可用。

## 配置文件

将 `config.example.json` 复制为 `config.local.json`，替换：

| 字段 | 内容 |
|---|---|
| runtime | 已有 OpenClaw 安装目录 |
| node | 可执行的 Node.js 文件绝对路径 |
| cli | 已安装 OpenClaw 的 openclaw.mjs 路径 |
| state | 包含 openclaw.json 的本机状态目录 |
| price_file | 本仓库 config/prices.json；历史参考估算，不是账单 |
| keychain_service / keychain_account | 下面创建的钥匙串项目名称和账号 |

也可用 `python3 scripts/configure.py --runtime /你的运行目录 --node /你的node路径 --cli /你的openclaw.mjs --state /你的状态目录` 生成。该脚本只生成本仓库的配置，不复制、重置或修改原 OpenClaw 数据；已有配置时拒绝覆盖。

OpenClaw 配置至少需要：`agents.defaults.workspace` 指向自己的绝对工作区，`agents.defaults.model.primary` 为已配置模型；对应 `models.providers` 使用官方 `https://api.deepseek.com`、兼容 `openai-completions` 的API配置和可用模型。官方余额目前只支持 DeepSeek 官方地址，代理或其他提供方不能直接套用。

在 OpenClaw 的模型提供方配置中，将 `apiKey` 设为 `${DEEPSEEK_API_KEY}`。海辛从钥匙串读取并仅传给受控子进程；不要把真实密钥写进本仓库或提交到 Git。

## macOS 钥匙串

使用“钥匙串访问”新建密码项目：

- 钥匙串项目名称：`codex-personal-agent-deepseek`
- 账户名称：`personal-agent`
- 密码：你自己的 DeepSeek API Key

若使用其他名称，同步修改 `config.local.json` 的两个 keychain 字段。首次读取若 macOS 询问授权，核对后允许。不要将密码贴进 Issue、日志或截图。

## 可选的 X 来源

基础聊天、记忆和本地资料不依赖 X。需要 X 时，单独建立 `work/twitter-runtime` 虚拟环境并安装 `config/twitter-requirements.txt`；依赖版本记录自验证环境，安装失败时核对兼容性，不宣称所有版本可用。连接器仅读取明确授权的公开账号/话题，需要自己的浏览器登录和网络条件。

Ego 路径使用它自己的钥匙串凭据；未配置时应返回未连接，不能将作者的浏览器会话复制给他人。

## 备份和恢复

`python3 scripts/backup.py create` 创建本实例快照。自动备份滚动保留30天。恢复前先停止本产品Web和worker，用 `python3 scripts/backup.py restore --path /快照完整路径` 恢复。恢复会建立当前数据的安全快照，并先重放最新遗忘约束。

记忆删除、原始资料删除和事项取消是不同操作。不要手工回拷旧数据库绕过删除记录，也不要把备份提交到Git。

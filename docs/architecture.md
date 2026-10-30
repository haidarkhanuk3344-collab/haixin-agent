# Architecture

海辛的主入口是持续对话。围绕同一个人保存的背景、关系、经历、短期处境和未完事项，是它区别于一次性通用任务工具的核心状态。

| 层 | 实现 |
|---|---|
| 交互 | frontend：React/TypeScript；对话、动态、事项、记忆管理 |
| API与任务 | web.py、jobs.py、worker.py：本机访问、持久队列、取消、恢复 |
| 工具循环 | OpenClaw + agent_tools.py + openclaw-plugin：有范围的资料/文档/记忆/事务操作 |
| 个人状态 | memory.py、life_memory.py、affairs.py：版本、来源、时效、遗忘与独立事项 |
| 知识 | sources.py、knowledge.py：授权来源、版本、关键词检索、分页补读 |
| 主动性 | watch.py、care.py：有依据的变化进入同一工具循环，先准备再接话 |
| 执行约束 | task_guard.py、completion.py：额度、路径、真实回执和有上限的纠错 |
| 触达与恢复 | notifications.py、backups.py、balance.py：独立本地时钟、持久通知、官方余额和恢复 |

程序以真实持久化结果为准；模型不能自行增加权限、扩大文件范围或无限重试。未知结果不默认重放，记忆更正/删除会切换上下文并拦住旧结果发布。删除不代表物理擦除，也不会自动删除用户原始资料。

当前无多用户租户边界。若部署完整公网服务，需要认证、按用户隔离的数据/工具授权、持久存储、任务执行环境、费用归属与渠道适配；不能只修改监听地址。

# Codex WorkBuddy Desktop Bridge

本项目把当前正在运行的 WorkBuddy 桌面 Agent 暴露为 Codex 可调用的本地 MCP Worker。

桥接器不会修改 WorkBuddy 安装目录。它通过 WorkBuddy sidecar 控制管道发现动态 ACP 端口和临时密码，然后使用 ACP HTTP/SSE 接口创建会话、发送任务并收集结果。

桥接器为每个任务启动独立的临时 WorkBuddy CLI Host，并从该任务自己的 POST SSE 流收集事件和 `session_end`。相邻 prompt 至少间隔 1 秒，已经启动的模型和工具调用在彼此隔离的 runtime 中并行运行。

所有 WorkBuddy Worker 会话在首次 prompt 前设置为 `fullAccess`。工具直接执行而不询问；若 WorkBuddy 仍发出 ACP 权限请求，桥接器选择 `allow_always`。身份行为边界由各角色提示词约束，不再由工具权限层强制。

## MCP 工具

- `workbuddy_status`：检查桌面连接或任务状态
- `workbuddy_start`：异步派发任务，可通过 `identity` 选择内置身份，并设置模型、推理强度和审查复用参数
- `workbuddy_wait`：最多等待 55 秒并返回当前状态
- `workbuddy_cancel`：取消任务
- `workbuddy_list`：列出桥接器进程内的任务

## 手动连通测试

```powershell
python -m workbuddy_bridge.test_hello
```

WorkBuddy 必须处于运行状态。精简动作日志保存在 `work/workbuddy-logs/`，认证密码只
保存在进程内存中，不会写入日志。

动作日志不会保存 `agent_thought_chunk`、`agent_message_chunk`、prompt、最终回答或
工具输出正文。它只根据工具类型记录“正在读取文件、正在搜索代码、正在运行测试、
正在检查依赖、正在访问资料”等结构化动作，以及任务完成、取消和失败状态。连续
同类动作会合并并记录 `count`，文件明细最多保留五个项目内相对路径。

成功完成的桥接会话会登记到 WorkBuddy 上方“任务”列表，并保留 WorkBuddy 自动生成的标题；被取消的会话不会登记。

`workbuddy_start` 的默认任务超时为 300 秒。路由技能在 5 分钟内未获得终态时会先取消仍在运行的 WorkBuddy session，再使用对应的 Codex 子代理完成兜底。

四个身份的完整提示词保存在桥接器内部。Codex 调用 `workbuddy_start` 时只传
`identity="online-search|S1|S2|S3"` 和任务正文，不再在每次 MCP 调用中
重复传递身份说明。省略 `identity` 时仍兼容原来的自由 prompt 调用。

## 审查与复审

S1、S2、S3 首次审查时传入绝对路径 `review_target`，桥接器会把
`sessionId + 身份 + cwd + 审查目标 + 文件 SHA-256` 持久绑定到
`%USERPROFILE%\.workbuddy\codex-review-sessions.json`。

再次审查同一目标时传入相同的 `identity`、`cwd`、`review_target`，并设置
`resume_review=true`。桥接器会查找该身份最近绑定的旧会话，通过 ACP
`session/load` 追加新一轮消息，不会另建会话。也可以用
`resume_session_id` 明确指定旧会话。

复审提示词由桥接器固定注入，要求 Worker 同时完成：

- 回归检查：逐项验证上一轮问题是否已修复；
- 增量检查：重新读取完整目标并从头审查，发现新引入或上一轮遗漏的问题。

找不到旧会话、身份不一致、目标不一致或对话记录缺失时，调用会明确失败，不会静默退化为全新审查。同一个旧 `sessionId` 也不允许同时执行两轮复审。

## 并发隔离

桥接器不会让多个任务共享 WorkBuddy ACP Host。每个任务通过 WorkBuddy sidecar 的
`session.create` 启动一个临时 CLI Host，并使用独立端口、ACP connection 和 session。
相邻任务仍至少间隔 1 秒派发，但已经启动的模型和工具调用可以并行执行。

任务结束、失败或取消后，桥接器会关闭 ACP connection，并通过准确的 runtime ID
终止对应临时 Host；完成的会话仍注册到 WorkBuddy 顶部“任务”历史中。
临时 Host 显式使用 WorkBuddy 的真实配置目录，因此 transcript 会写入 WorkBuddy
读取的 `projects` 目录，而不会落入独立 CLI 默认使用的 `.codebuddy` 目录。

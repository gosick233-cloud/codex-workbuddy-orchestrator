# Codex WorkBuddy Desktop Bridge

##简单说：让 Codex 负责理解需求、拆分任务和汇总结果，WorkBuddy 作为本地执行子代理，负责联网搜索、代码审查等具体工作；两者通过 MCP 桥接器协作。

本项目把当前正在运行的 WorkBuddy 桌面 Agent 暴露为 Codex 可调用的本地 MCP Worker。

桥接器不会修改 WorkBuddy 安装目录。它通过 WorkBuddy sidecar 控制管道发现动态 ACP 端口和临时密码，然后使用 ACP HTTP/SSE 接口创建会话、发送任务并收集结果。

桥接器为每个任务启动独立的临时 WorkBuddy CLI Host，并从该任务自己的 POST SSE 流收集事件和 `session_end`。相邻 prompt 至少间隔 1 秒，已经启动的模型和工具调用在彼此隔离的 runtime 中并行运行。

所有 WorkBuddy Worker 会话在首次 prompt 前设置为 `fullAccess`。工具直接执行而不询问；若 WorkBuddy 仍发出 ACP 权限请求，桥接器选择 `allow_always`。身份行为边界由各角色提示词约束，不再由工具权限层强制。

## WorkBuddy 会话界面示例

通过桥接器派发 `online-search` 后，WorkBuddy 会在自己的会话界面中显示角色提示词、任务内容和最终整理结果；会话标题仍由 WorkBuddy 自动生成。

<p align="center">
  <img src="docs/images/workbuddy-session-example.png" alt="WorkBuddy online-search 会话界面示例" width="820">
</p>

## MCP 工具

- `workbuddy_status`：检查桌面连接或任务状态
- `workbuddy_start`：异步派发任务，可通过 `identity` 选择内置身份，并设置模型、推理强度和审查复用参数
- `workbuddy_wait`：最多等待 55 秒并返回当前状态
- `workbuddy_cancel`：取消任务
- `workbuddy_list`：列出桥接器进程内的任务

## 身份角色与配置

桥接器内置四个身份。`identity` 只选择角色，角色的完整提示词由桥接器从
`workbuddy_bridge/identities.py` 注入；调用方不需要每次重复发送身份说明。

| 身份 | 默认职责 | 支持复用旧审查会话 |
| --- | --- | --- |
| `online-search` | 联网检索、资料核对、来源整理 | 否 |
| `S1` | 语法、拼写、明显代码错误和低级问题 | 是 |
| `S2` | 依赖漏洞、供应链和高风险依赖检查 | 是 |
| `S3` | 命名、格式、结构和可维护性检查 | 是 |

角色提示词只负责定义行为边界和输出要求。所有 Worker 会话仍然使用
`fullAccess`，工具不会逐次询问权限；如果需要限制工具能力，必须同时修改权限配置，
不能只依赖角色提示词。

### 调用时可以覆盖的参数

`workbuddy_start` 支持以下与角色相关的参数：

```json
{
  "identity": "S2",
  "model": "deepseek-v4-flash",
  "reasoning_effort": "low",
  "cwd": "C:\\path\\to\\project",
  "timeout_seconds": 300,
  "prompt": "检查这个项目的依赖风险，不要修改文件。"
}
```

- `identity`：填写 `online-search`、`S1`、`S2` 或 `S3`；也接受 `online_search`、`s1`、
  `s2`、`s3` 这些别名。省略时保留自由 prompt 的兼容行为。
- `model`：指定 WorkBuddy 模型。省略时，临时 Host 默认使用
  `deepseek-v4-flash`。
- `reasoning_effort`：传递 WorkBuddy 的推理强度，例如 `low`、`medium`、`high` 或
  `max`；省略时使用 WorkBuddy 当前默认值。通过路由技能调用时，可在技能配置中统一传入
  `low`。
- `cwd`：任务的绝对工作目录；省略时使用桥接器进程当前目录。审查任务应明确传入项目目录。
- `timeout_seconds`：单个 WorkBuddy 任务的最长执行时间，默认 300 秒。
- `review_target`、`resume_review`、`resume_session_id`：仅用于 S1/S2/S3 的审查复用，
  规则见[审查与复审](#审查与复审)。

### 修改现有角色或新增角色

直接编辑 `workbuddy_bridge/identities.py` 中的 `IDENTITIES` 字典即可修改角色提示词。
例如新增一个专门检查测试的身份：

```python
IDENTITIES = {
    # 保留现有 online-search、S1、S2、S3 …
    "S4": """你是 S4，负责测试质量检查。

重点检查：
- 测试是否覆盖关键路径
- 断言是否有效
- 是否存在明显的漏测和脆弱测试

不要修改文件，只返回审查结果。""",
}

_IDENTITY_ALIASES = {
    # 保留现有别名 …
    "s4": "S4",
}
```

新增角色后还需要：

1. 在 `_IDENTITY_ALIASES` 中加入至少一个规范化入口，否则 `workbuddy_start` 会拒绝该身份。
2. 如果该角色也需要复用旧审查会话，把它加入 `workbuddy_bridge/review_sessions.py` 的
   `REVIEW_IDENTITIES`，并为它设计同样的“回归检查 + 增量检查”规则。
3. 如果使用 Codex 的 `workbuddy-agent-routing` 技能，还要同步修改该技能的身份列表和路由条件；
   仅修改桥接器不会自动让 Codex 选择新角色。
4. 运行测试确认身份名、提示词拼接和复审约束没有回归：

```powershell
python -m unittest discover -s workbuddy_bridge -p 'test_*.py' -v
python -m compileall -q workbuddy_bridge
```

不要把 API 密钥、个人绝对路径、WorkBuddy 会话内容或用户数据写进角色提示词。角色定义会随
仓库公开；运行时日志和本地会话注册表位于被 `.gitignore` 排除的目录中。

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

---
name: workbuddy-agent-routing
description: Route delegated work to four named WorkBuddy identities. Use whenever a task requires internet search, current-information lookup, fact checking, source collection, code review, review follow-up, 联网搜索, 资料核对, 来源整理, 事实查证, 时效信息查询, 代码审核, 代码审查, 复审, or 再次审查. Send research to online-search and code reviews to S1, S2, and S3. Prefer WorkBuddy with deepseek-v4-flash and low reasoning.
---

# WorkBuddy Agent Routing

The bridge owns the four WorkBuddy identity prompts. Keep the identity scopes distinct.

## Runtime selection

1. Call `workbuddy_status` before dispatching any identity.
2. Treat WorkBuddy as available only when the tool returns both `ok: true` and `connected: true`.
3. If the WorkBuddy MCP tool is missing, its transport is closed, discovery fails, or no WorkBuddy runtime is running, report that WorkBuddy is unavailable and stop. Do not silently switch to another route.
4. Do not run multiple routes for the same task at the same time. Surface ordinary task-specific failures.

## WorkBuddy route

For every WorkBuddy task, pass only the task body and its identity key. Do not prepend, quote, or repeat the identity instructions:

```text
workbuddy_start(
  identity=<"online-search" | "S1" | "S2" | "S3">,
  prompt=<complete task only>,
  cwd=<working directory>,
  model="deepseek-v4-flash",
  reasoning_effort="low",
  timeout_seconds=300
)
```

For a first S1/S2/S3 review, also pass `review_target=<absolute reviewed file or directory>`. This binds the returned WorkBuddy session to that identity, working directory, and target.

Then call `workbuddy_wait` in intervals of at most 55 seconds until the task reaches a terminal state or 300 seconds have elapsed since dispatch. If the task is still non-terminal at that point, cancel it and report the timeout; do not launch another execution route.

- The bridge creates native WorkBuddy `working` tasks. Leave task titles to WorkBuddy's own automatic naming; never send or write a custom title.
- The bridge injects the registered identity instructions. Never include those instructions in `prompt`, even when the same identity is called repeatedly.
- The bridge runs every WorkBuddy identity in `fullAccess`; tool calls execute without interactive approval. Enforce each identity's behavioral restrictions through its complete identity instructions.
- Run every identity from the same absolute path of the current project.
- Start S1, S2, and S3 before waiting for results. The bridge waits until each prompt is accepted, spaces prompt starts by at least one second, then runs accepted tasks concurrently with sessionId-isolated event channels.
- Return the worker findings to the main task; the orchestrator remains responsible for synthesis and final decisions.

## Review continuation

When the user asks to review the same target again, treat it as a re-review unless they explicitly request a fresh or independent review.

```text
workbuddy_start(
  identity=<"S1" | "S2" | "S3">,
  prompt=<current re-review requirements only>,
  cwd=<same absolute project working directory>,
  review_target=<same absolute reviewed file or directory>,
  resume_review=true,
  model="deepseek-v4-flash",
  reasoning_effort="low",
  timeout_seconds=300
)
```

- Start S1, S2, and S3 re-reviews before waiting, as with first reviews. Each identity resumes its own most recently bound session for that target.
- The bridge loads the old WorkBuddy conversation and injects the mandatory re-review protocol. Do not repeat identity instructions or manually paste the previous findings.
- Every re-review must contain both a regression check of all prior findings and a complete incremental scan for newly introduced or previously missed issues.
- If a specific prior session is required, pass its ID as `resume_session_id=<sessionId>` instead of relying on automatic lookup.
- If the bridge reports that no matching old session exists, or that identity, cwd, target, or transcript validation failed, surface the failure. Never silently retry without `resume_review`, because that would create a new conversation.
- When the user explicitly asks for a fresh, independent, or clean-slate review, omit `resume_review` and start a new session with `review_target`.

## Result boundary

- After WorkBuddy returns a successful terminal result, treat that result as the sole research input for the main task.
- Do not call web search, fetch source URLs, use another research connector, spawn a research subagent, or dispatch another Worker to verify, supplement, or repeat the result.
- Only organize, translate, summarize, compare, and format the returned material. Do not add unsupported current facts from the orchestrator's own knowledge.
- If the returned material is incomplete, contradictory, or lacks evidence, state that limitation in the final answer instead of searching again.
- Perform another search only when the user explicitly asks for a new search or re-verification.

## Routing rules

- Internet research, current facts, fact checking, or source gathering: use `online-search`.
- Code review: use S1, S2, and S3 together unless the user explicitly requests one identity.
- A mixed research and code-review request may use `online-search` plus all three reviewers.
- Explicit identity names from the user override automatic routing.

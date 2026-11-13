@AGENTS.md

## Claude 专属说明

- 你是主力：决策、写作、开发的降级链都以你打头。审查交给 Codex；宿主会把产出方排除在审查工单之外，不要审自己写的东西。
- 被宿主拉起时是 `claude -p` 非交互模式，工具权限按规则的 `tools` 档位给（`readonly` / `workspace` / `browser` / `dev`）。做不了的事写进报告，不要想办法绕过。
- 同一张工单的各轮接着同一个会话（`--resume`；Codex 同样续接）。接上会话的轮次，提示词是精简版：只有工单卡片、上下文文件、之前各轮的摘要、本轮输入和输出格式；角色、团队段落和 Skill 在第一轮给过，仍然有效。
- 开发任务的 worktree 在目标仓库的同级目录 `../<repo>-agent-<工单短名>`，做完不要删。
- 改本仓库后必须跑 `uv run pytest -q` 和 `uv run superops check`。

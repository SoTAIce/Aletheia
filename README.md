# Aletheia 交接分支（handoff）

这个分支只放 Agent 之间交接用的文件，与 `main` 没有共同历史，永远不并入 `main`（D-077）。

- `session-handoff.md`：当前任务、阻塞和下一步。新会话从顶部“新会话从这里开始”读起。
- `progress.md`：执行记录。
- `feature_list.json`：功能状态和证据（`main` 的 `init.ps1` 在 `.handoff/` 存在时校验它）。
- `docs/harness/baseline.md`、`docs/harness/history/`：基线清单和旧状态存档。

用法（在 `main` 工作区根目录）：

```
git fetch origin handoff
git worktree add .handoff handoff
```

以后在 `.handoff/` 里 `git pull`；收尾时在 `.handoff/` 里提交，按授权推送。`main` 的 `.gitignore` 已忽略 `.handoff/`。

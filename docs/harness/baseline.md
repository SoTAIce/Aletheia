# 已记录基线（Recorded Baseline）

> 2026-11-12 起本文件和 `history/` 只在 `handoff` 分支上，检出在仓库内的 `.handoff/`（D-077）。核对命令要在 `main` 工作区根目录运行（它比较的是 `main` 工作区的 Git 状态），清单路径前面加 `.handoff/`。下面的命令已按此改过：读 `.handoff/docs/harness/history/...`，`self_path` 也按 `.handoff/` 解析。新清单照旧写 `self_path`（不带 `.handoff/`）。

当前未提交清单：[baseline-2026-11-11-stage4c.json](history/baseline-2026-11-11-stage4c.json)（stage4c-001，以 HEAD `70f7470`（4b）为起点；第 4 阶段 4c 删除规划模式和骨架后的工作区，D-076）。用户自己的未提交差异只剩 `scripts/` 下三个脚本的删除；`planner.py`、`step_executor.py`、`tool_calling_step_runner.py` 三个笔记文件已按用户确认（“已保存，做 4c-3”）删除，它们删除前的哈希记在清单的 `user_deleted_after_confirmation`；README 已在 `14cd9ca` 按用户决定重写并提交。上一份：[baseline-2026-11-09-kb3.json](history/baseline-2026-11-09-kb3.json)（kb-index-cli-003，已提交为 `64b2a49`）。上一份：[baseline-2026-11-09-kb2.json](history/baseline-2026-11-09-kb2.json)（kb-index-cli-002，已提交为 `44d435c`）。上一份：[baseline-2026-11-08-deps-kb.json](history/baseline-2026-11-08-deps-kb.json)（deps-cleanup-001、kb-index-cli-001、tool-list-dedup-001，均已提交）。上一份：[baseline-2026-11-08-cleanup-stage0.json](history/baseline-2026-11-08-cleanup-stage0.json)。本轮 cleanup-stage0-001 以 HEAD `2b76f01` 为起点，记录用户原有差异、九个已跟踪文件删除，以及本轮测试和文档修改；实现时未提交、索引未改，随后经用户授权（“修完再提交”）已提交，清单里的本轮条目此后应与提交内容一致。用户四个已修改文件的启动哈希另存于清单，三个用户脚本删除仍保留。

本轮清单的 `startup_snapshot` 保留开工时原清单，`user_protected_sha256` 固定用户四个修改文件的启动哈希；当前文件哈希单独刷新，不覆盖保护证据。原方案和上一轮 kb2 清单原样保留。用户 README、三个中文注释文件的哈希不变，三个脚本删除项继续保留；索引未改，未暂存、还原、提交、amend 或推送。

本轮验证限确定性测试和两次不带 --confirm、--prune 的文档预演；同入口离线审计拦截服务连接、嵌入及 socket 连接/地址解析，并逐块校验字节上限。计数对应刷新本轮清单前后的具体文档快照，详见 progress.md；真实服务验证仍待用户授权。

新清单记录修改/未跟踪文件 SHA-256 与已删路径；`self_path` 单独声明清单自身，不对自身做循环哈希。核对时只将这个清单路径排除在文件哈希集合之外，仍检查其存在。
清单是交接证据，不是备份，也不是提交授权。不得为了清除 Git 状态而 reset、覆盖或提交用户文件。

## 核对命令（只读）

```powershell
.venv/Scripts/python.exe -I -c "import json,hashlib,pathlib,subprocess as sp;m=json.load(open('.handoff/docs/harness/history/baseline-2026-11-11-stage4c.json',encoding='utf-8'));anc=sp.run(['git','merge-base','--is-ancestor',m['base_commit'],'HEAD']).returncode==0;d=[p for k in ('modified_tracked','untracked') for p,s in m[k].items() if not pathlib.Path(p).is_file() or hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()!=s];g=[p for p in m['deleted_tracked'] if pathlib.Path(p).exists()];known=set(m['modified_tracked'])|set(m['untracked'])|set(m['deleted_tracked'])|{m['self_path']};x=[l[3:] for l in sp.run(['git','status','--porcelain=v1','-uall'],capture_output=True,text=True).stdout.splitlines() if l[3:] not in known];print('base ancestor', 'OK' if anc and pathlib.Path('.handoff',m['self_path']).is_file() else 'NO');print('drift', d or 'none');print('deleted-restored', g or 'none');print('uncommitted-outside-manifest', x or 'none')"
```

预期四项：base ancestor OK；drift none；deleted-restored none；uncommitted-outside-manifest none。
之后新任务出现清单外变化很正常，应保留本清单，并记录新任务完成后的状态，不倒改原保护哈希来掩盖变化。

## 历史清单

- [2026-11-08 网页选择](history/baseline-2026-11-08-loop-web.json)：上一轮实现时的清单，后续提交和记录更新产生的漂移按历史解释，不倒改。

- [2026-11-06](history/baseline-2026-11-06.json)：长期记忆已提交后的用户选择未提交差异。
- [2026-11-04](history/baseline-2026-11-04.json)：首次 Harness 提交后的状态。
- [本轮开始时的用户差异](history/loop-web-start-2026-11-08.json)：验证 README、三个注释文件和三个脚本删除项未被本轮更改。

旧清单仅用于追溯，不应要求包含新功能的工作区与旧哈希完全一致。

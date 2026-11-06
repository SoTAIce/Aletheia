# 已记录基线（Recorded Baseline）

课程 L05/L06 要求一个已提交的 git checkpoint。2026-11-04 经用户授权，harness 管理文件、docs 和最小测试已在本地提交，提交的父提交是 `9138ac1`（Agent Team V1 冻结）。
用户选择暂不提交的差异（README、三个文件的中文注释、三个脚本的删除；长期记忆源码已于 2026-11-06 提交）记录在 [baseline-2026-11-06.json](history/baseline-2026-11-06.json) 中（2026-11-06 刷新：中文注释和后续提交改变了哈希，旧清单中被删的测试已按 D-030 恢复；旧清单 [baseline-2026-11-04.json](history/baseline-2026-11-04.json) 只作历史保留）：修改文件和未跟踪文件的 SHA-256，以及已删除路径列表。

核对内容：`base_commit` 是否为 HEAD 的祖先；用户差异是否原样；其余文件是否全部已提交（git status 中没有清单以外的条目）。清单不能防止工作区丢失；剩余批次的提交由用户授权。

## 核对命令（只读）

```powershell
.venv/Scripts/python.exe -I -c "import json,hashlib,pathlib,subprocess as sp;m=json.load(open('docs/harness/history/baseline-2026-11-06.json',encoding='utf-8'));anc=sp.run(['git','merge-base','--is-ancestor',m['base_commit'],'HEAD']).returncode==0;d=[p for k in ('modified_tracked','untracked') for p,s in m[k].items() if not pathlib.Path(p).is_file() or hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()!=s];g=[p for p in m['deleted_tracked'] if pathlib.Path(p).exists()];known=set(m['modified_tracked'])|set(m['untracked'])|set(m['deleted_tracked']);x=[l[3:] for l in sp.run(['git','status','--porcelain=v1','-uall'],capture_output=True,text=True).stdout.splitlines() if l[3:] not in known];print('base ancestor', 'OK' if anc else 'NO');print('drift', d or 'none');print('deleted-restored', g or 'none');print('uncommitted-outside-manifest', x or 'none')"
```

四项都应为 OK/none。`uncommitted-outside-manifest` 不为空，说明有新改动既没提交也没记录：要么提交，要么在会话结束时记入新清单（见 clean-state 清单）。

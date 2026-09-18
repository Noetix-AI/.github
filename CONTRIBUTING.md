# 贡献方式

本组织下**任何仓库**的代码更新都走分支 + Pull Request，不直推 `main`——**包括组织管理员**。

```bash
git config core.hooksPath .githooks   # clone 后装一次（仓库内带了 pre-push，会拦住推 main）
git switch -c <类型>/<简述>            # feat/ fix/ docs/ chore/ …
git push -u origin HEAD
gh pr create                          # PR 里写：改了什么、怎么验证的
```

- `main` 由仓库负责人合并；自己开的 PR 可以自己合，但要先让改动被看见。
- PR 描述写清验证方式（跑了什么、结果是什么），不要只写「已测试」。
- 不要 force push `main`。
- commit 信息：`<类型>: <摘要>`。

各仓库根目录的 `AGENTS.md` 是给人和 AI 编码助手共同看的约定，以它为准。

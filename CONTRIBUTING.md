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

## 测试驱动开发（2026-10-30 起）

有行为变化的代码改动走 TDD：先写测试，在改动前的代码上确认它失败，再实现到通过；PR 里贴失败 / 通过输出（PR 模板有对应栏）。
纯文档 / 注释 / 配置改动写「不适用 TDD：原因」；真机 / GPU / 听感等无法自动化的写手动验证。细则以各仓库 `AGENTS.md` 的「测试驱动开发」节为准。

**CI 检查**：本仓库提供可复用 workflow [`tdd-check.yml`](.github/workflows/tdd-check.yml)，在 PR 上检查两件事——

1. 改了代码（默认 `**/*.py`）却没改测试（默认 `tests/**`、`test_*.py`、`*_test.py`、`conftest.py`）→ 失败；
2. 把 PR 改动 / 新增的测试放到改动前的代码上运行，至少要有一个测试**因行为不符而失败**（import 不到新模块、收集错误单独标出，默认不算）；再在改动后的代码上运行相关测试，必须全部通过，且改动前失败的用例至少有一个在改动后实际通过。

CI 验证的是 base / head 之间的差异证据，不能证明实际编写顺序，也不能证明每个失败都由目标行为引起；仍须独立 Review 核对失败原因。

在一个仓库启用：新建 `.github/workflows/tdd.yml`

```yaml
name: tdd
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, unlabeled]
jobs:
  tdd:
    uses: Noetix-AI/.github/.github/workflows/tdd-check.yml@main
    with:
      install-command: python -m pip install pytest   # 在 base / head 两个独立环境分别执行
      head-test-paths: tests                          # 改动后跑的范围；留空只跑改过的测试文件
      # code-globs / test-globs / test-command / allow-import-only 等见 workflow 文件里的 inputs
```

- `install-command` 是 Bash 命令，在 base / head 各自的源码根目录、全新的 venv 中分别执行；环境不继承系统包，并清除 `PYTHONPATH` / `PYTHONHOME`。用 `python` / `pip` 指向当前 venv，需安装本项目时可用 `python -m pip install pytest -e .`，不要写宿主 Python / pip 的绝对路径。任一阶段创建或安装失败都标红，不能算作测试先失败。
- 配置安装命令时，`test-command` 支持以 `python`、`python3`、对应绝对解释器路径或 `pytest` 开始的命令；它们都会改用各阶段 venv 的解释器。测试命令不经 shell 展开。不要用包装脚本或其他环境管理器绕过隔离。
- 本地直接调用 `scripts/tdd_check.py` 时，省略 `--install-command` 保持原有行为、使用当前 Python 环境；CI 默认传入安装命令并使用隔离环境。豁免和不涉及代码的 PR 会在创建环境前返回。
- 确实不需要测试的 PR 加 `no-test-needed` 标签跳过。标签要在各仓库先建一次：
  `gh label create no-test-needed -R Noetix-AI/<仓库> --color cfd3d7 --description "不适用 TDD，PR 里写明原因"`。
- 新模块的测试在改动前只会报 import 错误，检查会标红并说明：先提交一个最小桩让测试能 import，再确认它因行为不符失败；仓库确有需要可设 `allow-import-only: true`（仍会标 ⚠️）。
- **限制**：组织是 GitHub Free，私有仓库开不了分支保护 / ruleset，这个检查只能让 PR 显示红叉，**挡不住合并**；合并前看一眼检查结果是合并人的责任。
- 检查脚本在 [`scripts/tdd_check.py`](scripts/tdd_check.py)，它自己的测试在 `tests/`（`python -m pytest tests`）。

各仓库根目录的 `AGENTS.md` 是给人和 AI 编码助手共同看的约定，以它为准。

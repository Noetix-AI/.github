#!/usr/bin/env python3
"""测试先行（TDD）检查：给 PR 的 base / head 两个提交做两件事。

1. 改了代码却没改测试 → 失败（带豁免标签则整体跳过）。
2. 测试先行证据：把 PR 改动 / 新增的测试文件放到改动前的代码（base 与 head 的 merge-base）上运行，
   至少要有一个测试「因行为不符而失败」；import 不到模块（ImportError / ModuleNotFoundError）
   和收集 / setup 错误单独标出，不算这种失败。再在 head 上运行相关测试，必须全部通过。

结果写到 --summary（GitHub Actions 里传 $GITHUB_STEP_SUMMARY），退出码 0 = 通过，1 = 失败。
只用标准库；测试命令里含 pytest 时会自动追加 --junitxml 用来逐条分类。
"""
import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_CODE_GLOBS = "**/*.py"
DEFAULT_TEST_GLOBS = "tests/**,**/test_*.py,**/*_test.py,**/conftest.py"
DEFAULT_RUNNABLE_GLOBS = "**/test_*.py,**/*_test.py"
IMPORT_RE = re.compile(r"\b(ModuleNotFoundError|ImportError)\b")
OUTPUT_TAIL = 150


# ---------- glob ----------

def _glob_regex(pattern):
    """gitignore 风格：不含 / 的模式匹配任意层的文件名；** 跨目录，* 和 ? 不跨 /。"""
    if "/" not in pattern:
        pattern = "**/" + pattern
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def match_any(path, patterns):
    return any(_glob_regex(p).match(path) for p in patterns)


def split_list(value):
    return [x.strip() for x in re.split(r"[,\n]", value or "") if x.strip()]


# ---------- git ----------

def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def changed_files(repo, base, head):
    """返回 [(状态, 路径)]，改名拆成删 + 增。"""
    out = git(repo, "diff", "--name-status", "--no-renames", base, head)
    result = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        result.append((status[0], path))
    return result


# ---------- 运行测试 ----------

class RunResult:
    def __init__(self, cmd, code, output, cases, junit):
        self.cmd, self.code, self.output, self.junit = cmd, code, output, junit
        self.cases = cases  # [(名字, 类别)]，类别：passed / failed / import / error / skipped

    def names(self, kind):
        return [n for n, k in self.cases if k == kind]


def parse_junit(path):
    cases = []
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        cls, name = case.get("classname", ""), case.get("name", "")
        full = f"{cls}::{name}" if cls else name
        failure, error = case.find("failure"), case.find("error")
        problem = failure if failure is not None else error
        if problem is None:
            kind = "skipped" if case.find("skipped") is not None else "passed"
        else:
            text = (problem.get("message") or "") + "\n" + (problem.text or "")
            if IMPORT_RE.search(text):
                kind = "import"
            elif failure is not None:
                kind = "failed"
            else:
                kind = "error"
        cases.append((full, kind))
    return cases


def run_tests(command, paths, cwd):
    cmd = shlex.split(command) + list(paths)
    junit = None
    if "pytest" in command:
        fd, junit = tempfile.mkstemp(suffix=".xml", prefix="tdd-junit-")
        os.close(fd)
        os.unlink(junit)
        # 有文件 import 失败时 pytest 默认整轮中止，其他文件里的行为失败就看不到了
        cmd += [f"--junitxml={junit}", "--continue-on-collection-errors"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    output = proc.stdout + proc.stderr
    cases = []
    if junit and os.path.exists(junit):
        try:
            cases = parse_junit(junit)
        except ET.ParseError:
            cases = []
        os.unlink(junit)
    shown = [c for c in cmd if not c.startswith("--junitxml=") and c != "--continue-on-collection-errors"]
    return RunResult(shown, proc.returncode, output, cases, bool(cases) or (junit is not None and proc.returncode in (0, 5)))


# ---------- 汇报 ----------

class Report:
    def __init__(self):
        self.rows, self.details, self.failed = [], [], False

    def step(self, name, icon, note, fail=False):
        self.rows.append((name, icon, note))
        self.failed = self.failed or fail

    def detail(self, title, run):
        tail = "\n".join(run.output.rstrip().splitlines()[-OUTPUT_TAIL:])
        self.details.append(
            f"<details><summary>{title}（退出码 {run.code}）</summary>\n\n"
            f"`{' '.join(shlex.quote(c) for c in run.cmd)}`\n\n```text\n{tail}\n```\n\n</details>\n")

    def render(self):
        lines = ["## 测试先行（TDD）检查", "", "| 步骤 | 结果 | 说明 |", "|---|---|---|"]
        for name, icon, note in self.rows:
            note = note.replace("|", "\\|")
            lines.append(f"| {name} | {icon} | {note} |")
        lines += ["", "**结论：失败**" if self.failed else "**结论：通过**", ""]
        lines += self.details
        lines += ["约定见各仓库 AGENTS.md「测试驱动开发」节；确实不需要测试的改动（纯文档 / 注释 / 配置）"
                  "加 `no-test-needed` 标签并在 PR 里写「不适用 TDD：原因」。", ""]
        return "\n".join(lines)


def fmt_list(items, limit=10):
    items = list(items)
    shown = ", ".join(f"`{x}`" for x in items[:limit])
    return shown + (f" 等 {len(items)} 个" if len(items) > limit else "")


# ---------- 主流程 ----------

def check(args):
    rep = Report()
    repo = os.path.abspath(args.repo)
    labels = split_list(args.labels)
    if args.skip_label and args.skip_label in labels:
        rep.step("豁免标签", "⏭️", f"PR 带 `{args.skip_label}` 标签，已跳过全部检查")
        return rep

    code_globs, test_globs = split_list(args.code_globs), split_list(args.test_globs)
    runnable_globs = split_list(args.runnable_globs)
    base = git(repo, "merge-base", args.base, args.head)
    changes = changed_files(repo, base, args.head)
    tests = [p for s, p in changes if s != "D" and match_any(p, test_globs)]
    code = [p for s, p in changes if match_any(p, code_globs) and not match_any(p, test_globs)]

    # 1. 改代码却没改测试
    if not code:
        rep.step("改代码是否带测试", "⏭️", "这个 PR 没有改代码（按 code-globs），不涉及 TDD 检查")
        return rep
    if not tests:
        rep.step("改代码是否带测试", "❌", f"改了代码但没有改测试：{fmt_list(code)}", fail=True)
        return rep
    rep.step("改代码是否带测试", "✅", f"代码 {len(code)} 个文件，测试相关 {len(tests)} 个文件：{fmt_list(tests)}")

    runnable = [p for p in tests if match_any(p, runnable_globs)]

    # 2. 改动前的代码上运行改动 / 新增的测试
    if not runnable:
        rep.step("改动前先失败", "❌", "改了测试相关文件，但没有可运行的测试文件（按 runnable-test-globs）", fail=True)
    else:
        tmp = tempfile.mkdtemp(prefix="tdd-base-")
        wt = os.path.join(tmp, "base")
        try:
            git(repo, "worktree", "add", "--detach", "-q", wt, base)
            for path in tests:
                data = subprocess.run(["git", "show", f"{args.head}:{path}"], cwd=repo,
                                      check=True, capture_output=True).stdout
                dest = Path(wt) / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            run = run_tests(args.test_command, runnable, wt)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo, capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)
        judge_base(rep, run, args.allow_import_only)
        rep.detail(f"改动前（{base[:8]}）运行改动 / 新增的测试", run)

    # 3. head 上运行相关测试
    head_sha = git(repo, "rev-parse", "HEAD")
    if head_sha != git(repo, "rev-parse", args.head):
        rep.step("改动后通过", "❌", f"工作区不在 head（{args.head[:8]}），无法运行", fail=True)
        return rep
    head_paths = split_list(args.head_test_paths) or runnable
    if not head_paths:
        rep.step("改动后通过", "❌", "没有可运行的测试", fail=True)
        return rep
    run = run_tests(args.test_command, head_paths, repo)
    if run.code == 0:
        rep.step("改动后通过", "✅", f"{fmt_list(head_paths, 5)} 全部通过（{len(run.names('passed'))} 个用例）")
    elif run.code == 5:
        rep.step("改动后通过", "❌", f"改动后测试未通过：没有收集到任何测试（{fmt_list(head_paths, 5)}）", fail=True)
    else:
        bad = run.names("failed") + run.names("import") + run.names("error")
        rep.step("改动后通过", "❌", "改动后测试未通过" + (f"：{fmt_list(bad)}" if bad else f"（退出码 {run.code}）"), fail=True)
    rep.detail("改动后运行相关测试", run)
    return rep


def judge_base(rep, run, allow_import_only):
    name = "改动前先失败"
    failed, imports, errors = run.names("failed"), run.names("import"), run.names("error")
    if not run.cases and run.code not in (0, 5):
        # 不是 pytest 或没产出 junit：只能看退出码和输出
        if IMPORT_RE.search(run.output):
            imports = ["（输出中出现 ImportError / ModuleNotFoundError，未逐条分类）"]
        else:
            failed = [f"（退出码 {run.code}，未逐条分类）"]
    import_note = f"；另有 import 错误（不算先失败）：{fmt_list(imports)}" if imports else ""
    if failed:
        rep.step(name, "✅", f"改动前有测试因行为不符而失败：{fmt_list(failed)}{import_note}")
    elif imports and not errors:
        note = (f"只有 import 错误（多半是新模块尚不存在），不能证明测试在检查行为：{fmt_list(imports)}。"
                "先提交一个最小桩让测试能 import、再确认它因行为不符失败")
        if allow_import_only:
            rep.step(name, "⚠️", note + "；本仓库配置 allow-import-only，记为通过")
        else:
            rep.step(name, "❌", note, fail=True)
    elif imports or errors:
        rep.step(name, "❌", f"只有收集 / setup 错误，没有因行为不符的失败：{fmt_list(errors + imports)}", fail=True)
    elif run.code == 5:
        rep.step(name, "❌", "改动前没有收集到任何测试", fail=True)
    elif run.code == 0:
        rep.step(name, "❌", "改动 / 新增的测试在改动前就全部通过，不能证明测试先行", fail=True)
    else:
        rep.step(name, "❌", f"测试命令异常退出（退出码 {run.code}），没有可判定的失败", fail=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="已检出到 head 的仓库目录")
    ap.add_argument("--base", required=True, help="PR 目标分支提交（实际取与 head 的 merge-base）")
    ap.add_argument("--head", required=True)
    ap.add_argument("--labels", default="", help="PR 标签，逗号分隔")
    ap.add_argument("--skip-label", default="no-test-needed")
    ap.add_argument("--code-globs", default=DEFAULT_CODE_GLOBS)
    ap.add_argument("--test-globs", default=DEFAULT_TEST_GLOBS, help="测试相关文件（含 fixtures / conftest），改动前运行时一并带上")
    ap.add_argument("--runnable-test-globs", dest="runnable_globs", default=DEFAULT_RUNNABLE_GLOBS,
                    help="测试相关文件里可直接交给测试命令的测试文件")
    ap.add_argument("--test-command", default="python -m pytest")
    ap.add_argument("--head-test-paths", default="", help="head 上运行的测试路径，留空 = 改动 / 新增的测试文件")
    ap.add_argument("--allow-import-only", action="store_true",
                    help="改动前只有 import 错误时记 ⚠️ 通过（默认失败）")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    args = ap.parse_args(argv)

    rep = check(args)
    text = rep.render()
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())

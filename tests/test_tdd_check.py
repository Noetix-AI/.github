"""tdd_check.py 的测试：每个用例在临时 git 仓库里造一对 base / head 提交，再跑检查脚本。"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tdd_check.py"
PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

BASE_CODE = """\
def add(a, b):
    return a + b
"""


class Sandbox:
    """临时仓库：write() 写文件，commit() 提交并返回 sha。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        env = {**os.environ, **GIT_ENV}
        return subprocess.run(["git", *args], cwd=self.root, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    def commit(self, msg="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")


class TddCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Sandbox(Path(self.tmp.name) / "repo")
        self.repo.write("calc.py", BASE_CODE)
        self.repo.write("tests/test_add.py", """\
            from calc import add

            def test_add():
                assert add(1, 2) == 3
            """)
        self.repo.write("README.md", "demo\n")
        self.base = self.repo.commit("base")

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, *extra):
        summary = Path(self.tmp.name) / "summary.md"
        cmd = [sys.executable, str(SCRIPT), "--repo", str(self.repo.root),
               "--base", self.base, "--head", self.repo.git("rev-parse", "HEAD"),
               "--test-command", PYTEST, "--summary", str(summary), *extra]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        text = summary.read_text(encoding="utf-8") if summary.exists() else ""
        return proc, text

    def assert_result(self, proc, text, passed):
        detail = f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}\nsummary:\n{text}"
        self.assertEqual(proc.returncode, 0 if passed else 1, detail)
        self.assertIn("结论：通过" if passed else "结论：失败", text, detail)

    # 1. 改代码却没改测试
    def test_code_change_without_test_change_fails(self):
        self.repo.write("calc.py", BASE_CODE + "\ndef sub(a, b):\n    return a - b\n")
        self.repo.commit("code only")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)
        self.assertIn("改了代码但没有改测试", text)
        self.assertIn("calc.py", text)

    # 2. 新测试在 base 上失败、在 head 上通过 —— 标准 TDD
    def test_new_test_failing_on_base_and_passing_on_head_passes(self):
        self.repo.write("calc.py", BASE_CODE.replace("return a + b", "return a + b if b != 99 else 0"))
        self.repo.commit("break on purpose")
        self.base = self.repo.git("rev-parse", "HEAD")
        self.repo.write("calc.py", BASE_CODE)
        self.repo.write("tests/test_add.py", """\
            from calc import add

            def test_add():
                assert add(1, 2) == 3

            def test_add_99():
                assert add(1, 99) == 100
            """)
        self.repo.commit("fix with test")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=True)
        self.assertIn("test_add_99", text)

    # 3. 新测试在 base 上就通过 —— 不能证明测试先行
    def test_new_test_already_passing_on_base_fails(self):
        self.repo.write("calc.py", BASE_CODE + "\n# refactor\n")
        self.repo.write("tests/test_add_more.py", """\
            from calc import add

            def test_add_zero():
                assert add(0, 0) == 0
            """)
        self.repo.commit("test passes on base")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)
        self.assertIn("在改动前就全部通过", text)

    # 4. 新测试 import 一个新模块：base 上只有 import 错误，要单独标出，默认不算先失败
    def write_new_module_change(self):
        self.repo.write("mul.py", "def mul(a, b):\n    return a * b\n")
        self.repo.write("tests/test_mul.py", """\
            from mul import mul

            def test_mul():
                assert mul(2, 3) == 6
            """)
        self.repo.commit("new module")

    def test_import_only_failure_on_base_is_flagged_and_fails(self):
        self.write_new_module_change()
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)
        self.assertIn("只有 import 错误", text)

    def test_import_only_failure_can_be_allowed_but_still_flagged(self):
        self.write_new_module_change()
        proc, text = self.run_check("--allow-import-only")
        self.assert_result(proc, text, passed=True)
        self.assertIn("只有 import 错误", text)

    def test_import_error_inside_test_body_is_not_behavior_failure(self):
        self.repo.write("mul.py", "def mul(a, b):\n    return a * b\n")
        self.repo.write("tests/test_mul.py", """\
            def test_mul():
                from mul import mul
                assert mul(2, 3) == 6
            """)
        self.repo.commit("lazy import")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)
        self.assertIn("只有 import 错误", text)

    def test_behavior_failure_plus_import_error_passes_and_lists_both(self):
        self.repo.write("calc.py", BASE_CODE + "\ndef sub(a, b):\n    return a - b\n")
        self.repo.write("mul.py", "def mul(a, b):\n    return a * b\n")
        self.repo.write("tests/test_add.py", """\
            import calc

            def test_add():
                assert calc.add(1, 2) == 3

            def test_sub():
                assert hasattr(calc, "sub") and calc.sub(3, 1) == 2
            """)
        self.repo.write("tests/test_mul.py", "from mul import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
        self.repo.commit("mixed")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=True)
        self.assertIn("test_sub", text)
        self.assertIn("import 错误", text)

    # 5. 豁免标签
    def test_skip_label_skips(self):
        self.repo.write("calc.py", BASE_CODE + "\n# no test needed\n")
        self.repo.commit("label")
        proc, text = self.run_check("--labels", "documentation,no-test-needed")
        self.assert_result(proc, text, passed=True)
        self.assertIn("已跳过", text)
        self.assertIn("no-test-needed", text)

    # 6. 只改文档：不涉及代码
    def test_docs_only_change_is_not_applicable(self):
        self.repo.write("README.md", "demo v2\n")
        self.repo.commit("docs")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=True)
        self.assertIn("没有改代码", text)

    # 7. head 上测试不通过
    def test_head_tests_failing_fails(self):
        self.repo.write("calc.py", BASE_CODE.replace("a + b", "a - b"))
        self.repo.write("tests/test_add.py", """\
            from calc import add

            def test_add():
                assert add(1, 2) == 3

            def test_add_neg():
                assert add(-1, -1) == -2
            """)
        self.repo.commit("broken impl")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)
        self.assertIn("改动后测试未通过", text)

    # 8. 可配置代码 / 测试范围；只改了不在范围内的代码就不涉及
    def test_custom_globs(self):
        self.repo.write("tools/helper.py", "X = 1\n")
        self.repo.commit("outside scope")
        proc, text = self.run_check("--code-globs", "src/**/*.py")
        self.assert_result(proc, text, passed=True)
        self.assertIn("没有改代码", text)

    # 9. head 上运行指定的全部相关测试，而不只是改过的文件
    def test_head_test_paths_runs_whole_suite(self):
        self.repo.write("tests/test_other.py", "def test_other():\n    assert False\n")
        self.repo.commit("pre-existing broken test")
        self.base = self.repo.git("rev-parse", "HEAD")
        self.repo.write("calc.py", BASE_CODE + "\ndef sub(a, b):\n    return a - b\n")
        self.repo.write("tests/test_sub.py", "from calc import sub\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
        self.repo.commit("sub")
        proc, text = self.run_check("--head-test-paths", "tests")
        self.assert_result(proc, text, passed=False)
        self.assertIn("改动后测试未通过", text)


class GlobTests(unittest.TestCase):
    def test_glob_semantics(self):
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            from tdd_check import match_any
        finally:
            sys.path.pop(0)
        self.assertTrue(match_any("a/b/c.py", ["**/*.py"]))
        self.assertTrue(match_any("c.py", ["**/*.py"]))
        self.assertTrue(match_any("pkg/test_x.py", ["test_*.py"]))
        self.assertTrue(match_any("tests/fixtures/data.json", ["tests/**"]))
        self.assertFalse(match_any("src/tests_util.py", ["tests/**"]))
        self.assertFalse(match_any("src/a/b.py", ["src/*.py"]))
        self.assertTrue(match_any("src/a/b.py", ["src/**/*.py"]))


if __name__ == "__main__":
    unittest.main()

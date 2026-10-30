"""tdd_check.py 的测试：每个用例在临时 git 仓库里造一对 base / head 提交，再跑检查脚本。"""
import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
import venv
from pathlib import Path
from unittest import mock

import pytest

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

    def test_missing_expected_import_error_is_behavior_failure(self):
        self.repo.write("calc.py", BASE_CODE + "\ndef optional_backend():\n    return None\n")
        self.base = self.repo.commit("missing required error behavior")
        for error in ("ImportError", "ModuleNotFoundError"):
            with self.subTest(error=error):
                self.repo.write("calc.py", BASE_CODE + f"\ndef optional_backend():\n    raise {error}('backend unavailable')\n")
                self.repo.write("tests/test_backend.py", f"""\
                    import pytest
                    from calc import optional_backend

                    def test_required_error():
                        with pytest.raises({error}):
                            optional_backend()
                    """)
                self.repo.commit("raise expected backend error")
                proc, text = self.run_check()
                self.assert_result(proc, text, passed=True)
                self.assertIn("DID NOT RAISE", text)

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

    def test_head_test_paths_cannot_omit_changed_tests(self):
        self.repo.write("calc.py", BASE_CODE + "\ndef sub(a, b):\n    return 0\n")
        self.repo.write("tests/test_sub.py", """\
            import calc

            def test_sub():
                assert hasattr(calc, "sub") and calc.sub(3, 1) == 2
            """)
        self.repo.commit("broken behavior outside configured head suite")
        proc, text = self.run_check("--head-test-paths", "tests/test_add.py")
        self.assert_result(proc, text, passed=False)
        self.assertIn("test_sub", text)

    def test_failure_on_base_then_skip_on_head_is_not_passed_evidence(self):
        self.repo.write("calc.py", BASE_CODE + "\nFEATURE_READY = True\n")
        self.repo.write("tests/test_feature.py", """\
            import pytest
            import calc

            def test_feature():
                if getattr(calc, "FEATURE_READY", False):
                    pytest.skip("feature is covered by an unavailable integration service")
                assert getattr(calc, "FEATURE_READY", False)

            def test_other():
                assert calc.add(1, 2) == 3
            """)
        self.repo.commit("feature with skipped verification")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)
        self.assertIn("test_feature", text)

    def test_quoted_code_filename_cannot_bypass_missing_test_check(self):
        self.repo.git("config", "core.quotePath", "true")
        for filename in ("统计.py", "has\ttab.py", "line\nbreak.py"):
            with self.subTest(filename=filename):
                self.repo.write("tools/" + filename, "VALUE = 2\n")
                self.repo.commit("code only with a quoted path")
                proc, text = self.run_check()
                self.assert_result(proc, text, passed=False)
                self.assertIn("改了代码但没有改测试", text)

    def test_pytest_startup_error_is_not_behavior_failure(self):
        self.repo.write("pytest.ini", "[pytest]\naddopts = --missing-option\n")
        self.base = self.repo.commit("old environment has invalid pytest option")
        self.repo.write("pytest.ini", "[pytest]\n")
        self.repo.write("calc.py", BASE_CODE.replace("a + b", "abs(a + b)"))
        self.repo.write("tests/test_positive.py", """\
            from calc import add

            def test_positive():
                assert add(2, 3) == 5
            """)
        self.repo.commit("test passes old behavior but old pytest cannot start")
        proc, text = self.run_check()
        self.assert_result(proc, text, passed=False)

    def prepare_installed_source_change(self):
        """离线安装器写真实 .pth，等价于 editable 的源码路径，不需要下载构建工具。"""
        self.repo.write("src/tdd_probe/__init__.py", "def value():\n    return 1\n")
        self.repo.write("install_source.py", """\
            import json
            import os
            from pathlib import Path
            import sys
            import sysconfig

            assert sys.prefix != sys.base_prefix, "must install inside a venv"
            assert "PYTHONPATH" not in os.environ
            assert "PYTHONHOME" not in os.environ
            source = str(Path.cwd() / "src")
            Path(sysconfig.get_path("purelib"), "tdd-probe.pth").write_text(source + "\\n")
            with open(os.environ["TDD_INSTALL_LOG"], "a") as stream:
                stream.write(json.dumps({"source": source, "prefix": sys.prefix}) + "\\n")
            """)
        self.base = self.repo.commit("src-layout project before behavior change")
        self.repo.write("src/tdd_probe/__init__.py", "def value():\n    return 2\n")
        self.repo.write("tests/test_probe.py", """\
            import os
            from pathlib import Path
            import sys
            import tdd_probe

            def test_new_behavior():
                with open(os.environ["TDD_TEST_LOG"], "a") as stream:
                    stream.write(str(Path(tdd_probe.__file__).resolve()) + "\\n")
                assert tdd_probe.value() == 2
            """)
        self.repo.commit("new behavior and regression")

    def run_installed_check(self, test_command=PYTEST, install_command="python install_source.py"):
        spec = importlib.util.spec_from_file_location("isolated_tdd_check", SCRIPT)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        args = argparse.Namespace(
            repo=str(self.repo.root), base=self.base, head=self.repo.git("rev-parse", "HEAD"),
            labels="", skip_label="no-test-needed", code_globs=checker.DEFAULT_CODE_GLOBS,
            test_globs=checker.DEFAULT_TEST_GLOBS, runnable_globs=checker.DEFAULT_RUNNABLE_GLOBS,
            test_command=test_command, head_test_paths="", allow_import_only=False,
            install_command=install_command,
        )
        install_log = Path(self.tmp.name) / "installs.jsonl"
        test_log = Path(self.tmp.name) / "test-imports.txt"
        # 仅 fixture 显式提供当前测试进程的依赖；嵌套 venv 不会继承外层 venv 的包。
        real_builder = venv.EnvBuilder
        dependency_sites = {str(Path(pytest.__file__).resolve().parent.parent)}
        dependency_sites.update(
            str(Path(path).resolve()) for path in sys.path
            if Path(path).name in ("site-packages", "dist-packages") and Path(path).is_dir()
        )

        class OfflineBuilder(real_builder):
            def create(builder, directory):
                super().create(directory)
                context = builder.ensure_directories(directory)
                query_env = {key: value for key, value in os.environ.items()
                             if key not in ("PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__")}
                purelib = subprocess.check_output(
                    [context.env_exe, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                    env=query_env, text=True).strip()
                self.assertIn(Path(directory).resolve(), Path(purelib).resolve().parents)
                Path(purelib, "fixture-test-dependencies.pth").write_text(
                    "\n".join(sorted(dependency_sites)) + "\n", encoding="utf-8")

        def offline_builder(*positional, **kwargs):
            self.assertFalse(kwargs.get("system_site_packages", False))
            return OfflineBuilder(*positional, **kwargs)

        with mock.patch.object(venv, "EnvBuilder", side_effect=offline_builder), mock.patch.dict(
            os.environ, {"PYTHONPATH": str(self.repo.root / "src"), "PYTHONHOME": "",
                         "TDD_INSTALL_LOG": str(install_log), "TDD_TEST_LOG": str(test_log)}
        ):
            rep = checker.check(args)
        installs = [json.loads(line) for line in install_log.read_text().splitlines()] if install_log.exists() else []
        imports = test_log.read_text().splitlines() if test_log.exists() else []
        return rep, installs, imports

    def test_installed_src_project_uses_separate_base_and_head_environments(self):
        self.prepare_installed_source_change()
        rep, installs, imports = self.run_installed_check()
        self.assertFalse(rep.failed, rep.render())
        self.assertEqual(len(installs), 2, rep.render())
        self.assertNotEqual(installs[0]["prefix"], installs[1]["prefix"])
        self.assertNotEqual(installs[0]["source"], installs[1]["source"])
        self.assertEqual(Path(installs[1]["source"]), (self.repo.root / "src").resolve())
        self.assertEqual(imports, [str(Path(item["source"]) / "tdd_probe/__init__.py") for item in installs])

    def test_absolute_pytest_launcher_uses_isolated_interpreter(self):
        self.prepare_installed_source_change()
        launcher = Path(self.tmp.name) / "host-bin" / "pytest"
        launcher.parent.mkdir()
        launcher.write_text(f"#!{sys.executable}\nimport pytest\nraise SystemExit(pytest.console_main())\n")
        launcher.chmod(0o755)
        rep, installs, imports = self.run_installed_check(shlex.quote(str(launcher)) + " -q")
        self.assertFalse(rep.failed, rep.render())
        self.assertEqual(len(installs), 2, rep.render())
        self.assertEqual(imports, [str(Path(item["source"]) / "tdd_probe/__init__.py") for item in installs])

    def test_py_test_alias_cannot_hide_failed_then_skipped_test(self):
        self.prepare_installed_source_change()
        self.repo.write("tests/test_probe.py", """\
            import pytest
            from tdd_probe import value

            def test_new_behavior():
                if value() == 2:
                    pytest.skip("verification unavailable")
                assert value() == 2

            def test_other():
                assert True
            """)
        self.repo.commit("py.test alias must retain per-case recovery evidence")
        rep, installs, imports = self.run_installed_check("py.test -q -p no:cacheprovider")
        self.assertTrue(rep.failed, rep.render())
        self.assertIn("没有实际通过", rep.render())
        self.assertIn("test_new_behavior", rep.render())

    def test_python_module_option_forms_retain_recovery_evidence(self):
        self.repo.write("calc.py", BASE_CODE + "\nFEATURE_READY = True\n")
        self.repo.write("tests/test_feature.py", """\
            import pytest
            import calc

            def test_feature():
                if getattr(calc, "FEATURE_READY", False):
                    pytest.skip("verification unavailable")
                assert getattr(calc, "FEATURE_READY", False)

            def test_other():
                assert True
            """)
        self.repo.commit("module entry forms must preserve failed-case recovery")
        for options in (
            "-m pytest", "-mpytest", "-Bmpytest", "-Bqm pytest",
            "-BW ignore -mpytest", "-BWignore -X dev -Bqm pytest",
            "-BXutf8 -W ignore --check-hash-based-pycs always -mpytest",
            "--check-hash-based-pycs never -BqW ignore -Xutf8 -m pytest",
            "-m pytest.__main__",
        ):
            with self.subTest(options=options):
                command = shlex.quote(sys.executable) + " " + options + " -q -p no:cacheprovider"
                proc, text = self.run_check("--test-command", command)
                self.assert_result(proc, text, passed=False)
                self.assertIn("没有实际通过", text)

        spec = importlib.util.spec_from_file_location("module_option_checker", SCRIPT)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        self.repo.write("pytest_driver.py", "print('pytest argument only')\n")
        # 真实 Python 执行这些命令：-c / 文件入口之后，以及 -W / -X 参数中的 -m 都不是模块入口。
        for options in (
            ["-c", "print('pytest')", "-m", "pytest"],
            ["-Bqc", "print('pytest')", "-mpytest"],
            ["-BW", "-mpytest", "-c", "pass"],
            ["-BX", "-mpytest", "-c", "pass"],
            ["--check-hash-based-pycs", "always", "-c", "pass", "-mpytest"],
            ["pytest_driver.py", "-m", "pytest"],
            ["--", "pytest_driver.py", "-mpytest"],
            ["-BqVmpytest"],
        ):
            with self.subTest(non_module_options=options):
                run = checker.run_tests(shlex.join([sys.executable, *options]), [], self.repo.root)
                self.assertEqual(run.code, 0, run.output)
                self.assertFalse(run.junit, run.output)

    def test_failed_environment_install_does_not_run_or_count_as_failed_test(self):
        self.prepare_installed_source_change()
        for command in ("python -c 'raise SystemExit(13)'", "false | true\npython install_source.py"):
            with self.subTest(command=command):
                rep, installs, imports = self.run_installed_check(install_command=command)
                self.assertTrue(rep.failed, rep.render())
                self.assertEqual(imports, [], "tests must not run after installation failed")
                self.assertIn("环境安装", rep.render())
                self.assertNotIn("改动前有测试因行为不符而失败", rep.render())


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

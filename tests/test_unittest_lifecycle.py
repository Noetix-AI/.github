"""Run actual pytest/unittest lifecycles; rendered traceback text is not evidence."""
import importlib.util
import shlex
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tdd_check.py"
spec = importlib.util.spec_from_file_location("lifecycle_tdd_check", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def run_pytest(directory, source, traceback="no"):
    (directory / "test_lifecycle.py").write_text(source, encoding="utf-8")
    command = shlex.join([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          f"--tb={traceback}"])
    return checker.run_tests(command, ["test_lifecycle.py"], directory)


def assert_not_behavior_failure(run):
    assert run.code == 1, run.output
    assert run.names("failed") == [], run.output
    assert run.names("error"), run.output
    report = checker.Report()
    checker.judge_base(report, run, False)
    assert report.failed, report.render()


@pytest.mark.parametrize("traceback", ["no", "line", "short"])
@pytest.mark.parametrize("stage", ["setUp", "tearDown", "cleanup", "asyncSetUp",
                                   "asyncTearDown", "asyncCleanup"])
def test_unittest_lifecycle_errors_do_not_count_as_behavior_failures(tmp_path, stage, traceback):
    asynchronous = stage.startswith("async")
    base = "unittest.IsolatedAsyncioTestCase" if asynchronous else "unittest.TestCase"
    source = "import unittest\nfrom pathlib import Path\nimport subject\n"
    source += f"class Regression({base}):\n"
    if stage == "cleanup":
        source += "    def setUp(self):\n        self.addCleanup(self.lifecycle)\n"
    elif stage == "asyncCleanup":
        source += "    async def asyncSetUp(self):\n        self.addAsyncCleanup(self.lifecycle)\n"
    method = "lifecycle" if stage in ("cleanup", "asyncCleanup") else stage
    source += f"    {'async ' if asynchronous else ''}def {method}(self):\n"
    source += "        subject.initialize()\n"
    source += f"    {'async ' if asynchronous else ''}def test_body(self):\n"
    source += "        Path('body-ran').write_text('yes')\n        self.assertTrue(True)\n"
    (tmp_path / "subject.py").write_text("def initialize():\n    raise RuntimeError('fixture unavailable')\n")
    run = run_pytest(tmp_path, source, traceback)
    assert_not_behavior_failure(run)
    assert (tmp_path / "body-ran").exists() == (stage not in ("setUp", "asyncSetUp"))
    (tmp_path / "subject.py").write_text("def initialize():\n    pass\n")
    recovered = run_pytest(tmp_path, source, traceback)
    assert recovered.code == 0, recovered.output
    assert recovered.names("passed"), recovered.output


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unittest_body_failure_remains_behavior_evidence(tmp_path, asynchronous):
    base = "unittest.IsolatedAsyncioTestCase" if asynchronous else "unittest.TestCase"
    source = f"import unittest\nclass Regression({base}):\n"
    source += f"    {'async ' if asynchronous else ''}def test_setUp_tearDown_cleanup(self):\n"
    source += "        self.fail('setUp tearDown cleanup are just message text')\n"
    run = run_pytest(tmp_path, source)
    assert run.code == 1, run.output
    assert run.names("failed"), run.output
    assert run.names("error") == [], run.output
    report = checker.Report()
    checker.judge_base(report, run, False)
    assert not report.failed, report.render()


def test_unittest_body_failure_is_preserved_with_cleanup_error(tmp_path):
    run = run_pytest(tmp_path, """import unittest
class Regression(unittest.TestCase):
    def cleanup(self):
        raise RuntimeError('cleanup unavailable')
    def test_body(self):
        self.addCleanup(self.cleanup)
        self.fail('actual behavior assertion')
""")
    assert run.code == 1, run.output
    assert run.names("failed"), run.output
    assert run.names("error"), run.output


@pytest.mark.parametrize("teardown", [False, True])
def test_pytest_fixture_errors_remain_errors(tmp_path, teardown):
    body = "yield\n    " if teardown else ""
    run = run_pytest(tmp_path, "import pytest\n@pytest.fixture\ndef fixture():\n    " + body +
                     "raise RuntimeError('fixture unavailable')\n" +
                     "def test_body(fixture):\n    assert True\n")
    assert_not_behavior_failure(run)


def test_unittest_lifecycle_import_policy_is_unchanged(tmp_path):
    run = run_pytest(tmp_path, """import unittest
class Regression(unittest.TestCase):
    def setUp(self):
        raise ModuleNotFoundError('missing optional module')
    def test_body(self):
        self.assertTrue(True)
""")
    assert run.code == 1, run.output
    assert run.names("import"), run.output
    assert run.names("failed") == [], run.output
    for allow_import_only in (False, True):
        report = checker.Report()
        checker.judge_base(report, run, allow_import_only)
        assert report.failed != allow_import_only, report.render()


@pytest.mark.parametrize("entry", ["isolated_python", "pytest_executable"])
def test_lifecycle_hook_uses_the_selected_pytest_interpreter(tmp_path, entry):
    (tmp_path / "test_lifecycle.py").write_text("""import unittest
class Regression(unittest.TestCase):
    def setUp(self):
        raise RuntimeError('fixture unavailable')
    def test_body(self):
        self.assertTrue(True)
""")
    if entry == "isolated_python":
        command = [sys.executable, "-I", "-m", "pytest"]
    else:
        launcher = tmp_path / "pytest"
        launcher.write_text(f"#!{sys.executable}\nimport pytest\nraise SystemExit(pytest.console_main())\n")
        launcher.chmod(0o755)
        command = [str(launcher)]
    command += ["-q", "--tb=no", "-p", "no:cacheprovider"]
    run = checker.run_tests(shlex.join(command), ["test_lifecycle.py"], tmp_path)
    assert_not_behavior_failure(run)


@pytest.mark.parametrize("stage", ["setUp", "tearDown", "cleanup", "asyncSetUp",
                                   "asyncTearDown", "asyncCleanup"])
def test_lifecycle_subtest_errors_do_not_count_as_behavior_failures(tmp_path, stage):
    asynchronous = stage.startswith("async")
    base = "unittest.IsolatedAsyncioTestCase" if asynchronous else "unittest.TestCase"
    source = "import unittest\nfrom pathlib import Path\nimport subject\n"
    source += f"class Regression({base}):\n"
    if stage == "cleanup":
        source += "    def setUp(self):\n        self.addCleanup(self.lifecycle)\n"
    elif stage == "asyncCleanup":
        source += "    async def asyncSetUp(self):\n        self.addAsyncCleanup(self.lifecycle)\n"
    method = "lifecycle" if stage in ("cleanup", "asyncCleanup") else stage
    source += f"    {'async ' if asynchronous else ''}def {method}(self):\n"
    source += "        with self.subTest(fixture=True):\n            subject.initialize()\n"
    source += f"    {'async ' if asynchronous else ''}def test_body(self):\n"
    source += "        Path('body-ran').write_text('yes')\n        self.assertTrue(True)\n"
    (tmp_path / "subject.py").write_text("def initialize():\n    raise RuntimeError('fixture unavailable')\n")
    run = run_pytest(tmp_path, source)
    assert_not_behavior_failure(run)
    assert (tmp_path / "body-ran").exists() == (stage not in ("setUp", "asyncSetUp"))
    (tmp_path / "subject.py").write_text("def initialize():\n    pass\n")
    recovered = run_pytest(tmp_path, source)
    assert recovered.code == 0, recovered.output
    assert recovered.names("passed"), recovered.output


@pytest.mark.parametrize("asynchronous", [False, True])
def test_body_subtest_failure_remains_behavior_evidence(tmp_path, asynchronous):
    base = "unittest.IsolatedAsyncioTestCase" if asynchronous else "unittest.TestCase"
    source = f"import unittest\nclass Regression({base}):\n"
    source += f"    {'async ' if asynchronous else ''}def test_body(self):\n"
    source += "        with self.subTest(body=True):\n            self.fail('actual body assertion')\n"
    run = run_pytest(tmp_path, source)
    assert run.code == 1, run.output
    assert run.names("failed"), run.output
    report = checker.Report()
    checker.judge_base(report, run, False)
    assert not report.failed, report.render()


@pytest.mark.parametrize("body_subtest", [False, True])
@pytest.mark.parametrize("stage", ["tearDown", "cleanup", "asyncTearDown", "asyncCleanup"])
def test_body_failure_is_preserved_with_lifecycle_subtest_errors(tmp_path, stage, body_subtest):
    asynchronous = stage.startswith("async")
    base = "unittest.IsolatedAsyncioTestCase" if asynchronous else "unittest.TestCase"
    source = f"import unittest\nclass Regression({base}):\n"
    if stage == "cleanup":
        source += "    def setUp(self):\n        self.addCleanup(self.lifecycle)\n"
    elif stage == "asyncCleanup":
        source += "    async def asyncSetUp(self):\n        self.addAsyncCleanup(self.lifecycle)\n"
    method = "lifecycle" if stage in ("cleanup", "asyncCleanup") else stage
    source += f"    {'async ' if asynchronous else ''}def {method}(self):\n"
    source += "        with self.subTest(fixture=True):\n            raise RuntimeError('fixture unavailable')\n"
    source += f"    {'async ' if asynchronous else ''}def test_body(self):\n"
    if body_subtest:
        source += "        with self.subTest(body=True):\n            self.fail('actual body assertion')\n"
    else:
        source += "        self.fail('actual body assertion')\n"
    run = run_pytest(tmp_path, source)
    assert run.code == 1, run.output
    assert run.names("failed"), run.output
    report = checker.Report()
    checker.judge_base(report, run, False)
    assert not report.failed, report.render()


@pytest.mark.parametrize("same_exception", [False, True])
@pytest.mark.parametrize("body_subtest", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_body_and_cleanup_can_raise_the_same_exception(tmp_path, asynchronous, body_subtest, same_exception):
    base = "unittest.IsolatedAsyncioTestCase" if asynchronous else "unittest.TestCase"
    mock = "AsyncMock" if asynchronous else "Mock"
    source = f"import unittest\nfrom unittest.mock import {mock}\nclass Regression({base}):\n"
    source += "    def setUp(self):\n"
    effect = "RuntimeError('backend unavailable')" if same_exception else "[RuntimeError('body unavailable'), RuntimeError('cleanup unavailable')]"
    source += f"        self.backend = {mock}(side_effect={effect})\n"
    source += f"        self.{'addAsyncCleanup' if asynchronous else 'addCleanup'}(self.backend)\n"
    source += f"    {'async ' if asynchronous else ''}def test_body(self):\n"
    operation = "await self.backend()" if asynchronous else "self.backend()"
    if body_subtest:
        source += f"        with self.subTest(body=True):\n            {operation}\n"
    else:
        source += f"        {operation}\n"
    run = run_pytest(tmp_path, source)
    assert run.code == 1, run.output
    assert run.names("failed"), run.output
    if not body_subtest:
        assert run.names("error"), run.output
    report = checker.Report()
    checker.judge_base(report, run, False)
    assert not report.failed, report.render()

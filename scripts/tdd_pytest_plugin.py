"""Retain unittest lifecycle failures before pytest renders them as call failures."""
from contextlib import contextmanager
from functools import wraps
import unittest

import pytest


def remember_exception(item, exc, lifecycle):
    if lifecycle is not None and exc.__traceback__ is not None:
        # One exception object may be raised again by cleanup (e.g. Mock's
        # side_effect). Each throw prepends distinct traceback nodes, while the
        # queued unittest report retains the traceback captured for that throw.
        item.__dict__.setdefault("_tdd_unittest_exceptions", {})[exc.__traceback__] = lifecycle


def exception_kind(item, excinfo):
    if excinfo is None:
        return None
    if isinstance(excinfo.value, ImportError):
        return "import"
    phases = getattr(item, "_tdd_unittest_exceptions", {})
    trace = excinfo.tb
    while trace is not None:
        if trace in phases:
            # The first match is this throw; older nodes may belong to an
            # earlier body exception reused by a subsequent lifecycle callback.
            return "error" if phases[trace] else "failed"
        trace = trace.tb_next
    return "failed"


class LifecyclePlugin:
    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        instance = getattr(item, "instance", None)
        if not isinstance(instance, unittest.TestCase):
            yield
            return
        names = ("_callSetUp", "_callTestMethod", "_callTearDown", "_callCleanup", "subTest")
        missing = object()
        previous = {name: instance.__dict__.get(name, missing) for name in names}

        def observe_dispatch(original, lifecycle):
            @wraps(original)
            def observed(*args, **kwargs):
                before = getattr(item, "_tdd_unittest_phase", None)
                item._tdd_unittest_phase = lifecycle
                try:
                    return original(*args, **kwargs)
                except BaseException as exc:
                    remember_exception(item, exc, lifecycle)
                    raise
                finally:
                    item._tdd_unittest_phase = before
            return observed

        original_subtest = instance.subTest

        @contextmanager
        def observed_subtest(*args, **kwargs):
            with original_subtest(*args, **kwargs):
                try:
                    yield
                except BaseException as exc:
                    # Observe before unittest swallows it. Python 3.10 reports
                    # these at run's end, after async frames/phase have gone.
                    remember_exception(item, exc, getattr(item, "_tdd_unittest_phase", None))
                    raise

        try:
            for name in names[:-1]:
                setattr(instance, name, observe_dispatch(getattr(instance, name), name != "_callTestMethod"))
            instance.subTest = observed_subtest
            yield
        finally:
            for name, value in previous.items():
                if value is missing:
                    instance.__dict__.pop(name, None)
                else:
                    setattr(instance, name, value)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        # pytest/unittest can replace a lifecycle subTest exception with a queued
        # body exception. Retain both real failures before that queue is consumed.
        initial_kind = exception_kind(item, call.excinfo)
        outcome = yield
        report = outcome.get_result()
        if (report.when == "call" and report.failed
                and isinstance(getattr(item, "instance", None), unittest.TestCase)):
            kinds = {kind for kind in (initial_kind, exception_kind(item, call.excinfo)) if kind is not None}
            failures = item.__dict__.setdefault("_tdd_unittest_failures", [])
            failures.append((report, kinds or {"failed"}))
        elif report.when == "teardown":
            failures = item.__dict__.pop("_tdd_unittest_failures", [])
            item.__dict__.pop("_tdd_unittest_exceptions", None)
            if not failures:
                return
            # Several subTest call reports share one JUnit testcase, followed by
            # a passing ordinary call report. Aggregate actual failures without
            # letting that pass erase them or lifecycle errors hide body failures.
            kinds = set().union(*(kinds for _, kinds in failures))
            kind = "failed" if "failed" in kinds else "error" if "error" in kinds else "import"
            for failed_report, _ in failures:
                failed_report.user_properties.append(("tdd-unittest-outcome", kind))
            if report.passed:
                # JUnit normally finalizes from teardown. If teardown itself
                # fails it finalizes each stored call report separately instead.
                report.user_properties.append(("tdd-unittest-outcome", kind))


def pytest_runtest_makereport(item, call):
    """Expose the same wrapper when a local pytest executable loads -p."""
    yield from LifecyclePlugin().pytest_runtest_makereport(item, call)


pytest_runtest_makereport = pytest.hookimpl(hookwrapper=True, tryfirst=True)(pytest_runtest_makereport)


def pytest_runtest_call(item):
    yield from LifecyclePlugin().pytest_runtest_call(item)


pytest_runtest_call = pytest.hookimpl(hookwrapper=True, tryfirst=True)(pytest_runtest_call)

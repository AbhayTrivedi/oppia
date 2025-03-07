# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Script for running backend tests in parallel.

This should not be run directly. Instead, navigate to the oppia/ folder and
execute:

    python -m scripts.run_backend_tests

You can also append the following options to the above command:

    --verbose prints the output of the tests to the console.

    --test_target=core.controllers.editor_test runs only the tests in the
        core.controllers.editor_test module. (You can change
        "core.controllers.editor_test" to any valid module path.)

    --test_path=core/controllers runs all tests in test files in the
        core/controllers directory. (You can change "core/controllers" to any
        valid subdirectory path.)

    --test_shard=1 runs all tests in shard 1.

    --generate_coverage_report generates a coverage report as part of the final
        test output (but it makes the tests slower).

    --ignore_coverage only has an affect when --generate_coverage_report
        is specified. In that case, the tests will not fail just because
        code coverage is not 100%.

Note: If you've made some changes and tests are failing to run at all, this
might mean that you have introduced a circular dependency (e.g. module A
imports module B, which imports module C, which imports module A). This needs
to be fixed before the tests will run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time

from . import install_third_party_libs
# This installs third party libraries before importing other files or importing
# libraries that use the builtins python module (e.g. build, python_utils).
install_third_party_libs.main()

from core import python_utils  # isort:skip  pylint: disable=wrong-import-position, wrong-import-order
from . import common  # isort:skip  pylint: disable=wrong-import-position, wrong-import-order
from . import concurrent_task_utils  # isort:skip  pylint: disable=wrong-import-position, wrong-import-order
from . import servers  # isort:skip  pylint: disable=wrong-import-position, wrong-import-order

COVERAGE_DIR = os.path.join(
    os.getcwd(), os.pardir, 'oppia_tools',
    'coverage-%s' % common.COVERAGE_VERSION)
COVERAGE_MODULE_PATH = os.path.join(
    os.getcwd(), os.pardir, 'oppia_tools',
    'coverage-%s' % common.COVERAGE_VERSION, 'coverage')
COVERAGE_EXCLUSION_LIST_PATH = os.path.join(
    os.getcwd(), 'scripts', 'backend_tests_incomplete_coverage.txt')

TEST_RUNNER_PATH = os.path.join(os.getcwd(), 'core', 'tests', 'gae_suite.py')
# This should be the same as core.test_utils.LOG_LINE_PREFIX.
LOG_LINE_PREFIX = 'LOG_INFO_TEST: '
# This path points to a JSON file that defines which modules belong to
# each shard.
SHARDS_SPEC_PATH = os.path.join(
    os.getcwd(), 'scripts', 'backend_test_shards.json')
SHARDS_WIKI_LINK = (
    'https://github.com/oppia/oppia/wiki/Writing-backend-tests#common-errors')
_LOAD_TESTS_DIR = os.path.join(os.getcwd(), 'core', 'tests', 'load_tests')

_PARSER = argparse.ArgumentParser(
    description="""
Run this script from the oppia root folder:
    python -m scripts.run_backend_tests
IMPORTANT: Only one of --test_path,  --test_target, and --test_shard
should be specified.
""")

_EXCLUSIVE_GROUP = _PARSER.add_mutually_exclusive_group()
_EXCLUSIVE_GROUP.add_argument(
    '--test_target',
    help='optional dotted module name of the test(s) to run',
    type=str)
_EXCLUSIVE_GROUP.add_argument(
    '--test_path',
    help='optional subdirectory path containing the test(s) to run',
    type=str)
_EXCLUSIVE_GROUP.add_argument(
    '--test_shard',
    help='optional name of shard to run',
    type=str)
_PARSER.add_argument(
    '--generate_coverage_report',
    help='optional; if specified, generates a coverage report',
    action='store_true')
_PARSER.add_argument(
    '--ignore_coverage',
    help='optional; if specified, tests will not fail due to coverage',
    action='store_true')
_PARSER.add_argument(
    '--exclude_load_tests',
    help='optional; if specified, exclude load tests from being run',
    action='store_true')
_PARSER.add_argument(
    '-v',
    '--verbose',
    help='optional; if specified, display the output of the tests being run',
    action='store_true')


def run_shell_cmd(
        exe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=None):
    """Runs a shell command and captures the stdout and stderr output.

    If the cmd fails, raises Exception. Otherwise, returns a string containing
    the concatenation of the stdout and stderr logs.
    """
    p = subprocess.Popen(exe, stdout=stdout, stderr=stderr, env=env)
    last_stdout_str, last_stderr_str = p.communicate()
    # Standard and error output is in bytes, we need to decode them to be
    # compatible with rest of the code.
    last_stdout_str = last_stdout_str.decode('utf-8')
    last_stderr_str = last_stderr_str.decode('utf-8')
    last_stdout = last_stdout_str.split('\n')

    if LOG_LINE_PREFIX in last_stdout_str:
        concurrent_task_utils.log('')
        for line in last_stdout:
            if line.startswith(LOG_LINE_PREFIX):
                concurrent_task_utils.log(
                    'INFO: %s' % line[len(LOG_LINE_PREFIX):])
        concurrent_task_utils.log('')

    result = '%s%s' % (last_stdout_str, last_stderr_str)

    if p.returncode != 0:
        raise Exception('Error %s\n%s' % (p.returncode, result))

    return result


class TestingTaskSpec:
    """Executes a set of tests given a test class name."""

    def __init__(self, test_target, generate_coverage_report):
        self.test_target = test_target
        self.generate_coverage_report = generate_coverage_report

    def run(self):
        """Runs all tests corresponding to the given test target."""
        env = os.environ.copy()
        test_target_flag = '--test_target=%s' % self.test_target
        if self.generate_coverage_report:
            exc_list = [
                sys.executable, COVERAGE_MODULE_PATH, 'run',
                TEST_RUNNER_PATH, test_target_flag
            ]
            rand = random.Random(os.urandom(8)).randint(0, 999999)
            data_file = '.coverage.%s.%s.%06d' % (
                socket.gethostname(), os.getpid(), rand)
            env['COVERAGE_FILE'] = data_file
            concurrent_task_utils.log('Coverage data for %s is in %s' % (
                self.test_target, data_file))
        else:
            exc_list = [sys.executable, TEST_RUNNER_PATH, test_target_flag]

        result = run_shell_cmd(exc_list, env=env)
        messages = [result]

        if self.generate_coverage_report:
            covered_path = self.test_target.replace('.', '/')
            covered_path = covered_path[:-len('_test')]
            covered_path += '.py'
            if os.path.exists(covered_path):
                report, coverage = _check_coverage(
                    False, data_file=data_file, include=(covered_path,))
            else:
                # Some test files (e.g. scripts/script_import_test.py)
                # have no corresponding code file, so we treat them as
                # fully covering their (nonexistent) associated code
                # file.
                report = ''
                coverage = 100
            messages.append(report)
            messages.append(coverage)

        return [concurrent_task_utils.TaskResult(
            None, None, None, messages)]


def _get_all_test_targets_from_path(test_path=None, include_load_tests=True):
    """Returns a list of test targets for all classes under test_path
    containing tests.
    """
    base_path = os.path.join(os.getcwd(), test_path or '')
    paths = []
    excluded_dirs = [
        '.git', 'third_party', 'node_modules', 'venv',
        'core/tests/data', 'core/tests/build_sources']
    for root in os.listdir(base_path):
        if any(s in root for s in excluded_dirs):
            continue
        if root.endswith('_test.py'):
            paths.append(os.path.join(base_path, root))
        for subroot, _, files in os.walk(os.path.join(base_path, root)):
            if any(s in subroot for s in excluded_dirs):
                continue
            if _LOAD_TESTS_DIR in subroot and not include_load_tests:
                continue
            for f in files:
                if f.endswith('_test.py'):
                    paths.append(os.path.join(subroot, f))
    result = [
        os.path.relpath(path, start=os.getcwd())[:-3].replace('/', '.')
        for path in paths]
    return result


def _get_all_test_targets_from_shard(shard_name):
    """Find all test modules in a shard.

    Args:
        shard_name: str. The name of the shard.

    Returns:
        list(str). The dotted module names that belong to the shard.
    """
    with python_utils.open_file(SHARDS_SPEC_PATH, 'r') as shards_file:
        shards_spec = json.load(shards_file)
    return shards_spec[shard_name]


def _check_shards_match_tests(include_load_tests=True):
    """Check whether the test shards match the tests that exist.

    Args:
        include_load_tests: bool. Whether to include load tests.

    Returns:
        str. A description of any problems found, or an empty string if
        the shards match the tests.
    """
    with python_utils.open_file(SHARDS_SPEC_PATH, 'r') as shards_file:
        shards_spec = json.load(shards_file)
    shard_modules = sorted([
        module for shard in shards_spec.values() for module in shard])
    test_modules = _get_all_test_targets_from_path(
        include_load_tests=include_load_tests)
    test_modules_set = set(test_modules)
    test_modules = sorted(test_modules_set)
    if test_modules == shard_modules:
        return ''
    if len(set(shard_modules)) != len(shard_modules):
        # A module is duplicated, so we find the duplicate.
        for module in shard_modules:
            if shard_modules.count(module) != 1:
                return '{} duplicated in {}'.format(
                    module, SHARDS_SPEC_PATH)
        raise Exception('Failed to find  module duplicated in shards.')
    # Since there are no duplicates among the shards, we know the
    # problem must be a module in one list but not the other.
    shard_modules_set = set(shard_modules)
    shard_extra = shard_modules_set - test_modules_set
    if shard_extra:
        return 'Modules {} in shards not found. See {}.'.format(
            shard_extra, SHARDS_WIKI_LINK)
    test_extra = test_modules_set - shard_modules_set
    assert test_extra
    return 'Modules {} not in shards. See {}.'.format(
        test_extra, SHARDS_WIKI_LINK)


def _load_coverage_exclusion_list(path):
    """Load modules excluded from per-file coverage checks.

    Args:
        path: str. Path to file with exclusion list. File should have
            one dotted module name per line. Blank lines and lines
            starting with `#` are ignored.

    Returns:
        list(str). Dotted names of excluded modules.
    """
    exclusion_list = []
    with open(path, 'r', encoding='utf-8') as exclusion_file:
        for line in exclusion_file:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            exclusion_list.append(line)
    return exclusion_list


def main(args=None):
    """Run the tests."""
    parsed_args = _PARSER.parse_args(args=args)

    for directory in common.DIRS_TO_ADD_TO_SYS_PATH:
        if not os.path.exists(os.path.dirname(directory)):
            raise Exception('Directory %s does not exist.' % directory)

        # The directories should only be inserted starting at index 1. See
        # https://stackoverflow.com/a/10095099 and
        # https://stackoverflow.com/q/10095037 for more details.
        sys.path.insert(1, directory)

    common.fix_third_party_imports()

    if parsed_args.generate_coverage_report:
        python_utils.PRINT(
            'Checking whether coverage is installed in %s'
            % common.OPPIA_TOOLS_DIR
        )
        if not os.path.exists(
                os.path.join(
                    common.OPPIA_TOOLS_DIR,
                    'coverage-%s' % common.COVERAGE_VERSION
                )
        ):
            raise Exception(
                'Coverage is not installed, please run the start script.')

    test_specs_provided = sum([
        1 if argument else 0
        for argument in (
            parsed_args.test_target,
            parsed_args.test_path,
            parsed_args.test_shard
        )
    ])

    if test_specs_provided > 1:
        raise Exception(
            'At most one of test_path, test_target and test_shard may '
            'be specified.')
    if parsed_args.test_path and '.' in parsed_args.test_path:
        raise Exception('The delimiter in test_path should be a slash (/)')
    if parsed_args.test_target and '/' in parsed_args.test_target:
        raise Exception('The delimiter in test_target should be a dot (.)')

    with contextlib.ExitStack() as stack:
        stack.enter_context(servers.managed_cloud_datastore_emulator())
        stack.enter_context(servers.managed_redis_server())
        if parsed_args.test_target:
            if '_test' in parsed_args.test_target:
                all_test_targets = [parsed_args.test_target]
            else:
                python_utils.PRINT('')
                python_utils.PRINT(
                    '---------------------------------------------------------')
                python_utils.PRINT(
                    'WARNING : test_target flag should point to the test file.')
                python_utils.PRINT(
                    '---------------------------------------------------------')
                python_utils.PRINT('')
                time.sleep(3)
                python_utils.PRINT(
                    'Redirecting to its corresponding test file...')
                all_test_targets = [parsed_args.test_target + '_test']
        elif parsed_args.test_shard:
            validation_error = _check_shards_match_tests(
                include_load_tests=True)
            if validation_error:
                raise Exception(validation_error)
            all_test_targets = _get_all_test_targets_from_shard(
                parsed_args.test_shard)
        else:
            include_load_tests = not parsed_args.exclude_load_tests
            all_test_targets = _get_all_test_targets_from_path(
                test_path=parsed_args.test_path,
                include_load_tests=include_load_tests)

        # Prepare tasks.
        max_concurrent_runs = 25
        concurrent_count = min(multiprocessing.cpu_count(), max_concurrent_runs)
        semaphore = threading.Semaphore(concurrent_count)

        task_to_taskspec = {}
        tasks = []
        for test_target in all_test_targets:
            test = TestingTaskSpec(
                test_target,
                parsed_args.generate_coverage_report)
            task = concurrent_task_utils.create_task(
                test.run, parsed_args.verbose, semaphore, name=test_target,
                report_enabled=False)
            task_to_taskspec[task] = test
            tasks.append(task)

        task_execution_failed = False
        try:
            concurrent_task_utils.execute_tasks(tasks, semaphore)
        except Exception:
            task_execution_failed = True

    python_utils.PRINT('')
    python_utils.PRINT('+------------------+')
    python_utils.PRINT('| SUMMARY OF TESTS |')
    python_utils.PRINT('+------------------+')
    python_utils.PRINT('')

    coverage_exclusions = _load_coverage_exclusion_list(
        COVERAGE_EXCLUSION_LIST_PATH)

    # Check we ran all tests as expected.
    total_count = 0
    total_errors = 0
    total_failures = 0
    incomplete_coverage = 0
    for task in tasks:
        spec = task_to_taskspec[task]

        if not task.finished:
            python_utils.PRINT('CANCELED  %s' % spec.test_target)
            test_count = 0
        elif task.exception and isinstance(
                task.exception, subprocess.CalledProcessError):
            python_utils.PRINT(
                'ERROR     %s: Error raised by subprocess.')
            raise task.exception
        elif task.exception and 'No tests were run' in task.exception.args[0]:
            python_utils.PRINT(
                'ERROR     %s: No tests found.' % spec.test_target)
            test_count = 0
        elif task.exception:
            exc_str = task.exception.args[0]
            python_utils.PRINT(exc_str[exc_str.find('='): exc_str.rfind('-')])

            tests_failed_regex_match = re.search(
                r'Test suite failed: ([0-9]+) tests run, ([0-9]+) errors, '
                '([0-9]+) failures',
                task.exception.args[0]
            )

            try:
                test_count = int(tests_failed_regex_match.group(1))
                errors = int(tests_failed_regex_match.group(2))
                failures = int(tests_failed_regex_match.group(3))
                total_errors += errors
                total_failures += failures
                python_utils.PRINT('FAILED    %s: %s errors, %s failures' % (
                    spec.test_target, errors, failures))
            except AttributeError:
                # There was an internal error, and the tests did not run (The
                # error message did not match `tests_failed_regex_match`).
                test_count = 0
                total_errors += 1
                python_utils.PRINT('')
                python_utils.PRINT(
                    '------------------------------------------------------')
                python_utils.PRINT(
                    '    WARNING: FAILED TO RUN %s' % spec.test_target)
                python_utils.PRINT('')
                python_utils.PRINT(
                    '    This is most likely due to an import error.')
                python_utils.PRINT(
                    '------------------------------------------------------')
                raise task.exception
        else:
            try:
                tests_run_regex_match = re.search(
                    r'Ran ([0-9]+) tests? in ([0-9\.]+)s',
                    task.task_results[0].get_report()[0])
                test_count = int(tests_run_regex_match.group(1))
                test_time = float(tests_run_regex_match.group(2))
                python_utils.PRINT(
                    'SUCCESS   %s: %d tests (%.1f secs)' %
                    (spec.test_target, test_count, test_time))
            except Exception:
                python_utils.PRINT(
                    'An unexpected error occurred. '
                    'Task output:\n%s' % task.task_results[0].get_report()[0])
            if parsed_args.generate_coverage_report:
                coverage = task.task_results[0].get_report()[-2]
                if spec.test_target in coverage_exclusions:
                    continue
                if coverage != 100:
                    python_utils.PRINT('INCOMPLETE COVERAGE (%s%%): %s' % (
                        coverage, spec.test_target))
                    incomplete_coverage += 1
                    python_utils.PRINT(task.task_results[0].get_report()[-3])

        total_count += test_count

    python_utils.PRINT('')
    if total_count == 0:
        raise Exception('WARNING: No tests were run.')

    python_utils.PRINT('Ran %s test%s in %s test module%s.' % (
        total_count, '' if total_count == 1 else 's',
        len(tasks), '' if len(tasks) == 1 else 's'))

    if total_errors or total_failures:
        python_utils.PRINT(
            '(%s ERRORS, %s FAILURES)' % (total_errors, total_failures))
    else:
        python_utils.PRINT('All tests passed.')

    if task_execution_failed:
        raise Exception('Task execution failed.')
    elif total_errors or total_failures:
        raise Exception(
            '%s errors, %s failures' % (total_errors, total_failures))
    elif incomplete_coverage:
        raise Exception(
            '%s tests incompletely cover associated code files.' %
            incomplete_coverage)

    if parsed_args.generate_coverage_report:
        subprocess.check_call([sys.executable, COVERAGE_MODULE_PATH, 'combine'])
        report_stdout, coverage = _check_coverage(True)
        python_utils.PRINT(report_stdout)

        if (coverage != 100
                and not parsed_args.ignore_coverage):
            raise Exception('Backend test coverage is not 100%')

    python_utils.PRINT('')
    python_utils.PRINT('Done!')


def _check_coverage(
        combine, data_file=None, include=tuple()):
    """Check code coverage of backend tests.

    Args:
        combine: bool. Whether to run `coverage combine` first to
            combine coverage data from multiple test runs.
        data_file: str|None. Path to the coverage data file to use.
        include: tuple(str). Paths of code files to consider when
            computing coverage. If an empty tuple is provided, all code
            files will be used.

    Returns:
        str, float. Tuple of the coverage report and the coverage
        percentage.
    """
    if combine:
        combine_process = subprocess.run(
            [sys.executable, COVERAGE_MODULE_PATH, 'combine'],
            capture_output=True, encoding='utf-8', check=False)
        no_combine = combine_process.stdout.strip() == 'No data to combine'
        if (combine_process.returncode and not no_combine):
            raise RuntimeError(
                'Failed to combine coverage because subprocess failed.'
                '\n%s' % combine_process)

    cmd = [
        sys.executable, COVERAGE_MODULE_PATH, 'report',
         '--omit="%s*","third_party/*","/usr/share/*"'
         % common.OPPIA_TOOLS_DIR, '--show-missing']
    if include:
        cmd.append('--include=%s' % ','.join(include))

    env = os.environ.copy()
    if data_file:
        env['COVERAGE_FILE'] = data_file

    process = subprocess.run(
        cmd, capture_output=True, encoding='utf-8', env=env,
        check=False)
    if process.stdout.strip() == 'No data to report.':
        # File under test is exempt from coverage according to the
        # --omit flag or .coveragerc.
        coverage = 100
    elif process.returncode:
        raise RuntimeError(
            'Failed to calculate coverage because subprocess failed. %s'
            % process
        )
    else:
        coverage_result = re.search(
            r'TOTAL\s+(\d+)\s+(\d+)\s+(?P<total>\d+)%\s+',
            process.stdout)
        coverage = float(coverage_result.group('total'))

    return process.stdout, coverage


if __name__ == '__main__':
    main()

import datetime
import os
import re
import signal
import sys
import time
from itertools import combinations
from pathlib import Path
from textwrap import dedent
from time import sleep
from typing import List
from unittest.mock import MagicMock

import anyio
import pendulum
import pytest
from starlette import status

import prefect.runner
from prefect import flow, serve, task
from prefect.client.orchestration import PrefectClient
from prefect.client.schemas.objects import StateType
from prefect.client.schemas.schedules import CronSchedule
from prefect.deployments.runner import (
    DeploymentApplyError,
    DeploymentImage,
    RunnerDeployment,
    deploy,
)
from prefect.flows import load_flow_from_entrypoint
from prefect.logging.loggers import flow_run_logger
from prefect.runner.runner import Runner
from prefect.runner.server import perform_health_check
from prefect.settings import (
    PREFECT_DEFAULT_WORK_POOL_NAME,
    PREFECT_RUNNER_POLL_FREQUENCY,
    PREFECT_RUNNER_PROCESS_LIMIT,
    temporary_settings,
)
from prefect.testing.utilities import AsyncMock
from prefect.utilities.dockerutils import parse_image_tag


@flow(version="test")
def dummy_flow_1():
    """I'm just here for tests"""
    pass


@task
def my_task(seconds: int):
    time.sleep(seconds)


def on_cancellation(flow, flow_run, state):
    logger = flow_run_logger(flow_run, flow)
    logger.info("This flow was cancelled!")


@flow(on_cancellation=[on_cancellation], log_prints=True)
def cancel_flow_submitted_tasks(sleep_time: int = 100):
    my_task.submit(sleep_time)


def on_crashed(flow, flow_run, state):
    logger = flow_run_logger(flow_run, flow)
    logger.info("This flow crashed!")


@flow(on_crashed=[on_crashed], log_prints=True)
def crashing_flow():
    print("Oh boy, here I go crashing again...")
    os.kill(os.getpid(), signal.SIGTERM)


@flow
def dummy_flow_2():
    pass


@flow()
def tired_flow():
    print("I am so tired...")

    for _ in range(100):
        print("zzzzz...")
        sleep(5)


class MockStorage:
    """
    A mock storage class that simulates pulling code from a remote location.
    """

    def __init__(self, pull_code_spy=None):
        self._base_path = Path.cwd()
        self._pull_code_spy = pull_code_spy

    def set_base_path(self, path: Path):
        self._base_path = path

    code = dedent(
        """\
        from prefect import flow

        @flow
        def test_flow():
            return 1
        """
    )

    @property
    def destination(self):
        return self._base_path

    @property
    def pull_interval(self):
        return 60

    async def pull_code(self):
        if self._pull_code_spy:
            self._pull_code_spy()

        if self._base_path:
            with open(self._base_path / "flows.py", "w") as f:
                f.write(self.code)

    def to_pull_step(self):
        return {"prefect.fake.module": {}}


class TestInit:
    async def test_runner_respects_limit_setting(self):
        runner = Runner()
        assert runner.limit == PREFECT_RUNNER_PROCESS_LIMIT.value()

        runner = Runner(limit=50)
        assert runner.limit == 50

        with temporary_settings({PREFECT_RUNNER_PROCESS_LIMIT: 100}):
            runner = Runner()
            assert runner.limit == 100

    async def test_runner_respects_poll_setting(self):
        runner = Runner()
        assert runner.query_seconds == PREFECT_RUNNER_POLL_FREQUENCY.value()

        runner = Runner(query_seconds=50)
        assert runner.query_seconds == 50

        with temporary_settings({PREFECT_RUNNER_POLL_FREQUENCY: 100}):
            runner = Runner()
            assert runner.query_seconds == 100


class TestServe:
    @pytest.fixture(autouse=True)
    async def mock_runner_start(self, monkeypatch):
        mock = AsyncMock()
        monkeypatch.setattr("prefect.runner.Runner.start", mock)
        return mock

    async def test_serve_prints_help_message_on_startup(self, capsys):
        await serve(
            await dummy_flow_1.to_deployment(__file__),
            await dummy_flow_2.to_deployment(__file__),
            await tired_flow.to_deployment(__file__),
        )

        captured = capsys.readouterr()

        assert (
            "Your deployments are being served and polling for scheduled runs!"
            in captured.out
        )
        assert "dummy-flow-1/test_runner" in captured.out
        assert "dummy-flow-2/test_runner" in captured.out
        assert "tired-flow/test_runner" in captured.out
        assert "$ prefect deployment run [DEPLOYMENT_NAME]" in captured.out

    is_python_38 = sys.version_info[:2] == (3, 8)

    async def test_serve_typed_container_inputs_flow(self, capsys):
        if self.is_python_38:

            @flow
            def type_container_input_flow(arg1: List[str]) -> str:
                print(arg1)
                return ",".join(arg1)

        else:

            @flow
            def type_container_input_flow(arg1: list[str]) -> str:
                print(arg1)
                return ",".join(arg1)

        await serve(
            await type_container_input_flow.to_deployment(__file__),
        )

        captured = capsys.readouterr()

        assert (
            "Your deployments are being served and polling for scheduled runs!"
            in captured.out
        )
        assert "type-container-input-flow/test_runner" in captured.out
        assert "$ prefect deployment run [DEPLOYMENT_NAME]" in captured.out

    async def test_serve_can_create_multiple_deployments(
        self,
        prefect_client: PrefectClient,
    ):
        deployment_1 = await dummy_flow_1.to_deployment(__file__, interval=3600)
        deployment_2 = await dummy_flow_2.to_deployment(__file__, cron="* * * * *")

        await serve(deployment_1, deployment_2)

        deployment = await prefect_client.read_deployment_by_name(
            name="dummy-flow-1/test_runner"
        )

        assert deployment is not None
        assert deployment.schedule.interval == datetime.timedelta(seconds=3600)

        deployment = await prefect_client.read_deployment_by_name(
            name="dummy-flow-2/test_runner"
        )

        assert deployment is not None
        assert deployment.schedule.cron == "* * * * *"

    async def test_serve_starts_a_runner(
        self, prefect_client: PrefectClient, mock_runner_start: AsyncMock
    ):
        deployment = await dummy_flow_1.to_deployment("test")

        await serve(deployment)

        mock_runner_start.assert_awaited_once()


class TestRunner:
    async def test_add_flows_to_runner(self, prefect_client: PrefectClient):
        """Runner.add should create a deployment for the flow passed to it"""
        runner = Runner()

        deployment_id_1 = await runner.add_flow(dummy_flow_1, __file__, interval=3600)
        deployment_id_2 = await runner.add_flow(
            dummy_flow_2, __file__, cron="* * * * *"
        )

        deployment_1 = await prefect_client.read_deployment(deployment_id_1)
        deployment_2 = await prefect_client.read_deployment(deployment_id_2)

        assert deployment_1 is not None
        assert deployment_1.name == "test_runner"
        assert deployment_1.schedule.interval == datetime.timedelta(seconds=3600)

        assert deployment_2 is not None
        assert deployment_2.name == "test_runner"
        assert deployment_2.schedule.cron == "* * * * *"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {**d1, **d2}
            for d1, d2 in combinations(
                [
                    {"interval": 3600},
                    {"cron": "* * * * *"},
                    {"rrule": "FREQ=MINUTELY"},
                    {"schedule": CronSchedule(cron="* * * * *")},
                ],
                2,
            )
        ],
    )
    async def test_add_flow_raises_on_multiple_schedules(self, kwargs):
        expected_message = (
            "Only one of interval, cron, rrule, or schedule can be provided."
        )
        runner = Runner()
        with pytest.raises(ValueError, match=expected_message):
            await runner.add_flow(dummy_flow_1, __file__, **kwargs)

    async def test_add_deployments_to_runner(self, prefect_client: PrefectClient):
        """Runner.add_deployment should apply the deployment passed to it"""
        runner = Runner()

        deployment_1 = await dummy_flow_1.to_deployment(__file__, interval=3600)
        deployment_2 = await dummy_flow_2.to_deployment(__file__, cron="* * * * *")

        deployment_id_1 = await runner.add_deployment(deployment_1)
        deployment_id_2 = await runner.add_deployment(deployment_2)

        deployment_1 = await prefect_client.read_deployment(deployment_id_1)
        deployment_2 = await prefect_client.read_deployment(deployment_id_2)

        assert deployment_1 is not None
        assert deployment_1.name == "test_runner"
        assert deployment_1.schedule.interval == datetime.timedelta(seconds=3600)

        assert deployment_2 is not None
        assert deployment_2.name == "test_runner"
        assert deployment_2.schedule.cron == "* * * * *"

    async def test_runner_can_pause_schedules_on_stop(
        self, prefect_client: PrefectClient, caplog
    ):
        runner = Runner()

        deployment_1 = await dummy_flow_1.to_deployment(__file__, interval=3600)
        deployment_2 = await dummy_flow_2.to_deployment(__file__, cron="* * * * *")

        await runner.add_deployment(deployment_1)
        await runner.add_deployment(deployment_2)

        deployment_1 = await prefect_client.read_deployment_by_name(
            name="dummy-flow-1/test_runner"
        )
        deployment_2 = await prefect_client.read_deployment_by_name(
            name="dummy-flow-2/test_runner"
        )

        assert deployment_1.is_schedule_active

        assert deployment_2.is_schedule_active

        await runner.start(run_once=True)

        deployment_1 = await prefect_client.read_deployment_by_name(
            name="dummy-flow-1/test_runner"
        )
        deployment_2 = await prefect_client.read_deployment_by_name(
            name="dummy-flow-2/test_runner"
        )

        assert not deployment_1.is_schedule_active

        assert not deployment_2.is_schedule_active

        assert "Pausing schedules for all deployments" in caplog.text
        assert "All deployment schedules have been paused" in caplog.text

    @pytest.mark.usefixtures("use_hosted_api_server")
    async def test_runner_executes_flow_runs(self, prefect_client: PrefectClient):
        runner = Runner()

        deployment = await dummy_flow_1.to_deployment(__file__)

        await runner.add_deployment(deployment)

        await runner.start(run_once=True)

        deployment = await prefect_client.read_deployment_by_name(
            name="dummy-flow-1/test_runner"
        )

        flow_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment.id
        )

        await runner.start(run_once=True)
        flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)

        assert flow_run.state.is_completed()

    @pytest.mark.usefixtures("use_hosted_api_server")
    @pytest.mark.flaky
    async def test_runner_can_cancel_flow_runs(
        self, prefect_client: PrefectClient, caplog
    ):
        runner = Runner(query_seconds=2)

        deployment = await cancel_flow_submitted_tasks.to_deployment(__file__)

        await runner.add_deployment(deployment)

        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.start)

            deployment = await prefect_client.read_deployment_by_name(
                name="cancel-flow-submitted-tasks/test_runner"
            )

            flow_run = await prefect_client.create_flow_run_from_deployment(
                deployment_id=deployment.id
            )

            # Need to wait for polling loop to pick up flow run and
            # start execution
            for _ in range(15):
                await anyio.sleep(1)
                flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)
                if flow_run.state.is_running():
                    break

            await prefect_client.set_flow_run_state(
                flow_run_id=flow_run.id,
                state=flow_run.state.copy(
                    update={"name": "Cancelled", "type": StateType.CANCELLING}
                ),
            )

            # Need to wait for polling loop to pick up flow run and then
            # finish cancellation
            for _ in range(15):
                await anyio.sleep(1)
                flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)
                if flow_run.state.is_cancelled():
                    break

            await runner.stop()
            tg.cancel_scope.cancel()

        assert flow_run.state.is_cancelled()
        # check to make sure on_cancellation hook was called
        assert "This flow was cancelled!" in caplog.text

    @pytest.mark.usefixtures("use_hosted_api_server")
    @pytest.mark.flaky
    async def test_runner_runs_on_cancellation_hooks_for_remotely_stored_flows(
        self, prefect_client: PrefectClient, caplog
    ):
        runner = Runner(query_seconds=2)

        storage = MockStorage()
        storage.code = dedent(
            """\
            from time import sleep

            from prefect import flow
            from prefect.logging.loggers import flow_run_logger

            def on_cancellation(flow, flow_run, state):
                logger = flow_run_logger(flow_run, flow)
                logger.info("This flow was cancelled!")

            @flow(on_cancellation=[on_cancellation], log_prints=True)
            def cancel_flow(sleep_time: int = 100):
                sleep(sleep_time)
            """
        )

        deployment_id = await runner.add_flow(
            await flow.from_source(source=storage, entrypoint="flows.py:cancel_flow"),
            name=__file__,
        )

        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.start)

            flow_run = await prefect_client.create_flow_run_from_deployment(
                deployment_id=deployment_id
            )

            # Need to wait for polling loop to pick up flow run and
            # start execution
            for _ in range(15):
                await anyio.sleep(1)
                flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)
                if flow_run.state.is_running():
                    break

            await prefect_client.set_flow_run_state(
                flow_run_id=flow_run.id,
                state=flow_run.state.copy(
                    update={"name": "Cancelling", "type": StateType.CANCELLING}
                ),
            )

            # Need to wait for polling loop to pick up flow run and then
            # finish cancellation
            for _ in range(15):
                await anyio.sleep(1)
                flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)
                if flow_run.state.is_cancelled():
                    break

            await runner.stop()
            tg.cancel_scope.cancel()

        assert flow_run.state.is_cancelled()
        # check to make sure on_cancellation hook was called
        assert "This flow was cancelled!" in caplog.text

    @pytest.mark.usefixtures("use_hosted_api_server")
    async def test_runner_runs_on_crashed_hooks_for_remotely_stored_flows(
        self, prefect_client: PrefectClient, caplog
    ):
        runner = Runner()
        storage = MockStorage()
        storage.code = dedent(
            """\
        import os
        import signal

        from prefect import flow
        from prefect.logging.loggers import flow_run_logger

        def on_crashed(flow, flow_run, state):
            logger = flow_run_logger(flow_run, flow)
            logger.info("This flow crashed!")


        @flow(on_crashed=[on_crashed], log_prints=True)
        def crashing_flow():
            print("Oh boy, here I go crashing again...")
            os.kill(os.getpid(), signal.SIGTERM)
        """
        )

        deployment_id = await runner.add_flow(
            await flow.from_source(source=storage, entrypoint="flows.py:crashing_flow"),
            name=__file__,
        )

        flow_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )
        await runner.execute_flow_run(flow_run.id)

        flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)
        assert flow_run.state.is_crashed()
        # check to make sure on_cancellation hook was called
        assert "This flow crashed!" in caplog.text

    @pytest.mark.usefixtures("use_hosted_api_server")
    async def test_runner_can_execute_a_single_flow_run(
        self, prefect_client: PrefectClient
    ):
        runner = Runner()

        deployment_id = await (await dummy_flow_1.to_deployment(__file__)).apply()

        flow_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )
        await runner.execute_flow_run(flow_run.id)

        flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)
        assert flow_run.state.is_completed()

    @pytest.mark.usefixtures("use_hosted_api_server")
    async def test_runner_respects_set_limit(
        self, prefect_client: PrefectClient, caplog
    ):
        runner = Runner(limit=1)

        deployment_id = await (await dummy_flow_1.to_deployment(__file__)).apply()

        good_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )
        bad_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )
        runner._acquire_limit_slot(good_run.id)
        await runner.execute_flow_run(bad_run.id)
        assert "run limit reached" in caplog.text

        flow_run = await prefect_client.read_flow_run(flow_run_id=bad_run.id)
        assert flow_run.state.is_scheduled()

        runner._release_limit_slot(good_run.id)
        await runner.execute_flow_run(bad_run.id)

        flow_run = await prefect_client.read_flow_run(flow_run_id=bad_run.id)
        assert flow_run.state.is_completed()

    async def test_handles_spaces_in_sys_executable(self, monkeypatch, prefect_client):
        """
        Regression test for https://github.com/PrefectHQ/prefect/issues/10820
        """
        import sys

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.pid = 4242

        mock_run_process_call = AsyncMock(
            return_value=mock_process,
        )

        monkeypatch.setattr(prefect.runner.runner, "run_process", mock_run_process_call)

        monkeypatch.setattr(sys, "executable", "C:/Program Files/Python38/python.exe")

        runner = Runner()

        deployment_id = await (await dummy_flow_1.to_deployment(__file__)).apply()

        flow_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )
        await runner._run_process(flow_run)

        # Previously the command would have been
        # ["C:/Program", "Files/Python38/python.exe", "-m", "prefect.engine"]
        assert mock_run_process_call.call_args[0][0] == [
            "C:/Program Files/Python38/python.exe",
            "-m",
            "prefect.engine",
        ]

    async def test_runner_sets_flow_run_env_var_with_dashes(
        self, monkeypatch, prefect_client
    ):
        """
        Regression test for https://github.com/PrefectHQ/prefect/issues/10851
        """
        env_var_value = None

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.pid = 4242

        def capture_env_var(*args, **kwargs):
            nonlocal env_var_value
            nonlocal mock_process
            env_var_value = kwargs["env"].get("PREFECT__FLOW_RUN_ID")
            return mock_process

        mock_run_process_call = AsyncMock(side_effect=capture_env_var)

        monkeypatch.setattr(prefect.runner.runner, "run_process", mock_run_process_call)

        runner = Runner()

        deployment_id = await (await dummy_flow_1.to_deployment(__file__)).apply()

        flow_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )
        await runner._run_process(flow_run)

        assert env_var_value == str(flow_run.id)
        assert env_var_value != flow_run.id.hex

    @pytest.mark.usefixtures("use_hosted_api_server")
    async def test_runner_runs_a_remotely_stored_flow(self, prefect_client):
        runner = Runner()

        deployment = await (
            await flow.from_source(
                source=MockStorage(), entrypoint="flows.py:test_flow"
            )
        ).to_deployment(__file__)

        deployment_id = await runner.add_deployment(deployment)

        flow_run = await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )

        await runner.start(run_once=True)
        flow_run = await prefect_client.read_flow_run(flow_run_id=flow_run.id)

        assert flow_run.state.is_completed()

    @pytest.mark.usefixtures("use_hosted_api_server")
    async def test_runner_caches_adhoc_pulls(self, prefect_client):
        runner = Runner()

        pull_code_spy = MagicMock()

        deployment = await RunnerDeployment.from_storage(
            storage=MockStorage(pull_code_spy=pull_code_spy),
            entrypoint="flows.py:test_flow",
            name=__file__,
        )

        deployment_id = await runner.add_deployment(deployment)

        await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )

        await runner.start(run_once=True)

        # 1 for deployment creation, 1 for runner start up, 1 for ad hoc pull
        assert runner._storage_objs[0]._pull_code_spy.call_count == 3

        await prefect_client.create_flow_run_from_deployment(
            deployment_id=deployment_id
        )

        # Should be 3 because the ad hoc pull should have been cached
        assert runner._storage_objs[0]._pull_code_spy.call_count == 3


class TestRunnerDeployment:
    @pytest.fixture
    def relative_file_path(self):
        return Path(__file__).relative_to(Path.cwd())

    @pytest.fixture
    def dummy_flow_1_entrypoint(self, relative_file_path):
        return f"{relative_file_path}:dummy_flow_1"

    def test_from_flow(self, relative_file_path):
        deployment = RunnerDeployment.from_flow(
            dummy_flow_1,
            __file__,
            tags=["test"],
            version="alpha",
            description="Deployment descriptions",
            enforce_parameter_schema=True,
        )

        assert deployment.name == "test_runner"
        assert deployment.flow_name == "dummy-flow-1"
        assert deployment.entrypoint == f"{relative_file_path}:dummy_flow_1"
        assert deployment.description == "Deployment descriptions"
        assert deployment.version == "alpha"
        assert deployment.tags == ["test"]
        assert deployment.enforce_parameter_schema

    def test_from_flow_accepts_interval(self):
        deployment = RunnerDeployment.from_flow(dummy_flow_1, __file__, interval=3600)

        assert deployment.schedule.interval == datetime.timedelta(seconds=3600)

    def test_from_flow_accepts_cron(self):
        deployment = RunnerDeployment.from_flow(
            dummy_flow_1, __file__, cron="* * * * *"
        )

        assert deployment.schedule.cron == "* * * * *"

    def test_from_flow_accepts_rrule(self):
        deployment = RunnerDeployment.from_flow(
            dummy_flow_1, __file__, rrule="FREQ=MINUTELY"
        )

        assert deployment.schedule.rrule == "FREQ=MINUTELY"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {**d1, **d2}
            for d1, d2 in combinations(
                [
                    {"interval": 3600},
                    {"cron": "* * * * *"},
                    {"rrule": "FREQ=MINUTELY"},
                    {"schedule": CronSchedule(cron="* * * * *")},
                ],
                2,
            )
        ],
    )
    def test_from_flow_raises_on_multiple_schedules(self, kwargs):
        expected_message = (
            "Only one of interval, cron, rrule, or schedule can be provided."
        )
        with pytest.raises(ValueError, match=expected_message):
            RunnerDeployment.from_flow(dummy_flow_1, __file__, **kwargs)

    def test_from_flow_uses_defaults_from_flow(self):
        deployment = RunnerDeployment.from_flow(dummy_flow_1, __file__)

        assert deployment.version == "test"
        assert deployment.description == "I'm just here for tests"

    def test_from_flow_raises_when_using_flow_loaded_from_entrypoint(self):
        da_flow = load_flow_from_entrypoint("tests/runner/test_runner.py:dummy_flow_1")

        with pytest.raises(
            ValueError,
            match=(
                "Cannot create a RunnerDeployment from a flow that has been loaded from"
                " an entrypoint"
            ),
        ):
            RunnerDeployment.from_flow(da_flow, __file__)

    def test_from_flow_raises_on_interactively_defined_flow(self):
        @flow
        def da_flow():
            pass

        # Clear __module__ to test it's handled correctly
        da_flow.__module__ = None

        with pytest.raises(
            ValueError,
            match="Flows defined interactively cannot be deployed.",
        ):
            RunnerDeployment.from_flow(da_flow, __file__)

        # muck up __module__ so that it looks like it was defined interactively
        da_flow.__module__ = "__main__"

        with pytest.raises(
            ValueError,
            match="Flows defined interactively cannot be deployed.",
        ):
            RunnerDeployment.from_flow(da_flow, __file__)

    def test_from_entrypoint(self, dummy_flow_1_entrypoint):
        deployment = RunnerDeployment.from_entrypoint(
            dummy_flow_1_entrypoint,
            __file__,
            tags=["test"],
            version="alpha",
            description="Deployment descriptions",
            enforce_parameter_schema=True,
        )

        assert deployment.name == "test_runner"
        assert deployment.flow_name == "dummy-flow-1"
        assert deployment.entrypoint == "tests/runner/test_runner.py:dummy_flow_1"
        assert deployment.description == "Deployment descriptions"
        assert deployment.version == "alpha"
        assert deployment.tags == ["test"]
        assert deployment.enforce_parameter_schema

    def test_from_entrypoint_accepts_interval(self, dummy_flow_1_entrypoint):
        deployment = RunnerDeployment.from_entrypoint(
            dummy_flow_1_entrypoint, __file__, interval=3600
        )

        assert deployment.schedule.interval == datetime.timedelta(seconds=3600)

    def test_from_entrypoint_accepts_cron(self, dummy_flow_1_entrypoint):
        deployment = RunnerDeployment.from_entrypoint(
            dummy_flow_1_entrypoint, __file__, cron="* * * * *"
        )

        assert deployment.schedule.cron == "* * * * *"

    def test_from_entrypoint_accepts_rrule(self, dummy_flow_1_entrypoint):
        deployment = RunnerDeployment.from_entrypoint(
            dummy_flow_1_entrypoint, __file__, rrule="FREQ=MINUTELY"
        )

        assert deployment.schedule.rrule == "FREQ=MINUTELY"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {**d1, **d2}
            for d1, d2 in combinations(
                [
                    {"interval": 3600},
                    {"cron": "* * * * *"},
                    {"rrule": "FREQ=MINUTELY"},
                    {"schedule": CronSchedule(cron="* * * * *")},
                ],
                2,
            )
        ],
    )
    def test_from_entrypoint_raises_on_multiple_schedules(
        self, dummy_flow_1_entrypoint, kwargs
    ):
        expected_message = (
            "Only one of interval, cron, rrule, or schedule can be provided."
        )
        with pytest.raises(ValueError, match=expected_message):
            RunnerDeployment.from_entrypoint(
                dummy_flow_1_entrypoint, __file__, **kwargs
            )

    def test_from_entrypoint_uses_defaults_from_entrypoint(
        self, dummy_flow_1_entrypoint
    ):
        deployment = RunnerDeployment.from_entrypoint(dummy_flow_1_entrypoint, __file__)

        assert deployment.version == "test"
        assert deployment.description == "I'm just here for tests"

    async def test_apply(self, prefect_client: PrefectClient):
        deployment = RunnerDeployment.from_flow(dummy_flow_1, __file__, interval=3600)

        deployment_id = await deployment.apply()

        deployment = await prefect_client.read_deployment(deployment_id)

        assert deployment.name == "test_runner"
        assert deployment.entrypoint == "tests/runner/test_runner.py:dummy_flow_1"
        assert deployment.version == "test"
        assert deployment.description == "I'm just here for tests"
        assert deployment.schedule.interval == datetime.timedelta(seconds=3600)
        assert deployment.work_pool_name is None
        assert deployment.work_queue_name is None
        assert deployment.path == "."
        assert deployment.enforce_parameter_schema is False
        assert deployment.infra_overrides == {}

    async def test_apply_with_work_pool(self, prefect_client: PrefectClient, work_pool):
        deployment = RunnerDeployment.from_flow(
            dummy_flow_1,
            __file__,
            interval=3600,
        )

        deployment_id = await deployment.apply(
            work_pool_name=work_pool.name, image="my-repo/my-image:latest"
        )

        deployment = await prefect_client.read_deployment(deployment_id)

        assert deployment.work_pool_name == work_pool.name
        assert deployment.infra_overrides == {
            "image": "my-repo/my-image:latest",
        }
        assert deployment.work_queue_name == "default"

    @pytest.mark.parametrize(
        "from_flow_kwargs, apply_kwargs, expected_message",
        [
            (
                {"work_queue_name": "my-queue"},
                {},
                (
                    "A work queue can only be provided when registering a deployment"
                    " with a work pool."
                ),
            ),
            (
                {"job_variables": {"foo": "bar"}},
                {},
                (
                    "Job variables can only be provided when registering a deployment"
                    " with a work pool."
                ),
            ),
            (
                {},
                {"image": "my-repo/my-image:latest"},
                (
                    "An image can only be provided when registering a deployment with a"
                    " work pool."
                ),
            ),
        ],
    )
    async def test_apply_no_work_pool_failures(
        self, from_flow_kwargs, apply_kwargs, expected_message
    ):
        deployment = RunnerDeployment.from_flow(
            dummy_flow_1,
            __file__,
            interval=3600,
            **from_flow_kwargs,
        )

        with pytest.raises(
            ValueError,
            match=expected_message,
        ):
            await deployment.apply(**apply_kwargs)

    async def test_apply_raises_on_api_errors(self, work_pool_with_image_variable):
        deployment = RunnerDeployment.from_flow(
            dummy_flow_1,
            __file__,
            work_pool_name=work_pool_with_image_variable.name,
            job_variables={"image_pull_policy": "blork"},
        )

        with pytest.raises(
            DeploymentApplyError,
            match=re.escape(
                "Error creating deployment: <ValidationError: \"'blork' is not one of"
                " ['IfNotPresent', 'Always', 'Never']\">"
            ),
        ):
            await deployment.apply()

    async def test_create_runner_deployment_from_storage(self):
        storage = MockStorage()

        deployment = await RunnerDeployment.from_storage(
            storage=storage,
            entrypoint="flows.py:test_flow",
            name="test-deployment",
            interval=datetime.timedelta(seconds=30),
            description="Test Deployment Description",
            tags=["tag1", "tag2"],
            version="1.0.0",
            enforce_parameter_schema=True,
        )

        # Verify the created RunnerDeployment's attributes
        assert deployment.name == "test-deployment"
        assert deployment.flow_name == "test-flow"
        assert deployment.schedule.interval == datetime.timedelta(seconds=30)
        assert deployment.tags == ["tag1", "tag2"]
        assert deployment.version == "1.0.0"
        assert deployment.description == "Test Deployment Description"
        assert deployment.enforce_parameter_schema is True
        assert "$STORAGE_BASE_PATH" in deployment._path
        assert deployment.entrypoint == "flows.py:test_flow"
        assert deployment.storage == storage


class TestServer:
    async def test_healthcheck_fails_as_expected(self):
        runner = Runner()
        runner.last_polled = pendulum.now("utc").subtract(minutes=5)

        health_check = perform_health_check(runner)
        assert health_check().status_code == status.HTTP_503_SERVICE_UNAVAILABLE

        runner.last_polled = pendulum.now("utc")
        assert health_check().status_code == status.HTTP_200_OK


class TestDeploy:
    @pytest.fixture
    def mock_build_image(self, monkeypatch):
        mock = MagicMock()
        monkeypatch.setattr("prefect.deployments.runner.build_image", mock)
        return mock

    @pytest.fixture
    def mock_docker_client(self, monkeypatch):
        mock = MagicMock()
        mock.return_value.__enter__.return_value = mock
        mock.api.push.return_value = []
        monkeypatch.setattr("prefect.deployments.runner.docker_client", mock)
        return mock

    @pytest.fixture
    def mock_generate_default_dockerfile(self, monkeypatch):
        mock = MagicMock()
        monkeypatch.setattr(
            "prefect.deployments.runner.generate_default_dockerfile", mock
        )
        return mock

    async def test_deploy(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        prefect_client: PrefectClient,
        capsys,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
            ),
        )
        assert len(deployment_ids) == 2
        mock_generate_default_dockerfile.assert_called_once()
        mock_build_image.assert_called_once_with(
            tag="test-registry/test-image:test-tag", context=Path.cwd(), pull=True
        )
        mock_docker_client.api.push.assert_called_once_with(
            repository="test-registry/test-image",
            tag="test-tag",
            stream=True,
            decode=True,
        )

        deployment_1 = await prefect_client.read_deployment_by_name(
            f"{dummy_flow_1.name}/test_runner"
        )
        assert deployment_1.id == deployment_ids[0]

        deployment_2 = await prefect_client.read_deployment_by_name(
            "test-flow/test_runner"
        )
        assert deployment_2.id == deployment_ids[1]
        assert deployment_2.pull_steps == [{"prefect.fake.module": {}}]

        console_output = capsys.readouterr().out
        assert (
            f"prefect worker start --pool {work_pool_with_image_variable.name!r}"
            in console_output
        )
        assert "prefect deployment run [DEPLOYMENT_NAME]" in console_output

    async def test_deploy_to_default_work_pool(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        prefect_client: PrefectClient,
        capsys,
    ):
        with temporary_settings(
            updates={PREFECT_DEFAULT_WORK_POOL_NAME: work_pool_with_image_variable.name}
        ):
            deployment_ids = await deploy(
                await dummy_flow_1.to_deployment(__file__),
                await (
                    await flow.from_source(
                        source=MockStorage(), entrypoint="flows.py:test_flow"
                    )
                ).to_deployment(__file__),
                image=DeploymentImage(
                    name="test-registry/test-image",
                    tag="test-tag",
                ),
            )
            assert len(deployment_ids) == 2
            mock_generate_default_dockerfile.assert_called_once()
            mock_build_image.assert_called_once_with(
                tag="test-registry/test-image:test-tag", context=Path.cwd(), pull=True
            )
            mock_docker_client.api.push.assert_called_once_with(
                repository="test-registry/test-image",
                tag="test-tag",
                stream=True,
                decode=True,
            )

            deployment_1 = await prefect_client.read_deployment_by_name(
                f"{dummy_flow_1.name}/test_runner"
            )
            assert deployment_1.id == deployment_ids[0]

            deployment_2 = await prefect_client.read_deployment_by_name(
                "test-flow/test_runner"
            )
            assert deployment_2.id == deployment_ids[1]
            assert deployment_2.pull_steps == [{"prefect.fake.module": {}}]

            console_output = capsys.readouterr().out
            assert (
                f"prefect worker start --pool {work_pool_with_image_variable.name!r}"
                in console_output
            )
            assert "prefect deployment run [DEPLOYMENT_NAME]" in console_output

    async def test_deploy_non_existent_work_pool(self):
        with pytest.raises(
            ValueError, match="Could not find work pool 'non-existent'."
        ):
            await deploy(
                await dummy_flow_1.to_deployment(__file__),
                work_pool_name="non-existent",
                image="test-registry/test-image",
            )

    async def test_deploy_non_image_work_pool(self, process_work_pool):
        with pytest.raises(
            ValueError,
            match=(
                f"Work pool {process_work_pool.name!r} does not support custom Docker"
                " images."
            ),
        ):
            await deploy(
                await dummy_flow_1.to_deployment(__file__),
                work_pool_name=process_work_pool.name,
                image="test-registry/test-image",
            )

    async def test_deployment_image_tag_handling(self):
        # test image tag has default
        image = DeploymentImage(
            name="test-registry/test-image",
        )
        assert image.name == "test-registry/test-image"
        assert image.tag.startswith(str(pendulum.now("utc").year))

        # test image tag can be inferred
        image = DeploymentImage(
            name="test-registry/test-image:test-tag",
        )
        assert image.name == "test-registry/test-image"
        assert image.tag == "test-tag"
        assert image.reference == "test-registry/test-image:test-tag"

        # test image tag can be provided
        image = DeploymentImage(name="test-registry/test-image", tag="test-tag")
        assert image.name == "test-registry/test-image"
        assert image.tag == "test-tag"
        assert image.reference == "test-registry/test-image:test-tag"

        # test both can't be provided
        with pytest.raises(
            ValueError, match="both 'test-tag' and 'bad-tag' were provided"
        ):
            DeploymentImage(name="test-registry/test-image:test-tag", tag="bad-tag")

    async def test_deploy_custom_dockerfile(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await dummy_flow_2.to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
                dockerfile="Dockerfile",
            ),
        )
        assert len(deployment_ids) == 2
        # Shouldn't be called because we're providing a custom Dockerfile
        mock_generate_default_dockerfile.assert_not_called()
        mock_build_image.assert_called_once_with(
            tag="test-registry/test-image:test-tag",
            context=Path.cwd(),
            pull=True,
            dockerfile="Dockerfile",
        )

    async def test_deploy_skip_build(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        prefect_client: PrefectClient,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await dummy_flow_2.to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
            ),
            build=False,
        )
        assert len(deployment_ids) == 2
        mock_generate_default_dockerfile.assert_not_called()
        mock_build_image.assert_not_called()
        mock_docker_client.api.push.assert_not_called()

        deployment_1 = await prefect_client.read_deployment(
            deployment_id=deployment_ids[0]
        )
        assert (
            deployment_1.infra_overrides["image"] == "test-registry/test-image:test-tag"
        )

        deployment_2 = await prefect_client.read_deployment(
            deployment_id=deployment_ids[1]
        )
        assert (
            deployment_2.infra_overrides["image"] == "test-registry/test-image:test-tag"
        )

    async def test_deploy_skip_push(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await dummy_flow_2.to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
            ),
            push=False,
        )
        assert len(deployment_ids) == 2
        mock_generate_default_dockerfile.assert_called_once()
        mock_build_image.assert_called_once_with(
            tag="test-registry/test-image:test-tag", context=Path.cwd(), pull=True
        )
        mock_docker_client.api.push.assert_not_called()

    async def test_deploy_do_not_print_next_steps(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        capsys,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
            ),
            print_next_steps_message=False,
        )
        assert len(deployment_ids) == 2

        assert "prefect deployment run [DEPLOYMENT_NAME]" not in capsys.readouterr().out

    async def test_deploy_push_work_pool(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        push_work_pool,
        capsys,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=push_work_pool.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
            ),
            print_next_steps_message=False,
        )
        assert len(deployment_ids) == 2

        console_output = capsys.readouterr().out
        assert "prefect worker start" not in console_output
        assert "prefect deployment run [DEPLOYMENT_NAME]" not in console_output

    async def test_deploy_managed_work_pool_doesnt_prompt_worker_start_or_build_image(
        self,
        managed_work_pool,
        capsys,
        mock_generate_default_dockerfile,
        mock_build_image,
        mock_docker_client,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=managed_work_pool.name,
            image=DeploymentImage(
                name="test-registry/test-image",
                tag="test-tag",
            ),
            print_next_steps_message=False,
        )

        assert len(deployment_ids) == 2

        console_output = capsys.readouterr().out
        assert "Successfully created/updated all deployments!" in console_output

        assert "Building image" not in capsys.readouterr().out
        assert "Pushing image" not in capsys.readouterr().out
        assert "prefect worker start" not in console_output
        assert "prefect deployment run [DEPLOYMENT_NAME]" not in console_output

        mock_generate_default_dockerfile.assert_not_called()
        mock_build_image.assert_not_called()
        mock_docker_client.api.push.assert_not_called()

    async def test_deploy_with_image_string(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image="test-registry/test-image:test-tag",
        )
        assert len(deployment_ids) == 2

        mock_build_image.assert_called_once_with(
            tag="test-registry/test-image:test-tag",
            context=Path.cwd(),
            pull=True,
        )

    async def test_deploy_without_image_with_flow_stored_remotely(
        self,
        work_pool_with_image_variable,
    ):
        deployment_id = await deploy(
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
        )

        assert len(deployment_id) == 1

    async def test_deploy_without_image_or_flow_storage_raises(
        self,
        work_pool_with_image_variable,
    ):
        with pytest.raises(ValueError):
            await deploy(
                await dummy_flow_1.to_deployment(__file__),
                work_pool_name=work_pool_with_image_variable.name,
            )

    async def test_deploy_with_image_and_flow_stored_remotely_raises(
        self,
        work_pool_with_image_variable,
    ):
        with pytest.raises(RuntimeError, match="Failed to generate Dockerfile"):
            await deploy(
                await (
                    await flow.from_source(
                        source=MockStorage(), entrypoint="flows.py:test_flow"
                    )
                ).to_deployment(__file__),
                work_pool_name=work_pool_with_image_variable.name,
                image="test-registry/test-image:test-tag",
            )

    async def test_deploy_multiple_flows_one_using_storage_one_without_raises_with_no_image(
        self,
        work_pool_with_image_variable,
    ):
        with pytest.raises(ValueError):
            await deploy(
                await dummy_flow_1.to_deployment(__file__),
                await (
                    await flow.from_source(
                        source=MockStorage(), entrypoint="flows.py:test_flow"
                    )
                ).to_deployment(__file__),
                work_pool_name=work_pool_with_image_variable.name,
            )

    async def test_deploy_with_image_string_no_tag(
        self,
        mock_build_image: MagicMock,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(__file__),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image="test-registry/test-image",
        )
        assert len(deployment_ids) == 2

        used_name, used_tag = parse_image_tag(
            mock_build_image.mock_calls[0].kwargs["tag"]
        )
        assert used_name == "test-registry/test-image"
        assert used_tag is not None

    async def test_deploy_with_partial_success(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        capsys,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(
                __file__, job_variables={"image_pull_policy": "blork"}
            ),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__),
            work_pool_name=work_pool_with_image_variable.name,
            image="test-registry/test-image",
        )
        assert len(deployment_ids) == 1

        console_output = capsys.readouterr().out
        assert (
            "Encountered errors while creating/updating deployments" in console_output
        )
        assert "failed" in console_output
        # just the start of the error due to wrapping
        assert "'blork' is not one" in console_output
        assert "prefect worker start" in console_output
        assert "To execute flow runs from these deployments" in console_output

    async def test_deploy_with_complete_failure(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        capsys,
    ):
        deployment_ids = await deploy(
            await dummy_flow_1.to_deployment(
                __file__, job_variables={"image_pull_policy": "blork"}
            ),
            await (
                await flow.from_source(
                    source=MockStorage(), entrypoint="flows.py:test_flow"
                )
            ).to_deployment(__file__, job_variables={"image_pull_policy": "blork"}),
            work_pool_name=work_pool_with_image_variable.name,
            image="test-registry/test-image",
        )
        assert len(deployment_ids) == 0

        console_output = capsys.readouterr().out
        assert (
            "Encountered errors while creating/updating deployments" in console_output
        )
        assert "failed" in console_output
        # just the start of the error due to wrapping
        assert "'blork' is not one" in console_output

        assert "prefect worker start" not in console_output
        assert "To execute flow runs from these deployments" not in console_output

    async def test_deploy_raises_with_only_deployment_failed(
        self,
        mock_build_image,
        mock_docker_client,
        mock_generate_default_dockerfile,
        work_pool_with_image_variable,
        capsys,
    ):
        with pytest.raises(
            DeploymentApplyError,
            match=re.escape(
                "Error creating deployment: <ValidationError: \"'blork' is not one of"
                " ['IfNotPresent', 'Always', 'Never']\">"
            ),
        ):
            await deploy(
                await dummy_flow_1.to_deployment(
                    __file__, job_variables={"image_pull_policy": "blork"}
                ),
                work_pool_name=work_pool_with_image_variable.name,
                image="test-registry/test-image",
            )

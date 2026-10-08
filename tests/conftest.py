import uuid
from pathlib import Path

import pytest

from goodhands.cli import sample_task
from goodhands.demo import create_demo
from goodhands.models import Check, Policy, RunnerSettings, Settings
from goodhands.store import Store


@pytest.fixture
def tmp_path():
    # Keep test fixtures in the writable workspace, with normal inherited Windows ACLs.
    root = Path(__file__).resolve().parents[1] / ".goodhands" / "test-fixtures"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    create_demo(root)
    return root


@pytest.fixture
def store(project):
    result = Store(project)
    yield result
    result.close()


@pytest.fixture
def task():
    return sample_task()


@pytest.fixture
def settings():
    return Settings(
        runner=RunnerSettings(kind="local"),
        checks={"unit": Check(argv=["python", "-m", "unittest", "discover", "-s", "tests", "-v"])},
        policy=Policy(max_model_calls=100),
    )

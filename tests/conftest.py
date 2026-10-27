import pytest

from gpu_lab.dataset import generate_dataset, split_dataset
from gpu_lab.labeling import ReferenceOracle
from gpu_lab.model import train_model
from gpu_lab.profiles import load_profiles


@pytest.fixture(scope="session")
def profiles():
    return load_profiles()


@pytest.fixture(scope="session")
def oracle():
    return ReferenceOracle.load()


@pytest.fixture(scope="session")
def rows(profiles, oracle):
    return generate_dataset(profiles, oracle, seed=41, episodes_per_profile=40, steps=16)[0]


@pytest.fixture(scope="session")
def splits(rows):
    return split_dataset(rows, seed=41)


@pytest.fixture(scope="session")
def fitted(splits):
    return train_model(splits["train"], seed=41, trees=40)

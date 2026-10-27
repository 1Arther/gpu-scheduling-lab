import random
import pytest

from gpu_lab.resources import Allocation, GPUState, ResourceRequest


@pytest.mark.parametrize("reservation", [ResourceRequest(1000000, 1), ResourceRequest(1, 101), ResourceRequest(1, 1, 11)])
def test_all_three_capacity_constraints_are_atomic(profiles, reservation):
    state = GPUState("test", profiles["rtx4090"])
    before = state.snapshot()
    assert not state.can_allocate(reservation)
    with pytest.raises(ValueError, match="Insufficient"):
        state.allocate(Allocation("too-large", "training", reservation))
    assert state.snapshot() == before


def test_exact_capacity_and_release(profiles):
    state = GPUState("test", profiles["rtx4090"])
    original = state.snapshot()
    request = ResourceRequest(state.profile.modeled_memory_mib, 100, 10)
    allocation = Allocation("a", "training", request)
    state.allocate(allocation)
    assert (state.remaining_memory_mib, state.remaining_compute_units, state.remaining_instances) == (0, 0, 0)
    assert state.instances_of_type("training") == 10
    assert state.release("a") == allocation
    after = state.snapshot()
    assert after["revision"] == 2
    after["revision"] = original["revision"]
    assert after == original


def test_duplicate_and_double_release(profiles):
    state = GPUState("test", profiles["l40"])
    a = Allocation("a", "inference", ResourceRequest(1000, 20))
    state.allocate(a)
    before = state.snapshot()
    with pytest.raises(ValueError, match="Duplicate"):
        state.allocate(a)
    assert state.snapshot() == before
    state.release("a")
    with pytest.raises(KeyError):
        state.release("a")
    assert state.revision == 2


@pytest.mark.parametrize("values", [(0, 1, 1), (-1, 1, 1), (1, 0, 1), (1, 1, 0), (True, 1, 1), (1, 1.5, 1)])
def test_invalid_reservation(values):
    with pytest.raises(ValueError):
        ResourceRequest(*values)


@pytest.mark.parametrize("profile_id", ["rtx4090", "rtx5090", "l40"])
def test_randomized_ledger_never_overallocates_and_recovers(profiles, profile_id):
    rng = random.Random(1234)
    state = GPUState("fuzz", profiles[profile_id])
    for i in range(600):
        if state.allocations and rng.random() < 0.4:
            state.release(rng.choice(list(state.allocations)))
        else:
            a = Allocation(str(i), rng.choice(("training", "inference")),
                           ResourceRequest(rng.randint(1, 16000), rng.randint(1, 70), rng.randint(1, 3)))
            before = state.snapshot()
            if state.can_allocate(a.request):
                state.allocate(a)
            else:
                with pytest.raises(ValueError):
                    state.allocate(a)
                assert state.snapshot() == before
        assert 0 <= state.remaining_memory_mib <= state.profile.modeled_memory_mib
        assert 0 <= state.remaining_compute_units <= 100
        assert 0 <= state.allocated_instances <= state.max_instances
        assert state.instances_of_type("training") + state.instances_of_type("inference") == state.allocated_instances
    for allocation_id in list(state.allocations):
        state.release(allocation_id)
    assert state.remaining_memory_mib == state.profile.modeled_memory_mib
    assert state.remaining_compute_units == 100
    assert state.allocated_instances == 0


def test_external_ledger_is_read_only(profiles):
    state = GPUState("test", profiles["l40"])
    with pytest.raises(TypeError):
        state.allocations["fake"] = Allocation("fake", "training", ResourceRequest(1, 1))

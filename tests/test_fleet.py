from collections import Counter

from gpu_lab.fleet import create_fleet


def test_user_supplied_4_4_3_layout_is_offline(profiles):
    fleet = create_fleet(profiles)
    assert len(fleet) == 11
    assert Counter(s.profile.profile_id for s in fleet.values()) == {"rtx4090": 4, "rtx5090": 4, "l40": 3}
    assert all(s.remaining_compute_units == 100 and s.allocated_instances == 0 for s in fleet.values())
    assert all(gpu_id.startswith("offline-") for gpu_id in fleet)

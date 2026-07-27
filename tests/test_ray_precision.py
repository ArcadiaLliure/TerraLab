import pytest

from TerraLab.terrain.ray_precision import ray_count


@pytest.mark.parametrize(
    ("step_deg", "expected"),
    ((5.0, 72), (0.5, 720), (0.05, 7_200), (0.005, 72_000)),
)
def test_ray_count_covers_supported_precision_range(step_deg, expected):
    assert ray_count(step_deg) == expected

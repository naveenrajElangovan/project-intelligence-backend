import pytest

from scripts.seed_project_record import _validated_retrieval_profile


def test_retrieval_profile_partial_update_accepts_complete_bounded_values() -> None:
    payload = {
        "retrievalProfile": {
            "maxChunksPerSource": 6,
            "rerankTopN": 16,
            "mixedSourceTopN": 12,
            "rerankScoreThreshold": 0.0,
        }
    }

    assert _validated_retrieval_profile(payload) == payload["retrievalProfile"]


def test_retrieval_profile_partial_update_requires_every_field() -> None:
    with pytest.raises(SystemExit, match="rerankScoreThreshold"):
        _validated_retrieval_profile(
            {
                "retrievalProfile": {
                    "maxChunksPerSource": 6,
                    "rerankTopN": 16,
                    "mixedSourceTopN": 12,
                }
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("maxChunksPerSource", 0), ("rerankTopN", 51), ("rerankScoreThreshold", 1.1)),
)
def test_retrieval_profile_partial_update_rejects_out_of_bounds_values(
    field: str, value: int | float
) -> None:
    profile = {
        "maxChunksPerSource": 6,
        "rerankTopN": 16,
        "mixedSourceTopN": 12,
        "rerankScoreThreshold": 0.0,
    }
    profile[field] = value

    with pytest.raises(SystemExit):
        _validated_retrieval_profile({"retrievalProfile": profile})

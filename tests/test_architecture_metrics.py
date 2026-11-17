from scripts.analyze_architecture import paired


def row(arm, passed, seconds, usage=True):
    return {
        "id": arm,
        "case": "fixture",
        "repeat": 1,
        "arm": arm,
        "passed": passed,
        "seconds": seconds,
        "budget_events": [
            {
                "usage": {"input_tokens": 100, "output_tokens": 10} if usage else None,
                "estimated_cny": "0.001",
            }
        ],
    }


def test_architecture_pairing_excludes_failed_speed_and_unknown_usage():
    result = paired([row("unified", False, 1), row("multi", True, 10)])
    assert result["n_pairs"] == 1 and result["multi_only_pass"] == 1
    assert result["both_contract_pass"] == 0 and result["co_success_unified_seconds"]["n"] == 0
    result = paired([row("unified", True, 8, False), row("multi", True, 10)])
    assert result["both_contract_pass"] == 1 and result["complete_usage_co_success_pairs"] == 0
    assert result["unified_tokens"] == result["multi_tokens"] == 0
    result = paired([row("unified", True, 8), row("multi", True, 10)])
    assert result["complete_usage_co_success_pairs"] == 1
    assert result["paired_multi_minus_unified_seconds"]["mean"] == 2
    assert result["unified_tokens"] == result["multi_tokens"] == 110

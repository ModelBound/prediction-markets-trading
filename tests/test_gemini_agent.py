from gemini_agent import _compose_system_prompt, _normalize_decision


def test_compose_system_prompt_appends_schema_for_modelbound():
    prompt = _compose_system_prompt("You are a trading agent.")
    assert '"action"' in prompt
    assert "Response Format" in prompt


def test_normalize_infers_trade_from_opportunities():
    decision = _normalize_decision({
        "reasoning": "Strong edge on gas market",
        "opportunities": [{
            "ticker": "KXGAS-26JUN",
            "side": "yes",
            "market_price": 5,
            "my_probability": 85,
            "edge": 80,
        }],
    })
    assert decision["action"] == "trade"
    assert decision["trades"][0]["ticker"] == "KXGAS-26JUN"


def test_normalize_pass_when_no_action_or_trades():
    decision = _normalize_decision({"reasoning": "Nothing looks good"})
    assert decision["action"] == "pass"
    assert decision["pass_reason"]

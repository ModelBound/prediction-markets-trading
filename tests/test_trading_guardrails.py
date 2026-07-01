import config


def test_is_blocked_series_15m_crypto():
    assert config.is_blocked_series("KXSOL15M")
    assert config.is_blocked_series("KXBNB15M")


def test_is_blocked_series_commodities():
    assert config.is_blocked_series("KXAAAGASD")
    assert config.is_blocked_series("KXBRENTW")
    assert config.is_blocked_series("KXWTI")
    assert config.is_blocked_series("KXCOPPERD")


def test_is_blocked_series_allowed():
    assert not config.is_blocked_series("KXNBA")
    assert not config.is_blocked_series("KXBILLSCOUNT")


def test_max_plausible_yes_probability():
    assert config.max_plausible_yes_probability(6) == 21
    assert config.max_plausible_yes_probability(25) == 40
    assert config.max_plausible_yes_probability(50) == 95

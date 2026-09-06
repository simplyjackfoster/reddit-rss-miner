from reddit_rss_miner.authors import AuthorFlagger


def test_specs_and_labels():
    f = AuthorFlagger.from_specs(["alice, u/Bob", "official:Team_Account,helper"])
    assert f.flags("ALICE") == ["flagged"] and f.flags("bob") == ["flagged"]
    assert f.flags("team_account") == ["official"] and f.flags("nobody") == [] and f.flags(None) == []


def test_empty_flagger():
    assert AuthorFlagger().flags("anyone") == [] and AuthorFlagger.from_specs([]).flags("x") == []

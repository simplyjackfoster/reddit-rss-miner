import pytest

from reddit_rss_miner.config import Credentials, MissingCredentials, load_credentials, parse_env_file


def test_parse_env_file_ignores_comments_and_quotes():
    assert parse_env_file('# c\nA=1\nB="two"\n\nC=\'3\'\nnoequals\n') == {"A": "1", "B": "two", "C": "3"}


def test_precedence_env_over_dotenv_over_home(tmp_path):
    dot, home = tmp_path / ".env", tmp_path / "home.env"
    dot.write_text("REDDIT_FEED_TOKEN=dot\nREDDIT_FEED_USER=dotuser\n")
    home.write_text("REDDIT_FEED_TOKEN=home\nREDDIT_FEED_USER=homeuser\n")
    assert load_credentials({}, [dot, home]) == Credentials("dot", "dotuser")
    assert load_credentials({}, [home]) == Credentials("home", "homeuser")
    assert load_credentials({"REDDIT_FEED_TOKEN": "env"}, [dot, home]) == Credentials("env", "dotuser")


def test_missing_raises(tmp_path):
    with pytest.raises(MissingCredentials):
        load_credentials({}, [tmp_path / "nope"])


def test_as_params():
    assert Credentials("t", "u").as_params() == {"feed": "t", "user": "u"}

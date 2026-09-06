from reddit_rss_miner.pacing import FileLockPacer, IntervalPacer, state_path_for


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def test_interval_pacer_spaces_requests_and_honours_penalty():
    c = Clock(); p = IntervalPacer(1.0, clock=c, sleep=c.sleep)
    p.acquire(); p.acquire(); p.acquire()
    assert c.t == 1002.0
    p.penalize(30); p.acquire()
    assert c.t == 1032.0


def test_two_processes_share_one_file(tmp_path):
    """Two pacers on the same state file behave like one 1 req/s stream, not two."""
    c = Clock(); path = tmp_path / "x.pace"
    a = FileLockPacer(path, 1.0, clock=c, sleep=c.sleep)
    b = FileLockPacer(path, 1.0, clock=c, sleep=c.sleep)
    a.acquire()          # t=1000, next=1001
    b.acquire()          # must wait until 1001
    assert c.t == 1001.0
    a.acquire()
    assert c.t == 1002.0


def test_penalty_written_by_one_process_is_honoured_by_the_other(tmp_path):
    c = Clock(); path = tmp_path / "x.pace"
    a = FileLockPacer(path, 1.0, clock=c, sleep=c.sleep)
    b = FileLockPacer(path, 1.0, clock=c, sleep=c.sleep)
    a.acquire()
    a.penalize(45)       # a got a 429 with reset=44
    b.acquire()
    assert c.t == 1045.0


def test_state_path_hashes_token(tmp_path):
    p = state_path_for("secret-token", tmp_path)
    assert "secret" not in str(p) and p.suffix == ".pace" and p.parent == tmp_path

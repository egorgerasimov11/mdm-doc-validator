"""Wave-2 enrichment hardening: one strict matcher for every registry (a single
shared token like "First" used to produce confident-looking false FOUNDs), a
best-overlap row pick, and http cache+retry behavior."""
import types

from mdmdoc.web_enrichment import http as wh
from mdmdoc.web_enrichment.match import best_match, name_matches


def test_single_shared_token_no_longer_matches():
    # only "first" is shared — the old FDIC matcher accepted this
    assert not name_matches("First National Bank", "First Community Credit Union")


def test_two_meaningful_tokens_match():
    assert name_matches("Banco Santander Chile", "BANCO SANTANDER-CHILE")
    assert name_matches("Intrust Bank NA", "INTRUST Bank, National Association")


def test_legal_form_words_are_not_meaningful():
    # "bank" + "trust" are stopwords — sharing only them must not match
    assert not name_matches("Community Bank & Trust", "Heritage Bank and Trust")


def test_best_match_prefers_max_overlap():
    cands = [{"name": "First Bank of Ohio"},
             {"name": "First National Bank of Omaha"}]
    got = best_match("First National Bank Omaha", cands, key=lambda c: c["name"])
    assert got and got["name"] == "First National Bank of Omaha"
    assert best_match("Zenith Credit Union", cands, key=lambda c: c["name"]) is None


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.ok = status < 400
        self._data = data or {}

    def json(self):
        return self._data


def test_http_cache_hit(monkeypatch):
    wh._CACHE.clear()
    calls = []
    monkeypatch.setattr(wh._SESSION, "send",
                        lambda prep, **kw: calls.append(1) or _Resp(200, {"n": 1}))
    monkeypatch.setattr(wh.egress, "assert_safe_outbound", lambda *a, **k: None)
    assert wh.get_json("https://example.test/api", params={"q": "intrust"}) == {"n": 1}
    assert wh.get_json("https://example.test/api", params={"q": "intrust"}) == {"n": 1}
    assert len(calls) == 1  # second answer came from the TTL cache


def test_http_retries_once_on_429(monkeypatch):
    wh._CACHE.clear()
    seq = [_Resp(429), _Resp(200, {"ok": True})]
    monkeypatch.setattr(wh._SESSION, "send", lambda prep, **kw: seq.pop(0))
    monkeypatch.setattr(wh.egress, "assert_safe_outbound", lambda *a, **k: None)
    monkeypatch.setattr(wh.time, "sleep", lambda s: None)
    assert wh.get_json("https://example.test/limited") == {"ok": True}


def test_http_gives_up_after_second_5xx(monkeypatch):
    wh._CACHE.clear()
    seq = [_Resp(503), _Resp(503)]
    monkeypatch.setattr(wh._SESSION, "send", lambda prep, **kw: seq.pop(0))
    monkeypatch.setattr(wh.egress, "assert_safe_outbound", lambda *a, **k: None)
    monkeypatch.setattr(wh.time, "sleep", lambda s: None)
    assert wh.get_json("https://example.test/down") is None

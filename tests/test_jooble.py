"""Tests for job_searcher.fetch_jooble with a fake urlopen: never calls Jooble (the free key has a lifetime limit)."""
import datetime as dt
import io
import json
import urllib.error

import pytest

import job_searcher as js

KEY = "FAKE-key-123"  # not a real key; the tests check it never reaches a log line or error message
DAY = dt.timedelta(days=1)


def item(id, updated, **kw):
    j = {"id": id, "title": "Junior Software Engineer", "company": "Acme", "location": "İstanbul",
         "snippet": "Python", "salary": "", "source": "workable.com", "type": "Full-time",
         "link": f"https://tr.jooble.org/desc/{id}", "updated": updated}
    j.update(kw)
    return j


FRESH = f"{dt.date.today() - 2 * DAY}T10:00:00.0000000"
PAGES = [  # one response per search in config.json, in order
    [item(111, FRESH, snippet="<b>Python</b> &amp; SQL ile backend&nbsp;geliştirme, <br/>Git"),
     item(222, f"{dt.date.today() - 60 * DAY}T10:00:00"),  # months old: dropped
     item(333, "not a date", title="Junior Data Analyst", location="Evden çalışmak", type="",
          salary="30.000 TL")],  # unreadable date: kept
    [item(111, FRESH, title="duplicate from the second search")],
    [],
]


@pytest.fixture
def fake(monkeypatch):
    """Records each request and log line; answers with PAGES (or raises an exception put in PAGES)."""
    reqs, lines, pages = [], [], list(PAGES)
    monkeypatch.setenv("JOOBLE_API_KEY", KEY)
    monkeypatch.setattr(js, "log", lines.append)
    monkeypatch.setattr(js.time, "sleep", lambda s: None)

    def urlopen(req, timeout):
        reqs.append(req)
        page = pages.pop(0) if pages else []  # more searches than pages: answer with no jobs
        if isinstance(page, BaseException):
            raise page
        return io.BytesIO(json.dumps({"totalCount": len(page), "jobs": page}).encode())
    monkeypatch.setattr(js.urllib.request, "urlopen", urlopen)
    return reqs, lines, pages


def test_maps_filters_and_dedupes(fake, cfg):
    jobs = {j["id"]: j for j in js.fetch_jooble(cfg)}
    assert set(jobs) == {"jooble:111", "jooble:333"}  # 222 too old, 111 only once
    a, b = jobs["jooble:111"], jobs["jooble:333"]
    assert a["title"] == "Junior Software Engineer"  # the duplicate from search 2 is ignored
    assert a["descriptionText"] == "Python & SQL ile backend geliştirme, Git"  # tags and entities gone
    assert a["companyName"] == "Acme" and a["location"] == "İstanbul, Türkiye"  # country added for restrictions()
    assert a["postedAt"] == (dt.date.today() - 2 * DAY).isoformat() and a["salary"] == ""
    assert a["employmentType"] == "Full-time" and a["_link"] == "https://tr.jooble.org/desc/111"
    assert a["_remote"] is False
    assert b["_remote"] is True and b["postedAt"] is None and b["employmentType"] is None
    assert b["salary"] == "30.000 TL" and b["location"] == "Evden çalışmak, Türkiye"


def test_requests_one_page_per_search(fake, cfg):
    reqs, _, _ = fake
    js.fetch_jooble(cfg)
    assert len(reqs) == len(cfg["jooble"]["searches"])  # page 1 only: each search costs one request of the quota
    for req, q in zip(reqs, cfg["jooble"]["searches"]):
        assert req.full_url == f"https://tr.jooble.org/api/{KEY}" and req.get_method() == "POST"
        assert req.get_header("User-agent") == js.JOOBLE_UA  # Python's default UA gets a 403
        assert req.get_header("Content-type") == "application/json"
        assert json.loads(req.data) == {"keywords": q, "location": "Türkiye", "page": 1, "ResultOnPage": 50}
        assert KEY not in req.data.decode() and all(KEY not in v for v in req.headers.values())


def test_missing_key_skips_without_a_request(fake, cfg, monkeypatch):
    reqs, lines, _ = fake
    monkeypatch.delenv("JOOBLE_API_KEY")
    assert js.fetch_jooble(cfg) == []
    assert reqs == [] and lines == ["  Jooble skipped: JOOBLE_API_KEY not set"]


def test_missing_key_is_not_a_network_failure(fake, cfg, monkeypatch):
    monkeypatch.delenv("JOOBLE_API_KEY")
    jobs, n_raw, network_down = js.fetch_round([("Jooble Türkiye", False, lambda: js.fetch_jooble(cfg))], set(), cfg)
    assert (jobs, n_raw, network_down) == ([], 0, False)


def forbidden():
    return urllib.error.HTTPError(f"https://tr.jooble.org/api/{KEY}", 403, "Forbidden", {}, io.BytesIO())


def test_http_403_raises_without_the_key(fake, cfg):
    reqs, lines, pages = fake
    pages[:] = [forbidden() for _ in cfg["jooble"]["searches"]]
    with pytest.raises(urllib.error.HTTPError) as e:
        js.fetch_jooble(cfg)
    assert e.value.code == 403 and e.value.url == "https://tr.jooble.org/api/<key>"
    assert KEY not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__
    assert not js.is_network_error(e.value)  # the server answered: no whole-round retry


def test_http_403_in_fetch_round_logs_no_key(fake, cfg):
    reqs, lines, pages = fake
    pages[:] = [forbidden() for _ in cfg["jooble"]["searches"]]
    _, _, network_down = js.fetch_round([("Jooble Türkiye", False, lambda: js.fetch_jooble(cfg))], set(), cfg)
    assert not network_down and any("Jooble Türkiye search failed" in l for l in lines)
    assert not any(KEY in l for l in lines)


def test_all_searches_down_raises_a_network_error_without_the_key(fake, cfg):
    reqs, lines, pages = fake
    pages[:] = [urllib.error.URLError(f"connection reset on https://tr.jooble.org/api/{KEY}")
                for _ in cfg["jooble"]["searches"]]
    with pytest.raises(urllib.error.URLError) as e:
        js.fetch_jooble(cfg)
    # still a network error, so a fully offline round is retried by fetch_all
    assert js.is_network_error(e.value) and KEY not in str(e.value) and "<key>" in str(e.value)
    assert len(lines) == len(cfg["jooble"]["searches"]) and not any(KEY in l for l in lines)


def test_one_failed_search_keeps_the_others(fake, cfg):
    reqs, lines, pages = fake
    server_error = urllib.error.HTTPError(f"https://tr.jooble.org/api/{KEY}", 503, "Unavailable", {}, io.BytesIO())
    pages[:] = [[item(1, FRESH)], server_error, [item(3, FRESH)]]  # a 5xx may pass: try the next search
    assert [j["id"] for j in js.fetch_jooble(cfg)] == ["jooble:1", "jooble:3"]
    assert len(reqs) == 3 and len(lines) == 1 and "Jooble search 'junior data engineer' failed" in lines[0]
    assert KEY not in lines[0]


@pytest.mark.parametrize("code", [401, 403])
def test_bad_key_or_used_up_quota_stops_at_the_first_search(fake, cfg, code):
    reqs, lines, pages = fake
    pages[:] = [urllib.error.HTTPError(f"https://tr.jooble.org/api/{KEY}", code, "No", {}, io.BytesIO()),
                [item(2, FRESH)], [item(3, FRESH)]]
    with pytest.raises(urllib.error.HTTPError) as e:
        js.fetch_jooble(cfg)
    assert len(reqs) == 1 and e.value.code == code  # the other searches would only spend quota
    assert e.value.url == "https://tr.jooble.org/api/<key>" and e.value.__cause__ is None


def test_url_quoted_key_is_masked_too(fake, cfg, monkeypatch):
    reqs, lines, pages = fake
    monkeypatch.setenv("JOOBLE_API_KEY", "a/b c")  # quoted in the URL as a%2Fb%20c
    pages[:] = [urllib.error.HTTPError("https://tr.jooble.org/api/a%2Fb%20c", 403, "Forbidden", {}, io.BytesIO())
                for _ in cfg["jooble"]["searches"]]
    with pytest.raises(urllib.error.HTTPError) as e:
        js.fetch_jooble(cfg)
    assert e.value.url == "https://tr.jooble.org/api/<key>" and not any("a%2Fb" in l for l in lines)


@pytest.mark.parametrize("location, expected", [
    ("İstanbul", "İstanbul, Türkiye"),  # a bare city: we searched Türkiye, so say so
    ("Berlin, Germany", "Berlin, Germany"),  # already names its country: left alone
    ("Ankara, Türkiye", "Ankara, Türkiye"),
    ("", "Türkiye"),
])
def test_country_added_only_to_bare_locations(fake, cfg, location, expected):
    reqs, lines, pages = fake
    pages[:] = [[item(1, FRESH, location=location)]]
    assert js.fetch_jooble(cfg)[0]["location"] == expected


def test_items_without_an_id_are_skipped(fake, cfg):
    reqs, lines, pages = fake
    pages[:] = [[item(None, FRESH), item(0, FRESH)]]  # 0 is a valid id, only a missing one is skipped
    assert [j["id"] for j in js.fetch_jooble(cfg)] == ["jooble:0"]


def test_relevance_and_score_on_short_snippets(fake, cfg):
    # tech titles pass relevant() on the title alone; the short snippet only limits the skill part of the score
    jobs = {j["id"]: j for j in js.fetch_jooble(cfg)}
    assert all(js.relevant(j, cfg) for j in jobs.values())
    a = js.analyse(jobs["jooble:111"], cfg)
    assert a["matched"] == ["Python", "SQL", "Git"] and a["restrictions"] == []
    # a generic title needs 3 tech skills in the snippet, which ~300 characters rarely holds
    stajyer = dict(jobs["jooble:111"], title="Stajyer", descriptionText="Python bilen stajyer")
    assert not js.relevant(stajyer, cfg)

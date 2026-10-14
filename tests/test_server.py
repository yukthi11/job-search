import json

import pandas as pd
import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "applied_store", server.JsonListStore(tmp_path / "a.json"))
    monkeypatch.setattr(server, "JOBS_CSV", tmp_path / "jobs.csv")
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_toggle_applied_adds_then_removes(client):
    job = {"job_url": "https://x.test/1", "title": "T"}
    assert client.post("/api/toggle_applied", json=job).get_json()["status"] == "added"
    assert client.get("/api/applied").get_json() == [job]
    assert client.post("/api/toggle_applied", json=job).get_json()["status"] == "removed"
    assert client.get("/api/applied").get_json() == []


@pytest.mark.parametrize("body", [None, [], {}, {"job_url": ""}])
def test_toggle_applied_rejects_bad_bodies(client, body):
    response = client.post("/api/toggle_applied", json=body)
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_non_json_body_is_400_not_500(client):
    response = client.post("/api/toggle_applied", data="x", content_type="text/plain")
    assert response.status_code == 400


def test_corrupt_store_is_backed_up_not_overwritten(client, tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json", encoding="utf-8")
    assert client.get("/api/applied").get_json() == []
    assert (tmp_path / "a.json.corrupt").read_text(encoding="utf-8") == "{not json"


def test_store_save_is_atomic_json(tmp_path):
    store = server.JsonListStore(tmp_path / "s.json")
    store.save([{"a": 1}])
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8")) == [{"a": 1}]
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost:5000",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5",
        "file:///etc/passwd",
        "ftp://example.com",
    ],
)
def test_enrich_blocks_non_public_urls(client, url):
    response = client.post("/api/enrich", json={"company_url": url})
    assert response.status_code == 400


def test_enrich_without_url_returns_existing_emails(client):
    response = client.post(
        "/api/enrich", json={"company_url": None, "existing_emails": "a@x.io, b@x.io"}
    )
    data = response.get_json()
    assert data["emails"] == ["a@x.io", "b@x.io"]
    assert data["company_website"] is None
    assert data["careers_url"] is None


LINKEDIN_PROFILE = """
<script type="application/ld+json">{"@type": "Organization", "name": "Acme",
 "sameAs": "http://www.acme.io", "description": "We make anvils.",
 "numberOfEmployees": {"@type": "QuantitativeValue", "value": 120},
 "address": {"streetAddress": "1 High St", "addressLocality": "London"}}</script>
"""


def test_enrich_resolves_website_from_job_board_profile(client, monkeypatch):
    pages = {
        "https://www.linkedin.com/company/acme": LINKEDIN_PROFILE,
        "http://www.acme.io": '<a href="/jobs">Careers</a> hi@acme.io',
    }
    monkeypatch.setattr(server, "is_public_http_url", lambda _url: True)
    monkeypatch.setattr(
        server,
        "scrape_page",
        lambda url, **_k: server.BeautifulSoup(pages[url], "html.parser") if url in pages else None,
    )
    data = client.post(
        "/api/enrich", json={"company_url": "https://www.linkedin.com/company/acme"}
    ).get_json()
    assert data["company_website"] == "http://www.acme.io"
    assert data["careers_url"] == "http://www.acme.io/jobs"
    assert data["emails"] == ["hi@acme.io"]
    assert data["profile"] == {
        "description": "We make anvils.",
        "employees": 120,
        "address": "1 High St, London",
    }


def test_enrich_extracts_emails_and_careers_link(client, monkeypatch):
    html = '<a href="/careers">Join us</a> contact: hi@acme.io logo@x.png'
    monkeypatch.setattr(server, "is_public_http_url", lambda _url: True)
    monkeypatch.setattr(
        server, "scrape_page", lambda *_a, **_k: server.BeautifulSoup(html, "html.parser")
    )
    data = client.post("/api/enrich", json={"company_url": "acme.io"}).get_json()
    assert data["emails"] == ["hi@acme.io"]
    assert data["careers_url"] == "https://acme.io/careers"


@pytest.mark.parametrize(
    "body", [{"results_wanted": "abc"}, {"results_wanted": 0}, {"results_wanted": 10_000}]
)
def test_scrape_validates_params(client, body):
    assert client.post("/api/scrape", json=body).status_code == 400


def test_scrape_writes_csv_and_jobs_endpoint_reads_it(client, monkeypatch, tmp_path):
    frame = pd.DataFrame([{"title": "T", "job_url": "u", "emails": None}])
    captured = {}

    def fake_scrape(**kwargs):
        captured.update(kwargs)
        return frame

    monkeypatch.setattr(server, "scrape_jobs", fake_scrape)
    response = client.post("/api/scrape", json={"results_wanted": 5})
    assert response.status_code == 200
    assert captured["results_wanted"] == 5
    assert client.get("/api/jobs").get_json()[0]["title"] == "T"


def test_scrape_unknown_country_is_400(client, monkeypatch):
    def fake_scrape(**_):
        raise ValueError("Invalid country string")

    monkeypatch.setattr(server, "scrape_jobs", fake_scrape)
    assert client.post("/api/scrape", json={"country_indeed": "mars"}).status_code == 400


def test_worldwide_only_scrapes_only_remote_boards():
    params = server.parse_scrape_params({"worldwide_only": True, "is_remote": False})
    assert params["site_name"] == server.REMOTE_BOARD_SITES
    assert params["is_remote"] is True

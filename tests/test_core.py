import pytest
from bs4 import BeautifulSoup

import jobspy
from jobspy import scrape_jobs
from jobspy.indeed import Indeed, _gql_escape
from jobspy.linkedin import LinkedIn
from jobspy.model import (
    Compensation,
    CompensationInterval,
    Country,
    JobPost,
    JobResponse,
    JobType,
    Location,
    ScraperInput,
    Site,
)
from jobspy.util import (
    RotatingProxySession,
    currency_parser,
    extract_salary,
    get_enum_from_value,
    map_str_to_site,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("$50,000", 50000.0),
        ("$1,234.50", 1234.5),
        ("1.234,56", 1234.56),
        ("£45000", 45000.0),
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_currency_parser(text, expected):
    assert currency_parser(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("$50k - $60k", ("yearly", 50000, 60000, "USD")),
        ("£30,000 - £40,000", ("yearly", 30000, 40000, "GBP")),
        ("$25.50 - $30", ("hourly", 25.5, 30, "USD")),
        ("$5,000 - $6,000", ("monthly", 5000, 6000, "USD")),
        ("$100,000 - $900", (None, None, None, None)),
        ("no pay listed", (None, None, None, None)),
        (None, (None, None, None, None)),
    ],
)
def test_extract_salary(text, expected):
    assert extract_salary(text) == expected


def test_extract_salary_enforce_annual():
    assert extract_salary("$25 - $30", enforce_annual_salary=True) == (
        "hourly",
        52000,
        62400,
        "USD",
    )


def test_enum_lookups():
    assert get_enum_from_value("fulltime") is JobType.FULL_TIME
    with pytest.raises(ValueError):
        get_enum_from_value("nonsense")
    assert map_str_to_site(" LinkedIn ") is Site.LINKEDIN
    with pytest.raises(ValueError):
        map_str_to_site("nope")


def test_proxy_formatting_keeps_scheme():
    assert RotatingProxySession.format_proxy("socks4://h:1")["http"] == "socks4://h:1"
    assert RotatingProxySession.format_proxy("h:1")["http"] == "http://h:1"


def test_country_parsing_and_display():
    assert Country.from_string("U.K.") is Country.UK
    assert Country.SLOVENIA.indeed_domain_value[0] == "si"
    assert Location(city="London", country="GB").display_location() == "London, UK"
    with pytest.raises(ValueError):
        Country.BAHRAIN.glassdoor_domain_value


class _FakeScraper:
    def __init__(self, **_):
        pass

    def scrape(self, _input):
        return JobResponse(
            jobs=[
                JobPost(title="a", job_url="u1", description="$50k - $60k"),
                JobPost(
                    title="b",
                    job_url="u2",
                    compensation=Compensation(
                        interval=CompensationInterval.HOURLY,
                        min_amount=10,
                        max_amount=20,
                    ),
                    job_type=[JobType.FULL_TIME],
                    skills=["x", "y"],
                ),
            ]
        )


class _BrokenScraper(_FakeScraper):
    def scrape(self, _input):
        raise RuntimeError("boom")


def test_scrape_jobs_isolates_failing_sites(monkeypatch):
    monkeypatch.setitem(jobspy.SCRAPER_MAPPING, Site.LINKEDIN, _FakeScraper)
    monkeypatch.setitem(jobspy.SCRAPER_MAPPING, Site.INDEED, _BrokenScraper)

    df = scrape_jobs(["linkedin", "indeed"], enforce_annual_salary=True)

    assert list(df["site"]) == ["linkedin", "linkedin"]
    by_title = df.set_index("title")
    assert by_title.loc["a", "salary_source"] == "description"
    assert by_title.loc["b", "salary_source"] == "direct_data"
    assert by_title.loc["b", "min_amount"] == 20800
    assert by_title.loc["b", "skills"] == "x, y"


def test_scrape_jobs_returns_empty_frame_when_nothing_found(monkeypatch):
    monkeypatch.setitem(jobspy.SCRAPER_MAPPING, Site.LINKEDIN, _BrokenScraper)
    assert scrape_jobs("linkedin").empty


def _linkedin_card(salary="", location="Pune, Maharashtra, India"):
    return BeautifulSoup(
        f"""
        <div class="base-search-card">
          <span class="sr-only">Dev</span>
          <h4 class="base-search-card__subtitle"><a href="https://x.co/c?a=1">Acme</a></h4>
          <div class="base-search-card__metadata">
            <span class="job-search-card__location">{location}</span>
          </div>
          {f'<span class="job-search-card__salary-info">{salary}</span>' if salary else ""}
        </div>""",
        "html.parser",
    ).div


@pytest.fixture
def linkedin():
    scraper = LinkedIn()
    scraper.scraper_input = ScraperInput(site_type=[Site.LINKEDIN])
    return scraper


@pytest.mark.parametrize("salary", ["$100K", "garbage", "$80K/yr - $100K/yr"])
def test_linkedin_salary_never_raises(linkedin, salary):
    job = linkedin._process_job(_linkedin_card(salary), "1", False)
    assert job.title == "Dev"


def test_linkedin_unknown_country_does_not_abort(linkedin):
    job = linkedin._process_job(_linkedin_card(location="A, B, Atlantis"), "1", False)
    assert job.location.country == "Atlantis"


def test_indeed_process_job_tolerates_missing_fields():
    indeed = Indeed()
    indeed.base_url = "https://uk.indeed.com"
    indeed.scraper_input = ScraperInput(site_type=[Site.INDEED])
    job = indeed._process_job(
        {"key": "k", "title": "T", "attributes": [], "employer": None}
    )
    assert job.job_url.endswith("jk=k")
    assert job.company_name is None and job.date_posted is None


def test_gql_escape():
    assert _gql_escape('a"b\\c') == 'a\\"b\\\\c'
    assert _gql_escape(None) == ""


# --- remote classification, Indeed remote filter, remote boards ---------------

from datetime import date, timedelta

from jobspy.indeed import Indeed
from jobspy.model import JobPost, ScraperInput
from jobspy.remote import classify_remote
from jobspy.remote_boards import RemoteOK, WeWorkRemotely


@pytest.mark.parametrize(
    "title,location,description,expected",
    [
        ("Engineer", "Remote", "", "remote"),
        ("Engineer", "London", "This is a fully remote role.", "remote"),
        ("Engineer", "London", "We are remote-first.", "remote"),
        ("Engineer", "London", "Hybrid, 3 days a week in the office.", "hybrid"),
        ("Engineer (Hybrid)", "Remote", "", "hybrid"),
        ("Engineer", "London", "Fully remote. Not hybrid.", "remote"),
        ("Engineer", "London", "No remote work is available.", "onsite"),
        ("Engineer", "London", "Remote work is not possible for this role.", "onsite"),
        ("Engineer", "London", "Competes with remote-only startups.", "unclear"),
        ("Engineer", "London", None, "unclear"),
    ],
)
def test_classify_remote(title, location, description, expected):
    assert classify_remote(title, location, description) == expected


def test_classify_remote_uses_work_from_home_type():
    assert classify_remote("Dev", "Pune", "", "Remote") == "remote"
    assert classify_remote("Dev", "Pune", "", "Hybrid") == "hybrid"


class _MixedScraper(_FakeScraper):
    def scrape(self, _input):
        return JobResponse(
            jobs=[
                JobPost(title="r", job_url="1", description="Fully remote role."),
                JobPost(title="h", job_url="2", description="Hybrid working."),
                JobPost(title="o", job_url="3", description="No remote work."),
                JobPost(title="u", job_url="4", description=None),
            ]
        )


def test_remote_only_drops_hybrid_and_onsite_and_sets_is_remote(monkeypatch):
    monkeypatch.setitem(jobspy.SCRAPER_MAPPING, Site.LINKEDIN, _MixedScraper)
    df = scrape_jobs("linkedin", remote_only=True)
    assert list(df["title"]) == ["r", "u"]
    assert list(df["is_remote"]) == [True, False]
    everything = scrape_jobs("linkedin")
    assert sorted(everything["remote_type"]) == ["hybrid", "onsite", "remote", "unclear"]


def test_indeed_remote_filter_wins_over_hours_old():
    indeed = Indeed()
    indeed.scraper_input = ScraperInput(site_type=[Site.INDEED], is_remote=True, hours_old=24)
    filters = indeed._build_filters()
    assert "DSQF7" in filters and "dateOnIndeed" not in filters
    indeed.scraper_input = ScraperInput(site_type=[Site.INDEED], hours_old=24)
    assert "dateOnIndeed" in indeed._build_filters()


def test_indeed_drops_old_jobs_locally():
    today = date.today()
    jobs = [
        JobPost(title="new", job_url="a", date_posted=today),
        JobPost(title="old", job_url="b", date_posted=today - timedelta(days=5)),
        JobPost(title="undated", job_url="c"),
    ]
    assert [j.title for j in Indeed._drop_older_than(jobs, 24)] == ["new", "undated"]


def test_remoteok_filters_by_search_and_age(monkeypatch):
    now = date.today().isoformat()
    feed = [
        {"legal": "notice"},
        {"id": "1", "position": "Senior Python Engineer", "company": "A", "tags": ["python"],
         "url": "https://remoteok.com/1", "date": now + "T00:00:00+00:00", "location": "", "description": "<p>x</p>"},
        {"id": "2", "position": "Designer", "company": "B", "tags": ["design"],
         "url": "https://remoteok.com/2", "date": now + "T00:00:00+00:00"},
        {"id": "3", "position": "Python Engineer", "company": "C", "tags": [],
         "url": "https://remoteok.com/3", "date": "2020-01-01T00:00:00+00:00"},
    ]

    class _Resp:
        def json(self):
            return feed

    board = RemoteOK()
    monkeypatch.setattr(board, "_get", lambda *_a, **_k: _Resp())
    result = board.scrape(ScraperInput(site_type=[Site.REMOTEOK], search_term="python engineer", hours_old=24))
    assert [j.id for j in result.jobs] == ["rok-1"]
    assert result.jobs[0].work_from_home_type == "Remote"


def test_weworkremotely_parses_rss(monkeypatch):
    rss = b"""<rss><channel><item>
      <title>Acme: Backend Developer</title><region>Anywhere</region><category>Programming</category>
      <type>Full-Time</type><pubDate>Tue, 06 Oct 2026 10:00:00 +0000</pubDate>
      <link>https://weworkremotely.com/remote-jobs/acme-backend</link>
      <description>&lt;p&gt;Hi&lt;/p&gt;</description></item></channel></rss>"""

    class _Resp:
        content = rss

    board = WeWorkRemotely()
    monkeypatch.setattr(board, "_get", lambda *_a, **_k: _Resp())
    jobs = board.scrape(ScraperInput(site_type=[Site.WEWORKREMOTELY], search_term="backend")).jobs
    assert len(jobs) == 1  # the same link in the other feeds is de-duplicated
    assert (jobs[0].company_name, jobs[0].title) == ("Acme", "Backend Developer")
    assert jobs[0].date_posted == date(2026, 10, 6)

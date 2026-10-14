"""Scrapers for job boards that list only remote jobs, via their public feeds.

Each board's terms ask for a link back to the original listing and a mention of
the source; `job_url` points at the board's own page and `site` names it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from jobspy.model import (
    Compensation,
    CompensationInterval,
    DescriptionFormat,
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import create_logger, create_session, markdown_converter

log = create_logger("RemoteBoards")

_STOP_WORDS = {"a", "an", "and", "for", "in", "of", "the", "to", "with"}
_JOB_TYPES = {
    "full-time": JobType.FULL_TIME,
    "full time": JobType.FULL_TIME,
    "fulltime": JobType.FULL_TIME,
    "part-time": JobType.PART_TIME,
    "part time": JobType.PART_TIME,
    "contract": JobType.CONTRACT,
    "contractor": JobType.CONTRACT,
    "internship": JobType.INTERNSHIP,
    "temporary": JobType.TEMPORARY,
}


def _tokens(text: str | None) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9+#.]+", (text or "").lower()) if t not in _STOP_WORDS]


def _job_types(*values: str | None) -> list[JobType] | None:
    found = [_JOB_TYPES[v.strip().lower()] for v in values if v and v.strip().lower() in _JOB_TYPES]
    return found or None


def _compensation(low, high, currency, interval=CompensationInterval.YEARLY):
    try:
        low, high = (float(low) if low else None), (float(high) if high else None)
    except (TypeError, ValueError):
        return None
    if not (low or high):
        return None
    return Compensation(
        interval=interval, min_amount=low, max_amount=high, currency=currency or "USD"
    )


def _utc_date(value) -> date | None:
    """Parses an ISO string, RFC 2822 string or epoch seconds into a date."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, OverflowError):
        return None


class RemoteBoard(Scraper):
    """Base class: fetch a feed, then filter by search term and age locally."""

    site: Site
    display: str

    def __init__(self, proxies=None, ca_cert=None, user_agent=None):
        super().__init__(self.site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.session = create_session(proxies=proxies, ca_cert=ca_cert, is_tls=False, has_retry=True)
        self.headers = {"User-Agent": user_agent or "Mozilla/5.0 JobSpy"}

    def _get(self, url: str, **params):
        response = self.session.get(
            url, headers=self.headers, params=params or None, timeout=20,
            verify=self.ca_cert or True,
        )
        response.raise_for_status()
        return response

    def _fetch(self, scraper_input: ScraperInput) -> list[JobPost]:
        raise NotImplementedError

    def _description(self, html: str | None, scraper_input: ScraperInput) -> str | None:
        if html and scraper_input.description_format == DescriptionFormat.MARKDOWN:
            return markdown_converter(html)
        return html

    @staticmethod
    def _is_fresh(job: JobPost, scraper_input: ScraperInput) -> bool:
        if not scraper_input.hours_old or not job.date_posted:
            return True
        # date_posted has day resolution, so round the cutoff up to whole days.
        oldest = datetime.now(timezone.utc).date() - timedelta(
            days=-(-scraper_input.hours_old // 24)
        )
        return job.date_posted >= oldest

    def _matches(self, job: JobPost, extra: str, scraper_input: ScraperInput) -> bool:
        """All search words must appear in the title or tags/category."""
        wanted = _tokens(scraper_input.search_term)
        haystack = set(_tokens(f"{job.title} {extra}"))
        return all(
            word in haystack or any(h.startswith(word) for h in haystack) for word in wanted
        )

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        jobs = self._fetch(scraper_input)
        for job in jobs:
            job.work_from_home_type = "Remote"  # every listing here is remote
        jobs = [j for j in jobs if self._is_fresh(j, scraper_input)]
        jobs.sort(key=lambda j: j.date_posted or date.min, reverse=True)
        start = scraper_input.offset or 0
        log.info(f"{self.display}: {len(jobs)} matching jobs")
        return JobResponse(jobs=jobs[start : start + scraper_input.results_wanted])


class RemoteOK(RemoteBoard):
    site = Site.REMOTEOK
    display = "RemoteOK"

    def _fetch(self, scraper_input):
        jobs = []
        for item in self._get("https://remoteok.com/api").json():
            if not isinstance(item, dict) or not item.get("position"):
                continue  # the first element is a legal notice
            job = JobPost(
                id=f"rok-{item['id']}",
                title=item["position"],
                company_name=item.get("company"),
                job_url=item.get("url") or item["apply_url"],
                job_url_direct=item.get("apply_url"),
                location=Location(country=item.get("location") or "Remote"),
                description=self._description(item.get("description"), scraper_input),
                company_logo=item.get("company_logo") or item.get("logo") or None,
                compensation=_compensation(item.get("salary_min"), item.get("salary_max"), "USD"),
                date_posted=_utc_date(item.get("date") or item.get("epoch")),
                is_remote=True,
            )
            if self._matches(job, " ".join(item.get("tags") or []), scraper_input):
                jobs.append(job)
        return jobs


class Remotive(RemoteBoard):
    site = Site.REMOTIVE
    display = "Remotive"

    def _fetch(self, scraper_input):
        params = {"search": scraper_input.search_term} if scraper_input.search_term else {}
        jobs = []
        for item in self._get("https://remotive.com/api/remote-jobs", **params).json().get("jobs", []):
            job = JobPost(
                id=f"rem-{item['id']}",
                title=item["title"],
                company_name=item.get("company_name"),
                job_url=item["url"],
                location=Location(country=item.get("candidate_required_location") or "Remote"),
                description=self._description(item.get("description"), scraper_input),
                company_logo=item.get("company_logo") or None,
                job_type=_job_types(item.get("job_type")),
                date_posted=_utc_date(item.get("publication_date")),
                job_function=item.get("category"),
                is_remote=True,
            )
            if self._matches(job, f"{item.get('category') or ''} {' '.join(item.get('tags') or [])}", scraper_input):
                jobs.append(job)
        return jobs


class WeWorkRemotely(RemoteBoard):
    site = Site.WEWORKREMOTELY
    display = "WeWorkRemotely"
    feeds = (
        "https://weworkremotely.com/remote-jobs.rss",
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    )

    def _fetch(self, scraper_input):
        jobs, seen = [], set()
        for feed in self.feeds:
            try:
                root = ET.fromstring(self._get(feed).content)
            except Exception as error:  # one bad feed should not lose the rest
                log.warning(f"{feed}: {error}")
                continue
            for item in root.iter("item"):
                link = (item.findtext("link") or item.findtext("guid") or "").strip()
                if not link or link in seen:
                    continue
                seen.add(link)
                raw_title = (item.findtext("title") or "").strip()
                company, _, title = raw_title.partition(": ")
                category = item.findtext("category") or ""
                job = JobPost(
                    id=f"wwr-{link.rstrip('/').rsplit('/', 1)[-1]}",
                    title=title or raw_title,
                    company_name=company if title else None,
                    job_url=link,
                    location=Location(country=item.findtext("region") or "Remote"),
                    description=self._description(item.findtext("description"), scraper_input),
                    job_type=_job_types(item.findtext("type")),
                    date_posted=_utc_date(item.findtext("pubDate")),
                    job_function=category or None,
                    skills=[s.strip() for s in (item.findtext("skills") or "").split(",") if s.strip()] or None,
                    is_remote=True,
                )
                if self._matches(job, f"{category} {item.findtext('skills') or ''}", scraper_input):
                    jobs.append(job)
        return jobs


class Himalayas(RemoteBoard):
    site = Site.HIMALAYAS
    display = "Himalayas"
    max_pages = 10

    def _fetch(self, scraper_input):
        term = scraper_input.search_term
        url = "https://himalayas.app/jobs/api/search" if term else "https://himalayas.app/jobs/api"
        params = {"q": term} if term else {}
        jobs, cursor = [], None
        for _ in range(self.max_pages):
            if cursor:
                params["cursor"] = cursor
            data = self._get(url, **params).json()
            for item in data.get("jobs", []):
                link = item.get("applicationLink") or item.get("guid")
                if not link:
                    continue
                restrictions = item.get("locationRestrictions") or []
                job = JobPost(
                    id=f"him-{item.get('guid') or link}"[-60:],
                    title=item["title"],
                    company_name=item.get("companyName"),
                    job_url=link,
                    location=Location(country=", ".join(restrictions) or "Worldwide"),
                    description=self._description(item.get("description") or item.get("excerpt"), scraper_input),
                    company_logo=item.get("companyLogo"),
                    job_type=_job_types(item.get("employmentType")),
                    compensation=_compensation(item.get("minSalary"), item.get("maxSalary"), item.get("currency")),
                    date_posted=_utc_date(item.get("pubDate")),
                    job_level=", ".join(item.get("seniority") or []) or None,
                    is_remote=True,
                )
                jobs.append(job)
            cursor = data.get("nextCursor")
            fresh = sum(self._is_fresh(j, scraper_input) for j in jobs)
            if not cursor or fresh >= scraper_input.results_wanted + (scraper_input.offset or 0):
                break
        return jobs

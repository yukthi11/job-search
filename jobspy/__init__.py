from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

import pandas as pd

from jobspy.bayt import BaytScraper
from jobspy.bdjobs import BDJobs
from jobspy.glassdoor import Glassdoor
from jobspy.google import Google
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.naukri import Naukri
from jobspy.model import JobType, Location, JobResponse, Country
from jobspy.model import SITE_DISPLAY_NAMES, SalarySource, ScraperInput, Site
from jobspy.util import (
    set_logger_level,
    extract_salary,
    create_logger,
    get_enum_from_value,
    map_str_to_site,
    convert_to_annual,
    desired_order,
)
from jobspy.remote import ONSITE, HYBRID, REMOTE, classify_remote
from jobspy.remote_boards import Himalayas, RemoteOK, Remotive, WeWorkRemotely
from jobspy.ziprecruiter import ZipRecruiter


SCRAPER_MAPPING = {
    Site.LINKEDIN: LinkedIn,
    Site.INDEED: Indeed,
    Site.ZIP_RECRUITER: ZipRecruiter,
    Site.GLASSDOOR: Glassdoor,
    Site.GOOGLE: Google,
    Site.BAYT: BaytScraper,
    Site.NAUKRI: Naukri,
    Site.BDJOBS: BDJobs,
    Site.REMOTEOK: RemoteOK,
    Site.REMOTIVE: Remotive,
    Site.WEWORKREMOTELY: WeWorkRemotely,
    Site.HIMALAYAS: Himalayas,
}

_PRIMARY_SORT_COLUMNS = ["site", "date_posted"]


def _resolve_sites(site_name: str | list[str] | Site | list[Site] | None) -> list[Site]:
    """Normalises the `site_name` argument into a list of Site enums."""
    if site_name is None:
        return list(Site)
    names = site_name if isinstance(site_name, list) else [site_name]
    return [map_str_to_site(name) if isinstance(name, str) else name for name in names]


def _join_values(values) -> str | None:
    return ", ".join(values) if values else None


def _apply_compensation(
    job_data: dict, country: Country, enforce_annual_salary: bool
) -> None:
    """Fills interval/min/max/currency/salary_source on `job_data` in place."""
    interval = min_amount = max_amount = currency = None
    compensation = job_data.get("compensation")

    if isinstance(compensation, dict):
        interval = compensation["interval"].value if compensation.get("interval") else None
        min_amount = compensation.get("min_amount")
        max_amount = compensation.get("max_amount")
        currency = compensation.get("currency", "USD")
        job_data.update(
            interval=interval,
            min_amount=min_amount,
            max_amount=max_amount,
            currency=currency,
        )
        if (
            enforce_annual_salary
            and interval
            and interval != "yearly"
            and min_amount
            and max_amount
        ):
            convert_to_annual(job_data)
    elif country == Country.USA:
        interval, min_amount, max_amount, currency = extract_salary(
            job_data["description"], enforce_annual_salary=enforce_annual_salary
        )
        job_data.update(
            interval=interval,
            min_amount=min_amount,
            max_amount=max_amount,
            currency=currency,
        )
    else:
        job_data.update(interval=None, min_amount=None, max_amount=None, currency=None)

    if not job_data["min_amount"]:
        job_data["salary_source"] = None
    elif isinstance(compensation, dict):
        job_data["salary_source"] = SalarySource.DIRECT_DATA.value
    else:
        job_data["salary_source"] = SalarySource.DESCRIPTION.value


def _job_to_row(job, site: str, country: Country, enforce_annual_salary: bool) -> dict:
    """Flattens a JobPost into a dict keyed by `desired_order` columns."""
    job_data = job.model_dump()
    job_data["site"] = site
    job_data["company"] = job_data["company_name"]
    job_data["job_type"] = _join_values(
        enum_member.value[0] for enum_member in (job_data["job_type"] or [])
    )
    job_data["emails"] = _join_values(job_data["emails"])
    job_data["skills"] = _join_values(job_data["skills"])
    if job_data["location"]:
        job_data["location"] = Location(**job_data["location"]).display_location()
    _apply_compensation(job_data, country, enforce_annual_salary)
    job_data["remote_type"] = classify_remote(
        job_data["title"],
        job_data["location"],
        job_data["description"],
        job_data.get("work_from_home_type"),
    )
    # The classifier reads the whole posting; the scraper's own flag is a loose
    # keyword match that also fires on "no remote work".
    job_data["is_remote"] = job_data["remote_type"] == REMOTE
    return job_data


def scrape_jobs(
    site_name: str | list[str] | Site | list[Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool = False,
    remote_only: bool = False,
    job_type: str | None = None,
    easy_apply: bool | None = None,
    results_wanted: int = 15,
    country_indeed: str = "usa",
    proxies: list[str] | str | None = None,
    ca_cert: str | None = None,
    description_format: str = "markdown",
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: list[int] | None = None,
    offset: int | None = 0,
    hours_old: int = None,
    enforce_annual_salary: bool = False,
    verbose: int = 0,
    user_agent: str = None,
    **kwargs,
) -> pd.DataFrame:
    """
    Scrapes job data from job boards concurrently.

    With `remote_only`, jobs classified as hybrid or on-site are dropped (jobs
    whose remote status is unclear are kept).

    A site that fails does not abort the others; its error is logged and its
    results are omitted.
    :return: Pandas DataFrame containing job data
    """
    set_logger_level(verbose)
    job_type_enum = get_enum_from_value(job_type) if job_type else None
    country_enum = Country.from_string(country_indeed)

    scraper_input = ScraperInput(
        site_type=_resolve_sites(site_name),
        country=country_enum,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        distance=distance,
        is_remote=is_remote,
        job_type=job_type_enum,
        easy_apply=easy_apply,
        description_format=description_format,
        linkedin_fetch_description=linkedin_fetch_description,
        results_wanted=results_wanted,
        linkedin_company_ids=linkedin_company_ids,
        offset=offset,
        hours_old=hours_old,
    )

    def scrape_site(site: Site) -> Tuple[str, JobResponse]:
        display_name = SITE_DISPLAY_NAMES[site]
        scraper = SCRAPER_MAPPING[site](
            proxies=proxies, ca_cert=ca_cert, user_agent=user_agent
        )
        try:
            scraped_data = scraper.scrape(scraper_input)
        except Exception as error:
            create_logger(display_name).error(f"scrape failed: {error}")
            return site.value, JobResponse(jobs=[])
        create_logger(display_name).info("finished scraping")
        return site.value, scraped_data

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(scrape_site, scraper_input.site_type))

    rows = [
        _job_to_row(job, site_value, country_enum, enforce_annual_salary)
        for site_value, job_response in results
        for job in job_response.jobs
    ]
    if remote_only:
        rows = [row for row in rows if row["remote_type"] not in (HYBRID, ONSITE)]
    if not rows:
        return pd.DataFrame()

    jobs_df = pd.DataFrame(rows).reindex(columns=desired_order)
    jobs_df["date_posted"] = pd.to_datetime(jobs_df["date_posted"], errors="coerce")
    return jobs_df.sort_values(
        by=_PRIMARY_SORT_COLUMNS, ascending=[True, False], na_position="last"
    ).reset_index(drop=True)


__all__ = ["scrape_jobs", "Site", "JobType", "Country", "BDJobs"]

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Tuple

from jobspy.indeed.constant import job_search_query, api_headers
from jobspy.indeed.util import is_job_remote, get_compensation, get_job_type
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    Location,
    JobResponse,
    JobType,
    DescriptionFormat,
)
from jobspy.util import (
    extract_emails_from_text,
    markdown_converter,
    create_session,
    create_logger,
)

log = create_logger("Indeed")


def _gql_escape(value: str | None) -> str:
    """Escapes a value for use inside a double-quoted GraphQL string."""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


class Indeed(Scraper):
    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        """
        Initializes IndeedScraper with the Indeed API url
        """
        super().__init__(
            Site.INDEED, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent
        )

        self.session = create_session(
            proxies=self.proxies, ca_cert=ca_cert, is_tls=False
        )
        self.scraper_input = None
        self.jobs_per_page = 100
        self.num_workers = 10
        self.seen_urls = set()
        self.headers = None
        self.api_country_code = None
        self.base_url = None
        self.api_url = "https://apis.indeed.com/graphql"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Indeed for jobs with scraper_input criteria
        :param scraper_input:
        :return: job_response
        """
        self.scraper_input = scraper_input
        domain, self.api_country_code = self.scraper_input.country.indeed_domain_value
        self.base_url = f"https://{domain}.indeed.com"
        self.headers = api_headers.copy()
        self.headers["indeed-co"] = self.scraper_input.country.indeed_domain_value
        job_list = []
        page = 1

        cursor = None

        while len(self.seen_urls) < scraper_input.results_wanted + scraper_input.offset:
            log.info(
                f"search page: {page} / {math.ceil(scraper_input.results_wanted / self.jobs_per_page)}"
            )
            jobs, cursor = self._scrape_page(cursor)
            if not jobs:
                log.info(f"found no jobs on page: {page}")
                break
            job_list += jobs
            page += 1
            if not cursor:
                break
        if scraper_input.hours_old and scraper_input.is_remote:
            job_list = self._drop_older_than(job_list, scraper_input.hours_old)
        return JobResponse(
            jobs=job_list[
                scraper_input.offset : scraper_input.offset
                + scraper_input.results_wanted
            ]
        )

    def _scrape_page(self, cursor: str | None) -> Tuple[list[JobPost], str | None]:
        """
        Scrapes a page of Indeed for jobs with scraper_input criteria
        :param cursor:
        :return: jobs found on page, next page cursor
        """
        jobs = []
        new_cursor = None
        filters = self._build_filters()
        search_term = _gql_escape(self.scraper_input.search_term)
        where = _gql_escape(self.scraper_input.location)
        query = job_search_query.format(
            what=(f'what: "{search_term}"' if search_term else ""),
            location=(
                f'location: {{where: "{where}", radius: {self.scraper_input.distance}, radiusUnit: MILES}}'
                if self.scraper_input.location
                else ""
            ),
            dateOnIndeed=self.scraper_input.hours_old,
            cursor=f'cursor: "{_gql_escape(cursor)}"' if cursor else "",
            filters=filters,
        )
        payload = {
            "query": query,
        }
        request_headers = api_headers.copy()
        request_headers["indeed-co"] = self.api_country_code
        if self.user_agent:
            request_headers["user-agent"] = self.user_agent
        response = self.session.post(
            self.api_url,
            headers=request_headers,
            json=payload,
            timeout=10,
            verify=self.ca_cert or True,
        )
        if not response.ok:
            log.info(
                f"responded with status code: {response.status_code} (submit GitHub issue if this appears to be a bug)"
            )
            return jobs, new_cursor
        job_search = (response.json().get("data") or {}).get("jobSearch") or {}
        jobs = job_search.get("results") or []
        new_cursor = (job_search.get("pageInfo") or {}).get("nextCursor")

        job_list = []
        for job in jobs:
            try:
                processed_job = self._process_job(job["job"])
            except (KeyError, TypeError, ValueError) as e:
                log.warning(f"skipping malformed job: {e!r}")
                continue
            if processed_job:
                job_list.append(processed_job)

        return job_list, new_cursor

    @staticmethod
    def _drop_older_than(jobs: list[JobPost], hours_old: int) -> list[JobPost]:
        """Applies hours_old locally; dates only have day resolution, so the cutoff rounds up."""
        oldest = datetime.now().date() - timedelta(days=-(-hours_old // 24))
        return [job for job in jobs if not job.date_posted or job.date_posted >= oldest]

    def _build_filters(self):
        """
        Builds the filters dict for job type/is_remote. If hours_old is provided, composite filter for job_type/is_remote is not possible.
        IndeedApply: filters: { keyword: { field: "indeedApplyScope", keys: ["DESKTOP"] } }
        """
        filters_str = ""
        # Indeed cannot combine its date filter with attribute filters. Remote
        # is the one that matters, so it wins and hours_old is applied locally.
        use_date_filter = self.scraper_input.hours_old and not self.scraper_input.is_remote
        if self.scraper_input.hours_old and self.scraper_input.job_type and not self.scraper_input.is_remote:
            log.warning("Indeed cannot combine hours_old with job_type; job_type is ignored")
        if use_date_filter:
            filters_str = """
            filters: {{
                date: {{
                  field: "dateOnIndeed",
                  start: "{start}h"
                }}
            }}
            """.format(
                start=self.scraper_input.hours_old
            )
        elif self.scraper_input.easy_apply:
            filters_str = """
            filters: {
                keyword: {
                  field: "indeedApplyScope",
                  keys: ["DESKTOP"]
                }
            }
            """
        elif self.scraper_input.job_type or self.scraper_input.is_remote:
            job_type_key_mapping = {
                JobType.FULL_TIME: "CF3CP",
                JobType.PART_TIME: "75GKK",
                JobType.CONTRACT: "NJXCK",
                JobType.INTERNSHIP: "VDTG7",
            }

            keys = []
            if self.scraper_input.job_type:
                key = job_type_key_mapping[self.scraper_input.job_type]
                keys.append(key)

            if self.scraper_input.is_remote:
                keys.append("DSQF7")

            if keys:
                keys_str = '", "'.join(keys)
                filters_str = f"""
                filters: {{
                  composite: {{
                    filters: [{{
                      keyword: {{
                        field: "attributes",
                        keys: ["{keys_str}"]
                      }}
                    }}]
                  }}
                }}
                """
        return filters_str

    def _process_job(self, job: dict) -> JobPost | None:
        """
        Parses the job dict into JobPost model
        :param job: dict to parse
        :return: JobPost if it's a new job
        """
        job_url = f'{self.base_url}/viewjob?jk={job["key"]}'
        if job_url in self.seen_urls:
            return
        self.seen_urls.add(job_url)
        description = (job.get("description") or {}).get("html")
        if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
            description = markdown_converter(description)

        job_type = get_job_type(job.get("attributes") or [])
        published_ms = job.get("datePublished")
        date_posted = (
            datetime.fromtimestamp(published_ms / 1000).strftime("%Y-%m-%d")
            if published_ms
            else None
        )
        employer_info = job.get("employer") or {}
        employer = employer_info.get("dossier")
        employer_details = employer.get("employerDetails", {}) if employer else {}
        rel_url = employer_info.get("relativeCompanyPageUrl")
        location = job.get("location") or {}
        return JobPost(
            id=f'in-{job["key"]}',
            title=job["title"],
            description=description,
            company_name=employer_info.get("name"),
            company_url=f"{self.base_url}{rel_url}" if rel_url else None,
            company_url_direct=((employer or {}).get("links") or {}).get(
                "corporateWebsite"
            ),
            location=Location(
                city=location.get("city"),
                state=location.get("admin1Code"),
                country=location.get("countryCode"),
            ),
            job_type=job_type,
            compensation=get_compensation(job.get("compensation")),
            date_posted=date_posted,
            job_url=job_url,
            job_url_direct=(
                job["recruit"].get("viewJobUrl") if job.get("recruit") else None
            ),
            emails=extract_emails_from_text(description) if description else None,
            is_remote=is_job_remote(job, description),
            company_addresses=(
                employer_details["addresses"][0]
                if employer_details.get("addresses")
                else None
            ),
            company_industry=(
                employer_details["industry"]
                .replace("Iv1", "")
                .replace("_", " ")
                .title()
                .strip()
                if employer_details.get("industry")
                else None
            ),
            company_num_employees=employer_details.get("employeesLocalizedLabel"),
            company_revenue=employer_details.get("revenueLocalizedLabel"),
            company_description=employer_details.get("briefDescription"),
            company_logo=(
                employer["images"].get("squareLogoUrl")
                if employer and employer.get("images")
                else None
            ),
        )

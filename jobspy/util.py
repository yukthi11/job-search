from __future__ import annotations

import logging
import re
from itertools import cycle

import numpy as np
import requests
import tls_client
from markdownify import markdownify as md
from requests.adapters import HTTPAdapter, Retry

from jobspy.model import CompensationInterval, JobType, Site

_log_level = logging.INFO


def create_logger(name: str):
    logger = logging.getLogger(f"JobSpy:{name}")
    logger.propagate = False
    if not logger.handlers:
        logger.setLevel(_log_level)
        console_handler = logging.StreamHandler()
        format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        formatter = logging.Formatter(format)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger


PROXY_SCHEMES = ("http://", "https://", "socks4://", "socks5://")


class RotatingProxySession:
    def __init__(self, proxies=None):
        if isinstance(proxies, str):
            self.proxy_cycle = cycle([self.format_proxy(proxies)])
        elif isinstance(proxies, list):
            self.proxy_cycle = (
                cycle([self.format_proxy(proxy) for proxy in proxies])
                if proxies
                else None
            )
        else:
            self.proxy_cycle = None

    @staticmethod
    def format_proxy(proxy):
        """Utility method to format a proxy string into a dictionary."""
        if proxy.startswith(PROXY_SCHEMES):
            return {"http": proxy, "https": proxy}
        return {"http": f"http://{proxy}", "https": f"http://{proxy}"}


class RequestsRotating(RotatingProxySession, requests.Session):
    def __init__(self, proxies=None, has_retry=False, delay=1, clear_cookies=False):
        RotatingProxySession.__init__(self, proxies=proxies)
        requests.Session.__init__(self)
        self.clear_cookies = clear_cookies
        self.allow_redirects = True
        self.setup_session(has_retry, delay)

    def setup_session(self, has_retry, delay):
        if has_retry:
            retries = Retry(
                total=3,
                connect=3,
                status=3,
                status_forcelist=[500, 502, 503, 504, 429],
                backoff_factor=delay,
            )
            adapter = HTTPAdapter(max_retries=retries)
            self.mount("http://", adapter)
            self.mount("https://", adapter)

    def request(self, method, url, **kwargs):
        if self.clear_cookies:
            self.cookies.clear()

        if self.proxy_cycle:
            next_proxy = next(self.proxy_cycle)
            if next_proxy["http"] != "http://localhost":
                self.proxies = next_proxy
            else:
                self.proxies = {}
        return requests.Session.request(self, method, url, **kwargs)


class TLSRotating(RotatingProxySession, tls_client.Session):
    def __init__(self, proxies=None):
        RotatingProxySession.__init__(self, proxies=proxies)
        tls_client.Session.__init__(self, random_tls_extension_order=True)

    def execute_request(self, *args, **kwargs):
        if self.proxy_cycle:
            next_proxy = next(self.proxy_cycle)
            if next_proxy["http"] != "http://localhost":
                self.proxies = next_proxy
            else:
                self.proxies = {}
        response = tls_client.Session.execute_request(self, *args, **kwargs)
        response.ok = response.status_code in range(200, 400)
        return response


def create_session(
    *,
    proxies: dict | str | None = None,
    ca_cert: str | None = None,
    is_tls: bool = True,
    has_retry: bool = False,
    delay: int = 1,
    clear_cookies: bool = False,
) -> requests.Session:
    """
    Creates a requests session with optional tls, proxy, and retry settings.
    :return: A session object
    """
    if is_tls:
        session = TLSRotating(proxies=proxies)
    else:
        session = RequestsRotating(
            proxies=proxies,
            has_retry=has_retry,
            delay=delay,
            clear_cookies=clear_cookies,
        )

    if ca_cert:
        session.verify = ca_cert

    return session


def set_logger_level(verbose: int | None):
    """
    Adjusts the log level of all JobSpy loggers, including ones created later.

    :param verbose: 0 -> ERROR, 1 -> WARNING, 2 -> INFO (anything else -> INFO).
        None leaves the current level untouched.
    """
    global _log_level
    if verbose is None:
        return
    _log_level = {2: logging.INFO, 1: logging.WARNING, 0: logging.ERROR}.get(
        verbose, logging.INFO
    )
    for logger_name in logging.root.manager.loggerDict:
        if logger_name.startswith("JobSpy:"):
            logging.getLogger(logger_name).setLevel(_log_level)


def markdown_converter(description_html: str):
    if description_html is None:
        return None
    markdown = md(description_html)
    return markdown.strip()

def plain_converter(decription_html:str):
    from bs4 import BeautifulSoup
    if decription_html is None:
        return None
    soup = BeautifulSoup(decription_html, "html.parser")
    text = soup.get_text(separator=" ")
    text = re.sub(r'\s+',' ',text)
    return text.strip()


def extract_emails_from_text(text: str) -> list[str] | None:
    if not text:
        return None
    email_regex = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    return email_regex.findall(text)


def get_enum_from_job_type(job_type_str: str) -> JobType | None:
    """
    Given a string, returns the corresponding JobType enum member if a match is found.
    """
    for job_type in JobType:
        if job_type_str in job_type.value:
            return job_type
    return None


def currency_parser(cur_str: str) -> float | None:
    """
    Parses a price such as "$1,234.50" or "1.234,50 EUR" into a float.
    Returns None when the string holds no parsable number.
    """
    cleaned = re.sub(r"[^-0-9.,]", "", cur_str or "")
    if not any(char.isdigit() for char in cleaned):
        return None
    # A separator followed by 1-2 digits at the end is the decimal mark;
    # every other "." or "," is a thousands separator.
    match = re.fullmatch(r"(-?[\d.,]*?)(?:[.,](\d{1,2}))?", cleaned)
    integer_part, decimals = match.group(1), match.group(2)
    integer_part = re.sub(r"[.,]", "", integer_part)
    try:
        return float(np.round(float(f"{integer_part or 0}.{decimals or 0}"), 2))
    except ValueError:
        return None


def remove_attributes(tag):
    for attr in list(tag.attrs):
        del tag[attr]
    return tag


CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}
HOURS_PER_YEAR = 2080
MONTHS_PER_YEAR = 12
_SALARY_PATTERN = re.compile(
    r"([$£€])(\d+(?:,\d+)*(?:\.\d+)?)\s*([kK]?)\s*[-—–]\s*(?:[$£€])?"
    r"(\d+(?:,\d+)*(?:\.\d+)?)\s*([kK]?)"
)


def extract_salary(
    salary_str,
    lower_limit=1000,
    upper_limit=700000,
    hourly_threshold=350,
    monthly_threshold=30000,
    enforce_annual_salary=False,
):
    """
    Extracts a salary range such as "$50k - $60k" from free text.

    :return: (interval, min_amount, max_amount, currency); all None when no
        plausible range is found. A "k" suffix on either number scales both,
        as in "$50 - $60k".
    """
    no_salary = (None, None, None, None)
    if not salary_str:
        return no_salary

    match = _SALARY_PATTERN.search(salary_str)
    if not match:
        return no_salary

    symbol, min_text, min_k, max_text, max_k = match.groups()
    multiplier = 1000 if (min_k or max_k) else 1
    min_salary = float(min_text.replace(",", "")) * multiplier
    max_salary = float(max_text.replace(",", "")) * multiplier

    if min_salary < hourly_threshold:
        interval, to_annual = CompensationInterval.HOURLY.value, HOURS_PER_YEAR
        fits_interval = max_salary < hourly_threshold
    elif min_salary < monthly_threshold:
        interval, to_annual = CompensationInterval.MONTHLY.value, MONTHS_PER_YEAR
        fits_interval = max_salary < monthly_threshold
    else:
        interval, to_annual = CompensationInterval.YEARLY.value, 1
        fits_interval = True

    annual_min, annual_max = min_salary * to_annual, max_salary * to_annual
    if not (
        fits_interval
        and lower_limit <= annual_min <= upper_limit
        and lower_limit <= annual_max <= upper_limit
        and annual_min < annual_max
    ):
        return no_salary

    currency = CURRENCY_SYMBOLS[symbol]
    if enforce_annual_salary:
        return interval, annual_min, annual_max, currency
    return interval, min_salary, max_salary, currency


def extract_job_type(description: str):
    if not description:
        return []

    keywords = {
        JobType.FULL_TIME: r"full\s?time",
        JobType.PART_TIME: r"part\s?time",
        JobType.INTERNSHIP: r"internship",
        JobType.CONTRACT: r"contract",
    }

    listing_types = []
    for key, pattern in keywords.items():
        if re.search(pattern, description, re.IGNORECASE):
            listing_types.append(key)

    return listing_types if listing_types else None


def map_str_to_site(site_name: str) -> Site:
    try:
        return Site[site_name.strip().upper()]
    except KeyError:
        valid = ", ".join(site.value for site in Site)
        raise ValueError(f"Unknown site '{site_name}'. Valid sites: {valid}") from None


def get_enum_from_value(value_str: str) -> JobType:
    job_type = get_enum_from_job_type(value_str)
    if job_type is None:
        raise ValueError(f"Invalid job type: {value_str}")
    return job_type


ANNUAL_MULTIPLIERS = {"hourly": 2080, "monthly": 12, "weekly": 52, "daily": 260}


def convert_to_annual(job_data: dict):
    multiplier = ANNUAL_MULTIPLIERS.get(job_data["interval"], 1)
    job_data["min_amount"] *= multiplier
    job_data["max_amount"] *= multiplier
    job_data["interval"] = "yearly"


desired_order = [
    "id",
    "site",
    "job_url",
    "job_url_direct",
    "title",
    "company",
    "location",
    "date_posted",
    "job_type",
    "salary_source",
    "interval",
    "min_amount",
    "max_amount",
    "currency",
    "is_remote",
    "remote_type",
    "job_level",
    "job_function",
    "listing_type",
    "emails",
    "description",
    "company_industry",
    "company_url",
    "company_logo",
    "company_url_direct",
    "company_addresses",
    "company_num_employees",
    "company_revenue",
    "company_description",
    # naukri-specific fields
    "skills",
    "experience_range",
    "company_rating",
    "company_reviews_count",
    "vacancy_count",
    "work_from_home_type",
]

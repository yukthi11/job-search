"""Flask UI/API for browsing scraped jobs and tracking applications."""

from __future__ import annotations

import csv
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests as http_requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from jobspy import scrape_jobs

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
APPLIED_JOBS_FILE = BASE_DIR / "applied_jobs.json"
JOBS_CSV = BASE_DIR / "remote_jobs.csv"

SCRAPE_SITES = ["indeed", "linkedin", "glassdoor"]
# Boards that list only remote jobs; they are added when "Remote Only" is on.
REMOTE_BOARD_SITES = ["remoteok", "remotive", "weworkremotely", "himalayas"]
MAX_RESULTS_WANTED = 100
DEFAULT_HOURS_OLD = 24
MAX_REDIRECTS = 3

SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".svg", ".gif", ".css", ".js")
CAREERS_KEYWORDS = (
    "careers",
    "jobs",
    "work-with-us",
    "work_with_us",
    "join-us",
    "join_us",
    "opportunities",
    "vacancies",
    "hiring",
)
# The scraped `company_url` is usually the job board's own profile page for the
# company, not the company's website.
JOB_BOARD_NAMES = ("linkedin", "indeed", "glassdoor", "naukri", "ziprecruiter", "bayt", "bdjobs")
# Applicant-tracking hosts: a direct apply link there says nothing about the company's site.
ATS_NAMES = (
    "greenhouse", "lever", "workable", "ashbyhq", "smartrecruiters", "myworkdayjobs",
    "workday", "bamboohr", "recruitee", "teamtailor", "personio", "jobvite", "icims",
    "breezy", "applytojob", "oraclecloud", "taleo", "successfactors", "dayforcehcm",
)
FALLBACK_PATHS = ("/contact", "/about", "/contact-us", "/about-us")

log = logging.getLogger("JobSpy:server")
app = Flask(__name__, static_folder=str(STATIC_DIR))


class ApiError(Exception):
    """An error that maps to a JSON response with the given HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify({"error": error.message}), error.status


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    return jsonify({"error": error.description}), error.code


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    log.exception("Unhandled error")
    return jsonify({"error": str(error)}), 500


def json_body() -> dict:
    """Returns the request's JSON object body or raises a 400."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object")
    return data


class JsonListStore:
    """A JSON file holding a list, with thread-safe, atomic read/write."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def load(self) -> list:
        with self.lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
                raise ValueError("expected a JSON list")
            except ValueError:
                backup = self.path.with_suffix(self.path.suffix + ".corrupt")
                log.error("%s is corrupt; moved to %s", self.path.name, backup.name)
                os.replace(self.path, backup)
                return []

    def save(self, items: list) -> None:
        with self.lock:
            fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    json.dump(items, tmp, indent=4)
                os.replace(tmp_name, self.path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise


applied_store = JsonListStore(APPLIED_JOBS_FILE)
scrape_lock = threading.Lock()


def jobs_response(df: pd.DataFrame) -> Response:
    """Serialises a DataFrame with pandas so NaN becomes null."""
    return Response(
        df.to_json(orient="records", default_handler=str),
        mimetype="application/json",
    )


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/applied", methods=["GET"])
def get_applied_jobs():
    return jsonify(applied_store.load())


@app.route("/api/toggle_applied", methods=["POST"])
def toggle_applied():
    job = json_body()
    job_url = job.get("job_url")
    if not job_url:
        raise ApiError("Invalid job data, must contain job_url")

    with applied_store.lock:
        applied = applied_store.load()
        remaining = [j for j in applied if j.get("job_url") != job_url]
        if len(remaining) < len(applied):
            applied, status = remaining, "removed"
        else:
            applied.append(job)
            status = "added"
        applied_store.save(applied)
    return jsonify({"status": status, "applied_jobs": applied})


def is_public_http_url(url: str) -> bool:
    """True if `url` is http(s) and every address its host resolves to is public."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except socket.gaierror:
        return False
    return bool(addresses) and all(
        ipaddress.ip_address(address).is_global for address in addresses
    )


def scrape_page(url: str, timeout: int = 6) -> BeautifulSoup | None:
    """Fetches a public page (following a few vetted redirects), or None."""
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not is_public_http_url(url):
                return None
            resp = http_requests.get(
                url, headers=SCRAPER_HEADERS, timeout=timeout, allow_redirects=False
            )
            if resp.is_redirect:
                url = urljoin(url, resp.headers.get("Location", ""))
                continue
            resp.raise_for_status()
            # Bytes, so BeautifulSoup detects the encoding instead of requests guessing.
            return BeautifulSoup(resp.content, "html.parser")
    except http_requests.RequestException:
        pass
    return None


def is_job_board_url(url: str) -> bool:
    """True if `url` is a job board's page (e.g. a LinkedIn company profile)."""
    labels = (urlparse(url).hostname or "").lower().split(".")
    return any(board in labels for board in JOB_BOARD_NAMES)


def as_http_url(value) -> str | None:
    """Returns `value` if it is a non-empty http(s) URL string, else None."""
    if isinstance(value, str) and urlparse(value.strip()).scheme in ("http", "https"):
        return value.strip()
    return None


def parse_company_profile(soup: BeautifulSoup) -> dict:
    """Reads the schema.org Organization a job board embeds in a company page.

    Returns any of `website`, `description`, `employees` and `address`.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict) or node.get("@type") != "Organization":
                continue
            same_as = node.get("sameAs")
            websites = same_as if isinstance(same_as, list) else [same_as]
            website = next(
                (u for u in map(as_http_url, websites) if u and not is_job_board_url(u)),
                None,
            )
            address = node.get("address")
            address_parts = [
                address.get(key)
                for key in ("streetAddress", "addressLocality", "addressRegion", "addressCountry")
            ] if isinstance(address, dict) else []
            employees = node.get("numberOfEmployees")
            profile = {
                "website": website,
                "description": str(node.get("description") or "")[:600].strip(),
                "employees": employees.get("value") if isinstance(employees, dict) else None,
                "address": ", ".join(p for p in address_parts if isinstance(p, str) and p),
            }
            return {key: value for key, value in profile.items() if value}
    return {}


def find_website_link(soup: BeautifulSoup) -> str | None:
    """Fallback: the company's own site as linked from a LinkedIn 'about' page."""
    for anchor in soup.find_all("a", href=True):
        if "about_website" in str(anchor.get("data-tracking-control-name", "")):
            target = parse_qs(urlparse(anchor["href"]).query).get("url", [None])[0]
            if as_http_url(target):
                return target
    return None


def find_emails_in_soup(soup: BeautifulSoup) -> set[str]:
    """Extracts all email addresses found in a page's text."""
    return {
        email
        for email in EMAIL_REGEX.findall(soup.get_text(" "))
        if not email.lower().endswith(IGNORED_EMAIL_SUFFIXES)
    }


def find_careers_link(soup: BeautifulSoup, base_url: str) -> str | None:
    """Returns the first <a> that looks like a careers/jobs page link."""
    for anchor in soup.find_all("a", href=True):
        link = urljoin(base_url, anchor["href"])
        parsed = urlparse(link)
        # Path and text only: a host like careers.example.com matches every link.
        haystack = f"{parsed.path} {anchor.get_text()}".lower()
        if parsed.scheme in ("http", "https") and any(
            keyword in haystack for keyword in CAREERS_KEYWORDS
        ):
            return link
    return None


def normalize_emails(raw) -> list[str]:
    """Accepts a list or comma-separated string of emails."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    return [email.strip() for email in raw if isinstance(email, str) and email.strip()]


def public_url_param(value, name: str) -> str | None:
    """Normalises an optional URL parameter, raising a 400 if it isn't public."""
    url = str(value or "").strip()
    if not url:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url
    if not is_public_http_url(url):
        raise ApiError(f"{name} must be a public http(s) URL")
    return url


def resolve_company_website(
    listed_url: str | None, direct_url: str | None, apply_url: str | None = None
) -> tuple[str | None, dict]:
    """Finds the company's own website and any profile info a job board shows.

    `company_url_direct` is already the real site when a board provides it;
    otherwise `company_url` is either the real site or a job-board profile page
    whose embedded metadata links to it.
    """
    if direct_url and not is_job_board_url(direct_url):
        return direct_url, {}
    if listed_url and not is_job_board_url(listed_url):
        return listed_url, {}

    profile_url = listed_url or direct_url
    soup = scrape_page(profile_url) if profile_url else None
    profile = parse_company_profile(soup) if soup else {}
    website = profile.pop("website", None) or (find_website_link(soup) if soup else None)
    if not website and apply_url:
        # Boards that block scraping: a company-hosted apply link still names the site.
        parsed = urlparse(apply_url)
        labels = (parsed.hostname or "").lower().split(".")
        if not is_job_board_url(apply_url) and not any(n in labels for n in ATS_NAMES):
            website = f"{parsed.scheme}://{parsed.netloc}"
    return website, profile


@app.route("/api/enrich", methods=["POST"])
def enrich_company():
    data = json_body()
    listed_url = public_url_param(data.get("company_url"), "company_url")
    direct_url = public_url_param(data.get("company_url_direct"), "company_url_direct")
    apply_url = public_url_param(data.get("job_url_direct"), "job_url_direct")
    emails = set(normalize_emails(data.get("existing_emails")))

    website, profile = resolve_company_website(listed_url, direct_url, apply_url)
    careers_url = None
    if website and is_public_http_url(website):
        soup = scrape_page(website)
        if soup:
            emails |= find_emails_in_soup(soup)
            careers_url = find_careers_link(soup, website)

        # Missing emails or careers link on the homepage: try the usual pages.
        for path in FALLBACK_PATHS:
            if emails and careers_url:
                break
            extra_soup = scrape_page(urljoin(website, path), timeout=5)
            if extra_soup:
                emails |= find_emails_in_soup(extra_soup)
                careers_url = careers_url or find_careers_link(extra_soup, website)

    return jsonify(
        {
            "company_website": website,
            "careers_url": careers_url,
            "emails": sorted(emails),
            "profile": profile,
        }
    )


@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    if not JOBS_CSV.exists():
        return Response("[]", mimetype="application/json")
    return jobs_response(pd.read_csv(JOBS_CSV))


def parse_scrape_params(data: dict) -> dict:
    """Validates a /api/scrape body into keyword arguments for scrape_jobs."""
    try:
        results_wanted = int(data.get("results_wanted", 50))
        hours_old = int(data.get("hours_old", DEFAULT_HOURS_OLD))
    except (TypeError, ValueError):
        raise ApiError("results_wanted and hours_old must be integers") from None
    if not 1 <= results_wanted <= MAX_RESULTS_WANTED:
        raise ApiError(f"results_wanted must be between 1 and {MAX_RESULTS_WANTED}")
    if hours_old < 1:
        raise ApiError("hours_old must be at least 1")

    # Country-bound boards only know "remote within a country", so a worldwide
    # search uses the remote-only boards.
    worldwide_only = bool(data.get("worldwide_only", False))
    is_remote = bool(data.get("is_remote", True)) or worldwide_only
    country = str(data.get("country_indeed") or "UK")
    return {
        "site_name": (
            REMOTE_BOARD_SITES
            if worldwide_only
            else SCRAPE_SITES + (REMOTE_BOARD_SITES if is_remote else [])
        ),
        "search_term": str(data.get("search_term") or "software engineer"),
        # Blank city/region searches the whole country.
        "location": str(data.get("location") or "").strip() or country,
        "country_indeed": country,
        "is_remote": is_remote,
        "remote_only": is_remote,
        "results_wanted": results_wanted,
        "hours_old": hours_old,
        # LinkedIn lists omit descriptions; the location and experience filters need them.
        "linkedin_fetch_description": True,
    }


@app.route("/api/scrape", methods=["POST"])
def scrape_jobs_api():
    body = request.get_json(silent=True)
    params = parse_scrape_params({} if body is None else json_body())
    if not scrape_lock.acquire(blocking=False):
        raise ApiError("A scrape is already in progress", 409)
    try:
        try:
            jobs = scrape_jobs(**params)
        except ValueError as error:  # e.g. unknown country
            raise ApiError(str(error)) from error
        jobs.to_csv(JOBS_CSV, quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)
    finally:
        scrape_lock.release()
    return jobs_response(jobs)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )

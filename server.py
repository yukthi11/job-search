# pyrefly: ignore [missing-import]
from flask import Flask, jsonify, send_from_directory, Response, request
import pandas as pd
import os
import csv
import json
import re
import requests as http_requests
# pyrefly: ignore [missing-import]
from bs4 import BeautifulSoup
from jobspy import scrape_jobs

app = Flask(__name__, static_folder='static')

APPLIED_JOBS_FILE = 'applied_jobs.json'

def load_applied_jobs():
    if not os.path.exists(APPLIED_JOBS_FILE):
        return []
    try:
        with open(APPLIED_JOBS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def save_applied_jobs(jobs_list):
    with open(APPLIED_JOBS_FILE, 'w') as f:
        json.dump(jobs_list, f, indent=4)

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

def jobs_response(df):
    """Use pandas JSON serializer to safely handle NaN -> null conversion."""
    json_str = df.to_json(orient='records', default_handler=str)
    return Response(json_str, mimetype='application/json')

@app.route('/api/applied', methods=['GET'])
def get_applied_jobs():
    return jsonify(load_applied_jobs())

@app.route('/api/toggle_applied', methods=['POST'])
def toggle_applied():
    try:
        job = request.json
        if not job or 'job_url' not in job:
            return jsonify({"error": "Invalid job data, must contain job_url"}), 400
        
        applied = load_applied_jobs()
        
        exists_idx = -1
        for i, j in enumerate(applied):
            if j.get('job_url') == job.get('job_url'):
                exists_idx = i
                break
                
        if exists_idx >= 0:
            applied.pop(exists_idx)
            status = 'removed'
        else:
            applied.append(job)
            status = 'added'
            
        save_applied_jobs(applied)
        return jsonify({"status": status, "applied_jobs": applied})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

SCRAPER_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
CAREERS_KEYWORDS = ['careers', 'jobs', 'work-with-us', 'work_with_us', 'join-us', 'join_us', 'opportunities', 'vacancies', 'hiring']

def scrape_page(url, timeout=6):
    """Fetch a URL and return its BeautifulSoup object, or None on failure."""
    try:
        resp = http_requests.get(url, headers=SCRAPER_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, 'html.parser')
    except Exception:
        return None

def find_emails_in_soup(soup):
    """Extract all email addresses found in a page's text."""
    return list({e for e in EMAIL_REGEX.findall(soup.get_text()) if not e.endswith(('.png', '.jpg', '.svg', '.gif', '.css', '.js'))})

def find_careers_link(soup, base_url):
    """Scan all <a> tags for a careers/jobs page link."""
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        text = a.get_text().lower()
        if any(kw in href or kw in text for kw in CAREERS_KEYWORDS):
            href_full = a['href']
            if href_full.startswith('http'):
                return href_full
            elif href_full.startswith('/'):
                return base_url.rstrip('/') + href_full
    return None

@app.route('/api/enrich', methods=['POST'])
def enrich_company():
    try:
        data = request.json or {}
        company_url = data.get('company_url', '').strip()
        # Emails already found by JobSpy in the job description
        existing_emails = data.get('existing_emails', [])
        if isinstance(existing_emails, str):
            existing_emails = [e.strip() for e in existing_emails.split(',') if e.strip()]

        if not company_url:
            return jsonify({'careers_url': None, 'emails': existing_emails})

        # Normalize URL
        if not company_url.startswith('http'):
            company_url = 'https://' + company_url

        all_emails = set(existing_emails)
        careers_url = None

        # Step 1: Scrape homepage
        soup = scrape_page(company_url)
        if soup:
            all_emails.update(find_emails_in_soup(soup))
            careers_url = find_careers_link(soup, company_url)

            # Step 2: If no emails yet, try /contact and /about pages
            if not all_emails:
                for path in ['/contact', '/about', '/contact-us', '/about-us']:
                    extra_soup = scrape_page(company_url.rstrip('/') + path, timeout=5)
                    if extra_soup:
                        all_emails.update(find_emails_in_soup(extra_soup))
                        if not careers_url:
                            careers_url = find_careers_link(extra_soup, company_url)

        return jsonify({
            'careers_url': careers_url,
            'emails': sorted(all_emails)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    csv_path = 'remote_jobs.csv'
    if not os.path.exists(csv_path):
        return Response('[]', mimetype='application/json')
    try:
        df = pd.read_csv(csv_path)
        return jobs_response(df)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/scrape', methods=['POST'])
def scrape_jobs_api():
    try:
        data = request.json or {}
        search_term = data.get('search_term', 'software engineer')
        location = data.get('location', 'UK')
        country_indeed = data.get('country_indeed', 'UK')
        is_remote = data.get('is_remote', True)
        results_wanted = int(data.get('results_wanted', 50))

        jobs = scrape_jobs(
            site_name=["indeed", "linkedin", "glassdoor"],
            search_term=search_term,
            location=location,
            country_indeed=country_indeed,
            is_remote=is_remote,
            results_wanted=results_wanted,
            hours_old=24
        )
        
        jobs.to_csv('remote_jobs.csv', quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False)
        return jobs_response(jobs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

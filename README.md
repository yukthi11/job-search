# JobSpy Dashboard 🚀

A sleek, premium, glassmorphic UI Dashboard for discovering, filtering, and managing remote job applications. 

This project provides a modern web frontend on top of a powerful backend scraping engine, allowing you to easily browse jobs from Indeed, LinkedIn, and Glassdoor, track the ones you've applied to, and instantly find company careers pages and contact emails!

## Features ✨
- **Dynamic Search & Filters:** Filter by Job Title, Location, Country, and Remote status.
- **Application Tracking System:** Mark jobs as "Applied" to move them into a dedicated tracking tab.
- **Persistent Storage:** Your applied jobs are safely stored in a local JSON file.
- **Smart Deduplication:** Strict frontend filtering prevents you from seeing duplicate jobs across different platforms.
- **Company Enrichment Layer:** Click "Company Details" on any job to automatically scrape the company's homepage for their direct Careers Page and HR/Contact Emails.

## Installation 🛠️

1. Clone this repository:
```bash
git clone <your-repo-url>
cd JobSpy
```

2. Install the required Python dependencies:
```bash
pip install -r requirements.txt
```

3. Run the Flask server:
```bash
python server.py
```

4. Open your browser and navigate to: `http://localhost:5000`

## Credits & Licensing 📜

This project is open source. 

- **Frontend & Dashboard UI:** Built as a custom Flask/Vanilla JS application.
- **Backend Scraping Engine:** This dashboard runs on top of the incredible **[JobSpy](https://github.com/Bunsan21/JobSpy)** library created by **Cullen Watson**. All scraping logic, API routes, and base data structures utilize the JobSpy framework (MIT License). See `README_original.md` for the original JobSpy documentation.

If you enjoy this dashboard, be sure to star the original JobSpy repository as well!

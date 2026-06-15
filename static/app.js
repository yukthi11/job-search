let allJobs = [];
let appliedJobs = [];
let currentTab = 'search'; // 'search' or 'applied'

const jobsGrid = document.getElementById('jobs-grid');
const appliedGrid = document.getElementById('applied-grid');
const searchInput = document.getElementById('search-input');
const refreshBtn = document.getElementById('refresh-btn');
const statsContainer = document.getElementById('stats-container');

// Tabs
const tabSearch = document.getElementById('tab-search');
const tabApplied = document.getElementById('tab-applied');
const appliedCount = document.getElementById('applied-count');

tabSearch.addEventListener('click', () => {
    currentTab = 'search';
    tabSearch.classList.add('active');
    tabApplied.classList.remove('active');
    jobsGrid.style.display = 'grid';
    appliedGrid.style.display = 'none';
    render();
});

tabApplied.addEventListener('click', () => {
    currentTab = 'applied';
    tabApplied.classList.add('active');
    tabSearch.classList.remove('active');
    appliedGrid.style.display = 'grid';
    jobsGrid.style.display = 'none';
    render();
});

async function fetchJobs() {
    try {
        const [jobsRes, appliedRes] = await Promise.all([
            fetch('/api/jobs'),
            fetch('/api/applied')
        ]);
        
        const jobsData = await jobsRes.json();
        const appliedData = await appliedRes.json();
        
        if (jobsData.error) throw new Error(jobsData.error);
        if (appliedData.error) throw new Error(appliedData.error);
        
        allJobs = jobsData;
        appliedJobs = appliedData;
        render();
    } catch (err) {
        statsContainer.innerHTML = `<p style="color: #ef4444;">Error loading jobs: ${err.message}</p>`;
    }
}

async function scrapeJobs() {
    refreshBtn.classList.add('spinning');
    refreshBtn.disabled = true;
    statsContainer.innerHTML = `<p>Scraping new jobs... This might take a minute.</p>`;
    
    const payload = {
        search_term: document.getElementById('filter-term').value,
        location: document.getElementById('filter-location').value,
        country_indeed: document.getElementById('filter-country').value,
        is_remote: document.getElementById('filter-remote').checked,
        results_wanted: document.getElementById('filter-results').value
    };
    
    try {
        const response = await fetch('/api/scrape', { 
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        
        if (data.error) {
            throw new Error(data.error);
        }
        
        allJobs = data;
        render();
    } catch (err) {
        statsContainer.innerHTML = `<p style="color: #ef4444;">Error scraping jobs: ${err.message}</p>`;
    } finally {
        refreshBtn.classList.remove('spinning');
        refreshBtn.disabled = false;
    }
}

async function toggleApplied(job) {
    try {
        const response = await fetch('/api/toggle_applied', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(job)
        });
        const data = await response.json();
        if(data.error) throw new Error(data.error);
        
        appliedJobs = data.applied_jobs;
        render(); // re-render UI to update grids
    } catch(err) {
        alert("Failed to save applied job: " + err.message);
    }
}

function render() {
    // Update count badge
    appliedCount.textContent = appliedJobs.length;
    
    // Filter out jobs from allJobs that are already in appliedJobs
    const appliedUrls = new Set(appliedJobs.map(j => j.job_url));
    let searchJobs = allJobs.filter(j => !appliedUrls.has(j.job_url));
    
    // Strict Deduplication Layer (removes duplicates from different sites)
    const seenJobs = new Set();
    searchJobs = searchJobs.filter(job => {
        // Create a unique fingerprint for this job (URL, or exact Title + Company match)
        const fingerprint1 = job.job_url;
        const fingerprint2 = `${(job.title||'').toLowerCase().trim()}|${(job.company||'').toLowerCase().trim()}`;
        
        if (seenJobs.has(fingerprint1) || seenJobs.has(fingerprint2)) {
            return false; // It's a duplicate, hide it!
        }
        
        if (fingerprint1) seenJobs.add(fingerprint1);
        seenJobs.add(fingerprint2);
        return true;
    });
    
    // Apply search input filter
    const term = searchInput.value.toLowerCase();
    
    const filterFn = job => {
        return (job.title && job.title.toLowerCase().includes(term)) ||
               (job.company && job.company.toLowerCase().includes(term)) ||
               (job.site && job.site.toLowerCase().includes(term));
    };
    
    const displaySearchJobs = searchJobs.filter(filterFn);
    const displayAppliedJobs = appliedJobs.filter(filterFn);
    
    if (currentTab === 'search') {
        statsContainer.innerHTML = `<p>Found <strong>${displaySearchJobs.length}</strong> new jobs</p>`;
    } else {
        statsContainer.innerHTML = `<p>You have applied to <strong>${displayAppliedJobs.length}</strong> jobs</p>`;
    }
    
    renderGrid(jobsGrid, displaySearchJobs, false);
    renderGrid(appliedGrid, displayAppliedJobs, true);
}

function renderGrid(container, jobs, isAppliedGrid) {
    if (jobs.length === 0) {
        container.innerHTML = `<p style="color: var(--text-secondary);">No jobs found in this section.</p>`;
        return;
    }
    
    container.innerHTML = '';
    
    jobs.forEach((job, index) => {
        const card = document.createElement('div');
        card.className = 'job-card';
        card.style.animationDelay = `${index * 0.05}s`;
        
        const locationStr = [job.city, job.state, job.country].filter(Boolean).join(', ') || 'Remote';
        
        card.innerHTML = `
            <div class="card-header-row">
                <div class="site-badge">${job.site || 'Unknown'}</div>
                <label class="applied-toggle">
                    <input type="checkbox" ${isAppliedGrid ? 'checked' : ''}>
                    Applied
                </label>
            </div>
            <h3 class="job-title">${job.title || 'Untitled Job'}</h3>
            <div class="company">${job.company || 'Unknown Company'}</div>
            <div class="job-details">
                <div class="detail-item">
                    📍 ${locationStr} ${job.is_remote ? '(Remote)' : ''}
                </div>
                ${job.job_type ? `<div class="detail-item">💼 ${job.job_type}</div>` : ''}
                ${job.date_posted ? `<div class="detail-item">🕒 Posted: ${job.date_posted}</div>` : ''}
            </div>
            <div style="display:flex; gap: 0.5rem; margin-top: 1.5rem;">
                ${job.job_url ? `<a target="_blank" class="apply-link" style="flex:1;" href="${job.job_url}">View & Apply</a>` : ''}
                <button class="apply-link enrich-btn" style="flex:1; border:none; cursor:pointer;">🔍 Company Details</button>
            </div>
        `;
        
        // Add event listener to the checkbox
        const checkbox = card.querySelector('input[type="checkbox"]');
        checkbox.addEventListener('change', () => toggleApplied(job));

        // Add event listener to the enrich button
        const enrichBtn = card.querySelector('.enrich-btn');
        enrichBtn.addEventListener('click', () => enrichCompany(job));
        
        container.appendChild(card);
    });
}

searchInput.addEventListener('input', render);
refreshBtn.addEventListener('click', scrapeJobs);

// Modal
const modalOverlay = document.getElementById('modal-overlay');
const modalClose = document.getElementById('modal-close');
const modalCompanyName = document.getElementById('modal-company-name');
const modalBody = document.getElementById('modal-body');

function openModal(company) {
    modalCompanyName.textContent = company;
    modalBody.innerHTML = `<div class="modal-loading"><div class="spinner"></div><p>Searching company website...</p></div>`;
    modalOverlay.style.display = 'flex';
}

function closeModal() {
    modalOverlay.style.display = 'none';
}

function renderModal(data) {
    const careersHtml = data.careers_url
        ? `<a href="${data.careers_url}" target="_blank">${data.careers_url}</a>`
        : `<span class="modal-not-found">No careers page found automatically</span>`;

    const emailsHtml = (data.emails && data.emails.length > 0)
        ? data.emails.map(e => `<span class="email-pill" onclick="copyEmail('${e}', this)" title="Click to copy">${e}</span>`).join('')
        : `<span class="modal-not-found">No emails found on their website</span>`;

    modalBody.innerHTML = `
        <div class="modal-row">
            <label>🌐 Company Website</label>
            ${data.company_url ? `<a href="${data.company_url}" target="_blank">${data.company_url}</a>` : '<span class="modal-not-found">Not available</span>'}
        </div>
        <div class="modal-row">
            <label>💼 Careers Page</label>
            ${careersHtml}
        </div>
        <div class="modal-row">
            <label>✉️ Contact Emails</label>
            <p>${emailsHtml}</p>
            <p style="font-size:0.75rem; color: var(--text-secondary); margin-top: 0.5rem;">Click an email to copy it to clipboard</p>
        </div>
    `;
}

function copyEmail(email, el) {
    navigator.clipboard.writeText(email).then(() => {
        el.textContent = '✓ Copied!';
        setTimeout(() => { el.textContent = email; }, 2000);
    });
}

async function enrichCompany(job) {
    openModal(job.company || 'Company');

    try {
        const response = await fetch('/api/enrich', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                company_url: job.company_url || '',
                existing_emails: job.emails || []
            })
        });
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        data.company_url = job.company_url;
        renderModal(data);
    } catch (err) {
        modalBody.innerHTML = `<p style="color: #ef4444;">Error: ${err.message}</p>`;
    }
}

modalClose.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });

// Initial load
fetchJobs();

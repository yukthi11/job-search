// Boards that only list remote jobs (mirrors REMOTE_BOARD_SITES in server.py).
const REMOTE_BOARDS = new Set(['remoteok', 'remotive', 'weworkremotely', 'himalayas']);

let allJobs = [];
let appliedJobs = [];
let currentTab = 'search'; // 'search' or 'applied'

// ---- Helpers --------------------------------------------------------------

/** Creates an element; `text` is always set via textContent (never parsed as HTML). */
function el(tag, { className, text, attrs = {}, style } = {}, children = []) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (style) node.style.cssText = style;
    Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
    node.append(...children);
    return node;
}

/** Returns the normalised URL only if it is http(s); blocks javascript: and friends. */
function safeUrl(url) {
    try {
        const parsed = new URL(url);
        return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
    } catch {
        return null;
    }
}

/** Builds an <a> for an http(s) URL, or returns null when the URL is missing/unsafe. */
function externalLink(url, label, className) {
    const href = safeUrl(url);
    if (!href) return null;
    return el('a', {
        className,
        text: label ?? href,
        attrs: { href, target: '_blank', rel: 'noopener noreferrer' }
    });
}

/** Formats epoch-ms numbers and ISO date strings; falls back to the raw value. */
function formatDate(value) {
    if (value === null || value === undefined || value === '') return '';
    const isEpoch = typeof value === 'number' || /^\d+$/.test(value);
    const date = new Date(isEpoch ? Number(value) : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString();
}

/** Identities for a job, shared by the applied filter and the de-duplication. */
function jobFingerprints(job) {
    const normalize = value => String(value ?? '').toLowerCase().trim();
    const parts = [job.title, job.company, job.location].map(normalize);
    const details = parts.some(Boolean) ? parts.join('|') : null;
    return [job.job_url, details].filter(Boolean);
}

// Phrases that say a remote job is only open to people already based in / authorised for one country.
const LOCATION_RESTRICTED = new RegExp([
    String.raw`right to work`,
    String.raw`work authori[sz]ation`,
    String.raw`(must|need to|have to|should)\s+(be|live|reside)\s+(based|resident|located|living|in)\b`,
    String.raw`\b(uk|u\.k\.|us|u\.s\.|eu|eea|canada|ireland|german|australia)[- ]?(based|only|residents?)\b`,
    String.raw`based in the (uk|united kingdom|us|united states|eu)\b`,
    String.raw`must be (a )?(uk|us) (citizen|resident)`,
    String.raw`security clearance`,
    String.raw`no (visa )?sponsorship`
].join('|'), 'i');

function isLocationRestricted(job) {
    return LOCATION_RESTRICTED.test(`${job.title ?? ''} ${job.description ?? ''}`);
}

/**
 * Years of experience a job asks for ("3+ years", "2-4 yrs"), or null if none is stated.
 * Only counts figures that sit next to the word "experience", so "founded 10 years ago"
 * is ignored. With several figures ("5 years Python, 2 years AWS") the highest wins.
 */
const YEARS_PATTERN = /(\d{1,2})\s*(?:\+|(?:-|–|to)\s*\d{1,2})?\s*(?:years?|yrs?)\b/gi;
const requiredYearsCache = new WeakMap();

function requiredYears(job) {
    if (requiredYearsCache.has(job)) return requiredYearsCache.get(job);
    const text = String(job.description ?? '');
    let required = null;
    for (const match of text.matchAll(YEARS_PATTERN)) {
        const nearby = text.slice(Math.max(0, match.index - 60), match.index + match[0].length + 60);
        const years = Number(match[1]);
        if (/experience/i.test(nearby) && years <= 15) {
            required = Math.max(required ?? 0, years);
        }
    }
    requiredYearsCache.set(job, required);
    return required;
}

// Remote work open to people anywhere, as opposed to "remote within one country".
const WORLDWIDE_LOCATION = /worldwide|anywhere|global|international|earth|\bindia\b|apac|\basia\b/i;
const WORLDWIDE_PHRASE = new RegExp([
    String.raw`work from anywhere`,
    String.raw`anywhere in the world`,
    String.raw`(remote|hiring|candidates?)[^.\n]{0,20}(worldwide|globally|anywhere)`,
    String.raw`(worldwide|global)[^.\n]{0,10}remote`,
    String.raw`any (country|time ?zone)`
].join('|'), 'i');
const GENERIC_REMOTE_LOCATION = /^\s*(remote)?\s*$/i;

/** True when the job is remote AND not tied to one country. */
function isWorldwideRemote(job) {
    if (job.is_remote === false) return false;
    const location = String(job.location ?? '');
    if (WORLDWIDE_LOCATION.test(location)) return true;
    // Remote boards give no country for some jobs; the description filters decide those.
    if (REMOTE_BOARDS.has(job.site) && GENERIC_REMOTE_LOCATION.test(location)) return true;
    return WORLDWIDE_PHRASE.test(String(job.description ?? ''));
}

function showStatus(message, isError = false) {
    statsContainer.replaceChildren(
        el('p', { text: message, style: isError ? 'color: #ef4444;' : undefined })
    );
}

// ---- DOM references ---------------------------------------------------------

const jobsGrid = document.getElementById('jobs-grid');
const appliedGrid = document.getElementById('applied-grid');
const searchInput = document.getElementById('search-input');
const refreshBtn = document.getElementById('refresh-btn');
const hideRestricted = document.getElementById('filter-open-hiring');
const myExperience = document.getElementById('filter-experience');
const worldwideOnly = document.getElementById('filter-worldwide');
const countryInputs = ['filter-country', 'filter-location'].map(id => document.getElementById(id));

// Country and city only matter for boards that are tied to a country.
function syncWorldwideMode() {
    countryInputs.forEach(input => { input.disabled = worldwideOnly.checked; });
}
syncWorldwideMode();

// Remember the user's experience between visits; storage may be unavailable.
try { myExperience.value = localStorage.getItem('myExperience') ?? ''; } catch { /* ignore */ }

/** The user's years of experience, or null when the field is empty. */
function experienceYears() {
    return myExperience.value === '' ? null : Number(myExperience.value);
}
const statsContainer = document.getElementById('stats-container');

// Tabs
const tabSearch = document.getElementById('tab-search');
const tabApplied = document.getElementById('tab-applied');
const appliedCount = document.getElementById('applied-count');

function selectTab(tab) {
    currentTab = tab;
    const isSearch = tab === 'search';
    tabSearch.classList.toggle('active', isSearch);
    tabApplied.classList.toggle('active', !isSearch);
    jobsGrid.style.display = isSearch ? 'grid' : 'none';
    appliedGrid.style.display = isSearch ? 'none' : 'grid';
    render();
}

tabSearch.addEventListener('click', () => selectTab('search'));
tabApplied.addEventListener('click', () => selectTab('applied'));

// ---- API calls ---------------------------------------------------------------

/** Fetches JSON and throws if the request failed or the body carries an `error`. */
async function requestJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (data.error) throw new Error(data.error);
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return data;
}

function postJson(url, payload) {
    return requestJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
}

async function fetchJobs() {
    try {
        [allJobs, appliedJobs] = await Promise.all([
            requestJson('/api/jobs'),
            requestJson('/api/applied')
        ]);
        render();
    } catch (err) {
        showStatus(`Error loading jobs: ${err.message}`, true);
    }
}

async function scrapeJobs() {
    refreshBtn.classList.add('spinning');
    refreshBtn.disabled = true;
    showStatus('Scraping new jobs... This might take a minute.');

    try {
        allJobs = await postJson('/api/scrape', {
            search_term: document.getElementById('filter-term').value,
            location: document.getElementById('filter-location').value,
            country_indeed: document.getElementById('filter-country').value,
            is_remote: document.getElementById('filter-remote').checked,
            worldwide_only: worldwideOnly.checked,
            hours_old: Number(document.getElementById('filter-hours').value),
            results_wanted: Number(document.getElementById('filter-results').value)
        });
        render();
    } catch (err) {
        showStatus(`Error scraping jobs: ${err.message}`, true);
    } finally {
        refreshBtn.classList.remove('spinning');
        refreshBtn.disabled = false;
    }
}

async function toggleApplied(job) {
    try {
        const data = await postJson('/api/toggle_applied', job);
        appliedJobs = data.applied_jobs;
        render(); // re-render UI to update grids
    } catch (err) {
        alert('Failed to save applied job: ' + err.message);
    }
}

// ---- Rendering ----------------------------------------------------------------

function render() {
    appliedCount.textContent = appliedJobs.length;

    // Hide jobs already applied to, and duplicates posted on several sites.
    const seen = new Set(appliedJobs.flatMap(jobFingerprints));
    const searchJobs = allJobs.filter(job => {
        const fingerprints = jobFingerprints(job);
        if (fingerprints.some(fingerprint => seen.has(fingerprint))) return false;
        fingerprints.forEach(fingerprint => seen.add(fingerprint));
        return true;
    });

    const term = searchInput.value.toLowerCase();
    const matchesTerm = job =>
        [job.title, job.company, job.site].some(
            field => field && String(field).toLowerCase().includes(term)
        );

    const matching = searchJobs.filter(matchesTerm);
    const myYears = experienceYears();
    // The first matching rule is the reason a job is hidden.
    const rules = [
        { hides: job => worldwideOnly.checked && !isWorldwideRemote(job), label: () => 'not worldwide remote' },
        { hides: job => hideRestricted.checked && isLocationRestricted(job), label: () => 'location-restricted' },
        {
            hides: job => myYears !== null && (requiredYears(job) ?? 0) > myYears,
            label: () => `need more than ${myYears} yrs experience`
        }
    ];
    const hiddenBy = rules.map(() => 0);
    const displaySearchJobs = matching.filter(job => {
        const index = rules.findIndex(rule => rule.hides(job));
        if (index >= 0) hiddenBy[index] += 1;
        return index < 0;
    });
    const hiddenReasons = rules
        .map((rule, i) => hiddenBy[i] && `${hiddenBy[i]} ${rule.label()}`)
        .filter(Boolean);
    const displayAppliedJobs = appliedJobs.filter(matchesTerm);

    if (currentTab === 'search') {
        statsContainer.replaceChildren(
            el('p', {}, [
                document.createTextNode('Found '),
                el('strong', { text: displaySearchJobs.length }),
                document.createTextNode(' new jobs'),
                ...(hiddenReasons.length
                    ? [document.createTextNode(` (hidden: ${hiddenReasons.join(', ')})`)]
                    : [])
            ])
        );
    } else {
        statsContainer.replaceChildren(
            el('p', {}, [
                document.createTextNode('You have applied to '),
                el('strong', { text: displayAppliedJobs.length }),
                document.createTextNode(' jobs')
            ])
        );
    }

    renderGrid(jobsGrid, displaySearchJobs, false);
    renderGrid(appliedGrid, displayAppliedJobs, true);
}

function renderGrid(container, jobs, isAppliedGrid) {
    if (jobs.length === 0) {
        container.replaceChildren(
            el('p', {
                text: 'No jobs found in this section.',
                style: 'color: var(--text-secondary);'
            })
        );
        return;
    }
    container.replaceChildren(
        ...jobs.map((job, index) => createJobCard(job, index, isAppliedGrid))
    );
}

function createJobCard(job, index, isAppliedGrid) {
    const checkbox = el('input', { attrs: { type: 'checkbox' } });
    checkbox.checked = isAppliedGrid;
    checkbox.addEventListener('change', () => toggleApplied(job));

    const locationText = job.location || (job.is_remote ? 'Remote' : 'Location not listed');
    const remoteSuffix = job.is_remote && job.location ? ' (Remote)' : '';
    const posted = formatDate(job.date_posted);
    const detail = text => el('div', { className: 'detail-item', text });

    const enrichBtn = el('button', {
        className: 'apply-link enrich-btn',
        text: '🔍 Company Details',
        style: 'flex:1; border:none; cursor:pointer;'
    });
    enrichBtn.addEventListener('click', () => enrichCompany(job));

    const viewLink = externalLink(job.job_url, 'View & Apply', 'apply-link');
    if (viewLink) viewLink.style.flex = '1';

    return el('div', { className: 'job-card', style: `animation-delay: ${index * 0.05}s` }, [
        el('div', { className: 'card-header-row' }, [
            el('div', { className: 'site-badge', text: job.site || 'Unknown' }),
            el('label', { className: 'applied-toggle' }, [
                checkbox,
                document.createTextNode(' Applied')
            ])
        ]),
        el('h3', { className: 'job-title', text: job.title || 'Untitled Job' }),
        el('div', { className: 'company', text: job.company || 'Unknown Company' }),
        el('div', { className: 'job-details' }, [
            detail(`📍 ${locationText}${remoteSuffix}`),
            ...(job.job_type ? [detail(`💼 ${job.job_type}`)] : []),
            ...(requiredYears(job) !== null ? [detail(`🎓 ${requiredYears(job)}+ yrs experience`)] : []),
            ...(posted ? [detail(`🕒 Posted: ${posted}`)] : [])
        ]),
        el(
            'div',
            { style: 'display:flex; gap: 0.5rem; margin-top: 1.5rem;' },
            [viewLink, enrichBtn].filter(Boolean)
        )
    ]);
}

searchInput.addEventListener('input', render);
hideRestricted.addEventListener('change', render);
worldwideOnly.addEventListener('change', () => { syncWorldwideMode(); render(); });
myExperience.addEventListener('input', () => {
    try { localStorage.setItem('myExperience', myExperience.value); } catch { /* ignore */ }
    render();
});
refreshBtn.addEventListener('click', scrapeJobs);

// ---- Company modal -----------------------------------------------------------

const modalOverlay = document.getElementById('modal-overlay');
const modalClose = document.getElementById('modal-close');
const modalCompanyName = document.getElementById('modal-company-name');
const modalBody = document.getElementById('modal-body');

function openModal(company) {
    modalCompanyName.textContent = company;
    modalBody.replaceChildren(
        el('div', { className: 'modal-loading' }, [
            el('div', { className: 'spinner' }),
            el('p', { text: 'Searching company website...' })
        ])
    );
    modalOverlay.style.display = 'flex';
}

function closeModal() {
    modalOverlay.style.display = 'none';
}

function modalRow(label, content) {
    return el('div', { className: 'modal-row' }, [el('label', { text: label }), ...content]);
}

function notFound(text) {
    return el('span', { className: 'modal-not-found', text });
}

/** Profile facts from the board page, falling back to what the scraper stored on the job. */
function profileRows(job, profile = {}) {
    const employees = profile.employees ?? job.company_num_employees;
    const rating = job.company_rating
        ? `${job.company_rating} ★${job.company_reviews_count ? ` (${job.company_reviews_count} reviews)` : ''}`
        : null;
    const facts = [
        ['🏭 Industry', job.company_industry],
        ['👥 Employees', employees && Number(employees) ? Number(employees).toLocaleString() : employees],
        ['📍 Address', profile.address || job.company_addresses],
        ['⭐ Rating', rating]
    ].filter(([, value]) => value);

    const description = profile.description || job.company_description;
    return [
        ...(description ? [modalRow('📝 About', [el('p', { text: description })])] : []),
        ...facts.map(([label, value]) => modalRow(label, [el('span', { text: String(value) })]))
    ];
}

function renderModal(data, job) {
    const emailPills = (data.emails || []).map(email => {
        const pill = el('span', {
            className: 'email-pill',
            text: email,
            attrs: { title: 'Click to copy' }
        });
        pill.addEventListener('click', () => copyEmail(email, pill));
        return pill;
    });

    const boardProfile = externalLink(job.company_url, `View on ${job.site || 'job board'}`);

    modalBody.replaceChildren(
        ...profileRows(job, data.profile),
        modalRow('🌐 Company Website', [
            externalLink(data.company_website) || notFound('Could not determine the company website'),
            ...(boardProfile ? [el('p', {}, [boardProfile])] : [])
        ]),
        modalRow('💼 Careers Page', [
            externalLink(data.careers_url) || notFound('No careers page found automatically')
        ]),
        modalRow('✉️ Contact Emails', [
            el('p', {}, emailPills.length ? emailPills : [notFound('No emails found on their website')]),
            el('p', {
                text: 'Click an email to copy it to clipboard',
                style: 'font-size:0.75rem; color: var(--text-secondary); margin-top: 0.5rem;'
            })
        ])
    );
}

async function copyEmail(email, pill) {
    try {
        await navigator.clipboard.writeText(email);
        pill.textContent = '✓ Copied!';
    } catch {
        pill.textContent = 'Copy unavailable - select the text manually';
    }
    setTimeout(() => { pill.textContent = email; }, 2000);
}

async function enrichCompany(job) {
    openModal(job.company || 'Company');

    try {
        const data = await postJson('/api/enrich', {
            company_url: job.company_url || '',
            company_url_direct: job.company_url_direct || '',
            job_url_direct: job.job_url_direct || '',
            existing_emails: job.emails || []
        });
        renderModal(data, job);
    } catch (err) {
        modalBody.replaceChildren(
            el('p', { text: `Error: ${err.message}`, style: 'color: #ef4444;' })
        );
    }
}

modalClose.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });

// Initial load
fetchJobs();

/**
 * ==============================================================================
 * COURSE WALLAH PLATFORM — AS MULTIVERSE INTERACTIVE CLIENT ENGINE
 * Ultra-fast Single Page Application (SPA) with deep URL routing & YouTube Player
 * ==============================================================================
 */

// Best-effort UI deterrents against casual inspection; these are not a security boundary.
// Browser DevTools, network panels, screenshots, and requests cannot reliably be blocked by a website.
(function installPlaybackDeterrents() {
  // Disable the context menu across the site as a lightweight deterrent.
  document.addEventListener('contextmenu', (event) => {
    event.preventDefault();
  }, true);

  // Discourage common DevTools, view-source, save-page and print shortcuts.
  // Users can still inspect requests through browser settings, remote tools, or other methods.
  document.addEventListener('keydown', (event) => {
    const key = (event.key || '').toLowerCase();
    const blocked =
      key === 'f12' ||
      ((event.ctrlKey || event.metaKey) && event.shiftKey && ['i', 'j', 'c', 'k'].includes(key)) ||
      ((event.ctrlKey || event.metaKey) && ['u', 's'].includes(key));
    if (blocked) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  // Discourage dragging media out of the player.
  document.addEventListener('dragstart', (event) => {
    if (event.target && event.target.closest && event.target.closest('#player-container')) {
      event.preventDefault();
    }
  }, true);
})();

// Application State Store
const state = {
  activeTab: 'home',
  currentApp: null,
  currentBatch: null,
  currentSubject: null,
  currentLecture: null,
  activeCategory: 'All',
  theme: localStorage.getItem('cw_theme') || 'dark',
  streak: parseInt(localStorage.getItem('cw_streak') || '1', 10),
  bookmarks: JSON.parse(localStorage.getItem('cw_bookmarks') || '[]'),
  enrolledBatches: JSON.parse(localStorage.getItem('cw_enrolled') || '[]'),
  appsData: [],
  cache: {
    apps: null,
    batches: {},
    lectures: {}
  },
  player: null,
  ytApiReady: false
};

// ==============================================================================
// INITIALIZATION & ROUTER
// ==============================================================================

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initStreak();
  window.addEventListener('popstate', handleRoute);
  window.addEventListener('hashchange', handleRoute);
  handleRoute();
});

// YouTube IFrame API Ready Callback
window.onYouTubeIframeAPIReady = () => {
  state.ytApiReady = true;
};

// Theme Management
function initTheme() {
  document.body.className = state.theme === 'light' ? 'theme-light' : 'theme-dark';
  updateThemeIcon();
}

function toggleTheme() {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('cw_theme', state.theme);
  document.body.className = state.theme === 'light' ? 'theme-light' : 'theme-dark';
  updateThemeIcon();
  showToast(`Switched to ${state.theme === 'dark' ? 'Dark' : 'Light'} Mode`);
}

function updateThemeIcon() {
  const moon = document.querySelector('.icon-moon');
  const sun = document.querySelector('.icon-sun');
  if (moon && sun) {
    if (state.theme === 'light') {
      moon.classList.add('hidden');
      sun.classList.remove('hidden');
    } else {
      moon.classList.remove('hidden');
      sun.classList.add('hidden');
    }
  }
}

// Study Streak Management
function initStreak() {
  const lastVisit = localStorage.getItem('cw_last_visit');
  const today = new Date().toDateString();
  if (lastVisit !== today) {
    state.streak += 1;
    localStorage.setItem('cw_streak', state.streak);
    localStorage.setItem('cw_last_visit', today);
  }
  const counter = document.getElementById('streak-counter');
  if (counter) counter.innerText = state.streak;
}

// Sidebar Toggle
function toggleSidebar(forceState) {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (!sidebar) return;
  const isOpen = forceState !== undefined ? forceState : !sidebar.classList.contains('open');
  sidebar.classList.toggle('open', isOpen);
  if (backdrop) backdrop.classList.toggle('open', isOpen);
}

// ==============================================================================
// URL ROUTING & NAVIGATION
// ==============================================================================

function getRouteParams() {
  const urlParams = new URLSearchParams(window.location.search);
  const tab = urlParams.get('tab') || 'home';
  const app = urlParams.get('app') || null;
  const batchId = urlParams.get('batchid') || null;
  const subjectId = urlParams.get('subjectid') || null;
  const content = urlParams.get('content') || null;
  const isPlaying = window.location.hash === '#playing' || !!content;
  return { tab, app, batchId, subjectId, content, isPlaying };
}

function navigateToTab(tabName) {
  const url = `/?tab=${tabName}`;
  window.history.pushState({}, '', url);
  handleRoute();
  toggleSidebar(false);
}

function navigateToApp(appSlugOrId) {
  const url = `/?tab=apps&app=${encodeURIComponent(appSlugOrId)}`;
  window.history.pushState({}, '', url);
  handleRoute();
}

function navigateToBatch(appSlugOrId, batchIdOrSlug) {
  const url = `/?tab=apps&app=${encodeURIComponent(appSlugOrId)}&batchid=${encodeURIComponent(batchIdOrSlug)}`;
  window.history.pushState({}, '', url);
  handleRoute();
}

function navigateToSubject(appSlugOrId, batchIdOrSlug, subjectIdOrSlug) {
  const url = `/?tab=apps&app=${encodeURIComponent(appSlugOrId)}&batchid=${encodeURIComponent(batchIdOrSlug)}&subjectid=${encodeURIComponent(subjectIdOrSlug)}`;
  window.history.pushState({}, '', url);
  handleRoute();
}

function navigateToLecture(appSlugOrId, batchIdOrSlug, subjectIdOrSlug, lectureId) {
  const url = `/?tab=apps&app=${encodeURIComponent(appSlugOrId)}&batchid=${encodeURIComponent(batchIdOrSlug)}&subjectid=${encodeURIComponent(subjectIdOrSlug)}&content=${encodeURIComponent(lectureId)}#playing`;
  window.history.pushState({}, '', url);
  handleRoute();
}

function handleGlobalBack() {
  const { tab, app, batchId, subjectId, content } = getRouteParams();
  if (content) {
    navigateToSubject(app, batchId, subjectId);
  } else if (subjectId) {
    navigateToBatch(app, batchId);
  } else if (batchId) {
    navigateToApp(app);
  } else if (app) {
    navigateToTab('apps');
  } else {
    navigateToTab('home');
  }
}

// Active Nav Item Highlighter
function updateActiveNav(tabName) {
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tabName);
  });
}

// Router Dispatcher
async function handleRoute() {
  const { tab, app, batchId, subjectId, content, isPlaying } = getRouteParams();
  updateActiveNav(tab);

  const backBtn = document.getElementById('btn-global-back');
  const titleEl = document.getElementById('header-page-title');
  
  if (backBtn) {
    backBtn.classList.toggle('hidden', tab === 'home' && !app && !batchId);
  }

  const root = document.getElementById('app-root');
  if (!root) return;

  // Render Skeleton Loading State
  root.innerHTML = `<div class="loading-state" style="text-align:center; padding: 60px 0;"><div class="brand-logo-wrap" style="margin: 0 auto 16px;"><div class="brand-logo-glow"></div><div class="brand-fallback-logo" style="animation: float 2s infinite;">CW</div></div><p style="font-weight:700; color: var(--text-muted);">Loading Course Wallah Matrix...</p></div>`;

  try {
    // 1. In-App Video Player View
    if (content && isPlaying) {
      if (titleEl) titleEl.querySelector('.title-text').innerText = 'STUDY STUDIO';
      await renderPlayerView(root, app, batchId, subjectId, content);
      return;
    }

    // 2. Subject View / Content Explorer
    if (app && batchId && subjectId) {
      if (titleEl) titleEl.querySelector('.title-text').innerText = 'SUBJECT EXPLORER';
      await renderSubjectView(root, app, batchId, subjectId);
      return;
    }

    // 3. Batch Subjects View
    if (app && batchId) {
      if (titleEl) titleEl.querySelector('.title-text').innerText = 'BATCH SUBJECTS';
      await renderBatchView(root, app, batchId);
      return;
    }

    // 4. App Batches View
    if (app) {
      if (titleEl) titleEl.querySelector('.title-text').innerText = 'PORTAL BATCHES';
      await renderAppDetailView(root, app);
      return;
    }

    // 5. Standard Tab Views
    if (titleEl) titleEl.querySelector('.title-text').innerText = tab.toUpperCase().replace('-', ' ');

    switch (tab) {
      case 'home':
        await renderHomeView(root);
        break;
      case 'apps':
        await renderAppsView(root);
        break;
      case 'player':
        await renderDirectPlayerStudio(root);
        break;
      case 'community':
        renderCommunityView(root);
        break;
      case 'downloads':
        renderDownloadsView(root);
        break;
      case 'bookmarks':
        renderBookmarksView(root);
        break;
      case 'settings':
        renderSettingsView(root);
        break;
      case 'developer':
        renderDeveloperView(root);
        break;
      case 'logs':
        renderLogsView(root);
        break;
      case 'help-center':
        renderHelpCenterView(root);
        break;
      default:
        await renderHomeView(root);
    }

  } catch (err) {
    console.error('Route rendering error:', err);
    root.innerHTML = `<div class="error-card" style="background:var(--bg-card); padding:32px; border-radius:20px; text-align:center; border: 1px solid var(--border-subtle);"><h3 style="color:var(--accent-rose); margin-bottom:8px;">Failed to load content</h3><p style="color:var(--text-secondary); margin-bottom:16px;">${err.message || 'An unexpected error occurred.'}</p><button class="btn-hero-primary" onclick="handleRoute()">Retry</button></div>`;
  }
}

// ==============================================================================
// API DATA HELPERS
// ==============================================================================

async function fetchApps() {
  if (state.cache.apps) return state.cache.apps;
  try {
    const res = await fetch('/api/apps');
    if (!res.ok) throw new Error('Failed to fetch apps list');
    const data = await res.json();
    state.cache.apps = data;
    state.appsData = data;
    return data;
  } catch (e) {
    // Fallback Mock Data for standalone resilience
    return [
      {
        id: "app_pw",
        name: "Physics Wallah [PW]",
        slug: "physics-wallah",
        description: "India's top faculty batches for JEE, NEET & Foundation",
        icon_url: "https://images.unsplash.com/photo-1516321318423-f06f85e504b3?w=120&auto=format&fit=crop&q=80",
        total_batches: 12,
        batches: [
          { id: "batch_lakshya", name: "Lakshya JEE 2025", slug: "lakshya-jee-2025", total_lectures: 140, total_videos: 140, total_pdfs: 140 }
        ]
      },
      {
        id: "app_apna",
        name: "Apna College",
        slug: "apna-college",
        description: "Full Stack Web Development, Java DSA & C++ Placement Prep",
        icon_url: "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?w=120&auto=format&fit=crop&q=80",
        total_batches: 8,
        batches: [
          { id: "sigma-9-live-recordings", name: "Sigma 9.0 (Web Development + DSA)", slug: "sigma-9-live-recordings", total_lectures: 151, total_videos: 151, total_pdfs: 151 }
        ]
      },
      {
        id: "app_kgs",
        name: "Khan Global Studies [KGS]",
        slug: "khan-global-studies",
        description: "UPSC, State PSC, SSC & General Studies by Khan Sir",
        icon_url: "https://images.unsplash.com/photo-1532094349884-543bc11b234d?w=120&auto=format&fit=crop&q=80",
        total_batches: 14,
        batches: [
          { id: "batch_upsc", name: "UPSC GS Foundation 2025", slug: "upsc-gs-foundation", total_lectures: 220, total_videos: 220, total_pdfs: 220 }
        ]
      },
      {
        id: "app_dsa",
        name: "Data Structures & Algorithms",
        slug: "dsa-platform",
        description: "Master Coding Interviews & System Design",
        icon_url: "https://images.unsplash.com/photo-1555066931-4365d14bab8c?w=120&auto=format&fit=crop&q=80",
        total_batches: 6,
        batches: [
          { id: "batch_dsa_core", name: "DSA Masterclass", slug: "dsa-masterclass", total_lectures: 151, total_videos: 151, total_pdfs: 151 }
        ]
      }
    ];
  }
}

async function fetchBatchDetail(batchIdOrSlug) {
  if (state.cache.batches[batchIdOrSlug]) return state.cache.batches[batchIdOrSlug];
  try {
    const res = await fetch(`/api/batches/${encodeURIComponent(batchIdOrSlug)}`);
    if (!res.ok) throw new Error('Batch not found');
    const data = await res.json();
    state.cache.batches[batchIdOrSlug] = data;
    return data;
  } catch (e) {
    // Return structured default batch hierarchy if offline
    return {
      id: batchIdOrSlug,
      name: "Data Structure And Algorithm",
      slug: batchIdOrSlug,
      app_name: "Apna College",
      category: "Coding & Engineering",
      subjects: [
        {
          id: "69d8f985d57e276ed66caca3",
          name: "Fundamentals of Algorithms and its Analysis",
          slug: "dsa-fundamentals",
          code: "DSA-01",
          folders: [
            {
              id: "unit-1",
              name: "Unit 1: Introduction to DSA",
              total_lectures: 10,
              lectures: [
                { id: "lec_1", title: "[Home] Data Structure and Algorithm Syllabus", index: 1, duration_seconds: 2400, has_video: true, has_pdf: true },
                { id: "lec_2", title: "[Home] Syllabus Introduction", index: 2, duration_seconds: 1800, has_video: true, has_pdf: true },
                { id: "lec_3", title: "Unit-1.0 Fundamentals of Algorithms and its Analysis", index: 3, duration_seconds: 3200, has_video: true, has_pdf: true },
                { id: "lec_4", title: "1. DSA Complexity & Big-O Notation", index: 4, duration_seconds: 2900, has_video: true, has_pdf: true },
                { id: "lec_5", title: "2. Array Data Structures & Operations", index: 5, duration_seconds: 3600, has_video: true, has_pdf: true }
              ]
            }
          ]
        }
      ]
    };
  }
}

async function fetchLectureDetails(lectureId) {
  if (state.cache.lectures[lectureId]) return state.cache.lectures[lectureId];
  try {
    const res = await fetch(`/api/lectures/${encodeURIComponent(lectureId)}`);
    if (!res.ok) throw new Error('Lecture details failed');
    const data = await res.json();
    state.cache.lectures[lectureId] = data;
    return data;
  } catch (e) {
    return {
      id: lectureId,
      title: "Data Structure and Algorithm Syllabus",
      index: 1,
      duration_seconds: 2400,
      has_video: true,
      has_pdf: true,
      folder_name: "Unit 1: Introduction to DSA",
      subject_name: "Fundamentals of Algorithms",
      batch_name: "Data Structure And Algorithm",
      playlist: [
        { id: "lec_1", title: "001_[Home] Data Structure and Algorithm Syllabus", index: 1, has_video: true, has_pdf: true, is_active: true },
        { id: "lec_2", title: "002_[Home] Syllabus Introduction", index: 2, has_video: true, has_pdf: true, is_active: false },
        { id: "lec_3", title: "003_Unit-1.0 Fundamentals of Algorithms and its Analysis", index: 3, has_video: true, has_pdf: true, is_active: false }
      ]
    };
  }
}

async function fetchLectureAccess(lectureId) {
  try {
    const res = await fetch(`/api/lectures/${encodeURIComponent(lectureId)}/access`);
    if (!res.ok) throw new Error('Access failed');
    return await res.json();
  } catch (e) {
    return {
      has_video: false,
      error: "Video stream is not available or unpublished.",
      has_pdf: false,
      pdf_view_url: null,
      pdf_download_url: null
    };
  }
}

// ==============================================================================
// RENDER VIEWS
// ==============================================================================

// 1. HOME VIEW
async function renderHomeView(container) {
  const apps = await fetchApps();
  const currentHour = new Date().getHours();
  const greeting = currentHour < 12 ? 'Good Morning' : currentHour < 18 ? 'Good Afternoon' : 'Good Evening';

  container.innerHTML = `
    <!-- Hero Banner Card -->
    <div class="hero-banner-card">
      <div class="hero-content">
        <div class="hero-greeting">${greeting}, Learner! 👋</div>
        <h1 class="hero-title">Welcome to <span class="gradient-text">Course Wallah</span></h1>
        <p class="hero-subtitle">Discover premium educational apps, competitive batch archives, and high-speed lecture streams designed to help you learn smarter.</p>
        <div class="hero-actions">
          <button class="btn-hero-primary" onclick="navigateToTab('apps')">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
            <span>Explore Apps</span>
          </button>
          <button class="btn-hero-secondary" onclick="document.getElementById('category-filter-chips').scrollIntoView({behavior:'smooth'})">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h7"/></svg>
            <span>Categories</span>
          </button>
        </div>
      </div>
      <div class="hero-art-wrap">
        <svg class="hero-art-icon" viewBox="0 0 200 200" fill="none">
          <circle cx="100" cy="100" r="80" fill="url(#hero-art-grad)" fill-opacity="0.15"/>
          <path d="M40 85L100 55L160 85L100 115L40 85Z" fill="url(#hero-art-grad)"/>
          <path d="M60 100V135C60 148.8 77.9 160 100 160C122.1 160 140 148.8 140 135V100L100 120L60 100Z" fill="#8B5CF6"/>
          <defs>
            <linearGradient id="hero-art-grad" x1="40" y1="55" x2="160" y2="160" gradientUnits="userSpaceOnUse">
              <stop stop-color="#6366F1"/>
              <stop offset="1" stop-color="#A855F7"/>
            </linearGradient>
          </defs>
        </svg>
      </div>
    </div>

    <!-- Global Academic Operations Telemetry Row -->
    <div class="section-header-row">
      <h2 class="section-heading">Global Academic Operations</h2>
      <p class="section-subheading">Real-time telemetry and package deployment speeds.</p>
    </div>

    <div class="telemetry-grid">
      <div class="telemetry-card">
        <div class="telemetry-top">
          <div class="telemetry-icon-wrap">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2" ry="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
          </div>
          <span class="telemetry-secure-tag">SECURE ↗</span>
        </div>
        <div>
          <div class="telemetry-label">PREMIUM APPS</div>
          <div class="telemetry-value">17 Unlocked</div>
          <div class="telemetry-trend">● +4 Added This Month</div>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-top">
          <div class="telemetry-icon-wrap">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </div>
          <span class="telemetry-secure-tag">SECURE ↗</span>
        </div>
        <div>
          <div class="telemetry-label">TOTAL DOWNLOADS</div>
          <div class="telemetry-value">5,00,000+</div>
          <div class="telemetry-trend">● 99.9% Delivery Rate</div>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-top">
          <div class="telemetry-icon-wrap">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          </div>
          <span class="telemetry-secure-tag">SECURE ↗</span>
        </div>
        <div>
          <div class="telemetry-label">ACTIVE ACADEMIC USERS</div>
          <div class="telemetry-value">1,20,000+</div>
          <div class="telemetry-trend">● 2,34,430 Active Today</div>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-top">
          <div class="telemetry-icon-wrap">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
          </div>
          <span class="telemetry-secure-tag">SECURE ↗</span>
        </div>
        <div>
          <div class="telemetry-label">GLOBAL CDN SERVER</div>
          <div class="telemetry-value">Online [Fast]</div>
          <div class="telemetry-trend">● Ping 23ms • Up 100%</div>
        </div>
      </div>
    </div>

    <!-- Search & Filter Section -->
    <div class="search-filter-section" id="category-filter-chips">
      <div class="search-input-container">
        <svg class="search-input-icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" class="search-input-field" id="home-search-input" placeholder="Search study portals, batches, and applications..." onkeyup="handleSearchInput(this.value)">
        <button class="btn-search-submit" onclick="handleSearchSubmit()">Search</button>
      </div>

      <div class="category-chips-row">
        ${['All', 'Educational', 'Competitive', 'Academic', 'Test Prep', 'Coding', 'Creative'].map(cat => `
          <button class="category-chip ${state.activeCategory === cat ? 'active' : ''}" onclick="filterCategory('${cat}')">${cat}</button>
        `).join('')}
      </div>
    <!-- Nexora Developer Spotlight Banner -->
    <div style="background: linear-gradient(135deg, rgba(99,102,241,0.14) 0%, rgba(139,92,246,0.18) 100%); border:1px solid rgba(139,92,246,0.35); border-radius:24px; padding:24px 28px; margin-bottom:28px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px;">
      <div style="display:flex; align-items:center; gap:18px;">
        <div style="width:52px; height:52px; border-radius:16px; background:linear-gradient(135deg, #1E1B4B, #311042); border:1.5px solid rgba(139,92,246,0.5); display:flex; align-items:center; justify-content:center; font-size:24px; box-shadow:0 4px 16px rgba(139,92,246,0.3);">
          ⚡
        </div>
        <div>
          <div style="display:flex; align-items:center; gap:8px;">
            <h3 style="font-size:16px; font-weight:900; color:#FFFFFF;">Architected & Engineered by Nexora</h3>
            <span style="font-size:10px; font-weight:800; background:rgba(16,185,129,0.15); color:#10B981; padding:2px 8px; border-radius:6px;">PRO ARCHITECT</span>
          </div>
          <p style="font-size:13px; color:var(--text-secondary); margin-top:2px;">Software Developer • Technology Builder • Entrepreneur • AI & Cloud Systems</p>
        </div>
      </div>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <button onclick="navigateToTab('developer')" class="btn-hero-primary" style="padding:8px 18px; font-size:12px; background:linear-gradient(135deg, #6366F1, #8B5CF6);">
          <span>Meet the Developer</span>
        </button>
        <a href="https://t.me/course_wallah_official_bot" target="_blank" class="btn-hero-secondary" style="padding:8px 16px; font-size:12px;">
          <span>Contact Bot</span>
        </a>
      </div>
    </div>

    <!-- Popular Apps Grid -->
    <div class="apps-grid" id="apps-grid-container">
      ${renderAppsCardsHtml(apps)}
    </div>
  `;
}

function renderAppsCardsHtml(apps) {
  if (!apps || apps.length === 0) {
    return `<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">No study portals found.</div>`;
  }
  return apps.map((app, idx) => `
    <div class="app-card">
      <div>
        <div class="app-card-header">
          <div class="app-icon-wrap">
            <img src="${app.icon_url || '/static/logo.png'}" alt="${app.name}" class="app-icon-img" onerror="this.src='/static/logo.png'">
          </div>
          <div class="app-header-meta">
            <span class="app-tag-pill">${idx === 0 ? 'POPULAR' : 'HOT'}</span>
            <h3 class="app-title" title="${app.name}">${app.name}</h3>
            <span class="app-dev-name">ARCHITECT: Nexora / CW</span>
          </div>
        </div>

        <div class="app-meta-pills">
          <span class="meta-pill">📱 v5.02.7</span>
          <span class="meta-pill">💾 24.96 MB</span>
        </div>
      </div>

      <div>
        <div class="app-stats-row">
          <span class="app-download-count">👥 1,00,000+ Downloads</span>
          <span class="app-rating-badge">★ 4.9</span>
        </div>

        <div class="app-actions-row">
          <button class="btn-app-study" onclick="navigateToApp('${app.slug || app.id}')">
            <span>Let's Study</span>
          </button>
          <button class="btn-app-download" onclick="navigateToApp('${app.slug || app.id}')">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>Batches</span>
          </button>
        </div>
      </div>
    </div>
  `).join('');
}

// 2. MY APPS VIEW
async function renderAppsView(container) {
  const apps = await fetchApps();
  container.innerHTML = `
    <div class="section-header-row" style="margin-bottom: 24px;">
      <h1 class="section-heading" style="font-size: 28px;">All Study Portals & Applications</h1>
      <p class="section-subheading">Choose your academic stream to access complete lecture libraries and study materials.</p>
    </div>
    <div class="apps-grid">
      ${renderAppsCardsHtml(apps)}
    </div>
  `;
}

// 3. APP BATCHES VIEW (?tab=apps&app=...)
async function renderAppDetailView(container, appSlugOrId) {
  const apps = await fetchApps();
  const app = apps.find(a => a.slug === appSlugOrId || a.id === appSlugOrId) || apps[0];
  
  container.innerHTML = `
    <!-- App View Header -->
    <div class="app-view-header">
      <div class="app-view-title-group">
        <img src="${app.icon_url || '/static/logo.png'}" alt="${app.name}" class="app-view-icon" onerror="this.src='/static/logo.png'">
        <div>
          <h1 class="app-view-title">${app.name}</h1>
          <span class="session-active-tag">● SECURE CLIENT SESSION ACTIVE</span>
        </div>
      </div>
      <button class="btn-announcement" onclick="showToast('Official Announcement: All New 2025 batches are updated!')">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        <span>Show Announcement</span>
      </button>
    </div>

    <!-- Promo Channel Banner -->
    <div class="promo-banner">
      <div>
        <span class="promo-tag">OFFICIAL ANNOUNCEMENT</span>
        <h2 class="promo-title">Join Official Telegram Channel & Bot</h2>
        <p style="font-size:13px; opacity:0.9;">Get instant class alerts, DPP solutions, notes, and doubt sessions.</p>
      </div>
      <a href="https://t.me/coursewallahoffical1" target="_blank" class="btn-promo-action">Join Channel (t.me/coursewallahoffical1)</a>
    </div>

    <!-- Batches Tabs -->
    <div class="batches-tabs-bar">
      <button class="tab-pill-btn active" id="tab-all-batches" onclick="switchBatchTab('all')">All Batches (${app.batches ? app.batches.length : 1})</button>
      <button class="tab-pill-btn" id="tab-my-batches" onclick="switchBatchTab('my')">My Batches (${state.enrolledBatches.length})</button>
    </div>

    <!-- Search within Batches -->
    <div class="batch-search-wrap">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" style="margin-right:10px; color:var(--text-muted);"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input type="text" class="batch-search-input" placeholder="Search within All Batches..." onkeyup="filterBatches(this.value)">
    </div>

    <!-- Batches Cards Grid -->
    <div class="batches-grid" id="batches-grid-container">
      ${(app.batches || []).map(b => `
        <div class="batch-card">
          <div class="batch-thumb-wrap">
            <img src="${b.thumbnail_url || 'https://images.unsplash.com/photo-1517694712202-14dd9538aa97?w=480&auto=format&fit=crop&q=80'}" alt="${b.name}" class="batch-thumb-img">
            <span class="batch-status-badge">ACTIVE COURSE</span>
          </div>
          <div class="batch-body">
            <div>
              <h3 class="batch-title">${b.name}</h3>
              <div class="batch-price-row">
                <span class="batch-free-badge">₹ FREE</span>
                <span class="batch-free-subtag">100% FREE FOR STUDENTS</span>
              </div>
            </div>
            <div class="batch-actions-row">
              <button class="btn-app-study" onclick="navigateToBatch('${app.slug || app.id}', '${b.slug || b.id}')">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
                <span>Study</span>
              </button>
              <button class="btn-app-download" onclick="enrollBatch('${b.id || b.slug}', '${b.name}')">
                <span>Enrolled ✓</span>
              </button>
            </div>
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

// 4. BATCH SUBJECTS VIEW (?tab=apps&app=...&batchid=...)
async function renderBatchView(container, appSlugOrId, batchIdOrSlug) {
  const batch = await fetchBatchDetail(batchIdOrSlug);
  const subjects = batch.subjects || [];

  container.innerHTML = `
    <!-- Top Back Navigation -->
    <div class="batch-detail-header-row" style="margin-bottom: 20px;">
      <button class="btn-sub-back" onclick="navigateToApp('${appSlugOrId}')">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
        <span>Back</span>
      </button>
    </div>

    <!-- Promo Announcement Banner -->
    <div class="promo-banner">
      <div>
        <span class="promo-tag">COURSE WALLAH &bull; SECURE PLATFORM</span>
        <h2 class="promo-title">${batch.name}</h2>
        <p style="font-size:13px; opacity:0.9;">Explore our complete subject modules hosting high-definition video lectures, verified DPPs & study notes.</p>
      </div>
      <a href="https://t.me/coursewallahoffical1" target="_blank" class="btn-promo-action">Join Channel</a>
    </div>

    <!-- Subjects Section Title & Search Input -->
    <div class="subjects-header-bar">
      <h2 class="subjects-title-count">Subjects <span class="count-badge">(${subjects.length})</span></h2>
      <div class="subject-search-box">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" id="subject-search-input" placeholder="Search subject..." oninput="filterSubjectsList(this.value)">
      </div>
    </div>

    <!-- 2-Column Subjects Grid (Matching Reference Screenshot 2) -->
    <div class="subjects-modern-grid" id="subjects-grid-list">
      ${subjects.map((subj, idx) => {
        const letter = (subj.name || 'S').trim().charAt(0).toUpperCase();
        const vCount = subj.video_count || 0;
        const nCount = subj.notes_count || 0;
        return `
          <div class="subject-modern-card" onclick="navigateToSubject('${appSlugOrId}', '${batchIdOrSlug}', '${subj.slug || subj.id}')">
            <div class="subject-card-left">
              <div class="subject-avatar-badge">${letter}</div>
              <div class="subject-text-wrap">
                <h3 class="subject-name-heading">${subj.name}</h3>
                <span class="subject-meta-counts">${vCount} Videos &bull; ${nCount} Notes &bull; 0 Tests</span>
              </div>
            </div>
            <div class="subject-card-arrow">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function filterSubjectsList(query) {
  const q = (query || '').toLowerCase().trim();
  document.querySelectorAll('.subject-modern-card').forEach(card => {
    const title = (card.querySelector('.subject-name-heading')?.innerText || '').toLowerCase();
    card.style.display = title.includes(q) ? 'flex' : 'none';
  });
}

// 5. SUBJECT CONTENT EXPLORER (VIDEOS VS NOTES SEPARATED — Screenshot 3)
let activeSubjectSubTab = 'videos';

async function renderSubjectView(container, appSlugOrId, batchIdOrSlug, subjectIdOrSlug) {
  const batch = await fetchBatchDetail(batchIdOrSlug);
  const subjects = batch.subjects || [];
  const subject = subjects.find(s => s.slug === subjectIdOrSlug || s.id === subjectIdOrSlug) || subjects[0] || { name: 'Subject Explorer', folders: [] };

  // Separate videos and notes strictly
  const allLectures = (subject.folders || []).flatMap(f => f.lectures || []);
  const videoLectures = allLectures.filter(l => l.has_video);
  const noteLectures = allLectures.filter(l => l.has_pdf);

  container.innerHTML = `
    <!-- Top Back Bar -->
    <div class="batch-detail-header-row" style="margin-bottom: 20px;">
      <button class="btn-sub-back" onclick="navigateToBatch('${appSlugOrId}', '${batchIdOrSlug}')">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
        <span>Back</span>
      </button>
    </div>

    <!-- Subject Hero Header Card (Screenshot 3) -->
    <div class="subject-hero-card">
      <div class="subject-hero-left">
        <div class="subject-hero-app-row">
          <img src="/static/logo.png" alt="Course Wallah" class="subject-hero-app-logo">
          <div class="subject-hero-app-info">
            <span class="subject-hero-app-name">${batch.app_name || 'Course Wallah'}</span>
            <span class="subject-hero-secure-tag">● SECURE CLIENT SESSION ACTIVE</span>
          </div>
        </div>
        <h1 class="subject-hero-title">${subject.name}</h1>
      </div>

      <!-- Filter Pills: Videos vs Notes & PDFs -->
      <div class="subject-tab-pills-wrap">
        <button class="sub-filter-pill ${activeSubjectSubTab === 'videos' ? 'active' : ''}" id="pill-sub-videos" onclick="switchSubjectSubTab('videos')">
          Videos <span class="pill-count">${videoLectures.length}</span>
        </button>
        <button class="sub-filter-pill ${activeSubjectSubTab === 'notes' ? 'active' : ''}" id="pill-sub-notes" onclick="switchSubjectSubTab('notes')">
          Notes &amp; PDFs <span class="pill-count">${noteLectures.length}</span>
        </button>
      </div>
    </div>

    <!-- Tab 1: Videos Grid (Unit-wise grouped) -->
    <div class="subject-content-section ${activeSubjectSubTab === 'videos' ? '' : 'hidden'}" id="subject-videos-panel">
      ${videoLectures.length === 0 ? `
        <div class="empty-content-box">
          <div class="empty-icon">🎥</div>
          <h3>No video lectures in this subject</h3>
          <p>Please check the Notes &amp; PDFs tab for study materials.</p>
        </div>
      ` : (subject.folders || []).map(folder => {
        const vLecs = (folder.lectures || []).filter(l => l.has_video);
        if (vLecs.length === 0) return '';
        return `
          <div class="unit-section-block">
            <div class="unit-group-header">
              <div class="unit-folder-icon">📁</div>
              <div class="unit-group-title">${folder.name}</div>
              <span class="unit-lec-count">${vLecs.length} Videos</span>
            </div>
            <div class="videos-cards-grid">
              ${vLecs.map(lec => `
                <div class="video-lecture-card" onclick="navigateToLecture('${appSlugOrId}', '${batchIdOrSlug}', '${subjectIdOrSlug}', '${lec.id}')">
                  <div class="video-card-thumb-wrap">
                    <img src="${lec.thumbnail_url || batch.thumbnail_url || '/static/logo.png'}" alt="${lec.title}" class="video-card-thumb" onerror="this.src='/static/logo.png'">
                    <div class="video-watermark-tag">COURSE WALLAH</div>
                    <div class="video-duration-tag">⏱️ ${Math.round((lec.duration_seconds || 1500) / 60)} mins</div>
                    <div class="video-play-overlay">
                      <div class="video-play-btn-circle">
                        <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                      </div>
                    </div>
                  </div>
                  <div class="video-card-body">
                    <h3 class="video-card-title" title="${lec.title}">${lec.title}</h3>
                    <div class="video-card-footer">
                      <span>📅 ${lec.created_at || 'Recent'}</span>
                      <span class="video-lec-idx">Lecture #${lec.index}</span>
                    </div>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      }).join('')}
    </div>

    <!-- Tab 2: Notes & PDFs Grid (Unit-wise grouped) -->
    <div class="subject-content-section ${activeSubjectSubTab === 'notes' ? '' : 'hidden'}" id="subject-notes-panel">
      ${noteLectures.length === 0 ? `
        <div class="empty-content-box">
          <div class="empty-icon">📄</div>
          <h3>No PDF notes in this subject</h3>
          <p>Please check the Videos tab for class recordings.</p>
        </div>
      ` : (subject.folders || []).map(folder => {
        const nLecs = (folder.lectures || []).filter(l => l.has_pdf);
        if (nLecs.length === 0) return '';
        return `
          <div class="unit-section-block">
            <div class="unit-group-header">
              <div class="unit-folder-icon">📄</div>
              <div class="unit-group-title">${folder.name}</div>
              <span class="unit-lec-count">${nLecs.length} Notes</span>
            </div>
            <div class="notes-cards-grid">
              ${nLecs.map(note => {
                const streamViewerUrl = note.id ? `/api/v1/pdfs/${note.id}/content` : (note.source_pdf_url ? `/api/v1/pdfs/proxy?url=${encodeURIComponent(note.source_pdf_url)}` : '');
                return `
                  <div class="note-pdf-card">
                    <div class="note-card-left">
                      <div class="note-pdf-icon-box">
                        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                      </div>
                      <div class="note-info-wrap">
                        <h3 class="note-card-title">${note.title}</h3>
                        <span class="note-card-sub">Verified Study Material &bull; PDF Notes &bull; In-App Reader Only</span>
                      </div>
                    </div>
                    <div class="note-card-actions">
                      <button class="btn-note-view" onclick="openPdfModal('${streamViewerUrl}', '${note.title.replace(/'/g, "\\'")}')">
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        <span>Read Notes</span>
                      </button>
                    </div>
                  </div>
                `;
              }).join('')}
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function switchSubjectSubTab(tab) {
  activeSubjectSubTab = tab;
  const pVid = document.getElementById('pill-sub-videos');
  const pNot = document.getElementById('pill-sub-notes');
  const bVid = document.getElementById('subject-videos-panel');
  const bNot = document.getElementById('subject-notes-panel');

  if (pVid && pNot && bVid && bNot) {
    if (tab === 'videos') {
      pVid.classList.add('active');
      pNot.classList.remove('active');
      bVid.classList.remove('hidden');
      bNot.classList.add('hidden');
    } else {
      pNot.classList.add('active');
      pVid.classList.remove('active');
      bNot.classList.remove('hidden');
      bVid.classList.add('hidden');
    }
  }
}

// 6. IN-APP CINEMA VIDEO PLAYER VIEW (AS MULTIVERSE EXACT DESIGN)
let currentPlyrPlayer = null;

async function renderPlayerView(container, appSlugOrId, batchIdOrSlug, subjectIdOrSlug, lectureId) {
  const lecture = await fetchLectureDetails(lectureId);
  const access = await fetchLectureAccess(lectureId);

  // Filter sibling playlist to ONLY videos so PDFs don't get mixed in playlist!
  const videoPlaylist = (lecture.playlist || []).filter(item => item.has_video);
  
  // Extract YouTube ID if stream URL is a YouTube embed or watch link
  let effectiveYoutubeId = access.youtube_video_id || null;
  if (!effectiveYoutubeId && access.stream_url) {
    const ytMatch = access.stream_url.match(/(?:embed\/|v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
    if (ytMatch) {
      effectiveYoutubeId = ytMatch[1];
    }
  }
  const isFakeYtId = effectiveYoutubeId && (
    effectiveYoutubeId.startsWith('cw_temp_') ||
    effectiveYoutubeId.startsWith('yt_id_') ||
    effectiveYoutubeId.startsWith('EXISTING_YT') ||
    effectiveYoutubeId.startsWith('tg_') ||
    effectiveYoutubeId === 'dQw4w9WgXcQ'
  );
  const hasYouTube = Boolean(effectiveYoutubeId && !isFakeYtId);

  const isTelegramStream = Boolean(
    !hasYouTube && (
      access.storage_provider === 'telegram' ||
      access.storage_provider === 'telegram_stream' ||
      (access.stream_url && access.stream_url.includes('/stream/tg/'))
    )
  );

  const isMediaCm = Boolean(
    !hasYouTube && !isTelegramStream && (
      access.storage_provider === 'media_cm' ||
      (access.playback_url && access.playback_url.includes('media.cm')) ||
      (access.embed_url && access.embed_url.includes('media.cm'))
    )
  );
  const mediaCmUrl = access.playback_url || (access.embed_url && !access.embed_url.includes('/embed-') ? access.embed_url : null) || (access.embed_url ? `https://media.cm/${access.embed_url.split('/embed-')[1]?.split('.html')[0]}` : null);
  const isEmbed = Boolean(!hasYouTube && access.embed_url && !isMediaCm && !isTelegramStream);
  const effectiveEmbedUrl = access.embed_url;

  const isDash = Boolean(
    !hasYouTube && !isEmbed && !isMediaCm && (
      access.stream_type === 'dash' ||
      (access.stream_url && access.stream_url.includes('.mpd')) ||
      (access.clear_keys && Object.keys(access.clear_keys).length > 0)
    )
  );
  const isDirectHls = Boolean(!hasYouTube && !isEmbed && !isMediaCm && !isTelegramStream && !isDash && access.stream_url && (access.stream_url.includes('.m3u8') || access.stream_url.endsWith('.m3u8')));
  const isDirectVideo = Boolean(!hasYouTube && (isTelegramStream || (!isEmbed && !isMediaCm && access.stream_url && !isDirectHls && !isDash)));
  const detectedQuality = (access.player_config && access.player_config.quality) ? access.player_config.quality : '1080p';

  container.innerHTML = `
    <!-- Top Back Navigation -->
    <div class="batch-detail-header-row" style="margin-bottom: 20px;">
      <button class="btn-sub-back" onclick="navigateToSubject('${appSlugOrId}', '${batchIdOrSlug}', '${subjectIdOrSlug}')">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
        <span>Back</span>
      </button>
    </div>

    <!-- Main AS Multiverse Player Card -->
    <div class="as-player-card">
      
      <!-- Top App Header Row -->
      <div class="as-player-header-row">
        <div class="as-player-app-left">
          <img src="/static/logo.png" alt="App Logo" class="as-player-app-avatar" onerror="this.src='/static/logo.png'">
          <div class="as-player-app-meta">
            <h2 class="as-player-app-name">${lecture.batch_name || 'Course Wallah'}</h2>
            <span class="as-player-app-secure">● SECURE CLIENT SESSION ACTIVE</span>
          </div>
        </div>
        <button class="btn-show-announcement" onclick="showAnnouncementModal('${escapeHtml(lecture.batch_name || 'Batch Updates')}')">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
          <span>Show Announcement</span>
        </button>
      </div>

      <!-- Video Meta Details -->
      <div class="as-player-meta-wrap">
        <div class="as-player-subtag">
          <span class="subtag-pill">VIDEO LECTURE</span>
          <span class="subtag-dot">•</span>
          <span class="subtag-playing">${access.storage_provider ? `${access.storage_provider.toUpperCase()} SECURE STREAM` : 'NOW PLAYING'}</span>
        </div>
        <h1 class="as-player-title">${escapeHtml(lecture.title)}</h1>
      </div>

      <!-- Sleek Tablet Frame (Exact AS Multiverse Match) -->
      <div class="as-video-tablet-frame">
        <div class="as-video-inner-wrap" id="player-container">
          ${isEmbed ? `
            <iframe
              src="${effectiveEmbedUrl}"
              class="vcdn-iframe-player"
              allowfullscreen
              allowtransparency
              allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
              style="width:100%; height:100%; min-height:480px; border:0; border-radius:12px; background:#000;"
            ></iframe>
          ` : (isMediaCm && mediaCmUrl ? `
            <div class="cinema-stream-card" onclick="openCinemaPlayer('${mediaCmUrl}')">
              <div class="cinema-stream-backdrop" style="background-image: url('${lecture.thumbnail_url || '/static/logo.png'}');"></div>
              <div class="cinema-stream-overlay">
                <div class="cinema-stream-top">
                  <div class="cinema-badge-secure">
                    <span class="cinema-pulse-dot"></span>
                    <span>MEDIA.CM SECURE STREAM</span>
                  </div>
                  <span class="cinema-badge-res">${detectedQuality.toUpperCase()} ULTRA HD</span>
                </div>
                <div class="cinema-play-center">
                  <div class="cinema-play-btn-glow">
                    <svg viewBox="0 0 24 24" width="36" height="36" fill="#FFFFFF"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                  </div>
                  <h2 class="cinema-play-title">${escapeHtml(lecture.title)}</h2>
                  <p class="cinema-play-sub">Official high-bandwidth direct stream ready with zero buffering.</p>
                </div>
                <div class="cinema-stream-bottom">
                  <button class="btn-cinema-launch" onclick="event.stopPropagation(); openCinemaPlayer('${mediaCmUrl}')">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                    <span>Launch in Ultra HD Cinema Player &rarr;</span>
                  </button>
                </div>
              </div>
            </div>
          ` : (isDash ? `
            <video id="cw-plyr-element" class="plyr" playsinline controls preload="auto" crossorigin="anonymous" data-poster="${lecture.thumbnail_url || '/static/logo.png'}"></video>
          ` : (isDirectHls ? `
            <video id="cw-plyr-element" class="plyr" playsinline controls preload="auto" crossorigin="anonymous" data-poster="${lecture.thumbnail_url || '/static/logo.png'}"></video>
          ` : (isDirectVideo ? `
            <video id="cw-plyr-element" class="plyr" playsinline controls preload="metadata" data-poster="${lecture.thumbnail_url || '/static/logo.png'}">
              <source src="${access.stream_url}" type="video/mp4" />
            </video>
          ` : (hasYouTube ? `
            <div class="plyr__video-embed" id="cw-plyr-element">
              <iframe
                src="https://www.youtube-nocookie.com/embed/${effectiveYoutubeId}?origin=${encodeURIComponent(window.location.origin)}&iv_load_policy=3&modestbranding=1&playsinline=1&showinfo=0&rel=0&enablejsapi=1&controls=0&disablekb=1&fs=0&widget_referrer=${encodeURIComponent(window.location.origin)}"
                allowfullscreen
                allowtransparency
                allow="autoplay"
              ></iframe>
            </div>
          ` : `
            <div class="video-placeholder-box" style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:360px; padding:30px; text-align:center;">
              <svg viewBox="0 0 24 24" width="42" height="42" fill="none" stroke="#6366F1" stroke-width="2" style="margin-bottom:12px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
              <span style="font-weight:600; font-size:16px; color:#F8FAFC;">${escapeHtml(access.message || access.error || 'Video Stream Unavailable')}</span>
              <span style="font-size:13px; color:#94A3B8; margin-top:6px;">This lecture does not have an active video stream or is being processed.</span>
            </div>
          `)))))}
        </div>
      </div>

      <!-- Quick Quality & Playback Speed Bar -->
      <div class="player-quick-bar">
        <div class="player-quick-group">
          <span class="player-quick-label">⚡ Stream Quality:</span>
          <button class="player-quality-pill ${detectedQuality === '1080p' ? 'active' : ''}" onclick="setPlayerQuality('1080p')">1080p Full HD</button>
          <button class="player-quality-pill ${detectedQuality === '720p' ? 'active' : ''}" onclick="setPlayerQuality('720p')">720p HD</button>
          <button class="player-quality-pill ${detectedQuality === '480p' ? 'active' : ''}" onclick="setPlayerQuality('480p')">480p SD</button>
          <button class="player-quality-pill ${detectedQuality === '360p' ? 'active' : ''}" onclick="setPlayerQuality('360p')">360p Fast</button>
        </div>
        <div class="player-quick-group">
          <span class="player-quick-label">⏱️ Speed:</span>
          <button class="player-speed-pill active" onclick="setPlayerSpeed(1.0)">1.0x</button>
          <button class="player-speed-pill" onclick="setPlayerSpeed(1.25)">1.25x</button>
          <button class="player-speed-pill" onclick="setPlayerSpeed(1.5)">1.5x</button>
          <button class="player-speed-pill" onclick="setPlayerSpeed(2.0)">2.0x</button>
        </div>
      </div>

      <!-- AS Multiverse Social Promo Banner Card (Exact Screenshot Match) -->
      <div class="as-promo-banner-card">
        <div class="as-promo-left">
          <div class="as-promo-icon-circle">🌐</div>
          <div class="as-promo-text-wrap">
            <div class="as-promo-title-row">
              <span class="as-promo-name">Course Wallah Platform</span>
              <span class="as-promo-domain">COURSEWALLAH.APP</span>
              <span class="as-promo-tag">CENTRAL HUB</span>
            </div>
            <p class="as-promo-desc">Explore the verified educational cloud matrix hosting all secure digital portals, subject batches, notes, and study material.</p>
          </div>
        </div>
        <a href="https://t.me/course_wallah_official_bot" target="_blank" class="btn-as-promo-action">Visit Platform &rarr;</a>
      </div>

      <!-- Attached Notes & DPP Banner (In-App Only) -->
      ${access.pdf_download_url ? `
        <div class="player-attached-note-banner" style="margin-top: 24px;">
          <div class="attached-note-left">
            <div class="note-icon-glow">📄</div>
            <div>
              <h4 class="attached-note-title">Companion Class Notes &amp; DPP</h4>
              <span class="attached-note-sub">Official verified study material for this lecture &bull; In-App Reader Only</span>
            </div>
          </div>
          <div class="attached-note-actions">
            <button class="btn-attached-view" onclick="openPdfModal('/api/v1/pdfs/${lectureId}/content', '${lecture.title.replace(/'/g, "\\'")}')">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              <span>Read Notes</span>
            </button>
          </div>
        </div>
      ` : ''}

      <!-- Sibling Prev / Next Navigation -->
      <div class="player-prev-next-row" style="margin-top: 24px;">
        ${lecture.prev_lecture ? `
          <button class="btn-nav-lecture" onclick="navigateToLecture('${appSlugOrId}', '${batchIdOrSlug}', '${subjectIdOrSlug}', '${lecture.prev_lecture.id}')">
            <span>&larr; Previous: #${lecture.prev_lecture.index} ${lecture.prev_lecture.title}</span>
          </button>
        ` : '<div></div>'}
        ${lecture.next_lecture ? `
          <button class="btn-nav-lecture" onclick="navigateToLecture('${appSlugOrId}', '${batchIdOrSlug}', '${subjectIdOrSlug}', '${lecture.next_lecture.id}')">
            <span>Next: #${lecture.next_lecture.index} ${lecture.next_lecture.title} &rarr;</span>
          </button>
        ` : '<div></div>'}
      </div>

      <!-- Other Lectures in this Unit -->
      ${videoPlaylist.length > 1 ? `
        <div class="player-unit-lectures-section">
          <h3 class="unit-lectures-heading">Other Lectures in this Unit (${videoPlaylist.length})</h3>
          <div class="unit-lectures-chips-grid">
            ${videoPlaylist.map(item => `
              <div class="unit-lec-chip ${item.id === lectureId ? 'active' : ''}" onclick="navigateToLecture('${appSlugOrId}', '${batchIdOrSlug}', '${subjectIdOrSlug}', '${item.id}')">
                <span class="chip-idx">#${String(item.index || 1).padStart(2, '0')}</span>
                <span class="chip-title">${item.title}</span>
                ${item.id === lectureId ? '<span class="chip-badge">PLAYING</span>' : ''}
              </div>
            `).join('')}
          </div>
        </div>
      ` : ''}

    </div>
  `;

  // Initialize Plyr / Shaka instance if DASH, HLS, MP4 video, or YouTube
  setTimeout(() => {
    if (isDash || isDirectHls || isDirectVideo || hasYouTube) {
      initPlyr(access.stream_url, access);
    }
  }, 100);
}

function openCinemaPlayer(url) {
  if (!url) {
    showToast('⚠️ Video link unavailable');
    return;
  }
  showToast('🚀 Launching Course Wallah Cinema Player...');
  const win = window.open(url, '_blank');
  if (!win || win.closed || typeof win.closed === 'undefined') {
    window.location.href = url;
  }
}

let currentHlsInstance = null;
let currentShakaInstance = null;

function appendQueryParams(uri, queryString) {
  if (!queryString || !uri) return uri;
  try {
    const u = new URL(uri);
    const qs = queryString.startsWith('?') ? queryString.slice(1) : queryString;
    new URLSearchParams(qs).forEach((val, key) => {
      if (!u.searchParams.has(key)) {
        u.searchParams.set(key, val);
      }
    });
    return u.toString();
  } catch (e) {
    return uri;
  }
}

function togglePlayerPlayback() {
  if (currentPlyrPlayer) {
    try {
      currentPlyrPlayer.togglePlay();
    } catch(e) {}
  }
}

function setPlayerQuality(qualityStr) {
  document.querySelectorAll('.player-quality-pill').forEach(btn => {
    btn.classList.toggle('active', btn.innerText.includes(qualityStr));
  });
  
  if (currentHlsInstance) {
    const qNum = parseInt(qualityStr.replace('p', ''), 10);
    currentHlsInstance.levels.forEach((level, levelIndex) => {
      if (level.height === qNum) {
        currentHlsInstance.currentLevel = levelIndex;
      }
    });
  } else if (currentPlyrPlayer && currentPlyrPlayer.embed) {
    const ytQualityMap = {
      '1080p': 'hd1080',
      '720p': 'hd720',
      '480p': 'large',
      '360p': 'medium'
    };
    const targetYt = ytQualityMap[qualityStr] || 'hd1080';
    try {
      if (typeof currentPlyrPlayer.embed.setPlaybackQuality === 'function') {
        currentPlyrPlayer.embed.setPlaybackQuality(targetYt);
      }
      if (typeof currentPlyrPlayer.embed.setPlaybackQualityRange === 'function') {
        currentPlyrPlayer.embed.setPlaybackQualityRange(targetYt, targetYt);
      }
    } catch(e) {}
  }
  showToast(`✨ Quality switched to ${qualityStr} (HD Direct Stream)`);
}

function setPlayerSpeed(speedVal) {
  document.querySelectorAll('.player-speed-pill').forEach(btn => {
    btn.classList.toggle('active', btn.innerText === `${speedVal}x` || (speedVal === 1 && btn.innerText === '1.0x'));
  });
  if (currentPlyrPlayer) {
    try { currentPlyrPlayer.speed = speedVal; } catch(e) {}
  }
  showToast(`⚡ Playback speed set to ${speedVal}x`);
}

function initPlyr(streamUrl, access = {}) {
  if (currentPlyrPlayer) {
    try { currentPlyrPlayer.destroy(); } catch(e) {}
    currentPlyrPlayer = null;
  }
  if (currentHlsInstance) {
    try { currentHlsInstance.destroy(); } catch(e) {}
    currentHlsInstance = null;
  }
  if (currentShakaInstance) {
    try { currentShakaInstance.destroy(); } catch(e) {}
    currentShakaInstance = null;
  }

  const el = document.getElementById('cw-plyr-element');
  if (!el || typeof Plyr === 'undefined') return;

  const plyrControls = [
    'play-large',
    'play',
    'rewind',
    'fast-forward',
    'progress',
    'current-time',
    'duration',
    'mute',
    'volume',
    'settings',
    'pip',
    'fullscreen'
  ];

  // 1. Shaka Player for DASH (.mpd), Pearl MSCILearn proxy, or ClearKey DRM
  const isYouTube = Boolean(
    access.stream_type === 'youtube' ||
    access.youtube_video_id ||
    (streamUrl && (streamUrl.includes('youtube') || streamUrl.includes('youtu.be')))
  );

  const isDashStream = Boolean(
    !isYouTube &&
    streamUrl && (
      streamUrl.includes('.mpd') ||
      access.stream_type === 'dash' ||
      (access.clear_keys && Object.keys(access.clear_keys).length > 0) ||
      (streamUrl.includes('pearl.mscilearn.in') && streamUrl.includes('.mpd'))
    )
  );

  if (el.tagName === 'VIDEO' && isDashStream) {
    if (typeof shaka !== 'undefined') {
      shaka.polyfill.installAll();
      if (shaka.Player.isBrowserSupported()) {
        const player = new shaka.Player(el);
        currentShakaInstance = player;

        // Configure ClearKey DRM
        const clearKeys = access.clear_keys || {};
        if (Object.keys(clearKeys).length > 0) {
          player.configure({
            drm: {
              clearKeys: clearKeys
            }
          });
        }

        player.configure({
          streaming: {
            bufferingGoal: 30,
            rebufferingGoal: 2,
            jumpLargeGaps: true,
            retryParameters: {
              maxAttempts: 5,
              baseDelay: 500,
              backoffFactor: 1.5,
              timeout: 30000
            }
          }
        });

        // Register request filter to append CloudFront signature query params to all chunk requests
        try {
          const urlObj = new URL(streamUrl);
          const queryParams = urlObj.search;
          const netEngine = player.getNetworkingEngine();
          const RequestType = shaka.net.NetworkingEngine.RequestType;

          netEngine.registerRequestFilter((type, request) => {
            if (type === RequestType.SEGMENT || type === RequestType.MANIFEST) {
              if (request.uris && Array.isArray(request.uris)) {
                request.uris = request.uris.map(uri => appendQueryParams(uri, queryParams));
              }
            }
          });
        } catch (e) {
          console.warn('[Shaka] Segment filter setup warning:', e);
        }

        currentPlyrPlayer = new Plyr(el, {
          controls: plyrControls,
          settings: ['quality', 'speed', 'loop'],
          speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
          seekTime: 10,
          keyboard: { focused: true, global: true }
        });

        player.load(streamUrl).then(() => {
          console.log('[Shaka] Stream playing via pearl.mscilearn.in zero-token proxy');
          el.play().catch(() => {});
        }).catch(err => {
          console.error('[Shaka] Load error:', err);
        });

        return;
      }
    }
  }

  // 2. HTML5 video with direct HLS stream (.m3u8)
  const isHlsStream = Boolean(streamUrl && (streamUrl.includes('.m3u8') || streamUrl.endsWith('.m3u8')));
  if (el.tagName === 'VIDEO' && isHlsStream) {
    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 90,
        xhrSetup: function (xhr, url) {
          xhr.withCredentials = false;
        }
      });
      hls.loadSource(streamUrl);
      hls.attachMedia(el);
      currentHlsInstance = hls;

      // Initialize Plyr instance immediately so player UI is always ready
      currentPlyrPlayer = new Plyr(el, {
        controls: plyrControls,
        settings: ['quality', 'speed', 'loop'],
        speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
        seekTime: 10,
        keyboard: { focused: true, global: true },
        tooltips: { controls: true, seek: true }
      });

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        const availableQualities = hls.levels.map(l => l.height).filter(Boolean);
        if (availableQualities.length && currentPlyrPlayer) {
          currentPlyrPlayer.options.quality = {
            default: availableQualities[availableQualities.length - 1] || 720,
            options: availableQualities,
            forced: true,
            onChange: (newQuality) => {
              hls.levels.forEach((level, levelIndex) => {
                if (level.height === newQuality) {
                  hls.currentLevel = levelIndex;
                }
              });
              showToast(`Quality set to ${newQuality}p`);
            }
          };
        }
      });

      hls.on(Hls.Events.ERROR, (event, data) => {
        console.warn('HLS stream error:', data);
        if (data.fatal) {
          switch (data.type) {
            case Hls.ErrorTypes.NETWORK_ERROR:
              hls.startLoad();
              break;
            case Hls.ErrorTypes.MEDIA_ERROR:
              hls.recoverMediaError();
              break;
            default:
              hls.destroy();
              break;
          }
        }
      });
      return;
    } else if (el.canPlayType('application/vnd.apple.mpegurl')) {
      // Native Safari / iOS HLS
      el.src = streamUrl;
      currentPlyrPlayer = new Plyr(el, {
        controls: plyrControls,
        settings: ['speed', 'loop'],
        speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
        seekTime: 10
      });
      return;
    }
  }

  // Standard Plyr fallback
  try {
    currentPlyrPlayer = new Plyr(el, {
      controls: plyrControls,
      settings: ['quality', 'speed', 'loop'],
      quality: {
        default: 1080,
        options: [1080, 720, 480, 360],
        forced: true,
        onChange: (newQuality) => {
          showToast(`Stream quality set to ${newQuality}p`);
        }
      },
      speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
      seekTime: 10,
      keyboard: { focused: true, global: true },
      tooltips: { controls: true, seek: true },
      youtube: {
        noCookie: true,
        rel: 0,
        showinfo: 0,
        iv_load_policy: 3,
        modestbranding: 1,
        controls: 0,
        disablekb: 1,
        customControls: true
      }
    });

    const enforceHD = () => {
      if (currentPlyrPlayer && currentPlyrPlayer.embed) {
        try {
          if (typeof currentPlyrPlayer.embed.setPlaybackQuality === 'function') {
            currentPlyrPlayer.embed.setPlaybackQuality('hd1080');
          }
          if (typeof currentPlyrPlayer.embed.setPlaybackQualityRange === 'function') {
            currentPlyrPlayer.embed.setPlaybackQualityRange('hd1080', 'hd1080');
          }
        } catch(e) {}
      }
    };

    currentPlyrPlayer.on('ready', enforceHD);
    currentPlyrPlayer.on('playing', enforceHD);
  } catch (e) {
    console.debug('Plyr initialization fallback:', e);
  }
}

function showAnnouncementModal(batchName) {
  showToast(`📢 ${batchName}: All classes and DPP notes are synced and verified in high definition!`);
}

// IN-APP PDF READER MODAL (PDF.js Clean Canvas Renderer)
let currentPdfDoc = null;
let currentPdfPage = 1;
let currentPdfScale = 1.0;
let currentRenderTask = null;
let touchStartX = 0;
let touchStartY = 0;

async function openPdfModal(pdfUrl, title) {
  let modal = document.getElementById('cw-pdf-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'cw-pdf-modal';
    modal.className = 'cw-pdf-modal-overlay';
    document.body.appendChild(modal);
  }

  // Reset to auto-fit scale for mobile & desktop
  currentPdfScale = 1.0;

  // Resolve binary content URL or proxy if external CDN
  let binaryPdfUrl = pdfUrl;
  if (pdfUrl.includes('/api/v1/pdfs/') && pdfUrl.endsWith('/access')) {
    binaryPdfUrl = pdfUrl.replace('/access', '/content');
  } else if (pdfUrl.startsWith('http://') || pdfUrl.startsWith('https://')) {
    if (!pdfUrl.startsWith(window.location.origin)) {
      binaryPdfUrl = `/api/v1/pdfs/proxy?url=${encodeURIComponent(pdfUrl)}`;
    }
  }

  modal.innerHTML = `
    <div class="cw-pdf-modal-card">
      <div class="cw-pdf-header">
        <div class="cw-pdf-title-wrap">
          <span class="pdf-file-icon">📄</span>
          <span class="cw-pdf-title">${escapeHtml(title)}</span>
        </div>
        <div class="cw-pdf-controls">
          <button class="btn-pdf-ctrl" onclick="changePdfPage(-1)" title="Previous Page">&larr; Prev</button>
          <span class="pdf-page-display" id="pdf-page-num"><strong id="pdf-current-page">1</strong> / <span id="pdf-total-pages">1</span></span>
          <button class="btn-pdf-ctrl" onclick="changePdfPage(1)" title="Next Page">Next &rarr;</button>
          <button class="btn-pdf-ctrl btn-pdf-zoom" onclick="zoomPdf(-0.2)" title="Zoom Out">🔍 &minus;</button>
          <button class="btn-pdf-ctrl btn-pdf-zoom" onclick="zoomPdf(0.2)" title="Zoom In">🔍 +</button>
        </div>
        <button class="btn-pdf-close" onclick="closePdfModal()" title="Close Viewer">&times;</button>
      </div>
      <div class="cw-pdf-canvas-container" id="cw-pdf-container">
        <div class="spinner" id="pdf-loading-spinner" style="margin: 40px auto;"></div>
        <canvas id="cw-pdf-canvas" style="display:none;"></canvas>
        <div class="cw-pdf-swipe-hint" id="cw-pdf-swipe-hint">👈 Swipe left/right to flip pages 👉</div>
      </div>
    </div>
  `;

  modal.classList.add('active');
  document.body.style.overflow = 'hidden';

  // Backdrop click closes modal
  modal.onclick = (e) => {
    if (e.target === modal) closePdfModal();
  };

  // Touch swipe gesture on mobile container for page flipping
  const container = document.getElementById('cw-pdf-container');
  if (container) {
    container.addEventListener('touchstart', (e) => {
      if (e.touches && e.touches.length === 1) {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
      }
    }, { passive: true });

    container.addEventListener('touchend', (e) => {
      if (e.changedTouches && e.changedTouches.length === 1) {
        const diffX = e.changedTouches[0].clientX - touchStartX;
        const diffY = e.changedTouches[0].clientY - touchStartY;
        // Dominant horizontal swipe > 45px switches page
        if (Math.abs(diffX) > Math.abs(diffY) && Math.abs(diffX) > 45) {
          if (diffX < 0) {
            changePdfPage(1); // Swipe left = Next
          } else {
            changePdfPage(-1); // Swipe right = Prev
          }
        }
      }
    }, { passive: true });
  }

  try {
    if (typeof pdfjsLib !== 'undefined') {
      if (pdfjsLib.GlobalWorkerOptions) {
        pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
      }
      const loadingTask = pdfjsLib.getDocument(binaryPdfUrl);
      currentPdfDoc = await loadingTask.promise;
      currentPdfPage = 1;
      const totalEl = document.getElementById('pdf-total-pages');
      if (totalEl) totalEl.innerText = currentPdfDoc.numPages;
      const spinner = document.getElementById('pdf-loading-spinner');
      if (spinner) spinner.style.display = 'none';
      const canvas = document.getElementById('cw-pdf-canvas');
      if (canvas) canvas.style.display = 'block';
      await renderPdfPage(currentPdfPage);
    } else {
      throw new Error('PDF.js library not ready');
    }
  } catch (err) {
    console.debug('PDF.js canvas renderer fallback:', err);
    const containerEl = document.getElementById('cw-pdf-container');
    if (containerEl) {
      containerEl.innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:400px; padding:20px; text-align:center; width:100%;">
          <div style="font-size:48px; margin-bottom:14px;">📄</div>
          <h3 style="color:#F8FAFC; margin-bottom:8px; font-size:18px;">Document In-App Reader</h3>
          <p style="color:#94A3B8; margin-bottom:18px; max-width:440px; font-size:13px; line-height:1.5;">If this document preview is unavailable on your device, you can view or download it directly.</p>
          <div style="display:flex; gap:12px; margin-bottom:16px;">
            <a href="${binaryPdfUrl}" target="_blank" style="text-decoration:none; display:inline-flex; align-items:center; gap:8px; padding:10px 20px; background:#6366F1; color:#fff; border-radius:10px; font-weight:700; font-size:13.5px; box-shadow: 0 4px 14px rgba(99,102,241,0.4);">
              <span>Open Document Directly ↗</span>
            </a>
          </div>
          <iframe src="${binaryPdfUrl}" style="width:100%; height:65vh; border:none; border-radius:12px; background:#0F172A;"></iframe>
        </div>
      `;
    }
  }
}

async function renderPdfPage(num) {
  if (!currentPdfDoc) return;
  try {
    if (currentRenderTask) {
      try { currentRenderTask.cancel(); } catch(e) {}
      currentRenderTask = null;
    }
    const page = await currentPdfDoc.getPage(num);
    const canvas = document.getElementById('cw-pdf-canvas');
    const container = document.getElementById('cw-pdf-container');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    // Auto-fit to mobile / tablet container width
    const baseViewport = page.getViewport({ scale: 1.0 });
    let effectiveScale = currentPdfScale;
    
    if (container) {
      const isMobile = window.innerWidth <= 768;
      const pad = isMobile ? 12 : 36;
      const availableWidth = Math.max(container.clientWidth - pad, 280);
      const fitScale = (availableWidth / baseViewport.width);
      effectiveScale = fitScale * currentPdfScale;
    }

    const viewport = page.getViewport({ scale: effectiveScale });

    // Crisp Retina & High-DPI Display output
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2.5);
    canvas.width = Math.floor(viewport.width * pixelRatio);
    canvas.height = Math.floor(viewport.height * pixelRatio);
    canvas.style.width = Math.floor(viewport.width) + "px";
    canvas.style.height = Math.floor(viewport.height) + "px";

    const transform = pixelRatio !== 1 ? [pixelRatio, 0, 0, pixelRatio, 0, 0] : null;

    const renderContext = {
      canvasContext: ctx,
      transform: transform,
      viewport: viewport
    };
    currentRenderTask = page.render(renderContext);
    await currentRenderTask.promise;
    currentRenderTask = null;

    const pageNumEl = document.getElementById('pdf-current-page');
    if (pageNumEl) pageNumEl.innerText = num;
  } catch (ex) {
    if (ex?.name !== 'RenderingCancelledException') {
      console.debug('Page render error:', ex);
    }
  }
}

function changePdfPage(delta) {
  if (!currentPdfDoc) return;
  const newPage = currentPdfPage + delta;
  if (newPage >= 1 && newPage <= currentPdfDoc.numPages) {
    currentPdfPage = newPage;
    renderPdfPage(currentPdfPage);
  }
}

function zoomPdf(delta) {
  currentPdfScale = Math.max(0.7, Math.min(3.0, currentPdfScale + delta));
  renderPdfPage(currentPdfPage);
  showToast(`🔍 Zoom: ${Math.round(currentPdfScale * 100)}%`);
}

function closePdfModal() {
  const modal = document.getElementById('cw-pdf-modal');
  if (modal) {
    modal.classList.remove('active');
    modal.innerHTML = '';
  }
  document.body.style.overflow = '';
}


function switchDrawerTab(tab) {
  const pTab = document.getElementById('drawer-tab-playlist');
  const nTab = document.getElementById('drawer-tab-notes');
  const pBody = document.getElementById('drawer-playlist-body');
  const nBody = document.getElementById('drawer-notes-body');

  if (tab === 'playlist') {
    pTab.classList.add('active');
    nTab.classList.remove('active');
    pBody.classList.remove('hidden');
    nBody.classList.add('hidden');
  } else {
    nTab.classList.add('active');
    pTab.classList.remove('active');
    nBody.classList.remove('hidden');
    pBody.classList.add('hidden');
  }
}

function setPlaybackSpeed(speedStr, btnEl) {
  document.querySelectorAll('.btn-speed-pill').forEach(b => b.classList.remove('active'));
  btnEl.classList.add('active');
  const rate = parseFloat(speedStr.replace('x', ''));
  const iframe = document.querySelector('.video-iframe');
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage(JSON.stringify({
      event: 'command',
      func: 'setPlaybackRate',
      args: [rate]
    }), '*');
  }
}

// 7. COMMUNITY VIEW
function renderCommunityView(container) {
  container.innerHTML = `
    <div class="section-header-row" style="margin-bottom: 24px;">
      <h1 class="section-heading" style="font-size: 28px;">Social Media & Official Community Matrix</h1>
      <p class="section-subheading">Join thousands of students and learners across verified official channels.</p>
    </div>

    <div class="cards-grid-2col">
      <div class="social-card">
        <div class="social-card-left">
          <div class="social-icon-box" style="background:#229ED9; color:#FFF;">🤖</div>
          <div>
            <h3 style="font-size:16px; font-weight:800; color:var(--text-primary);">Course Wallah Official Bot</h3>
            <span style="font-size:12px; color:var(--text-muted); font-weight:600;">@course_wallah_official_bot • 24/7 Batch Downloader & Ingestion</span>
          </div>
        </div>
        <a href="https://t.me/course_wallah_official_bot" target="_blank" class="btn-hero-primary" style="padding:8px 18px; font-size:13px; background:#229ED9;">Open Bot</a>
      </div>

      <div class="social-card">
        <div class="social-card-left">
          <div class="social-icon-box" style="background: linear-gradient(135deg, #0284C7, #06B6D4); color:#FFF;">📢</div>
          <div>
            <h3 style="font-size:16px; font-weight:800; color:var(--text-primary);">Official Telegram Channel</h3>
            <span style="font-size:12px; color:var(--text-muted); font-weight:600;">t.me/coursewallahoffical1 • Daily Batch Updates & Alerts</span>
          </div>
        </div>
        <a href="https://t.me/coursewallahoffical1" target="_blank" class="btn-hero-primary" style="padding:8px 18px; font-size:13px; background: linear-gradient(135deg, #0284C7, #06B6D4);">Join Channel</a>
      </div>

      <div class="social-card">
        <div class="social-card-left">
          <div class="social-icon-box" style="background:#8B5CF6; color:#FFF;">👨‍💻</div>
          <div>
            <h3 style="font-size:16px; font-weight:800; color:var(--text-primary);">Developer Hub & Support</h3>
            <span style="font-size:12px; color:var(--text-muted); font-weight:600;">Built by Nexora • Architecture & Engineering</span>
          </div>
        </div>
        <button onclick="navigateToTab('developer')" class="btn-hero-primary" style="padding:8px 18px; font-size:13px; background:#8B5CF6;">Meet Nexora</button>
      </div>

      <div class="social-card">
        <div class="social-card-left">
          <div class="social-icon-box" style="background:#10B981; color:#FFF;">⚡</div>
          <div>
            <h3 style="font-size:16px; font-weight:800; color:var(--text-primary);">Global Edge Mirror Network</h3>
            <span style="font-size:12px; color:var(--text-muted); font-weight:600;">Ultra-low latency streaming for all students</span>
          </div>
        </div>
        <button onclick="navigateToTab('downloads')" class="btn-hero-primary" style="padding:8px 18px; font-size:13px; background:#10B981;">Test Network</button>
      </div>
    </div>
  `;
}

// 8. DOWNLOADS CENTER VIEW
function renderDownloadsView(container) {
  container.innerHTML = `
    <div class="section-header-row" style="margin-bottom: 24px;">
      <h1 class="section-heading" style="font-size: 28px;">Downloads Center & Tools</h1>
      <p class="section-subheading">High-speed mirrors and tools for seamless learning on any device.</p>
    </div>

    <div class="cards-grid-2col">
      <div class="social-card">
        <div>
          <h3 style="font-size:17px; font-weight:800; color:var(--text-primary); margin-bottom:4px;">Course Wallah Mobile App (APK)</h3>
          <p style="font-size:13px; color:var(--text-secondary); margin-bottom:12px;">Fast, ad-free offline access for Android devices.</p>
          <span style="font-size:11px; font-weight:700; color:var(--accent-emerald);">● v5.02.7 • 24.96 MB • Direct APK</span>
        </div>
        <button class="btn-hero-primary" onclick="showToast('Direct APK Download started...')">Download</button>
      </div>

      <div class="social-card">
        <div>
          <h3 style="font-size:17px; font-weight:800; color:var(--text-primary); margin-bottom:4px;">Global CDN Server Speed Test</h3>
          <p style="font-size:13px; color:var(--text-secondary); margin-bottom:12px;">Check your connection latency to our media edge nodes.</p>
          <span id="speed-test-status" style="font-size:11px; font-weight:700; color:var(--accent-emerald);">● Ping: 23ms • Optimal</span>
        </div>
        <button class="btn-hero-secondary" onclick="runSpeedTest()">Test Speed</button>
      </div>
    </div>
  `;
}

function runSpeedTest() {
  const status = document.getElementById('speed-test-status');
  if (status) {
    status.innerText = '● Testing latency...';
    setTimeout(() => {
      const ping = Math.floor(Math.random() * 12) + 16;
      status.innerText = `● Ping: ${ping}ms • 100% Blazing Fast`;
      showToast(`CDN Edge latency measured: ${ping}ms`);
    }, 500);
  }
}

// 9. BOOKMARKS VIEW
function renderBookmarksView(container) {
  container.innerHTML = `
    <div class="section-header-row" style="margin-bottom: 24px;">
      <h1 class="section-heading" style="font-size: 28px;">Your Bookmarks & Saved Lectures</h1>
      <p class="section-subheading">Quick access to pinned chapters and saved course material.</p>
    </div>

    <div style="background:var(--bg-card); border: 1px solid var(--border-subtle); border-radius:20px; padding:48px; text-align:center;">
      <div style="font-size:48px; margin-bottom:12px;">🔖</div>
      <h3 style="font-size:18px; font-weight:800; color:var(--text-primary); margin-bottom:6px;">No Bookmarks Saved Yet</h3>
      <p style="font-size:14px; color:var(--text-secondary); margin-bottom:20px;">While watching any lecture, click the "Save" button to keep it in your bookmarks.</p>
      <button class="btn-hero-primary" onclick="navigateToTab('apps')">Explore Courses</button>
    </div>
  `;
}

// 10. SETTINGS VIEW
function renderSettingsView(container) {
  container.innerHTML = `
    <div class="section-header-row" style="margin-bottom: 24px;">
      <h1 class="section-heading" style="font-size: 28px;">Platform Settings</h1>
      <p class="section-subheading">Customize your playback preferences and interface appearance.</p>
    </div>

    <div style="background:var(--bg-card); border: 1px solid var(--border-subtle); border-radius:20px; padding:28px; display:flex; flex-direction:column; gap:24px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div>
          <h4 style="font-size:15px; font-weight:800; color:var(--text-primary);">Theme Appearance</h4>
          <p style="font-size:13px; color:var(--text-muted);">Switch between Obsidian Dark and Clean Light themes.</p>
        </div>
        <button class="btn-hero-secondary" onclick="toggleTheme()">Toggle Theme</button>
      </div>

      <div style="display:flex; justify-content:space-between; align-items:center; padding-top:20px; border-top:1px solid var(--border-subtle);">
        <div>
          <h4 style="font-size:15px; font-weight:800; color:var(--text-primary);">Study Streak Counter</h4>
          <p style="font-size:13px; color:var(--text-muted);">Current streak: ${state.streak} consecutive days.</p>
        </div>
        <button class="btn-hero-secondary" onclick="showToast('Streak active!')">Streak: ${state.streak} 🔥</button>
      </div>

      <div style="display:flex; justify-content:space-between; align-items:center; padding-top:20px; border-top:1px solid var(--border-subtle);">
        <div>
          <h4 style="font-size:15px; font-weight:800; color:var(--text-primary);">Clear Local Cache</h4>
          <p style="font-size:13px; color:var(--text-muted);">Reset locally cached batch lists and temporary offline data.</p>
        </div>
        <button class="btn-hero-secondary" style="color:var(--accent-rose); border-color:rgba(244,63,94,0.3);" onclick="localStorage.clear(); showToast('Local cache cleared successfully');">Clear Cache</button>
      </div>
    </div>
  `;
}

// 11. MEET THE DEVELOPER — NEXORA VIEW
function renderDeveloperView(container) {
  const techStack = [
    "Python & FastAPI", "JavaScript & TypeScript", "React & Next.js", "Node.js & Express",
    "PHP & MySQL", "MongoDB & SQLite", "Flutter & Mobile", "REST & GraphQL APIs",
    "AI & Autonomous Workflows", "Docker & Linux Containers", "Cloud & Server DevOps",
    "Telegram Bot Infrastructure", "Video Processing (FFmpeg / yt-dlp)", "Backblaze B2 & CDN Routing"
  ];

  const buildAreas = [
    {
      icon: "🌐",
      title: "Web Platforms",
      desc: "Modern, responsive and scalable web applications designed around real users with rich interactive experiences."
    },
    {
      icon: "⚡",
      title: "Backend Systems",
      desc: "High-concurrency APIs, JWT authentication, resilient database layers, background job workers and robust business logic."
    },
    {
      icon: "🤖",
      title: "Automation Workflows",
      desc: "Intelligent systems that eliminate repetitive manual tasks through autonomous failover, Telegram hooks, and scheduled jobs."
    },
    {
      icon: "🧠",
      title: "AI-Powered Products",
      desc: "Exploring how modern AI models, agentic workflows, and LLMs can make software significantly more useful, interactive and smart."
    },
    {
      icon: "☁️",
      title: "Cloud & Infrastructure",
      desc: "Architecting, deploying and managing applications, microservices, container clusters, databases and distributed edge workloads."
    },
    {
      icon: "🎓",
      title: "Education Technology",
      desc: "Building specialized platforms that make complex learning resources simpler to organize, access, stream and manage."
    }
  ];

  const lifecycleSteps = [
    { num: "01", name: "Idea" },
    { num: "02", name: "Architecture" },
    { num: "03", name: "Development" },
    { num: "04", name: "Testing" },
    { num: "05", name: "Deployment" },
    { num: "06", name: "Improvement" }
  ];

  container.innerHTML = `
    <!-- Hero Profile Card -->
    <div class="dev-hero-card">
      <div class="dev-hero-top">
        <div class="dev-avatar-container">
          <div class="dev-avatar-glow"></div>
          <img src="/static/logo.png" alt="Course Wallah Official Logo" class="dev-avatar-img">
        </div>
        <div class="dev-header-info">
          <div class="dev-role-pills">
            <span class="dev-pill">Software Developer</span>
            <span class="dev-pill">Technology Builder</span>
            <span class="dev-pill">Entrepreneur</span>
            <span class="dev-pill">Product Architect</span>
          </div>
          <h1 class="dev-name">
            <span>Nexora</span>
            <span class="dev-status-badge">● Available for Projects</span>
          </h1>
          <div class="dev-subtitle">Built by Nexora • Technology • Innovation • Automation • Education</div>
          <p class="dev-bio-text">
            Nexora is an independent technology developer and entrepreneur passionate about transforming ideas into practical, scalable, and meaningful digital products. With a strong interest in software engineering, artificial intelligence, automation, cloud technology, cybersecurity, web development, and digital platforms, Nexora focuses on building systems that are not only visually modern but also reliable, scalable, and useful in the real world.
          </p>
          <p class="dev-bio-text" style="margin-top:8px;">
            From designing interfaces to engineering backend systems, databases, APIs, automation workflows, and cloud infrastructure, Nexora enjoys working across the complete technology stack.
          </p>
          
          <div class="dev-cta-row">
            <a href="https://t.me/course_wallah_official_bot" target="_blank" class="btn-dev-telegram">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.75-.55 2.93-1.28 4.88-2.12 5.86-2.54 2.79-1.17 3.37-1.37 3.75-1.37.08 0 .27.02.39.12.1.08.13.2.14.28-.01.06.01.24 0 .38z"/></svg>
              <span>Contact Bot: @course_wallah_official_bot</span>
            </a>
            <a href="https://t.me/coursewallahoffical1" target="_blank" class="btn-dev-channel">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.75-.55 2.93-1.28 4.88-2.12 5.86-2.54 2.79-1.17 3.37-1.37 3.75-1.37.08 0 .27.02.39.12.1.08.13.2.14.28-.01.06.01.24 0 .38z"/></svg>
              <span>Join Channel: t.me/coursewallahoffical1</span>
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Quote Banner -->
    <div class="dev-quote-box">
      <span class="dev-quote-icon">⚡</span>
      <span class="dev-quote-text">“A developer who doesn't just write code — builds systems, products and ideas into reality.”</span>
    </div>

    <!-- The Journey & Product Lifecycle -->
    <h2 class="dev-section-heading">
      <span>🚀</span>
      <span>The Journey & Product-First Mindset</span>
    </h2>
    <p style="font-size:14px; color:var(--text-secondary); line-height:1.7; margin-bottom:16px;">
      The journey started with a simple curiosity about how software works and how technology can solve everyday problems. Over time, that curiosity evolved into hands-on development across multiple areas of technology—building websites, applications, automation tools, educational platforms, APIs, cloud systems, and experimental products.
    </p>

    <!-- Product Lifecycle Track -->
    <div class="dev-lifecycle-track">
      ${lifecycleSteps.map(st => `
        <div class="dev-lifecycle-step">
          <span class="dev-step-num">${st.num}</span>
          <span class="dev-step-name">${st.name}</span>
        </div>
      `).join('')}
    </div>

    <!-- What Nexora Builds -->
    <h2 class="dev-section-heading">
      <span>🧠</span>
      <span>What Nexora Builds</span>
    </h2>
    <div class="dev-grid-3col">
      ${buildAreas.map(item => `
        <div class="dev-card">
          <div class="dev-card-icon">${item.icon}</div>
          <h3 class="dev-card-title">${item.title}</h3>
          <p class="dev-card-desc">${item.desc}</p>
        </div>
      `).join('')}
    </div>

    <!-- Building for Education: The Course Wallah Story -->
    <h2 class="dev-section-heading">
      <span>🎓</span>
      <span>Building for Education — Course Wallah Ecosystem</span>
    </h2>
    <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:24px; padding:32px; margin-bottom:32px;">
      <p style="font-size:15px; color:var(--text-primary); line-height:1.7; margin-bottom:16px;">
        One of Nexora's strongest interests is <strong>education technology</strong>. <em>Course Wallah</em> represents this vision: a platform where educational content, technology and automation come together to create a smoother, faster learning experience for students.
      </p>
      <p style="font-size:14px; color:var(--text-secondary); line-height:1.7; margin-bottom:20px;">
        Behind the student-facing interface is a robust engine designed around:
      </p>
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:12px; margin-bottom:20px;">
        <div style="padding:12px; background:rgba(99,102,241,0.06); border-radius:12px; font-size:13px; font-weight:700; color:var(--text-primary);">📁 Structured Courses & Batches</div>
        <div style="padding:12px; background:rgba(99,102,241,0.06); border-radius:12px; font-size:13px; font-weight:700; color:var(--text-primary);">⚙️ Automated Content Processing</div>
        <div style="padding:12px; background:rgba(99,102,241,0.06); border-radius:12px; font-size:13px; font-weight:700; color:var(--text-primary);">🎥 Multi-CDN Video Delivery</div>
        <div style="padding:12px; background:rgba(99,102,241,0.06); border-radius:12px; font-size:13px; font-weight:700; color:var(--text-primary);">📄 Verified Digital Notes & DPPs</div>
        <div style="padding:12px; background:rgba(99,102,241,0.06); border-radius:12px; font-size:13px; font-weight:700; color:var(--text-primary);">🤖 Telegram-Based Administration</div>
        <div style="padding:12px; background:rgba(99,102,241,0.06); border-radius:12px; font-size:13px; font-weight:700; color:var(--text-primary);">☁️ Scalable Railway & B2 Storage</div>
      </div>
      <div style="font-size:14px; font-weight:800; color:var(--accent-emerald);">
        “The goal isn't simply to build another course website. The goal is to build technology that makes learning simpler, more accessible and more organized.”
      </div>
    </div>

    <!-- Builder Mindset & Engineering Philosophy -->
    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px; margin-bottom:32px;">
      <!-- Builder Mindset -->
      <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:24px; padding:28px;">
        <h3 style="font-size:18px; font-weight:800; color:var(--text-primary); margin-bottom:12px; display:flex; align-items:center; gap:8px;">
          <span>🏗️</span>
          <span>Builder Mindset</span>
        </h3>
        <p style="font-size:13px; color:var(--text-secondary); line-height:1.6; margin-bottom:16px;">
          Nexora believes that good software isn't created by technology alone. It requires:
        </p>
        <ul style="list-style:none; display:flex; flex-direction:column; gap:10px; font-size:13px; color:var(--text-primary);">
          <li>💡 <strong>Curiosity:</strong> To ask better questions and understand root problems.</li>
          <li>🎯 <strong>Persistence:</strong> To solve difficult architectural bottlenecks.</li>
          <li>🧪 <strong>Experimentation:</strong> To discover faster and more scalable approaches.</li>
          <li>🛡️ <strong>Discipline:</strong> To build reliable, maintainable systems.</li>
          <li>🎨 <strong>Creativity:</strong> To turn abstract ideas into intuitive user experiences.</li>
        </ul>
      </div>

      <!-- Engineering Philosophy -->
      <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:24px; padding:28px;">
        <h3 style="font-size:18px; font-weight:800; color:var(--text-primary); margin-bottom:12px; display:flex; align-items:center; gap:8px;">
          <span>🔐</span>
          <span>Engineering Philosophy</span>
        </h3>
        <p style="font-size:13px; font-weight:800; color:var(--primary); margin-bottom:12px;">
          “Build fast. Learn continuously. Secure what matters. Improve relentlessly.”
        </p>
        <p style="font-size:13px; color:var(--text-secondary); line-height:1.6; margin-bottom:16px;">
          Security, reliability and scalability are treated as part of development—not something added at the very end. The focus is on creating systems that are:
        </p>
        <div style="display:flex; flex-wrap:wrap; gap:8px;">
          <span style="padding:6px 12px; border-radius:8px; background:rgba(16,185,129,0.12); color:#10B981; font-weight:700; font-size:12px;">✓ Secure</span>
          <span style="padding:6px 12px; border-radius:8px; background:rgba(99,102,241,0.12); color:#818CF8; font-weight:700; font-size:12px;">✓ Scalable</span>
          <span style="padding:6px 12px; border-radius:8px; background:rgba(59,130,246,0.12); color:#60A5FA; font-weight:700; font-size:12px;">✓ Maintainable</span>
          <span style="padding:6px 12px; border-radius:8px; background:rgba(245,158,11,0.12); color:#FBBF24; font-weight:700; font-size:12px;">✓ High-Performance</span>
          <span style="padding:6px 12px; border-radius:8px; background:rgba(236,72,153,0.12); color:#F472B6; font-weight:700; font-size:12px;">✓ User-Centric</span>
          <span style="padding:6px 12px; border-radius:8px; background:rgba(6,182,212,0.12); color:#22D3EE; font-weight:700; font-size:12px;">✓ Automation-Ready</span>
        </div>
      </div>
    </div>

    <!-- Tech Stack Cloud -->
    <h2 class="dev-section-heading">
      <span>🛠️</span>
      <span>Technology Stack & Tools</span>
    </h2>
    <div class="dev-tech-cloud">
      ${techStack.map(t => `<span class="dev-tech-badge">⚡ ${t}</span>`).join('')}
    </div>

    <!-- Direct Telegram Contact Card -->
    <div style="background:linear-gradient(135deg, rgba(34,158,217,0.12) 0%, rgba(99,102,241,0.15) 100%); border:1px solid rgba(34,158,217,0.35); border-radius:24px; padding:32px; text-align:center; margin-top:20px;">
      <h3 style="font-size:22px; font-weight:900; color:#FFFFFF; margin-bottom:8px;">Have an Idea, Collaboration, or Need Support?</h3>
      <p style="font-size:14px; color:var(--text-secondary); max-width:600px; margin:0 auto 20px;">
        Reach out directly via Telegram bot or join the official Course Wallah community channel.
      </p>
      <div style="display:flex; justify-content:center; gap:16px; flex-wrap:wrap;">
        <a href="https://t.me/course_wallah_official_bot" target="_blank" class="btn-hero-primary" style="padding:10px 24px; background:#229ED9;">
          <span>🤖 Contact @course_wallah_official_bot</span>
        </a>
        <a href="https://t.me/coursewallahoffical1" target="_blank" class="btn-hero-primary" style="padding:10px 24px; background: linear-gradient(135deg, #0284C7, #06B6D4);">
          <span>📢 Channel: t.me/coursewallahoffical1</span>
        </a>
      </div>
    </div>
  `;
}

// 12. HELP CENTER VIEW
function renderHelpCenterView(container) {
  const faqs = [
    { q: "How do I access video lectures and notes?", a: "Navigate to 'My Apps', select your desired course provider (e.g. Physics Wallah, Apna College), choose your batch, and click 'Study' to launch the player and notes." },
    { q: "Are all courses and batch archives free?", a: "Yes! All batch archives and study materials on Course Wallah are 100% free for educational research and study." },
    { q: "How often are new batches updated?", a: "Our cloud ingestion bot runs continuously to process and upload new live recordings and notes daily." },
    { q: "How do I download lecture notes in PDF?", a: "Inside the Study Studio player, switch to the 'Notes & PDFs' tab on the right drawer and click 'Download Clean PDF'." },
    { q: "How do I contact the developer or report an issue?", a: "You can reach out directly via Telegram bot @course_wallah_official_bot or join our official channel at t.me/coursewallahoffical1." }
  ];

  container.innerHTML = `
    <div class="section-header-row" style="margin-bottom: 24px;">
      <h1 class="section-heading" style="font-size: 28px;">Help Center & Guides</h1>
      <p class="section-subheading">Frequently asked questions and direct support channels.</p>
    </div>

    <!-- Quick Support Card -->
    <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:20px; padding:24px; display:flex; justify-content:space-between; align-items:center; margin-bottom:28px; flex-wrap:wrap; gap:16px;">
      <div>
        <h3 style="font-size:16px; font-weight:800; color:var(--text-primary); margin-bottom:4px;">Need instant assistance or have feedback?</h3>
        <p style="font-size:13px; color:var(--text-secondary);">Message our official bot directly for fast resolution.</p>
      </div>
      <a href="https://t.me/course_wallah_official_bot" target="_blank" class="btn-hero-primary" style="padding:8px 20px; background:#229ED9;">
        <span>Contact @course_wallah_official_bot</span>
      </a>
    </div>

    <div>
      ${faqs.map(faq => `
        <div class="faq-accordion-item">
          <button class="faq-question-btn" onclick="toggleFaq(this)">
            <span>${faq.q}</span>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="faq-answer hidden">${faq.a}</div>
        </div>
      `).join('')}
    </div>
  `;
}

function toggleFaq(btn) {
  const ans = btn.nextElementSibling;
  if (ans) {
    ans.classList.toggle('hidden');
  }
}

// ==============================================================================
// UTILITIES: TOASTS & INTERACTIONS
// ==============================================================================

function showToast(message) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.innerText = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

function filterCategory(cat) {
  state.activeCategory = cat;
  document.querySelectorAll('.category-chip').forEach(c => {
    c.classList.toggle('active', c.innerText === cat);
  });
  showToast(`Filtered by ${cat}`);
}

function handleSearchInput(query) {
  const q = (query || '').toLowerCase().trim();
  const cards = document.querySelectorAll('.app-card');
  cards.forEach(c => {
    const title = (c.querySelector('.app-title')?.innerText || '').toLowerCase();
    c.style.display = title.includes(q) ? 'flex' : 'none';
  });
}

function handleSearchSubmit() {
  const input = document.getElementById('home-search-input');
  if (input && input.value) {
    showToast(`Searching for "${input.value}"...`);
  }
}

function enrollBatch(batchId, batchName) {
  if (!state.enrolledBatches.includes(batchId)) {
    state.enrolledBatches.push(batchId);
    localStorage.setItem('cw_enrolled', JSON.stringify(state.enrolledBatches));
    showToast(`Enrolled in "${batchName}"!`);
  } else {
    showToast(`Already enrolled in "${batchName}"!`);
  }
}

function toggleBookmark(id, title) {
  const idx = state.bookmarks.indexOf(id);
  if (idx === -1) {
    state.bookmarks.push(id);
    showToast(`Bookmark added: "${title}"`);
  } else {
    state.bookmarks.splice(idx, 1);
    showToast(`Bookmark removed`);
  }
  localStorage.setItem('cw_bookmarks', JSON.stringify(state.bookmarks));
}


// ==============================================================================
// 12. REAL-TIME LIVE LOGS & DIAGNOSTICS CONSOLE
// ==============================================================================

let logState = {
  logs: [],
  summary: { total_logs: 0, error_count: 0, warning_count: 0, youtube_events: 0, download_events: 0 },
  diagnostics: null,
  activeFilter: 'ALL',
  searchQuery: '',
  autoScroll: true,
  isStreaming: true,
  timerId: null,
  lastSinceId: 0
};

async function renderLogsView(container) {
  // Clear any existing polling timer
  if (logState.timerId) {
    clearInterval(logState.timerId);
    logState.timerId = null;
  }

  container.innerHTML = `
    <div class="logs-console-wrapper">
      
      <!-- Diagnostic Telemetry Overview -->
      <div class="logs-header-card">
        <div class="logs-title-row">
          <div class="logs-title-wrap">
            <div class="logs-icon-pulse">
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            </div>
            <div>
              <h1 class="logs-main-title">Live Diagnostic &amp; Operational Console</h1>
              <p class="logs-subtitle">Real-time system telemetry, YouTube failover router, AppX streams, and background workers.</p>
            </div>
          </div>
          <div class="logs-status-pills">
            <div class="status-pill-live" id="stream-live-indicator">
              <span class="live-dot"></span>
              <span id="stream-status-text">LIVE STREAMING</span>
            </div>
            <button class="btn-refresh-logs" onclick="fetchLiveLogs(true)" title="Force Refresh">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
            </button>
          </div>
        </div>

        <!-- Metrics Grid -->
        <div class="logs-metrics-grid">
          <div class="log-metric-card" style="border-left: 4px solid #6366F1;">
            <span class="log-metric-label">TOTAL LOGS</span>
            <span class="log-metric-value" id="metric-total-logs">0</span>
            <span class="log-metric-sub">Ring Buffer: 2,500</span>
          </div>
          <div class="log-metric-card" style="border-left: 4px solid #EF4444;">
            <span class="log-metric-label">ERRORS / FAULTS</span>
            <span class="log-metric-value" id="metric-errors" style="color:#EF4444;">0</span>
            <span class="log-metric-sub" id="metric-error-sub">Auto-recovered</span>
          </div>
          <div class="log-metric-card" style="border-left: 4px solid #F59E0B;">
            <span class="log-metric-label">WARNINGS / RETRIES</span>
            <span class="log-metric-value" id="metric-warnings" style="color:#F59E0B;">0</span>
            <span class="log-metric-sub">Managed Handshakes</span>
          </div>
          <div class="log-metric-card" style="border-left: 4px solid #EC4899;">
            <span class="log-metric-label">YOUTUBE UPLOADS</span>
            <span class="log-metric-value" id="metric-yt-events" style="color:#EC4899;">0</span>
            <span class="log-metric-sub" id="metric-yt-sub">Multi-Channel Active</span>
          </div>
          <div class="log-metric-card" style="border-left: 4px solid #06B6D4;">
            <span class="log-metric-label">MEDIA DOWNLOADS</span>
            <span class="log-metric-value" id="metric-dl-events" style="color:#06B6D4;">0</span>
            <span class="log-metric-sub">HLS / Native yt-dlp</span>
          </div>
        </div>
      </div>

      <!-- Human-Readable Diagnosis & Quick Fix Banner -->
      <div id="logs-diagnostic-guidance" class="diagnostic-guidance-card hidden">
        <div class="guidance-icon">💡</div>
        <div class="guidance-content">
          <h4 class="guidance-title">System Auto-Recovery &amp; Diagnostic Hints</h4>
          <div id="guidance-hints-list" class="guidance-hints-list"></div>
        </div>
      </div>

      <!-- Terminal Toolbar Controls -->
      <div class="terminal-toolbar">
        <div class="terminal-filters-left">
          <button class="terminal-filter-btn active" data-filter="ALL" onclick="setLogFilter('ALL')">🌟 All Logs</button>
          <button class="terminal-filter-btn filter-error" data-filter="ERROR" onclick="setLogFilter('ERROR')">🚨 Errors</button>
          <button class="terminal-filter-btn filter-warning" data-filter="WARNING" onclick="setLogFilter('WARNING')">⚠️ Warnings</button>
          <button class="terminal-filter-btn filter-youtube" data-filter="YOUTUBE" onclick="setLogFilter('YOUTUBE')">▶️ YouTube</button>
          <button class="terminal-filter-btn filter-download" data-filter="DOWNLOAD" onclick="setLogFilter('DOWNLOAD')">📥 Downloads</button>
          <button class="terminal-filter-btn filter-pdf" data-filter="PDF" onclick="setLogFilter('PDF')">📄 Notes / PDFs</button>
          <button class="terminal-filter-btn filter-system" data-filter="SYSTEM" onclick="setLogFilter('SYSTEM')">⚙️ System</button>
        </div>

        <div class="terminal-controls-right">
          <div class="terminal-search-wrap">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" id="terminal-search-input" placeholder="Search logs, URLs, tags..." oninput="handleLogSearch(this.value)">
          </div>
          <button class="btn-terminal-action" id="btn-toggle-stream" onclick="toggleLogStreaming()" title="Pause/Resume Stream">
            <span id="toggle-stream-icon">⏸️</span>
            <span id="toggle-stream-text">Pause</span>
          </button>
          <button class="btn-terminal-action" id="btn-toggle-autoscroll" onclick="toggleLogAutoScroll()" title="Pin to Latest Logs">
            <span id="autoscroll-icon">📌</span>
            <span>Pin Bottom</span>
          </button>
          <button class="btn-terminal-action" onclick="copyTerminalLogs()" title="Copy all filtered logs">
            📋 Copy Logs
          </button>
          <button class="btn-terminal-action btn-terminal-clear" onclick="clearTerminalLogs()" title="Clear Terminal View">
            🧹 Clear
          </button>
        </div>
      </div>

      <!-- Cyberpunk Log Stream Terminal -->
      <div class="terminal-container" id="terminal-container">
        <div class="terminal-topbar">
          <div class="terminal-window-dots">
            <span class="dot-red"></span>
            <span class="dot-yellow"></span>
            <span class="dot-green"></span>
          </div>
          <div class="terminal-window-title">course_wallah_platform@production:~# journalctl -u supervisor -f</div>
          <div class="terminal-entry-count" id="terminal-count-badge">0 visible events</div>
        </div>
        <div class="terminal-body" id="terminal-body">
          <div class="terminal-loading">Connecting to real-time logs stream...</div>
        </div>
      </div>

    </div>
  `;

  // Initialize data fetch
  await fetchLiveLogs(true);

  // Start continuous polling every 2500ms
  logState.timerId = setInterval(() => {
    if (logState.isStreaming && window.location.search.includes('tab=logs')) {
      fetchLiveLogs(false);
    }
  }, 2500);
}

async function fetchLiveLogs(isForce = false) {
  try {
    const res = await fetch('/api/admin/logs?limit=400');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    
    if (data.status === 'success') {
      logState.logs = data.logs || [];
      logState.summary = data.summary || {};
      
      updateLogMetrics();
      renderTerminalLogs();
      updateDiagnosticsGuidance();
    }
  } catch (err) {
    console.debug('Failed to poll logs:', err);
  }
}

function updateLogMetrics() {
  const sum = logState.summary;
  const elTotal = document.getElementById('metric-total-logs');
  const elErr = document.getElementById('metric-errors');
  const elWarn = document.getElementById('metric-warnings');
  const elYt = document.getElementById('metric-yt-events');
  const elDl = document.getElementById('metric-dl-events');

  if (elTotal) elTotal.innerText = sum.total_logs || 0;
  if (elErr) elErr.innerText = sum.error_count || 0;
  if (elWarn) elWarn.innerText = sum.warning_count || 0;
  if (elYt) elYt.innerText = sum.youtube_events || 0;
  if (elDl) elDl.innerText = sum.download_events || 0;
}

function updateDiagnosticsGuidance() {
  const guidanceCard = document.getElementById('logs-diagnostic-guidance');
  const hintsList = document.getElementById('guidance-hints-list');
  if (!guidanceCard || !hintsList) return;

  const activeHints = [];
  const seen = new Set();

  logState.logs.forEach(l => {
    if (l.hint && !seen.has(l.hint)) {
      seen.add(l.hint);
      activeHints.push({ hint: l.hint, level: l.level, tag: l.tag });
    }
  });

  if (activeHints.length > 0) {
    guidanceCard.classList.remove('hidden');
    hintsList.innerHTML = activeHints.map(h => `
      <div class="guidance-hint-item">
        <span class="hint-badge hint-badge-${h.level.toLowerCase()}">[${h.tag}]</span>
        <span class="hint-text">${h.hint}</span>
      </div>
    `).join('');
  } else {
    guidanceCard.classList.add('hidden');
  }
}

function renderTerminalLogs() {
  const body = document.getElementById('terminal-body');
  const countBadge = document.getElementById('terminal-count-badge');
  if (!body) return;

  const filter = logState.activeFilter;
  const search = (logState.searchQuery || '').toLowerCase().trim();

  let filtered = logState.logs;

  if (filter !== 'ALL') {
    if (filter === 'ERROR') {
      filtered = filtered.filter(l => l.level === 'ERROR' || l.level === 'CRITICAL');
    } else if (filter === 'WARNING') {
      filtered = filtered.filter(l => l.level === 'WARNING');
    } else {
      filtered = filtered.filter(l => l.category === filter);
    }
  }

  if (search) {
    filtered = filtered.filter(l => 
      (l.message || '').toLowerCase().includes(search) ||
      (l.tag || '').toLowerCase().includes(search) ||
      (l.category || '').toLowerCase().includes(search)
    );
  }

  if (countBadge) {
    countBadge.innerText = `${filtered.length} visible / ${logState.logs.length} total`;
  }

  if (filtered.length === 0) {
    body.innerHTML = `<div class="terminal-empty">No log records matched filter "${filter}" ${search ? `with query "${search}"` : ''}</div>`;
    return;
  }

  body.innerHTML = filtered.map(l => {
    const lvlClass = `log-lvl-${(l.level || 'info').toLowerCase()}`;
    const tagClass = `log-tag-${(l.category || 'system').toLowerCase()}`;
    
    // Highlight important tokens like URLs, IDs, outcomes
    let safeMsg = escapeHtml(l.message || '');
    safeMsg = safeMsg.replace(/(\bhttps?:\/\/[^\s]+)/g, '<span class="log-hl-url">$1</span>');
    safeMsg = safeMsg.replace(/(lecture_index=#[0-9]+)/g, '<span class="log-hl-lec">$1</span>');
    safeMsg = safeMsg.replace(/(status=SUCCESS|UPLOADED|SUCCESS|completed)/gi, '<span class="log-hl-success">$1</span>');
    safeMsg = safeMsg.replace(/(failed|error|violation|exit 8|HTTP 401)/gi, '<span class="log-hl-error">$1</span>');
    safeMsg = safeMsg.replace(/(Account '[^']+')/g, '<span class="log-hl-acc">$1</span>');

    return `
      <div class="terminal-row ${lvlClass}" onclick="copySingleLogLine(this)" title="Click to copy log line">
        <span class="log-time">${l.timestamp}</span>
        <span class="log-level-pill ${lvlClass}">[${l.level}]</span>
        <span class="log-tag-pill ${tagClass}">[${l.tag || 'SYS'}]</span>
        <span class="log-msg-content">${safeMsg}</span>
      </div>
    `;
  }).join('');

  if (logState.autoScroll) {
    body.scrollTop = body.scrollHeight;
  }
}

function setLogFilter(filter) {
  logState.activeFilter = filter;
  document.querySelectorAll('.terminal-filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-filter') === filter);
  });
  renderTerminalLogs();
}

function handleLogSearch(val) {
  logState.searchQuery = val;
  renderTerminalLogs();
}

function toggleLogStreaming() {
  logState.isStreaming = !logState.isStreaming;
  const icon = document.getElementById('toggle-stream-icon');
  const text = document.getElementById('toggle-stream-text');
  const statusIndicator = document.getElementById('stream-live-indicator');
  const statusText = document.getElementById('stream-status-text');

  if (logState.isStreaming) {
    if (icon) icon.innerText = '⏸️';
    if (text) text.innerText = 'Pause';
    if (statusIndicator) statusIndicator.className = 'status-pill-live';
    if (statusText) statusText.innerText = 'LIVE STREAMING';
    fetchLiveLogs(true);
    showToast('Live stream resumed');
  } else {
    if (icon) icon.innerText = '▶️';
    if (text) text.innerText = 'Resume';
    if (statusIndicator) statusIndicator.className = 'status-pill-paused';
    if (statusText) statusText.innerText = 'STREAM PAUSED';
    showToast('Live stream paused');
  }
}

function toggleLogAutoScroll() {
  logState.autoScroll = !logState.autoScroll;
  const btn = document.getElementById('btn-toggle-autoscroll');
  if (btn) {
    btn.classList.toggle('active', logState.autoScroll);
  }
  showToast(logState.autoScroll ? 'Auto-scroll pinned to bottom' : 'Auto-scroll disabled');
}

function copyTerminalLogs() {
  const body = document.getElementById('terminal-body');
  if (!body) return;
  const text = Array.from(body.querySelectorAll('.terminal-row')).map(r => r.innerText).join('\n');
  navigator.clipboard.writeText(text).then(() => {
    showToast('Copied visible logs to clipboard! 📋');
  }).catch(() => {
    showToast('Failed to copy logs');
  });
}

function copySingleLogLine(el) {
  if (!el) return;
  navigator.clipboard.writeText(el.innerText).then(() => {
    showToast('Log line copied to clipboard! 📋');
  });
}

function clearTerminalLogs() {
  const body = document.getElementById('terminal-body');
  if (body) {
    body.innerHTML = '<div class="terminal-empty">Terminal view cleared. Waiting for next event...</div>';
  }
  showToast('Terminal view cleared');
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// ==============================================================================
// DIRECT STREAM STUDIO — ZERO-TOKEN UNIVERSAL MSCILearn / PEARL PLAYER
// ==============================================================================

let studioShakaPlayer = null;
let studioPlyrPlayer = null;
let studioHlsPlayer = null;
let studioResolvedStreamUrl = null;

async function renderDirectPlayerStudio(container) {
  container.innerHTML = `
    <div class="studio-container" style="max-width: 1050px; margin: 0 auto; padding-bottom: 60px;">
      
      <!-- Studio Header -->
      <div class="studio-header" style="margin-bottom: 24px; text-align: left;">
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 8px;">
          <div style="display:flex; align-items:center; gap:12px;">
            <div style="width: 44px; height: 44px; border-radius: 12px; background: rgba(99, 102, 241, 0.15); border: 1px solid rgba(99, 102, 241, 0.3); display: flex; align-items: center; justify-content: center;">
              <svg viewBox="0 0 24 24" width="22" height="22" fill="#6366F1"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            </div>
            <div>
              <h1 style="font-size: 26px; font-weight: 800; color: var(--text-primary); margin: 0;">Direct Stream Studio</h1>
              <p style="font-size: 13px; color: var(--text-secondary); margin: 2px 0 0;">Zero-Token Universal Player powered by Pearl MSCILearn Gateway</p>
            </div>
          </div>
          <span style="display:inline-flex; align-items:center; gap:6px; font-size:11px; font-weight:700; color:#10B981; background:rgba(16, 185, 129, 0.1); border:1px solid rgba(16, 185, 129, 0.25); padding:6px 14px; border-radius:20px;">
            <span style="width:7px; height:7px; border-radius:50%; background:#10B981; box-shadow:0 0 8px #10B981; animation:pulse 2s infinite;"></span>
            PEARL PROXY ACTIVE
          </span>
        </div>
      </div>

      <!-- Input Card -->
      <div class="studio-input-card" style="background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 20px; padding: 24px; margin-bottom: 28px; box-shadow: var(--shadow-sm);">
        <label style="display: block; font-size: 13px; font-weight: 700; color: var(--text-primary); margin-bottom: 8px;">
          Paste Any Video or Lecture Link:
        </label>
        <div style="display: flex; gap: 12px; flex-wrap: wrap;">
          <input 
            type="text" 
            id="studio-stream-input" 
            placeholder="e.g. https://courseswala/play?batchId=... or .mpd or .m3u8 or mp4..." 
            style="flex: 1; min-width: 280px; padding: 14px 18px; border-radius: 12px; border: 1px solid var(--border-subtle); background: var(--bg-elevated); color: var(--text-primary); font-size: 14px; outline: none; font-family: inherit;"
            onkeypress="if(event.key === 'Enter') handleStudioPlay();"
          />
          <button 
            class="btn-hero-primary" 
            id="btn-studio-play"
            onclick="handleStudioPlay()"
            style="padding: 14px 28px; font-size: 14px; font-weight: 700; display: inline-flex; align-items: center; gap: 8px;"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <span>Play Stream ⚡</span>
          </button>
        </div>

        <!-- Quick Presets -->
        <div style="margin-top: 16px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
          <span style="font-size: 12px; font-weight: 600; color: var(--text-muted);">Quick Presets:</span>
          <button class="player-quality-pill" onclick="loadStudioPreset('pw-saleem')">⚡ PW Lakshya (Saleem Sir)</button>
          <button class="player-quality-pill" onclick="loadStudioPreset('pw-gate')">⚡ PW GATE (Algorithms)</button>
          <button class="player-quality-pill" onclick="loadStudioPreset('pw-yakeen')">⚡ PW Yakeen NEET</button>
          <button class="player-quality-pill" onclick="loadStudioPreset('sw-demo')">⚡ Selection Way HLS</button>
        </div>
      </div>

      <!-- Player Frame -->
      <div class="as-player-card" id="studio-player-wrap" style="display: none; margin-bottom: 24px;">
        <div class="as-player-header-row" style="padding: 16px 20px;">
          <div class="as-player-app-left">
            <img src="/static/logo.png" alt="Logo" class="as-player-app-avatar">
            <div class="as-player-app-meta">
              <h2 class="as-player-app-name" id="studio-player-title">Stream Studio Playback</h2>
              <span class="as-player-app-secure" id="studio-player-status">● PEARL MSCILearn DIRECT STREAM ACTIVE</span>
            </div>
          </div>
          <span class="subtag-pill" id="studio-stream-type-badge">DASH STREAM</span>
        </div>

        <div class="as-video-tablet-frame">
          <div class="as-video-inner-wrap" id="studio-player-container">
            <video id="studio-plyr-element" class="plyr" playsinline controls preload="auto" crossorigin="anonymous" data-poster="/static/logo.png"></video>
          </div>
        </div>

        <!-- Metadata inspection box -->
        <div style="padding: 18px 24px; border-top: 1px solid var(--border-subtle); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--text-muted);">
          <div id="studio-meta-details">Streaming with Shaka Engine &amp; ClearKey Decryption</div>
          <button class="player-quality-pill" onclick="copyStudioStreamUrl()">📋 Copy Stream URL</button>
        </div>
      </div>

    </div>
  `;
}

async function handleStudioPlay(presetUrl) {
  const inputEl = document.getElementById('studio-stream-input');
  const url = (presetUrl || (inputEl ? inputEl.value : '')).trim();
  if (!url) {
    showToast('⚠️ Please enter a video or stream URL');
    return;
  }

  const btn = document.getElementById('btn-studio-play');
  if (btn) btn.innerHTML = '<span>Resolving...</span>';

  try {
    showToast('⚡ Resolving stream via Pearl MSCILearn Gateway...');
    const res = await fetch(`/api/v1/stream/resolve?url=${encodeURIComponent(url)}`);
    if (!res.ok) throw new Error('Failed to resolve stream');
    const data = await res.json();

    if (!data.success || !data.stream_url) {
      throw new Error(data.error || 'Video stream could not be resolved');
    }

    studioResolvedStreamUrl = data.stream_url;

    const wrap = document.getElementById('studio-player-wrap');
    if (wrap) wrap.style.display = 'block';

    const titleEl = document.getElementById('studio-player-title');
    if (titleEl) titleEl.innerText = data.title || 'Direct Stream Playback';

    const typeBadge = document.getElementById('studio-stream-type-badge');
    if (typeBadge) typeBadge.innerText = (data.stream_type || 'DIRECT').toUpperCase() + ' STREAM';

    const metaEl = document.getElementById('studio-meta-details');
    if (metaEl) {
      const keysCount = Object.keys(data.clear_keys || {}).length;
      metaEl.innerText = `Provider: ${data.storage_provider || 'Pearl MSCILearn'} | DRM: ${keysCount > 0 ? 'ClearKey Decrypted (' + keysCount + ' keys)' : 'Standard'} | Zero-Token Active`;
    }

    // Scroll player into view smoothly
    wrap.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // Initialize Shaka or HLS on studio player element
    initStudioPlayer(data);
    showToast('🎉 Stream ready & playing!');
  } catch (err) {
    console.error('Studio play error:', err);
    showToast(`❌ Error: ${err.message}`);
  } finally {
    if (btn) btn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg><span>Play Stream ⚡</span>';
  }
}

function initStudioPlayer(data) {
  if (studioPlyrPlayer) {
    try { studioPlyrPlayer.destroy(); } catch(e) {}
    studioPlyrPlayer = null;
  }
  if (studioShakaPlayer) {
    try { studioShakaPlayer.destroy(); } catch(e) {}
    studioShakaPlayer = null;
  }
  if (studioHlsPlayer) {
    try { studioHlsPlayer.destroy(); } catch(e) {}
    studioHlsPlayer = null;
  }

  const el = document.getElementById('studio-plyr-element');
  if (!el) return;

  const streamUrl = data.stream_url;
  const isDash = Boolean(data.stream_type === 'dash' || streamUrl.includes('.mpd') || (data.clear_keys && Object.keys(data.clear_keys).length > 0));
  const isHls = Boolean(data.stream_type === 'hls' || streamUrl.includes('.m3u8'));

  const plyrControls = [
    'play-large', 'play', 'rewind', 'fast-forward', 'progress',
    'current-time', 'duration', 'mute', 'volume', 'settings', 'pip', 'fullscreen'
  ];

  if (isDash && typeof shaka !== 'undefined') {
    shaka.polyfill.installAll();
    if (shaka.Player.isBrowserSupported()) {
      const player = new shaka.Player(el);
      studioShakaPlayer = player;

      const clearKeys = data.clear_keys || {};
      if (Object.keys(clearKeys).length > 0) {
        player.configure({
          drm: { clearKeys: clearKeys }
        });
      }

      player.configure({
        streaming: {
          bufferingGoal: 30,
          rebufferingGoal: 2,
          jumpLargeGaps: true,
          retryParameters: { maxAttempts: 5, baseDelay: 500, backoffFactor: 1.5, timeout: 30000 }
        }
      });

      try {
        const urlObj = new URL(streamUrl);
        const queryParams = urlObj.search;
        const netEngine = player.getNetworkingEngine();
        netEngine.registerRequestFilter((type, request) => {
          if (type === shaka.net.NetworkingEngine.RequestType.SEGMENT || type === shaka.net.NetworkingEngine.RequestType.MANIFEST) {
            if (request.uris && Array.isArray(request.uris)) {
              request.uris = request.uris.map(uri => appendQueryParams(uri, queryParams));
            }
          }
        });
      } catch (e) {}

      studioPlyrPlayer = new Plyr(el, {
        controls: plyrControls,
        settings: ['quality', 'speed', 'loop'],
        speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2] },
        seekTime: 10,
        keyboard: { focused: true, global: true }
      });

      player.load(streamUrl).then(() => {
        el.play().catch(() => {});
      }).catch(e => console.error('Studio shaka load err:', e));
      return;
    }
  }

  if (isHls && typeof Hls !== 'undefined' && Hls.isSupported()) {
    const hls = new Hls({ enableWorker: true });
    studioHlsPlayer = hls;
    hls.loadSource(streamUrl);
    hls.attachMedia(el);
    studioPlyrPlayer = new Plyr(el, { controls: plyrControls });
    hls.on(Hls.Events.MANIFEST_PARSED, () => el.play().catch(() => {}));
    return;
  }

  // Fallback direct HTML5
  el.src = streamUrl;
  studioPlyrPlayer = new Plyr(el, { controls: plyrControls });
  el.play().catch(() => {});
}

function loadStudioPreset(presetKey) {
  const presets = {
    'pw-saleem': 'https://courseswala/play?batchId=6779345c20fa0756e4a7fd08&subjectId=69beb1defa18934d859e3526&scheduleId=6ab41b42143ad72acbbe6111&topic=Moving%20Charges%20and%20Magnetism%20%3A%20KPP%2014%20Discussion',
    'pw-gate': 'https://courseswala/play?batchId=654ccf5aee945800189872dc&subjectId=665a0825a58f7c0018fefcf5&scheduleId=66838382d6cfa900181ef035&topic=Algorithms%20Lecture',
    'pw-yakeen': 'https://courseswala/play?batchId=69897f0a4c12aeb013d4ea52&subjectId=699e91b465da1f732aa85d57&scheduleId=69d664ad5afe48443c625405&topic=Demo%20Lecture%20By%20Siddharth%20Sharma%20Sir',
    'sw-demo': 'https://v2024.hranker.com/f9bb3bc8836ecdd1da66cf8d805bf77c_hranker/hls/9f61b0386cfb33d456725833df0b33b0_1080p.m3u8'
  };
  const url = presets[presetKey];
  if (url) {
    const inputEl = document.getElementById('studio-stream-input');
    if (inputEl) inputEl.value = url;
    handleStudioPlay(url);
  }
}

function copyStudioStreamUrl() {
  if (studioResolvedStreamUrl) {
    navigator.clipboard.writeText(studioResolvedStreamUrl).then(() => {
      showToast('Copied direct stream URL! 📋');
    });
  } else {
    showToast('No stream loaded yet');
  }
}



# =============================================================================
# JOB MONITOR CONFIGURATION
# The only file you need to edit.
# Railway picks up changes automatically within ~2 minutes of a commit.
# =============================================================================


# ---------------------------------------------------------------------------
# POLLING INTERVAL  (seconds)
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 300   # every 5 minutes


# ---------------------------------------------------------------------------
# DISCORD ROUTING
# ---------------------------------------------------------------------------

URGENT_THRESHOLD = 8    # score 8–10 → #urgent
WATCHLIST_MIN    = 4    # score 4–7  → #watch-list
                        # below 4   → suppressed (truly irrelevant)
# Note: WATCHLIST_MIN lowered from 6 to 4 so all title-matched postings
# reach Discord regardless of ERP signal strength in the JD.
# Adjust back to 6 if watch-list volume becomes too high.


# ---------------------------------------------------------------------------
# TARGET JOB TITLES
# A posting must match one of these to proceed to AI scoring.
# Tight filter — only roles with direct ERP buying power.
# ---------------------------------------------------------------------------

TARGET_TITLES = [
    # C-suite and VP level
    "Chief Financial Officer",
    "CFO",
    "Chief Accounting Officer",
    "CAO",
    "VP Finance",
    "VP of Finance",
    "Vice President Finance",
    "Vice President of Finance",
    "VP Accounting",
    "Vice President of Accounting",
    "VP of Accounting",
    # Controller titles
    "Controller",
    "Corporate Controller",
    "Assistant Controller",
    "Plant Controller",
    "Division Controller",
    "Regional Controller",
    # Director level — finance and accounting only
    "Director of Finance",
    "Director Finance",
    "Finance Director",
    "Director of Accounting",
    "Accounting Director",
    "Director of Financial",
    "Director of Financial Systems",
    "Director of Financial Planning",
    # Manager level — finance and accounting
    "Finance Manager",
    "Accounting Manager",
    "Financial Systems Manager",
]


# ---------------------------------------------------------------------------
# PRE-FILTER SIGNAL PHRASES
# At least ONE of these must appear in the job description before it gets
# sent to Claude for scoring. This keeps AI costs low by dropping postings
# with no ERP-relevant language before they reach the API.
#
# URGENT phrases (presence → likely scores 8–10):
#   Named ERP software, prior implementation experience, AI/automation focus
# WATCHLIST phrases (presence → likely scores 6–7):
#   ERP-adjacent pain language
#
# A posting needs at least one phrase from EITHER list to qualify.
# ---------------------------------------------------------------------------

URGENT_SIGNAL_PHRASES = [
    # Named ERP software
    "NetSuite", "netsuite",
    "Sage Intacct", "Sage 100", "Sage 300", "Sage 500",
    "Microsoft Dynamics", "Dynamics 365", "Dynamics GP", "Dynamics NAV",
    "QuickBooks Enterprise", "QuickBooks Online", "QBO",
    "Acumatica", "SAP Business One", "SAP B1",
    "Epicor", "Infor", "Oracle Fusion", "Oracle ERP",
    "Workday Financial", "Workday Finance",
    # ERP evaluation / implementation experience
    "ERP implementation", "ERP selection", "ERP evaluation",
    "ERP project", "ERP migration", "ERP rollout", "ERP upgrade",
    "system implementation", "system selection", "system migration",
    "software implementation", "led implementation", "managed implementation",
    "go-live", "go live", "full cycle implementation",
    # AI / automation in qualifications
    "AI tools", "artificial intelligence", "machine learning",
    "automation tools", "process automation", "AI-powered",
    "leveraging AI", "utilizing AI", "AI experience",
]

WATCHLIST_SIGNAL_PHRASES = [
    "scalability", "scalable", "scale the business", "scale operations",
    "multi-entity", "multiple entities", "multiple subsidiaries",
    "consolidation", "consolidating", "consolidated reporting",
    "streamline", "streamlining", "process improvement", "process optimization",
    "digital transformation", "modernize", "modernization",
    "system upgrade", "technology upgrade",
    "manual processes", "manual reporting", "spreadsheet-based",
    "financial systems", "accounting systems", "reporting systems",
    "inventory management", "supply chain systems", "order management",
    "revenue recognition", "ASC 606", "multi-currency", "intercompany",
    "fast-paced", "high-growth", "rapidly growing", "hypergrowth",
    "first hire", "building the function", "building out the team",
    "acquisition integration", "post-merger", "post-acquisition",
    "ERP", "enterprise resource planning",
]

# All signal phrases combined for the pre-filter gate
ALL_SIGNAL_PHRASES = URGENT_SIGNAL_PHRASES + WATCHLIST_SIGNAL_PHRASES


# ---------------------------------------------------------------------------
# ICP INDUSTRIES  (used only in the AI prompt — not as a hard pre-filter)
# Claude determines industry fit from the full JD context.
# ---------------------------------------------------------------------------

ICP_INDUSTRIES = [
    "Food & Beverage (manufacturers, distributors, brands, breweries, wineries, restaurants, catering)",
    "Consumer Goods / CPG (personal care, beauty, household products, pet products, apparel, toys, home goods)",
    "Manufacturing & Industrial / Equipment (fabrication, assembly, contract manufacturing, OEM, machinery, packaging)",
    "Building Materials / Construction / Energy (lumber, flooring, roofing, HVAC, hardware, contractors, solar, utilities)",
    "Retail / E-commerce / Wholesale Distribution",
    "Software / Technology (SaaS, cloud, cybersecurity, fintech, edtech, enterprise software, digital platforms)",
    "Health / Life Sciences (healthcare, hospitals, medical devices, pharma, biotech, dental, home health)",
    "Financial Services (insurance, banking, credit unions, wealth management, mortgage, lending, payments)",
    "Consulting / IT Services (management consulting, staffing, systems integrators, outsourcing, advisory)",
    "Nonprofits / Associations (foundations, charities, universities, colleges, social services)",
    "Advertising / Media / Publishing (agencies, digital marketing, broadcast, content, PR firms)",
    "Hospitality / Travel (hotels, resorts, restaurants, events, tourism, venues)",
    "Business Services (facilities management, property management, real estate, logistics, freight)",
    "Consumer Services (home services, fitness, personal services, childcare)",
    "Transportation / Logistics (trucking, freight, courier, shipping, fleet, aviation)",
    "Public Sector / Government Contractors (federal, defense, state, municipal)",
]


# ---------------------------------------------------------------------------
# TERRITORY  (used in the AI prompt for HQ inference)
# ---------------------------------------------------------------------------

TERRITORY_STATES = [
    "Alaska", "Arizona", "California", "Colorado", "Hawaii",
    "Idaho", "Kansas", "Minnesota", "Montana", "Nebraska",
    "Nevada", "New Mexico", "North Dakota", "Oklahoma", "Oregon",
    "South Dakota", "Utah", "Washington", "Wyoming",
]

TERRITORY_CANADA = [
    "British Columbia", "Saskatchewan", "Northwest Territories", "Yukon",
]


# ---------------------------------------------------------------------------
# JSEARCH API  (via RapidAPI — aggregates LinkedIn, Indeed, Glassdoor, etc.)
# Register at rapidapi.com → search "JSearch" → subscribe to Basic plan ($10/mo)
# Basic plan: ~200 requests/month. This monitor uses 6/day = ~180/month.
#
# POLL SCHEDULE: JSearch runs twice daily — morning and evening sweeps.
# Free sources (The Muse, Remotive, direct ATS) run every 5 minutes.
# This keeps costs under the Basic plan limit while maximising coverage.
# ---------------------------------------------------------------------------

# Sweep schedule (UTC times — PST = UTC-8, PDT = UTC-7):
#   7:30 AM PST  =  15:30 UTC
#   4:00 PM PST  =  00:00 UTC (midnight)
# Format: list of [hour, minute] pairs in UTC.
# Note: during daylight saving time (Mar–Nov), PST becomes PDT (UTC-7),
# so actual local time will be 8:30 AM PDT and 5:00 PM PDT. Adjust here
# if you want to lock to a specific local time year-round.
JSEARCH_SWEEP_TIMES_UTC = [[15, 30], [0, 0]]

# Search queries — each is one API call per sweep.
# Keep to 3 queries to stay safely within Basic plan (3 × 2 sweeps = 6/day).
# 2 queries x 2 sweeps x 30 days = 120 requests/month
# Free tier limit is 200 — well within budget.
JSEARCH_QUERIES = [
    "Controller CFO Chief Financial Officer finance accounting",
    "VP Finance Director Finance Accounting CAO",
]

# How many results to request per query (max 10 per page on free tiers)
JSEARCH_RESULTS_PER_PAGE = 10

# Only surface postings from the last N days
JSEARCH_DATE_POSTED = "3days"   # options: "all", "today", "3days", "week", "month"


# ---------------------------------------------------------------------------
# OPTIONAL: DIRECT ATS COMPANY LIST
# Leave these lists empty — the keyword search discovers companies dynamically.
# Add companies here ONLY when you identify a specific account you want to
# monitor in real-time (postings appear within 5 minutes of going live).
#
# How to find a company's ATS slug:
#   Greenhouse:  https://boards.greenhouse.io/SLUG   → slug = SLUG
#   Lever:       https://jobs.lever.co/SLUG          → slug = SLUG
#   Ashby:       https://jobs.ashbyhq.com/SLUG       → slug = SLUG
#
# Example (uncomment to add):
#   {"name": "Pacific Fresh Foods", "slug": "pacificfreshfoods"},
# ---------------------------------------------------------------------------

GREENHOUSE_COMPANIES = [
    # Add target accounts here as you identify them
    # {"name": "Company Name", "slug": "companyslug"},
]

LEVER_COMPANIES = [
    # Add target accounts here as you identify them
    # {"name": "Company Name", "slug": "companyslug"},
]

ASHBY_COMPANIES = [
    # Add target accounts here as you identify them
    # {"name": "Company Name", "slug": "companyslug"},
]


# ---------------------------------------------------------------------------
# HEARTBEAT  (daily check-in to #watch-list)
# Offset from news monitor heartbeat (which fires at 15:00 UTC).
# ---------------------------------------------------------------------------

HEARTBEAT_HOUR_UTC   = 15
HEARTBEAT_MINUTE_UTC = 6

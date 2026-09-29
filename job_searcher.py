"""Daily LinkedIn job search (remote worldwide/EU + Türkiye, past 24h, intern/entry) via Apify.

Writes:
  <output_dir>/daily/jobs_YYYY-MM-DD.xlsx  - today's jobs + today's skill demand
  <output_dir>/job_market_master.xlsx      - rolling window of all jobs + skill demand
                                             (point your Copilot agent at this file)
Needs the APIFY_TOKEN environment variable.
"""
import glob, json, os, re, sys, time, datetime as dt, urllib.request, urllib.parse, urllib.error, traceback
from collections import Counter
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import ColorScaleRule

HERE = os.path.dirname(os.path.abspath(__file__))
ACTOR = "curious_coder~linkedin-jobs-scraper"

# canonical skill -> (category, regex). Names must match cv_skills in config.json.
SKILLS = {
    "Python": ("Languages", r"\bpython\b"),
    "Java": ("Languages", r"\bjava\b(?!\s*script)"),
    "C": ("Languages", r"(?<![\w#+])c(?![\w#+])(?=\s*(?:,|/|\)|programming|dili|language))"),
    "C++": ("Languages", r"\bc\+\+"),
    "C#": ("Languages", r"\bc#"),
    "Go": ("Languages", r"\bgolang\b|\bgo\b(?=\s*(?:\(|,|/|programming|language|dili|developer))|\bgo programming"),
    "JavaScript": ("Languages", r"\bjavascript\b|\bjs\b"),
    "TypeScript": ("Languages", r"\btypescript\b"),
    "Kotlin": ("Languages", r"\bkotlin\b"),
    "Swift": ("Languages", r"\bswift\b|\bswiftui\b"),
    "Dart/Flutter": ("Languages", r"\bflutter\b|\bdart\b"),
    "PHP": ("Languages", r"\bphp\b"),
    "R": ("Languages", r"(?<![\w-])r(?=\s*(?:,|/|\)|programming|language))"),
    "MATLAB": ("Languages", r"\bmatlab\b"),
    "HTML/CSS": ("Languages", r"\bhtml5?\b|\bcss3?\b"),
    ".NET": ("Backend", r"\.net\b|asp\.net|dotnet"),
    "Spring Boot": ("Backend", r"\bspring\b"),
    "Node.js": ("Backend", r"\bnode\.?js\b|\bnestjs\b"),
    "Express": ("Backend", r"\bexpress(?:\.js)?\b"),
    "Django/Flask/FastAPI": ("Backend", r"\bdjango\b|\bflask\b|\bfastapi\b"),
    "REST API": ("Backend", r"\brest(?:ful)?\b|\bapi\b"),
    "Microservices": ("Backend", r"micro-?services?|mikroservis"),
    "Message queues (Kafka/RabbitMQ)": ("Backend", r"\bkafka\b|rabbitmq|message bus|\bredis\b"),
    "React": ("Frontend & Mobile", r"\breact(?:\.js|js)?\b(?!\s*native)"),
    "Next.js": ("Frontend & Mobile", r"\bnext\.?js\b"),
    "Angular/Vue": ("Frontend & Mobile", r"\bangular\b|\bvue(?:\.js)?\b"),
    "React Native": ("Frontend & Mobile", r"react native"),
    "Android/iOS native": ("Frontend & Mobile", r"\bandroid\b|\bios\b|jetpack compose"),
    "Unity": ("Frontend & Mobile", r"\bunity\b"),
    "SQL": ("Data", r"\bsql\b|t-sql|pl/sql|stored procedure"),
    "SQL Server": ("Data", r"sql server|\bmssql\b|\bt-sql\b"),
    "PostgreSQL": ("Data", r"postgre(?:s|sql)"),
    "MySQL": ("Data", r"\bmysql\b"),
    "Oracle": ("Data", r"\boracle\b"),
    "NoSQL (MongoDB etc.)": ("Data", r"\bnosql\b|mongo"),
    "Firebase": ("Data", r"\bfirebase\b"),
    "ETL": ("Data", r"\betl\b|\belt\b|data pipeline|veri hatt"),
    "Data Warehouse": ("Data", r"data ?warehouse|\bdwh\b|veri ambar"),
    "Spark/Big Data": ("Data", r"\bspark\b|hadoop|databricks|big data|büyük veri"),
    "Airflow/dbt": ("Data", r"\bairflow\b|\bdbt\b"),
    "Azure Data Factory/Fabric": ("Data", r"data factory|\bfabric\b|synapse"),
    "Power BI": ("Data", r"power ?bi"),
    "Tableau/Qlik": ("Data", r"tableau|qlik"),
    "Excel": ("Data", r"\bexcel\b|pivot"),
    "Pandas": ("Data", r"\bpandas\b"),
    "NumPy": ("Data", r"\bnumpy\b"),
    "Data Visualization": ("Data", r"dashboard|visuali[sz]ation|görselleştir|raporlama|reporting"),
    "Statistics / A-B testing": ("Data", r"statistic|istatisti|a/b test"),
    "Dash/Plotly": ("Data", r"\bplotly\b|\bdash\b"),
    "Web Scraping": ("Data", r"scrap(?:ing|er)"),
    "Machine Learning": ("AI/ML", r"machine learning|makine öğrenme|\bml\b|predictive|xgboost|scikit"),
    "Deep Learning (PyTorch/TF)": ("AI/ML", r"deep learning|derin öğrenme|pytorch|tensorflow|keras"),
    "Computer Vision": ("AI/ML", r"computer vision|görüntü işleme|\bocr\b|opencv"),
    "Multi-agent / LLM": ("AI/ML", r"\bllm|large language|generative ai|genai|\brag\b|prompt|agent|openai|langchain|yapay zeka"),
    "XGBoost": ("AI/ML", r"xgboost"),
    "Pydantic": ("AI/ML", r"pydantic"),
    "AWS": ("Cloud & DevOps", r"\baws\b|amazon web services"),
    "Azure": ("Cloud & DevOps", r"\bazure\b"),
    "GCP": ("Cloud & DevOps", r"\bgcp\b|google cloud|bigquery"),
    "Docker": ("Cloud & DevOps", r"\bdocker\b|container"),
    "Kubernetes": ("Cloud & DevOps", r"kubernetes|\bk8s\b"),
    "CI/CD": ("Cloud & DevOps", r"ci/cd|continuous integration|github actions|jenkins|gitlab ci"),
    "Linux": ("Cloud & DevOps", r"\blinux\b|\bbash\b|shell script"),
    "Git": ("Tools & Practices", r"\bgit\b|github|gitlab|version control|versiyon kontrol"),
    "Jira": ("Tools & Practices", r"\bjira\b"),
    "Agile/Scrum": ("Tools & Practices", r"\bagile\b|\bscrum\b|çevik"),
    "Testing": ("Tools & Practices", r"unit test|test[- ]driven|\btdd\b|automated test|test otomasyon"),
    "OOP / Design Patterns": ("Tools & Practices", r"\boop\b|object[- ]oriented|nesne yönelimli|design pattern"),
    "UML": ("Tools & Practices", r"\buml\b"),
    "Figma": ("Tools & Practices", r"\bfigma\b"),
    "SAP": ("Tools & Practices", r"\bsap\b|\babap\b"),
    "English": ("Languages (spoken)", r"english|ingilizce"),
}
SKILL_RE = {k: re.compile(v[1], re.I) for k, v in SKILLS.items()}
YEARS_RE = re.compile(r"(?:minimum|min\.?|at least|en az)?\s*(\d{1,2})\s*(?:\+|-\s*\d+)?\s*(?:\+\s*)?(?:years?|yrs?|yıl)", re.I)
ENTRY_TITLE_RE = re.compile(r"intern|stajyer|staj|junior|jr\.?|new grad|graduate|trainee|gennext|yeni mezun|aday|yardımcı|assistant|working student", re.I)


def log(msg):
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(os.path.join(HERE, "logs", "run.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_region(cfg, region, token):
    urls = [
        "https://www.linkedin.com/jobs/search/?keywords=" + urllib.parse.quote(q)
        + f"&geoId={region['geo_id']}&location=" + urllib.parse.quote(region["name"])
        + "&f_TPR=r86400&f_E=1%2C2" + ("&f_WT=2" if region.get("remote_only") else "")
        for q in cfg["searches"]
    ]
    body = json.dumps({"urls": urls, "limitPerSource": region["limit_per_search"],
                       "scrapeCompany": False, "autoConvertToAiSearch": True}).encode()
    req = urllib.request.Request(
        f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items?clean=1&timeout=600",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=660) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
            log(f"  HTTP {e.code}, retrying in 30s...")
            time.sleep(30)


def fetch_himalayas(cfg):
    """Remote jobs from himalayas.app that accept applicants living in cfg['himalayas']['country'].
    Free public API; returned in the same shape as the LinkedIn scraper's items."""
    h = cfg["himalayas"]
    cutoff = time.time() - h["window_hours"] * 3600
    jobs = {}
    for q in h.get("searches") or cfg["searches"]:
        url = "https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode(
            {"q": q, "seniority": "entry-level", "country": h["country"], "sort": "recent"})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal job tracker)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            items = json.load(r).get("jobs", [])
        for j in items:
            if j.get("pubDate", 0) < cutoff:
                continue
            locs = j.get("locationRestrictions") or []
            if not locs:
                location = "Worldwide"
            elif len(locs) <= 3:
                location = ", ".join(locs)
            else:
                location = f"{len(locs)} countries incl. Türkiye (Europe/EMEA)"
            sal = ""
            if j.get("minSalary"):
                sal = f"{j['minSalary']:,}–{j.get('maxSalary') or j['minSalary']:,} {j.get('currency') or ''}/{j.get('salaryPeriod') or 'year'}"
            desc = re.sub(r"<[^>]+>", " ", j.get("description") or "")
            jobs[j["guid"]] = {
                "id": j["guid"], "title": j.get("title"), "companyName": j.get("companyName"),
                "location": location, "postedAt": time.strftime("%Y-%m-%d", time.gmtime(j["pubDate"])),
                "employmentType": j.get("employmentType"), "seniorityLevel": ", ".join(j.get("seniority") or []),
                "salary": sal, "descriptionText": desc, "_link": j.get("applicationLink") or j["guid"],
                "_remote": True,
            }
        time.sleep(1)
    return list(jobs.values())


def years_required(text):
    yrs = [int(m.group(1)) for m in YEARS_RE.finditer(text) if 0 < int(m.group(1)) <= 15]
    return min(yrs) if yrs else None


# who a posting says it can hire; checked against cfg["work_authorization"]
RESTRICTIONS = {
    "United States": r"(?:authori[sz]ed|authori[sz]ation|eligible|legally able) to work in the (?:us\b|u\.s\.|united states)|"
                     r"(?:must|need to) (?:be )?(?:located|reside|residing|based|live) in the (?:us\b|u\.s\.|united states)|"
                     r"\bu\.?s\.? citizen|green card|\bus[- ]based (?:candidates|applicants)|\bw-?2\b",
    "United Kingdom": r"right to work in the uk|(?:located|reside|based) in the uk\b|uk residents? only",
    "EU": r"(?:eu|european) (?:work permit|citizen)|right to work in (?:the )?(?:eu|european union|germany|netherlands|spain|poland)|"
          r"(?:located|reside|based) in (?:the )?(?:eu|european union)\b",
    "Canada": r"(?:authori[sz]ed|eligible) to work in canada|(?:located|reside|based) in canada",
    "India": r"(?:located|reside|based) in india|india only",
}
RESTRICTION_RE = {k: re.compile(v, re.I) for k, v in RESTRICTIONS.items()}
NO_SPONSOR_RE = re.compile(r"(?:not|unable to|cannot|can't|won't|will not) (?:provide|offer|sponsor)[^.]{0,20}(?:visa|sponsorship)|no visa sponsorship", re.I)
REMOTE_RE = re.compile(r"\bremote\b|uzaktan|work from (?:home|anywhere)|fully distributed", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b|hibrit", re.I)


RED_FLAGS = {
    "Unpaid": r"\bunpaid\b|no salary|without (?:pay|salary)",
    "Stipend only on performance": r"performance[- ]based stipend|stipend (?:based on|depending on) performance",
    "Certificate/LOR as the reward": r"certificate of (?:completion|internship)|letter of recommendation|internship certificate",
    "Asks you to pay": r"(?:registration|training|enrol?ment|security) (?:fee|charge)|pay (?:a |the )?fee",
}
RED_FLAG_RE = {k: re.compile(v, re.I) for k, v in RED_FLAGS.items()}


def red_flags(text):
    return [k for k, rx in RED_FLAG_RE.items() if rx.search(text)]


def is_mill(flags):
    """Internship-mill pattern: unpaid / pay-to-join, or two softer signals together."""
    return "Unpaid" in flags or "Asks you to pay" in flags or len(flags) >= 2


def restrictions(text, job, wtype, cfg):
    """Returns (list of restrictions, score penalty)."""
    ok = set(cfg.get("work_authorization", []))
    found = [f"{k} work authorization" for k, rx in RESTRICTION_RE.items() if k not in ok and rx.search(text)]
    penalty = 30 if found else 0
    # On LinkedIn a remote job's location is normally the country you must live in.
    loc = job.get("location") or ""
    if not any(t.lower() in loc.lower() for t in cfg.get("open_locations", [])):
        country = loc.split(",")[-1].strip() or "unknown"
        if wtype.startswith("Remote"):
            found.append(f"Remote within {country} (likely)")
            penalty = max(penalty, 20)
        else:
            found.append(f"On-site in {country} (visa/relocation)")
            penalty = max(penalty, 30)
    if NO_SPONSOR_RE.search(text):
        found.append("No visa sponsorship")
    return found, penalty


def work_type(job):
    head = f"{job.get('title') or ''} {job.get('location') or ''}"
    desc = job.get("descriptionText") or ""
    if job.get("_remote") or REMOTE_RE.search(head) or (job.get("_region_remote") and REMOTE_RE.search(desc)):
        return "Remote"
    if HYBRID_RE.search(head) or HYBRID_RE.search(desc):
        return "Hybrid"
    if REMOTE_RE.search(desc):
        return "Remote?"
    return "On-site"


def analyse(job, cfg):
    title = job.get("title") or ""
    text = f"{title}\n{job.get('descriptionText') or ''}"
    wtype = work_type(job)
    restr, penalty = restrictions(text, job, wtype, cfg)
    found = [s for s, rx in SKILL_RE.items() if rx.search(text)]
    have = set(cfg["cv_skills"])
    matched = [s for s in found if s in have]
    missing = [s for s in found if s not in have]
    yrs = years_required(text)

    # smoothed so a posting that names only 1-2 skills can't score a perfect match
    skill_pct = 100 * (len(matched) + 1) / (len(found) + 2)
    seniority = job.get("seniorityLevel") or ""
    if ENTRY_TITLE_RE.search(title):
        level = 35
    elif seniority in ("Internship", "Entry level"):
        level = 30
    else:
        level = 20
    if yrs is not None and yrs >= 2:
        level -= 10 * (yrs - 1)
    # a job you aren't legally allowed to take isn't a match, however good the skills
    if wtype == "Remote":
        level += 5
    score = max(0, min(100, round(0.65 * skill_pct + level - penalty)))
    return {"skills_found": found, "matched": matched, "missing": missing, "years": yrs, "score": score,
            "restrictions": restr, "work_type": wtype, "red_flags": red_flags(text)}


TECH_TITLE_RE = re.compile(r"software|yazılım|developer|geliştirici|back-?end|front-?end|full ?stack|data engineer|"
                           r"data analyst|data scien|veri|machine learning|devops|mobile|business intelligence|"
                           r"\bbi\b|analytics|programmer|\bai\b|\bml\b", re.I)
NON_TECH_SKILLS = {"English", "Excel", "Data Visualization", "Statistics / A-B testing", "Agile/Scrum", "Jira",
                   "Figma", "UML", "SAP", "REST API"}


def relevant(job, cfg):
    t = (job.get("title") or "").lower() + " "
    company = (job.get("companyName") or "").lower()
    if any(c.lower() in company for c in cfg.get("exclude_companies", [])):
        return False
    if job.get("seniorityLevel") in cfg["exclude_seniority"] and not ENTRY_TITLE_RE.search(t):
        return False
    if any(k in t for k in cfg["exclude_title_keywords"]):
        return False
    if not any(k in t for k in cfg["include_title_keywords"]):
        return False
    if TECH_TITLE_RE.search(t):
        return True
    # generic titles (intern, trainee, mühendis, analyst...) must show real tech content
    desc = job.get("descriptionText") or ""
    return sum(1 for s, rx in SKILL_RE.items() if s not in NON_TECH_SKILLS and rx.search(desc)) >= 3


# ---------- Excel ----------
HDR_FILL = PatternFill("solid", fgColor="1F3A5F")
THIN = Side(style="thin", color="D0D5DD")
# (column name, width)
JOB_COLS = [("Run Date", 11), ("Region", 13), ("Job Title", 36), ("Company Name", 24), ("Location", 22),
            ("Work Type", 10), ("Open To You?", 10), ("Location Restrictions", 24), ("Red Flags", 16), ("Date Posted", 11), ("Employment Type", 13),
            ("Seniority Level", 14), ("Salary", 13), ("Direct Application Link", 45), ("Match Score (/100)", 11),
            ("Years Required", 10), ("Skills You Have", 36), ("Skills To Learn", 36), ("Job ID", 12),
            ("Job Description", 90)]
COL = {name: i for i, (name, _) in enumerate(JOB_COLS)}


def style_header(ws, widths, freeze="D2"):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = HDR_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = freeze


def job_row(run_date, job, a):
    return {
        "Run Date": run_date, "Region": job.get("_region"), "Job Title": job.get("title"),
        "Company Name": job.get("companyName"), "Location": job.get("location"), "Work Type": a["work_type"],
        "Open To You?": "No" if any(r != "No visa sponsorship" for r in a["restrictions"]) else "Yes",
        "Location Restrictions": ", ".join(a["restrictions"]) or "None found",
        "Red Flags": ", ".join(a["red_flags"]),
        "Date Posted": job.get("postedAt"), "Employment Type": job.get("employmentType"),
        "Seniority Level": job.get("seniorityLevel"), "Salary": job.get("salary") or "Not disclosed",
        "Direct Application Link": job.get("_link") or f"https://www.linkedin.com/jobs/view/{job['id']}/",
        "Match Score (/100)": a["score"], "Years Required": a["years"] if a["years"] is not None else "-",
        "Skills You Have": ", ".join(a["matched"]), "Skills To Learn": ", ".join(a["missing"]),
        "Job ID": str(job["id"]), "Job Description": " ".join((job.get("descriptionText") or "").split())[:8000],
    }


def write_jobs_sheet(ws, rows):
    ws.append([n for n, _ in JOB_COLS])
    for r in sorted(rows, key=lambda r: (str(r["Run Date"]), r.get("Open To You?") == "Yes",
                                         r["Match Score (/100)"] or 0), reverse=True):
        ws.append([r.get(n) for n, _ in JOB_COLS])
    style_header(ws, [w for _, w in JOB_COLS])
    link, score, desc = COL["Direct Application Link"], COL["Match Score (/100)"], COL["Job Description"]
    restr, open_ = COL["Location Restrictions"], COL["Open To You?"]
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=c.column != desc + 1)
            c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
        row[link].hyperlink = row[link].value; row[link].font = Font(color="0563C1", underline="single")
        row[score].font = Font(bold=True)
        if row[restr].value not in (None, "", "None found"):
            row[restr].font = Font(color="B42318", bold=True)
        if row[open_].value == "Yes":
            row[open_].font = Font(color="1E7B34", bold=True)
            row[open_].fill = PatternFill("solid", fgColor="E3F4E8")
    n = ws.max_row
    sc = ws.cell(row=1, column=score + 1).column_letter
    ws.conditional_formatting.add(f"{sc}2:{sc}{n}", ColorScaleRule(
        start_type="num", start_value=30, start_color="F8696B", mid_type="num", mid_value=55,
        mid_color="FFEB84", end_type="num", end_value=80, end_color="63BE7B"))
    ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(JOB_COLS)).column_letter}{n}"


def write_skill_sheet(ws, rows, cfg):
    have = set(cfg["cv_skills"])
    total, counts, remote = max(len(rows), 1), Counter(), Counter()
    for r in rows:
        for s in filter(None, f"{r['Skills You Have'] or ''}, {r['Skills To Learn'] or ''}".split(", ")):
            counts[s] += 1
            if r["Work Type"] == "Remote":
                remote[s] += 1
    ws.append(["Skill", "Category", "Jobs Asking", "% of Jobs", "Remote Jobs Asking", "On Your CV?", "Priority"])
    for skill, n in counts.most_common():
        pct = n / total
        on_cv = skill in have
        prio = "Keep sharp" if on_cv else ("HIGH - learn next" if pct >= 0.20 else "MEDIUM" if pct >= 0.08 else "Low")
        ws.append([skill, SKILLS[skill][0], n, round(pct, 3), remote[skill], "Yes" if on_cv else "No", prio])
    style_header(ws, [30, 20, 12, 11, 14, 12, 18], freeze="A2")
    for row in ws.iter_rows(min_row=2):
        row[3].number_format = "0%"
        if row[6].value.startswith("HIGH"):
            for c in row: c.fill = PatternFill("solid", fgColor="FDE2E1")
        elif row[5].value == "Yes":
            row[5].font = Font(color="1E7B34", bold=True)
    ws.auto_filter.ref = f"A1:G{ws.max_row}"


def write_about(ws, cfg, rows, window_label):
    by_region = Counter(r["Region"] for r in rows)
    by_type = Counter(r["Work Type"] for r in rows)
    lines = [
        "LinkedIn job market tracker - internship/entry level, Türkiye + remote worldwide",
        f"Last updated: {dt.datetime.now():%Y-%m-%d %H:%M}   |   Jobs in this file: {len(rows)}   |   Window: {window_label}",
        "Jobs per region: " + ", ".join(f"{k}: {v}" for k, v in by_region.most_common()),
        "Jobs per work type: " + ", ".join(f"{k or 'unknown'}: {v}" for k, v in by_type.most_common()),
        "Source: Apify actor curious_coder/linkedin-jobs-scraper, LinkedIn public search, posted in the past 24h, Internship + Entry level.",
        "Regions searched: " + "; ".join(f"{r['name']}{' (remote only)' if r.get('remote_only') else ''}" for r in cfg["regions"]),
        "Searches: " + ", ".join(cfg["searches"]),
        "Work authorization assumed: " + ", ".join(cfg.get("work_authorization", [])),
        "Sheet 'Jobs': one row per posting. 'Skills You Have' / 'Skills To Learn' are keywords detected in the description, compared with the CV skill list in config.json.",
        "'Location Restrictions': 'Remote within X (likely)' = LinkedIn lists a remote job under the country you must live in (-20 points). "
        "'On-site in X' = needs relocation and a work visa (-30). 'X work authorization' = the posting says so explicitly (-30). "
        "Jobs located in " + ", ".join(cfg.get("open_locations", [])) + " count as open to you.",
        "'Open To You?' = Yes when nothing suggests you'd need another country's work permit or residence. Jobs are sorted Yes first, then by Match Score.",
        "'Red Flags': internship-mill signals. Unpaid, pay-to-join, or 2+ signals are dropped automatically; single signals are shown here.",
        "'Work Type' Remote? = remote is mentioned in the description but not confirmed in the title/location.",
        "Sheet 'Skill Demand': how many postings ask for each skill. Priority HIGH = not on the CV and asked for by 20%+ of postings.",
        "Match Score = 65% skill overlap + up to 35% for entry-level fit (+5 if remote), minus 10 per required year beyond 1, minus the location penalty above. Keyword-based, so treat it as a first filter.",
    ]
    for l in lines: ws.append([l])
    ws["A1"].font = Font(bold=True, size=13); ws.column_dimensions["A"].width = 140


def build_workbook(path, rows, cfg, window_label):
    wb = Workbook()
    write_jobs_sheet(wb.active, rows); wb.active.title = "Jobs"
    write_skill_sheet(wb.create_sheet("Skill Demand"), rows, cfg)
    write_about(wb.create_sheet("About"), cfg, rows, window_label)
    tmp = path + ".tmp.xlsx"
    wb.save(tmp)
    try:
        os.replace(tmp, path)
        return True
    except PermissionError:  # usually the file is open in Excel
        alt = path.replace(".xlsx", f"_{dt.datetime.now():%H%M}.xlsx")
        os.replace(tmp, alt)
        log(f"WARNING: {os.path.basename(path)} is locked (open in Excel?). Saved as {alt} instead; "
            "the next run merges it back.")
        return False


def load_master_rows(path):
    """Rows as dicts keyed by header, so files written with older column layouts still merge."""
    if not os.path.exists(path):
        return []
    wb = load_workbook(path, read_only=True)
    try:
        it = wb["Jobs"].iter_rows(values_only=True)
        header = next(it)
        rows = [dict(zip(header, r)) for r in it if r[0]]
    finally:
        wb.close()  # read-only mode keeps the file handle open, which blocks replacing it on Windows
    for r in rows:
        r.setdefault("Region", "Türkiye")
        r.setdefault("Work Type", "")
        r.setdefault("Location Restrictions", "")
        r.setdefault("Red Flags", "")
        if not r.get("Open To You?") and r.get("Location Restrictions"):
            restr = [x.strip() for x in r["Location Restrictions"].split(",")]
            r["Open To You?"] = "Yes" if all(x in ("None found", "No visa sponsorship") for x in restr) else "No"
    return rows


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        log("ERROR: APIFY_TOKEN environment variable is not set."); sys.exit(1)
    out = os.path.join(HERE, cfg["output_dir"]) if not os.path.isabs(cfg["output_dir"]) else cfg["output_dir"]
    os.makedirs(os.path.join(out, "daily"), exist_ok=True)
    today = dt.date.today().isoformat()

    master = os.path.join(out, "job_market_master.xlsx")
    # if Excel had the master open last time, the newest data is in a job_market_master_HHMM.xlsx side file
    side = sorted(glob.glob(os.path.join(out, "job_market_master_*.xlsx")), key=os.path.getmtime)
    newest = max([master] + side, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    master_rows = load_master_rows(newest)
    # jobs recorded on earlier days are skipped, so each daily file only shows new postings
    known = {str(r["Job ID"]) for r in master_rows if str(r["Run Date"]) < today}

    sources = [(r["name"], r.get("remote_only", False), lambda r=r: fetch_region(cfg, r, token))
               for r in cfg["regions"]]
    if cfg.get("himalayas", {}).get("enabled"):
        sources.append(("Remote (Himalayas)", True, lambda: fetch_himalayas(cfg)))

    seen, jobs, n_raw = set(known), [], 0
    for name, remote_only, fetch in sources:
        log(f"Searching {name}{' (remote only)' if remote_only else ''}...")
        try:
            raw = fetch()
        except Exception as e:  # one failing source shouldn't lose the others
            log(f"WARNING: {name} search failed: {e}")
            continue
        n_raw += len(raw)
        kept = mills = not_remote = 0
        for j in raw:
            if not j.get("id") or str(j["id"]) in seen or not relevant(j, cfg):
                continue
            j["_region"], j["_region_remote"] = name, remote_only
            if cfg.get("drop_internship_mills", True) and is_mill(red_flags(j.get("descriptionText") or "")):
                mills += 1; continue
            # LinkedIn's remote filter is only a hint in AI search, so enforce it here
            if remote_only and not work_type(j).startswith("Remote"):
                not_remote += 1; continue
            seen.add(str(j["id"])); jobs.append(j); kept += 1
        log(f"  {name}: {len(raw)} postings, {kept} kept "
            f"({mills} internship mills and {not_remote} non-remote dropped)")
    if not n_raw:
        log("ERROR: no postings returned from any source; keeping previous files."); sys.exit(1)
    log(f"Total: {n_raw} postings; {len(jobs)} new and relevant after filtering and de-duplication.")

    rows = [job_row(today, j, analyse(j, cfg)) for j in jobs]
    daily = os.path.join(out, "daily", f"jobs_{today}.xlsx")
    build_workbook(daily, rows, cfg, f"{today} only")

    cutoff = (dt.date.today() - dt.timedelta(days=cfg["master_window_days"])).isoformat()
    new_ids = {r["Job ID"] for r in rows}
    old = [r for r in master_rows
           if cutoff <= str(r["Run Date"]) < today and str(r["Job ID"]) not in new_ids]
    if build_workbook(master, old + rows, cfg, f"last {cfg['master_window_days']} days"):
        for p in side:  # their rows are now in the master
            os.remove(p)
    log(f"Saved {daily} and updated master ({len(old) + len(rows)} jobs).")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR:\n" + traceback.format_exc()); sys.exit(1)

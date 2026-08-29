import os
import io
import re
import time
import socket
import smtplib
import json
import urllib.request
import dns.resolver
from flask import Flask, render_template_string, request
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4MB upload limit

# Get API keys from environment variables (NEVER hardcode)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Production settings
DEBUG = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
PORT = int(os.environ.get("PORT", 5051))

# ---------------------------------------------------------------------------
# LinkedIn to Email Functions
# ---------------------------------------------------------------------------
def extract_name_from_linkedin(linkedin_url):
    """Extract name from LinkedIn URL or using basic pattern matching"""
    url = linkedin_url.strip()

    # Try to extract from URL pattern
    pattern = r'linkedin\.com/in/([^/?#]+)'
    match = re.search(pattern, url)

    if match:
        profile_id = match.group(1)
        name_part = re.sub(r'[-]\d+$', '', profile_id)
        name_part = name_part.replace('-', ' ')
        return name_part.title()

    return None

def extract_company_from_linkedin(linkedin_url):
    """Extract company name from LinkedIn URL"""
    company_match = re.search(r'linkedin\.com/company/([^/?#]+)', linkedin_url)
    if company_match:
        company_name = company_match.group(1)
        company_name = company_name.replace('-', '')
        return company_name.lower()

    company_match2 = re.search(r'linkedin\.com/in/[^/]+/([^/?#]+)', linkedin_url)
    if company_match2:
        company_name = company_match2.group(1)
        company_name = company_name.replace('-', '')
        return company_name.lower()

    return None

def detect_company_domain_from_url(linkedin_url):
    """Try to detect the company domain from LinkedIn URL"""
    company_name = extract_company_from_linkedin(linkedin_url)

    if not company_name:
        return None

    company_name = re.sub(r'[^a-zA-Z0-9]', '', company_name)

    domain_candidates = [
        f"{company_name}.com",
        f"{company_name}corp.com",
        f"{company_name}inc.com",
        f"{company_name}company.com",
        f"{company_name}corp.org",
        f"{company_name}.io",
    ]

    for candidate in domain_candidates[:3]:
        mx = get_mx_record(candidate, timeout=3)
        if mx:
            return candidate

    return f"{company_name}.com"

def linkedin_to_email(linkedin_url, company_domain=None):
    """Find email from LinkedIn URL"""
    full_name = extract_name_from_linkedin(linkedin_url)

    if not full_name:
        return None, "Could not extract name from LinkedIn URL. Please check the URL format."

    first, last = split_full_name(full_name)

    if not first or not last:
        return None, f"Could not parse name: {full_name}"

    if not company_domain:
        company_domain = detect_company_domain_from_url(linkedin_url)
        if company_domain:
            return None, f"Detected possible company domain: {company_domain}. Please confirm or enter the correct domain."
        else:
            return None, "Could not detect company domain from URL. Please enter the company domain."

    domain, mx_server = resolve_domain_and_mx(company_domain)

    if not mx_server:
        return None, f"No mail server found for '{company_domain}' or its common TLD variants"

    if is_catch_all(mx_server, domain):
        candidates = generate_permutations(first, last, domain)
        guess = candidates[0] if candidates else "unknown"
        return None, f"Domain {domain} is catch-all — can't confirm reliably. Best guess: {guess}"

    found_email = find_email(first, last, mx_server, domain)

    if found_email:
        return found_email, None

    return None, f"No valid email found for {full_name} on {domain}"

# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------
def generate_permutations(first, last, domain):
    f = re.sub(r"[^a-z]", "", first.lower().strip())
    l = re.sub(r"[^a-z]", "", last.lower().strip())
    d = domain.lower().strip()
    if not f and not l:
        return []
    if not l:
        return [f"{f}@{d}"]
    return [
        f"{f}.{l}@{d}",
        f"{f}{l}@{d}",
        f"{f[0]}{l}@{d}",
        f"{f}{l[0]}@{d}",
        f"{f}@{d}",
        f"{l}@{d}",
        f"{f}_{l}@{d}",
        f"{l}.{f}@{d}",
    ]

def get_mx_record(domain, timeout=5):
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        records = resolver.resolve(domain, "MX")
        mx_records = sorted(records, key=lambda r: r.preference)
        return str(mx_records[0].exchange).rstrip(".")
    except Exception:
        return None

def verify_email_smtp(mx_server, email, timeout=4):
    try:
        server = smtplib.SMTP(mx_server, 25, timeout=timeout)
        server.helo("localhost")
        server.mail("verify@local-tool.com")
        code, _ = server.rcpt(email)
        server.quit()
        return code == 250
    except Exception:
        return False

def is_catch_all(mx_server, domain):
    fake = f"completelyfake123456789@{domain}"
    return verify_email_smtp(mx_server, fake)

def find_email(first, last, mx_server, domain, delay=0.4):
    for email in generate_permutations(first, last, domain):
        if verify_email_smtp(mx_server, email):
            return email
        time.sleep(delay)
    return None

def clean_domain(raw):
    raw = raw.strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^www\.", "", raw)
    return raw.split("/")[0]

def generate_domain_variants(raw_input):
    base = clean_domain(raw_input)
    if not base:
        return []

    root = base.split(".")[0]
    common_tlds = ["com", "co.in", "in", "org", "net", "io", "co", "biz", "us"]

    variants = []
    seen = set()

    if base not in seen:
        variants.append(base)
        seen.add(base)

    for tld in common_tlds:
        candidate = f"{root}.{tld}"
        if candidate not in seen:
            variants.append(candidate)
            seen.add(candidate)

    return variants

def resolve_domain_and_mx(raw_input, timeout=5):
    for candidate in generate_domain_variants(raw_input):
        mx = get_mx_record(candidate, timeout=timeout)
        if mx:
            return candidate, mx
    return None, None

def split_full_name(full_name):
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]

# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verifi — Email Finder</title>
<style>
    :root {
        --bg: #0f1115;
        --panel: #171a21;
        --panel-2: #1e2229;
        --border: #2a2f3a;
        --text: #e8eaed;
        --muted: #8b93a1;
        --accent: #4f7cff;
        --accent-2: #3a63d9;
        --success: #2ecc71;
        --success-bg: rgba(46, 204, 113, 0.12);
        --error: #ff5c6c;
        --error-bg: rgba(255, 92, 108, 0.12);
        --warn: #f5b942;
        --warn-bg: rgba(245, 185, 66, 0.12);
    }
    * { box-sizing: border-box; }
    @keyframes fadeInUp {
        from { opacity: 0; transform: translateY(14px); }
        to { opacity: 1; transform: translateY(0); }
    }
    @keyframes floatGlow {
        0%, 100% { transform: translateY(0px); }
        50% { transform: translateY(-6px); }
    }
    @keyframes shimmer {
        0% { background-position: -200% 0; }
        100% { background-position: 200% 0; }
    }
    body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background: radial-gradient(circle at 20% 0%, #161a22 0%, var(--bg) 55%);
        color: var(--text);
        min-height: 100vh;
        padding: 48px 20px;
    }
    .wrap { max-width: 680px; margin: 0 auto; }
    .brand {
        display: flex; align-items: center; gap: 10px;
        margin-bottom: 28px;
        animation: fadeInUp 0.5s ease both;
    }
    .brand-mark {
        width: 36px; height: 36px; border-radius: 10px;
        background: linear-gradient(135deg, var(--accent), var(--accent-2));
        display: flex; align-items: center; justify-content: center;
        font-size: 18px;
        animation: floatGlow 4s ease-in-out infinite;
        box-shadow: 0 8px 24px -8px rgba(79,124,255,0.6);
    }
    .brand-name { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }
    .brand-sub { color: var(--muted); font-size: 13px; margin-top: -2px; }

    .card {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 28px;
        box-shadow: 0 20px 40px -20px rgba(0,0,0,0.5);
        animation: fadeInUp 0.6s ease 0.1s both;
    }
    .tabs {
        display: flex; gap: 4px; margin-bottom: 24px;
        background: var(--panel-2); border-radius: 10px; padding: 4px;
    }
    .tab {
        flex: 1; text-align: center; padding: 10px 12px;
        border-radius: 8px; font-size: 14px; font-weight: 600;
        color: var(--muted); text-decoration: none; cursor: pointer;
        transition: all 0.2s ease;
    }
    .tab:hover { color: var(--text); background: rgba(255,255,255,0.04); }
    .tab.active { background: var(--accent); color: white; box-shadow: 0 6px 16px -6px rgba(79,124,255,0.7); }

    h2 { font-size: 17px; margin: 0 0 4px 0; }
    .hint { color: var(--muted); font-size: 13px; margin-bottom: 20px; }

    label {
        display: block; font-size: 13px; font-weight: 600;
        color: var(--muted); margin: 16px 0 6px 0;
    }
    input[type="text"], input[type="file"], input[type="url"] {
        width: 100%; padding: 12px 14px;
        background: var(--panel-2); border: 1px solid var(--border);
        border-radius: 9px; color: var(--text); font-size: 14px;
        outline: none; transition: border-color 0.15s ease;
    }
    input[type="text"]:focus, input[type="url"]:focus { border-color: var(--accent); }
    input[type="file"] { padding: 10px 12px; cursor: pointer; }

    .row { display: flex; gap: 12px; }
    .row > div { flex: 1; }

    input[type="submit"], .btn {
        width: 100%; margin-top: 22px;
        background: var(--accent); color: white;
        padding: 13px; border: none; border-radius: 9px;
        font-size: 15px; font-weight: 600; cursor: pointer;
        transition: background 0.15s ease;
    }
    input[type="submit"]:hover, .btn:hover { background: var(--accent-2); }

    .result {
        margin-top: 20px; padding: 14px 16px;
        border-radius: 9px; font-weight: 600; font-size: 14px;
        animation: fadeInUp 0.4s ease both;
    }
    .success { background: var(--success-bg); color: var(--success); border: 1px solid rgba(46,204,113,0.3); }
    .error { background: var(--error-bg); color: var(--error); border: 1px solid rgba(255,92,108,0.3); }
    .warn { background: var(--warn-bg); color: var(--warn); border: 1px solid rgba(245,185,66,0.3); }

    table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px; animation: fadeInUp 0.4s ease both; }
    th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
    tr:hover td { background: rgba(255,255,255,0.03); }
    td.email { color: var(--success); font-weight: 600; }
    .summary { color: var(--muted); font-size: 13px; margin-top: 12px; }

    .email-output {
        margin-top: 20px; padding: 18px; background: var(--panel-2);
        border: 1px solid var(--border); border-radius: 10px;
        font-size: 13.5px; line-height: 1.6; white-space: pre-wrap;
        animation: fadeInUp 0.4s ease both;
        max-height: 420px; overflow-y: auto;
    }
    .loading-bar {
        height: 3px; width: 100%; margin-top: 4px; border-radius: 3px; overflow: hidden;
        background: linear-gradient(90deg, var(--panel-2) 25%, var(--accent) 50%, var(--panel-2) 75%);
        background-size: 200% 100%; animation: shimmer 1.4s linear infinite;
    }

    .footnote { color: var(--muted); font-size: 12px; margin-top: 22px; line-height: 1.6; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
    <div class="brand">
        <div class="brand-mark">📧</div>
        <div>
            <div class="brand-name">E-Verifi</div>
            <div class="brand-sub">Company email finder &amp; verifier</div>
        </div>
    </div>

    <div class="card">
        <div class="tabs">
            <a class="tab {{ 'active' if mode == 'single' else '' }}" href="/">Single Lookup</a>
            <a class="tab {{ 'active' if mode == 'linkedin' else '' }}" href="/linkedin">LinkedIn → Email</a>
            <a class="tab {{ 'active' if mode == 'bulk' else '' }}" href="/bulk">Bulk Upload</a>
        </div>

        {% if mode == 'bulk' %}
            <h2>Bulk name check</h2>
            <div class="hint">Upload a .txt file with one full name per line, plus the company domain to check against. Max 100 names per file.</div>
            <form method="POST" enctype="multipart/form-data">
                <label>Name list (.txt)</label>
                <input type="file" name="namefile" accept=".txt" required>
                <label>Company Domain</label>
                <input type="text" name="domain" required placeholder="example.com" value="{{ domain_input or '' }}">
                <input type="submit" value="Find &amp; Verify Emails">
            </form>

            {% if bulk_error %}
                <div class="result error">{{ bulk_error }}</div>
            {% endif %}

            {% if bulk_results is not none %}
                {% if catch_all_notice %}
                    <div class="result warn">{{ catch_all_notice }}</div>
                {% elif bulk_results %}
                    <div class="summary">Domain checked: {{ resolved_domain or domain_input }}</div>
                    <table>
                        <tr><th>Name</th><th>Email</th></tr>
                        {% for r in bulk_results %}
                        <tr><td>{{ r.name }}</td><td class="email">{{ r.email }}</td></tr>
                        {% endfor %}
                    </table>
                    <div class="summary">{{ bulk_total }} checked · {{ bulk_results|length }} verified email(s) found</div>
                {% else %}
                    <div class="result error">No valid emails found for the names provided.</div>
                {% endif %}
            {% endif %}

        {% elif mode == 'linkedin' %}
            <h2>LinkedIn to Email</h2>
            <div class="hint">
                Paste a LinkedIn profile URL and we'll try to find the person's email address.<br>
                <strong>Note:</strong> If you don't provide a company domain, we'll try to detect it from the URL.
            </div>
            <form method="POST">
                <label>LinkedIn Profile URL</label>
                <input type="url" name="linkedin_url" required placeholder="https://www.linkedin.com/in/john-smith/" value="{{ linkedin_url or '' }}">

                <label>Company Domain <span style="color: var(--muted); font-weight: normal;">(optional)</span></label>
                <input type="text" name="company_domain" placeholder="acme.com" value="{{ company_domain or '' }}">
                <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">
                    Leave blank to auto-detect from the LinkedIn URL
                </div>

                <input type="submit" value="Find Email from LinkedIn">
            </form>

            {% if linkedin_result %}
                <div class="summary">
                    {% if name_extracted %}
                        Name: {{ name_extracted }}
                    {% endif %}
                    {% if company_domain %}
                        &middot; Domain: {{ company_domain }}
                    {% endif %}
                </div>
                {% if linkedin_status == 'success' %}
                    <div class="result success">✓ Found email: {{ linkedin_result }}</div>
                {% elif linkedin_status == 'warn' %}
                    <div class="result warn">{{ linkedin_result }}</div>
                {% else %}
                    <div class="result error">✗ {{ linkedin_result }}</div>
                {% endif %}
            {% endif %}

        {% else %}
            <h2>Find a single email</h2>
            <div class="hint">Enter a name and company domain (e.g. "acme" or "acme.com") - we'll automatically try common TLD variants like .com, .co.in, .org and verify via SMTP.</div>
            <form method="POST">
                <div class="row">
                    <div>
                        <label>First Name</label>
                        <input type="text" name="first" required placeholder="John" value="{{ first or '' }}">
                    </div>
                    <div>
                        <label>Last Name</label>
                        <input type="text" name="last" required placeholder="Doe" value="{{ last or '' }}">
                    </div>
                </div>
                <label>Company Domain</label>
                <input type="text" name="domain" required placeholder="example.com" value="{{ domain_input or '' }}">
                <input type="submit" value="Find &amp; Verify Email">
            </form>
            {% if result %}
                <div class="summary">Searched: {{ first }} {{ last }} &middot; {{ domain_input }}</div>
                {% if status == 'success' %}
                    <div class="result success">✓ Found valid email: {{ result }}</div>
                {% elif status == 'warn' %}
                    <div class="result warn">{{ result }}</div>
                {% else %}
                    <div class="result error">✗ {{ result }}</div>
                {% endif %}
            {% endif %}
        {% endif %}
    </div>

    <div class="footnote">
        Verification runs live SMTP checks against the domain's mail server.<br>
        Use only for contacts you have a legitimate reason to reach sales, recruiting, or your own team.
    </div>
</div>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        first = request.form.get("first", "")
        last = request.form.get("last", "")
        domain_input = request.form.get("domain", "")

        domain, mx_server = resolve_domain_and_mx(domain_input)
        if not mx_server:
            return render_template_string(
                HTML_TEMPLATE, mode="single",
                first=first, last=last, domain_input=domain_input,
                result=f"No mail server found for '{domain_input}' or its common TLD variants",
                status="error"
            )

        if is_catch_all(mx_server, domain):
            candidates = generate_permutations(first, last, domain)
            guess = candidates[0] if candidates else "unknown"
            return render_template_string(
                HTML_TEMPLATE, mode="single",
                first=first, last=last, domain_input=domain_input,
                result=f"Domain {domain} is catch-all — can't confirm reliably. Best guess: {guess}",
                status="warn"
            )

        found = find_email(first, last, mx_server, domain)
        if found:
            return render_template_string(
                HTML_TEMPLATE, mode="single",
                first=first, last=last, domain_input=domain_input,
                result=found, status="success"
            )
        return render_template_string(
            HTML_TEMPLATE, mode="single",
            first=first, last=last, domain_input=domain_input,
            result=f"No valid email patterns found on {domain}.", status="error"
        )

    return render_template_string(HTML_TEMPLATE, mode="single", result=None)

@app.route("/linkedin", methods=["GET", "POST"])
def linkedin():
    if request.method == "POST":
        linkedin_url = request.form.get("linkedin_url", "")
        company_domain = request.form.get("company_domain", "").strip()

        if not linkedin_url:
            return render_template_string(
                HTML_TEMPLATE, mode="linkedin",
                linkedin_result="Please enter a LinkedIn URL.",
                linkedin_status="error"
            )

        name_extracted = extract_name_from_linkedin(linkedin_url)

        if not company_domain:
            detected_domain = detect_company_domain_from_url(linkedin_url)
            if detected_domain:
                return render_template_string(
                    HTML_TEMPLATE, mode="linkedin",
                    linkedin_url=linkedin_url,
                    company_domain=company_domain,
                    linkedin_result=f"Detected possible company domain: {detected_domain}. Please confirm or enter the correct domain.",
                    linkedin_status="warn",
                    name_extracted=name_extracted
                )
            else:
                return render_template_string(
                    HTML_TEMPLATE, mode="linkedin",
                    linkedin_url=linkedin_url,
                    company_domain=company_domain,
                    linkedin_result="Could not detect company domain from URL. Please enter the company domain.",
                    linkedin_status="error",
                    name_extracted=name_extracted
                )

        email, error = linkedin_to_email(linkedin_url, company_domain)

        if email:
            return render_template_string(
                HTML_TEMPLATE, mode="linkedin",
                linkedin_url=linkedin_url,
                company_domain=company_domain,
                linkedin_result=email,
                linkedin_status="success",
                name_extracted=name_extracted
            )
        else:
            return render_template_string(
                HTML_TEMPLATE, mode="linkedin",
                linkedin_url=linkedin_url,
                company_domain=company_domain,
                linkedin_result=error,
                linkedin_status="error",
                name_extracted=name_extracted
            )

    return render_template_string(HTML_TEMPLATE, mode="linkedin")

@app.route("/bulk", methods=["GET", "POST"])
def bulk():
    if request.method == "POST":
        uploaded = request.files.get("namefile")
        domain_input = request.form.get("domain", "")

        if not uploaded or uploaded.filename == "":
            return render_template_string(HTML_TEMPLATE, mode="bulk", bulk_error="Please choose a file to upload.")

        file_name = uploaded.filename

        try:
            raw = uploaded.read().decode("utf-8", errors="ignore")
        except Exception:
            return render_template_string(HTML_TEMPLATE, mode="bulk", bulk_error="Could not read that file.")

        names = [line.strip() for line in raw.splitlines() if line.strip()]
        if not names:
            return render_template_string(HTML_TEMPLATE, mode="bulk", domain_input=domain_input,
                                           bulk_filename=file_name, bulk_error="No names found in the file.")

        MAX_NAMES = 100
        if len(names) > MAX_NAMES:
            names = names[:MAX_NAMES]

        domain, mx_server = resolve_domain_and_mx(domain_input)
        if not mx_server:
            return render_template_string(
                HTML_TEMPLATE, mode="bulk", domain_input=domain_input, bulk_filename=file_name,
                bulk_error=f"No mail server found for '{domain_input}' or its common TLD variants"
            )

        if is_catch_all(mx_server, domain):
            return render_template_string(
                HTML_TEMPLATE, mode="bulk", domain_input=domain_input, bulk_filename=file_name,
                bulk_results=[],
                catch_all_notice=f"Domain {domain} is catch-all — bulk verification skipped since results would be unreliable.",
                bulk_total=len(names)
            )

        results = []
        for name in names:
            f, l = split_full_name(name)
            found = find_email(f, l, mx_server, domain)
            if found:
                results.append({"name": name, "email": found})

        return render_template_string(HTML_TEMPLATE, mode="bulk", domain_input=domain_input, bulk_filename=file_name,
                                       bulk_results=results, bulk_total=len(names), resolved_domain=domain)

    return render_template_string(HTML_TEMPLATE, mode="bulk", bulk_results=None)

if __name__ == "__main__":
    app.run(host="everify.local", port=PORT, debug=True)

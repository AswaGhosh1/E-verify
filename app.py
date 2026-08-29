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

# Get API key from environment variable properly
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.3-70b-versatile"

# ---------------------------------------------------------------------------
# CV parsing
# ---------------------------------------------------------------------------
def extract_text_from_cv(file_storage):
    filename = file_storage.filename.lower()
    data = file_storage.read()

    if filename.endswith(".txt"):
        return data.decode("utf-8", errors="ignore"), None

    if filename.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            return None, "Missing dependency 'pypdf'. Install with: pip install pypdf"
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return text, None
        except Exception as e:
            return None, f"Could not read PDF: {e}"

    if filename.endswith(".docx"):
        try:
            import docx
        except ImportError:
            return None, "Missing dependency 'python-docx'. Install with: pip install python-docx"
        try:
            document = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in document.paragraphs)
            return text, None
        except Exception as e:
            return None, f"Could not read DOCX: {e}"

    return None, "Unsupported file type. Please upload a .txt, .pdf, or .docx file."


def generate_ats_resume(cv_text):
    """Reformats a CV into a clean, ATS-friendly plain-text version"""
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY environment variable is not set."

    cv_text = cv_text[:10000]

    system_prompt = (
        "You convert resumes/CVs into ATS-friendly (Applicant Tracking System) "
        "plain text format. Rules: use only facts present in the original CV, "
        "never invent employers, dates, titles, or skills. Use standard section "
        "headers (CONTACT INFORMATION, SUMMARY, WORK EXPERIENCE, EDUCATION, "
        "SKILLS, CERTIFICATIONS if present). Single column, no tables, no bullet "
        "symbols beyond simple dashes, no special characters, no graphics "
        "references. Keep dates and job titles exactly as given. Use clear line "
        "breaks between sections."
    )
    user_prompt = (
        f"Original CV text:\n{cv_text}\n\n"
        "Convert this into an ATS-friendly plain text resume now."
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"], None
    except Exception as e:
        return None, f"Groq API request failed: {e}"

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
        f"{f}@{d}",
        f"{l}@{d}",
        f"{f}.{l}@{d}",
        f"{f}{l}@{d}",
        f"{f[0]}{l}@{d}",
        f"{f}{l[0]}@{d}",
        f"{f}_{l}@{d}",
        f"{l}.{f}@{d}",
        f"{l}{f}@{d}",
        f"{f[0]}.{l}@{d}",
        f"{f}.{l[0]}@{d}",
        f"{f[0]}{l[0]}@{d}",
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


def verify_email_smtp(mx_server, email, timeout=5):
    ports = [25, 587, 465, 2525]

    for port in ports:
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(mx_server, port, timeout=timeout)
            else:
                server = smtplib.SMTP(mx_server, port, timeout=timeout)

            if port != 465:
                try:
                    server.starttls()
                except:
                    pass

            server.helo("localhost")
            server.mail("verify@local-tool.com")
            code, _ = server.rcpt(email)
            server.quit()

            if code in [250, 251]:
                return True
        except:
            continue

    return False


def is_catch_all(mx_server, domain):
    fake = f"completelyfake{int(time.time())}@{domain}"
    return verify_email_smtp(mx_server, fake)


def find_email(first, last, mx_server, domain, delay=0.3):
    permutations = generate_permutations(first, last, domain)

    priority_patterns = [
        f"{first.lower()}@{domain}",
        f"{last.lower()}@{domain}",
        f"{first.lower()}.{last.lower()}@{domain}",
        f"{first.lower()}{last.lower()}@{domain}",
        f"{first.lower()[0]}{last.lower()}@{domain}",
    ]

    for email in priority_patterns:
        if email in permutations:
            if verify_email_smtp(mx_server, email):
                return email
            time.sleep(delay)

    for email in permutations:
        if email in priority_patterns:
            continue
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
    input[type="text"], input[type="file"] {
        width: 100%; padding: 12px 14px;
        background: var(--panel-2); border: 1px solid var(--border);
        border-radius: 9px; color: var(--text); font-size: 14px;
        outline: none; transition: border-color 0.15s ease;
    }
    input[type="text"]:focus { border-color: var(--accent); }
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
            <a class="tab {{ 'active' if mode == 'bulk' else '' }}" href="/bulk">Bulk Upload</a>
            <a class="tab {{ 'active' if mode == 'ats' else '' }}" href="/ats">CV → ATS Converter</a>
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

        {% elif mode == 'ats' %}
            <h2>Convert your CV to an ATS-friendly format</h2>
            <div class="hint">Upload your CV (.txt, .pdf, or .docx) - we'll reformat it into clean, single-column plain text that Applicant Tracking Systems can parse reliably, using only what's already in your CV.</div>
            <form method="POST" enctype="multipart/form-data">
                <label>Your CV</label>
                <input type="file" name="cvfile" accept=".txt,.pdf,.docx" required>
                <input type="submit" value="Convert to ATS Format">
            </form>

            {% if ats_error %}
                <div class="result error">{{ ats_error }}</div>
            {% endif %}

            {% if ats_filename %}
                <div class="summary">File: {{ ats_filename }}</div>
            {% endif %}

            {% if ats_resume %}
                <div class="email-output">{{ ats_resume }}</div>
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
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, mode="single", result=None)

@app.route("/", methods=["POST"])
def index_post():
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


@app.route("/ats", methods=["GET", "POST"])
def ats():
    if request.method == "POST":
        uploaded = request.files.get("cvfile")

        if not uploaded or uploaded.filename == "":
            return render_template_string(HTML_TEMPLATE, mode="ats", ats_error="Please choose a CV file to upload.")

        filename = uploaded.filename
        cv_text, err_msg = extract_text_from_cv(uploaded)
        if err_msg:
            return render_template_string(HTML_TEMPLATE, mode="ats", ats_filename=filename, ats_error=err_msg)
        if not cv_text or not cv_text.strip():
            return render_template_string(HTML_TEMPLATE, mode="ats", ats_filename=filename,
                                           ats_error="Could not extract any text from that CV.")

        ats_resume, gen_err = generate_ats_resume(cv_text)
        if gen_err:
            return render_template_string(HTML_TEMPLATE, mode="ats", ats_filename=filename, ats_error=gen_err)

        return render_template_string(HTML_TEMPLATE, mode="ats", ats_filename=filename, ats_resume=ats_resume)

    return render_template_string(HTML_TEMPLATE, mode="ats")


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

        results = []
        for name in names:
            f, l = split_full_name(name)
            found = find_email(f, l, mx_server, domain)
            if found:
                results.append({"name": name, "email": found})

        if not results:
            return render_template_string(
                HTML_TEMPLATE, mode="bulk", domain_input=domain_input, bulk_filename=file_name,
                bulk_results=[],
                catch_all_notice=f"No valid emails found for the {len(names)} names provided.",
                bulk_total=len(names)
            )

        return render_template_string(HTML_TEMPLATE, mode="bulk", domain_input=domain_input, bulk_filename=file_name,
                                       bulk_results=results, bulk_total=len(names), resolved_domain=domain)

    return render_template_string(HTML_TEMPLATE, mode="bulk", bulk_results=None)


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5051))
    app.run(host="0.0.0.0", port=port, debug=False)

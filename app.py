import io
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
MAT2_BIN = os.environ.get("MAT2_BIN", "mat2")
MAX_REQUESTS_PER_HOUR = int(os.environ.get("MAX_REQUESTS_PER_HOUR", "12"))
PROCESSING_TIMEOUT = int(os.environ.get("PROCESSING_TIMEOUT", "90"))

# Temporary working files are removed after each request.
TEMP_ROOT = os.environ.get("TEMP_ROOT", tempfile.gettempdir())
Path(TEMP_ROOT).mkdir(parents=True, exist_ok=True)

# Simple per-process rate limit. For a larger public service, add a hosting-provider
# WAF/rate limit because this in-memory limit resets on restart and is per instance.
_rate_buckets = defaultdict(deque)


def client_address():
    # On Render, REMOTE_ADDR is normally the proxy. Prefer the first forwarded
    # address only when the app is deployed behind the platform proxy.
    forwarded = request.headers.get("X-Forwarded-For", "")
    if os.environ.get("TRUST_PROXY_HEADERS", "0") == "1" and forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def enforce_rate_limit():
    ip = client_address()
    now = time.time()
    bucket = _rate_buckets[ip]
    while bucket and now - bucket[0] > 3600:
        bucket.popleft()
    if len(bucket) >= MAX_REQUESTS_PER_HOUR:
        return False
    bucket.append(now)
    return True


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a1220">
<meta name="description" content="Remove hidden metadata from files before sharing them.">
<title>Metadata Remover</title>
<style>
:root{color-scheme:dark;--bg:#080f1b;--panel:#111d2e;--line:#2a4059;--text:#edf4ff;--muted:#9aacc1;--blue:#77b7ff;--mint:#7ce0d4}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% 0,#172b45 0,var(--bg) 54%);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
header{max-width:1050px;margin:auto;padding:22px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #ffffff10}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:17px;letter-spacing:-.4px}.mark{height:34px;width:34px;display:grid;place-items:center;border-radius:10px;background:linear-gradient(140deg,#75b6ff,#7ce0d4);color:#071321;font-weight:900}
.pill{border:1px solid #2b4a5b;border-radius:30px;padding:6px 11px;color:#b6cbdc;font-size:11px}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#70e0ae;margin-right:6px}
main{max-width:790px;margin:auto;padding:65px 22px 45px;text-align:center}.eyebrow{font-size:10px;font-weight:800;letter-spacing:2px;color:#91bff0}
h1{font-size:clamp(40px,7vw,62px);line-height:1.05;letter-spacing:-2.8px;margin:18px 0 15px}h1 span{background:linear-gradient(90deg,#7bb8ff,#82e1d6);color:transparent;background-clip:text}
.intro{max-width:590px;margin:0 auto;color:var(--muted);font-size:15px}
.card{max-width:590px;margin:34px auto 0;padding:17px;background:linear-gradient(150deg,#14243a,#101a2a);border:1px solid var(--line);border-radius:18px;box-shadow:0 25px 75px #0006;text-align:left}
.drop{padding:31px 15px;border:1px dashed #4a6683;border-radius:12px;text-align:center;background:#0b1625;cursor:pointer}.drop.over{border-color:var(--blue);background:#12243a}
.upload{height:46px;width:46px;margin:0 auto 11px;border-radius:13px;background:#203a58;color:#91c8ff;display:grid;place-items:center;font-size:23px}
.drop h2{font-size:17px;margin:0}.drop p{font-size:12px;color:var(--muted);margin:3px 0}.browse{border:0;background:none;color:var(--blue);text-decoration:underline;cursor:pointer;font:inherit;padding:0}
.limit{font-size:10px;color:#7589a0;margin-top:15px;display:block}
.file{display:flex;align-items:center;gap:11px;padding:11px;margin-top:12px;border:1px solid var(--line);border-radius:10px;background:#0a1523}.fileicon{width:35px;height:39px;border-radius:7px;background:#203a58;color:#9bcaff;font-size:9px;font-weight:900;display:grid;place-items:center}.fileinfo{min-width:0;flex:1;display:flex;flex-direction:column}.fileinfo strong{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fileinfo small{font-size:10px;color:#8da1b8}.remove{border:0;background:none;color:#aebed0;font-size:22px;cursor:pointer}
button.primary{width:100%;border:0;border-radius:9px;padding:14px 17px;margin-top:15px;background:linear-gradient(100deg,#79b7ff,#79ddd2);color:#071522;font-weight:850;font-size:13px;cursor:pointer}button.primary:disabled{opacity:.4;cursor:not-allowed}
.status{min-height:23px;padding-top:9px;text-align:center;color:#91a4ba;font-size:11px}
.result{display:flex;align-items:center;gap:10px;margin-top:12px;padding:12px;border:1px solid #286452;border-radius:10px;background:#102a25}.check{height:26px;width:26px;border-radius:50%;background:#1b5946;display:grid;place-items:center;color:#9af0c4}.resulttext{min-width:0;flex:1;display:flex;flex-direction:column}.resulttext strong{font-size:12px}.resulttext small{color:#a0b9ad;font-size:10px;overflow:hidden;text-overflow:ellipsis}.download{padding:9px 11px;border-radius:7px;background:#8ce8bd;color:#071a13;font-size:11px;font-weight:800;text-decoration:none;white-space:nowrap}
.trust{display:flex;justify-content:center;gap:20px;flex-wrap:wrap;margin-top:20px;color:#8b9eb3;font-size:10px}
.how{max-width:950px;margin:0 auto;padding:30px 22px 55px}.how h2{text-align:center;font-size:23px;letter-spacing:-.5px;margin:7px 0 22px}.label{text-align:center;color:#84b4e4;font-size:10px;font-weight:800;letter-spacing:2px}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}.step{padding:19px;border:1px solid #24364d;background:#0e1928;border-radius:12px}.num{font-size:10px;color:#83baff;font-weight:800}.step h3{font-size:14px;margin:10px 0 5px}.step p{font-size:11px;color:#91a4b9;margin:0}
.note{max-width:900px;margin:0 auto 42px;padding:14px 17px;border:1px solid #50472e;background:#211e16;border-radius:10px;color:#c6b991;font-size:11px}.note strong{color:#f0dca7}
footer{max-width:1050px;margin:auto;border-top:1px solid #ffffff10;padding:20px 24px 28px;display:flex;justify-content:space-between;color:#74869c;font-size:10px}
.hidden{display:none!important}
@media(max-width:620px){header{padding:17px}.pill{font-size:9px;padding:5px 7px}main{padding:48px 15px 35px}h1{letter-spacing:-1.8px}.card{padding:11px;margin-top:26px}.drop{padding:25px 10px}.steps{grid-template-columns:1fr}.how{padding:25px 16px 40px}.note{margin:0 15px 30px}footer{padding:18px 16px;gap:15px}}
</style>
</head>
<body>
<header><div class="brand"><span class="mark">M</span>Metadata Remover</div><div class="pill"><span class="dot"></span>Privacy-first processing</div></header>
<main>
<div class="eyebrow">YOUR FILES. YOUR PRIVACY.</div>
<h1>Clean your files.<br><span>Keep your privacy.</span></h1>
<p class="intro">Remove hidden metadata before sharing photos, documents, audio, and more. Your original file is never overwritten.</p>
<section class="card">
  <div class="drop" id="drop">
    <div class="upload">↑</div><h2>Drop a file here</h2>
    <p>or <button class="browse" id="browse" type="button">browse your device</button></p>
    <span class="limit">Maximum file size: 50 MB</span>
    <input id="picker" type="file" hidden>
  </div>
  <div class="file hidden" id="filebox"><div class="fileicon">FILE</div><div class="fileinfo"><strong id="filename"></strong><small id="filesize"></small></div><button class="remove" id="remove" aria-label="Remove file">×</button></div>
  <button class="primary" id="clean" type="button" disabled>Remove metadata&nbsp; →</button>
  <div class="status" id="status" role="status" aria-live="polite">Choose a file to get started.</div>
  <div class="result hidden" id="result"><div class="check">✓</div><div class="resulttext"><strong>Your cleaned file is ready</strong><small id="resultname"></small></div><a class="download" id="download" href="#">Download ↓</a></div>
</section>
<div class="trust"><span>◇ Original stays untouched</span><span>◷ Temporary processing</span><span>⌑ No public file gallery</span></div>
</main>
<section class="how"><div class="label">HOW IT WORKS</div><h2>Three steps. More peace of mind.</h2><div class="steps">
<article class="step"><span class="num">01</span><h3>Choose your file</h3><p>Select a supported file from your device. Your original file stays unchanged.</p></article>
<article class="step"><span class="num">02</span><h3>Remove hidden data</h3><p>MAT2 processes a temporary copy on the server.</p></article>
<article class="step"><span class="num">03</span><h3>Download and share</h3><p>Download the cleaned copy. Temporary server files are deleted after processing.</p></article>
</div></section>
<div class="note"><strong>Privacy reminder:</strong> Metadata removal cannot hide information visible in the file itself, such as faces, street signs, house numbers, reflections, or landmarks. Not every format or metadata type is supported. Review files before sharing.</div>
<footer><span>© <span id="year"></span> Metadata Remover</span><span>Built for safer sharing.</span></footer>
<script>
const picker=document.getElementById('picker'),drop=document.getElementById('drop'),clean=document.getElementById('clean'),statusBox=document.getElementById('status');
let chosen=null,downloadUrl=null;
document.getElementById('year').textContent=new Date().getFullYear();
function say(t,error=false){statusBox.textContent=t;statusBox.style.color=error?'#ff9999':'#91a4ba'}
function size(n){return n<1048576?Math.round(n/1024)+' KB':(n/1048576).toFixed(2)+' MB'}
function selectFile(f){if(!f)return;document.getElementById('result').classList.add('hidden');if(downloadUrl){URL.revokeObjectURL(downloadUrl);downloadUrl=null}
if(f.size>50*1024*1024){chosen=null;clean.disabled=true;document.getElementById('filebox').classList.add('hidden');say('That file is larger than the 50 MB limit.',true);return}
chosen=f;document.getElementById('filename').textContent=f.name;document.getElementById('filesize').textContent=size(f.size)+' · '+(f.type||'Unknown file type');document.getElementById('filebox').classList.remove('hidden');clean.disabled=false;say('Ready to clean. Your original file will not be changed.')}
document.getElementById('browse').onclick=()=>picker.click();
drop.onclick=e=>{if(e.target.id!=='browse')picker.click()};
drop.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();picker.click()}};
picker.onchange=()=>selectFile(picker.files[0]);
['dragenter','dragover'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>selectFile(e.dataTransfer.files[0]));
document.getElementById('remove').onclick=()=>{chosen=null;picker.value='';clean.disabled=true;document.getElementById('filebox').classList.add('hidden');document.getElementById('result').classList.add('hidden');say('Choose a file to get started.')};
clean.onclick=async()=>{if(!chosen)return;clean.disabled=true;clean.textContent='Cleaning file…';document.getElementById('result').classList.add('hidden');say('Uploading a temporary copy for processing…');
const data=new FormData();data.append('file',chosen,chosen.name);
try{const r=await fetch('/clean',{method:'POST',body:data});if(!r.ok){let msg='The server could not process this file.';try{msg=(await r.json()).error||msg}catch{}throw new Error(msg)}
const blob=await r.blob();downloadUrl=URL.createObjectURL(blob);const disposition=r.headers.get('content-disposition')||'';const match=disposition.match(/filename="?([^";]+)"?/i);const name=match?match[1]:'cleaned-'+chosen.name;document.getElementById('resultname').textContent=name;const a=document.getElementById('download');a.href=downloadUrl;a.download=name;document.getElementById('result').classList.remove('hidden');say('Finished. Download your cleaned copy below.')
}catch(e){say(e.message||'Something went wrong. Please try again.',true)}
finally{clean.disabled=!chosen;clean.textContent='Remove metadata  →'}};
</script>
</body></html>"""


@app.get("/")
def home():
    return render_template_string(PAGE)


@app.get("/health")
def health():
    return jsonify(status="ok", service="Metadata Remover")


@app.post("/clean")
def clean_file():
    if not enforce_rate_limit():
        return jsonify(error="Upload limit reached for this connection. Please try again later."), 429

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="Choose a file first."), 400

    filename = secure_filename(uploaded.filename)
    if not filename or filename in {".", ".."}:
        return jsonify(error="That filename is not valid."), 400

    workdir = tempfile.mkdtemp(prefix="metadata-remover-", dir=TEMP_ROOT)
    input_path = Path(workdir) / filename

    try:
        total = 0
        with input_path.open("wb") as output:
            while True:
                chunk = uploaded.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > app.config["MAX_CONTENT_LENGTH"]:
                    return jsonify(error="File exceeds the 50 MB upload limit."), 413
                output.write(chunk)

        if total == 0:
            return jsonify(error="The uploaded file is empty."), 400

        try:
            result = subprocess.run(
                [MAT2_BIN, "--inplace", str(input_path)],
                capture_output=True,
                text=True,
                timeout=PROCESSING_TIMEOUT,
                check=False,
                cwd=workdir,
                env={**os.environ, "HOME": workdir, "TMPDIR": workdir},
            )
        except FileNotFoundError:
            app.logger.error("MAT2 executable was not found: %s", MAT2_BIN)
            return jsonify(error="The metadata-removal engine is not installed on the server."), 503
        except subprocess.TimeoutExpired:
            return jsonify(error="Processing took too long. Try a smaller file."), 408

        if result.returncode != 0 or not input_path.is_file() or input_path.stat().st_size == 0:
            app.logger.warning("MAT2 failed for an upload (exit %s).", result.returncode)
            return jsonify(error="This file type may not be supported or could not be cleaned."), 422

        stem, suffix = os.path.splitext(filename)
        download_name = f"{stem}-metadata-removed{suffix}"
        file_bytes = input_path.read_bytes()

        response = send_file(
            io.BytesIO(file_bytes),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    finally:
        # The response is held in memory, so the temporary directory can be
        # removed immediately after the cleaned bytes have been read.
        shutil.rmtree(workdir, ignore_errors=True)


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="File exceeds the 50 MB upload limit."), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

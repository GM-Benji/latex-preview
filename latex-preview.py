#!/usr/bin/env python3
"""LaTeX editor + compile + live PDF preview server with document picker and password.

Server: latex-preview.py [ROOT_DIR] [--port 8090] [--host 0.0.0.0] [--engine xelatex|pdflatex|lualatex]
  Finds every main .tex (has \\documentclass) under ROOT_DIR; the browser picks one from a list.
  Text files in the document's directory can be edited in the browser (CodeMirror); saving rebuilds.
  Open documents are built into <their dir>/.build/ and rebuilt on save.
  Password (HTTP Basic, any username): env LATEX_PREVIEW_PASSWORD; unset = no auth.
One-shot: latex-preview.py DIR_OR_TEX --once   (build, print errors, exit 0/1)

Pipeline (Praca-AMP Makefile): engine -> biber -> makeglossaries -> engine -> engine.
Endpoints: /  /docs  /pdf?doc=  /status?doc=  /log?doc=  POST /rebuild?doc=
           /files?doc=  GET|PUT /file?doc=&path=   (PUT header X-Base-Mtime -> 409 on conflict)
           /synctex?doc=&page=&x=&y=  (PDF point -> file:line)
           POST /visual-edit?doc=  {page,x,y,old,new}  (edit text seen in the PDF -> patch the source)
"""
import argparse, base64, hmac, json, os, re, shutil, subprocess, sys, threading, time, unicodedata
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

WATCH_EXT = {".tex", ".bib", ".cls", ".sty", ".bst", ".png", ".jpg", ".jpeg", ".pdf", ".svg", ".eps"}
EDIT_EXT = {".tex", ".bib", ".cls", ".sty", ".bst", ".txt", ".md", ".cfg", ".def", ".latexmkrc"}
EDIT_NAMES = {"Makefile", ".latexmkrc"}
ENGINES = ("xelatex", "pdflatex", "lualatex")
ACTIVE_SECS = 1800  # keep watching a document this long after a browser last polled it
MAX_EDIT_BYTES = 5_000_000


def is_main(p: Path) -> bool:
    try:
        with p.open(errors="ignore") as f:
            head = f.read(20000)
    except OSError:
        return False
    return re.search(r"^[^%\n]*\\documentclass", head, re.M) is not None


def find_docs(root: Path, depth=4):
    docs = []
    for dirpath, dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules")
        if len(rel.parts) >= depth:
            dirs[:] = []
        for f in sorted(files):
            if f.endswith(".tex") and is_main(Path(dirpath) / f):
                docs.append(str(rel / f) if rel.parts else f)
    return docs


def find_main(proj: Path) -> Path:
    mk = proj / "Makefile"
    if mk.exists():
        m = re.search(r"^\s*MAIN_FILE\s*=\s*(\S+)", mk.read_text(errors="ignore"), re.M)
        if m and (proj / (m.group(1).removesuffix(".tex") + ".tex")).exists():
            return proj / (m.group(1).removesuffix(".tex") + ".tex")
    cands = [p for p in sorted(proj.glob("*.tex")) if is_main(p)]
    if not cands:
        sys.exit(f"no main .tex with \\documentclass in {proj}")
    return cands[0]


def editable(p: Path) -> bool:
    return p.suffix.lower() in EDIT_EXT or p.name in EDIT_NAMES


def write_atomic(p: Path, data: bytes):
    tmp = p.with_name(f".{p.name}.lp-tmp")
    tmp.write_bytes(data)
    if p.exists():
        shutil.copymode(p, tmp)
    os.replace(tmp, p)


# --- visual (in-PDF) editing: map text seen in the PDF back to the LaTeX source ---
_MARKUP = r"(?:\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?\{|[{}])*?"  # \textit{ , { , } between words (lazy: insert inside braces)
_WS = r"(?:[\s~]|\\[ ,]|" + r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?\{|[{}])+"
_CHARMAP = {"—": "(?:—|---)", "–": "(?:–|--)", "“": "(?:“|``|\")", "”": "(?:”|''|\")", "„": "(?:„|,,)",
            "’": "(?:’|')", "‘": "(?:‘|`)", "\u00a0": "[\\s~]"}


def _pat(s: str) -> str:
    """Regex for text as it appears in the PDF, tolerant to TeX spacing/escapes/markup at word gaps."""
    out = []
    for part in re.split(r"(\s+)", s):
        if not part:
            continue
        if part.isspace():
            out.append(_WS)
            continue
        for c in part:
            if c in "%&#_$":
                out.append("\\\\?" + re.escape(c))
            else:
                out.append(_CHARMAP.get(c, re.escape(c)))
    return "".join(out)


def _ctx_left(s, k):
    toks = list(re.finditer(r"\S+", s))
    return s[toks[-k].start():] if 0 < k <= len(toks) else (s if k else "")


def _ctx_right(s, k):
    toks = list(re.finditer(r"\S+", s))
    return s[:toks[k - 1].end()] if 0 < k <= len(toks) else (s if k else "")


def tex_escape(s):
    return re.sub(r"(?<!\\)([%&#_$])", r"\\\1", s)


class Builder:
    def __init__(self, main: Path, engine):
        self.main, self.engine = main, engine
        self.proj = main.parent
        self.out = self.proj / ".build"
        self.out.mkdir(exist_ok=True)
        self.preview = self.out / (main.stem + ".preview.pdf")
        self.buildlog = self.out / (main.stem + ".build.out")
        self.lock = threading.Lock()
        self.dirty = False
        self.last_seen = time.time()
        self.snap = None
        self.state = {"status": "idle", "version": 1 if self.preview.exists() else 0, "errors": [],
                      "duration": 0, "finished": None, "main": main.name, "engine": engine}

    def request(self):
        """Coalescing async build: if one is running, run exactly one more after it."""
        self.dirty = True
        if self.lock.locked():
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.dirty:
            self.dirty = False
            self.build()

    def build(self):
        with self.lock:
            self.state["status"] = "building"
            t0 = time.time()
            env = dict(os.environ, max_print_line="1000")
            tex = [self.engine, "-interaction=nonstopmode", "-file-line-error", "-synctex=1",
                   f"-output-directory={self.out}", self.main.name]
            stem, out = self.main.stem, str(self.out)
            steps = [tex,
                     ["biber", "--input-directory", out, "--output-directory", out, stem],
                     ["makeglossaries", "-d", out, stem],
                     tex, tex]
            log, rc = [], 0
            for cmd in steps:
                if cmd[0] == "biber" and not (self.out / (stem + ".bcf")).exists():
                    continue
                if cmd[0] == "makeglossaries" and not (self.out / (stem + ".glo")).exists():
                    continue
                p = subprocess.run(cmd, cwd=self.proj, capture_output=True, text=True, env=env,
                                   stdin=subprocess.DEVNULL)
                log.append(f"$ {' '.join(cmd)}  -> rc={p.returncode}\n{p.stdout}{p.stderr}")
                if cmd[0] == self.engine:
                    rc = p.returncode
            self.buildlog.write_text("\n".join(log))
            pdf = self.out / (stem + ".pdf")
            errors = self.parse_errors()
            ok = rc == 0 and pdf.exists()
            if pdf.exists() and (ok or not self.preview.exists() or pdf.stat().st_mtime > self.preview.stat().st_mtime):
                tmp = self.out / (stem + ".preview.tmp")
                shutil.copyfile(pdf, tmp)
                os.replace(tmp, self.preview)
                self.state["version"] += 1
            self.state.update(status="ok" if ok and not errors else "error", errors=errors,
                              duration=round(time.time() - t0, 1), finished=time.strftime("%H:%M:%S"))
            print(f"[{self.state['finished']}] {self.main} {self.state['status']} in {self.state['duration']}s"
                  + (f", {len(errors)} errors" if errors else ""), flush=True)
            return ok

    def parse_errors(self):
        log = self.out / (self.main.stem + ".log")
        if not log.exists():
            return [{"msg": "no log produced - see Log"}]
        errs, seen = [], set()
        lines = log.read_text(errors="ignore").splitlines()
        for i, l in enumerate(lines):
            m = re.match(r"^((?:\./|/)?\S+\.\w+):(\d+): (.*)", l)
            if m or l.startswith("! "):
                ctx = " | ".join(x.strip() for x in lines[i + 1:i + 3] if x.strip())
                msg = l.strip() + (f"  [{ctx}]" if ctx else "")
                if msg in seen:
                    continue
                seen.add(msg)
                e = {"msg": msg}
                if m:
                    f = Path(m.group(1))
                    f = (self.proj / f).resolve() if not f.is_absolute() else f
                    if f.is_relative_to(self.proj):
                        e.update(file=str(f.relative_to(self.proj)), line=int(m.group(2)))
                errs.append(e)
        return errs[:30]

    def snapshot(self):
        snap = {}
        for root, dirs, files in os.walk(self.proj):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if os.path.splitext(f)[1].lower() in WATCH_EXT:
                    try:
                        snap[os.path.join(root, f)] = os.stat(os.path.join(root, f)).st_mtime_ns
                    except FileNotFoundError:
                        pass
        return snap

    def files(self):
        res = []
        for root, dirs, files in os.walk(self.proj):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                p = Path(root) / f
                if editable(p):
                    res.append(str(p.relative_to(self.proj)))
        main = self.main.name
        return [main] + [f for f in res if f != main]

    def synctex(self, page, x, y):
        """PDF point (big points from top-left) -> (path relative to project, line) or None."""
        pdf = self.out / (self.main.stem + ".pdf")
        if not (self.out / (self.main.stem + ".synctex.gz")).exists():
            return None
        p = subprocess.run(["synctex", "edit", "-o", f"{int(page)}:{float(x):.2f}:{float(y):.2f}:{pdf}",
                            "-d", str(self.out)], capture_output=True, text=True, cwd=self.proj, timeout=20)
        m_in = re.search(r"^Input:(.+)$", p.stdout, re.M)
        m_ln = re.search(r"^Line:(\d+)$", p.stdout, re.M)
        if not (m_in and m_ln):
            return None
        f = Path(m_in.group(1).strip())
        f = (self.proj / f if not f.is_absolute() else f).resolve()
        if not f.is_relative_to(self.proj):
            return None
        return str(f.relative_to(self.proj)), int(m_ln.group(1))

    def visual_edit(self, page, x, y, old, new):
        """Replace the changed part of `old` (a text run as shown in the PDF) by `new` in the source."""
        loc = self.synctex(page, x, y)
        if not loc:
            return {"ok": False, "reason": "SyncTeX found no source for this spot (wait for a successful build)"}
        rel, line = loc
        res = {"file": rel, "line": line}
        p = self.resolve(rel)
        if p is None:
            return dict(res, ok=False, reason=f"{rel} is not editable here")
        old, new = unicodedata.normalize("NFKC", old), unicodedata.normalize("NFKC", new)
        if old == new:
            return dict(res, ok=True, noop=True)
        a = 0
        while a < min(len(old), len(new)) and old[a] == new[a]:
            a += 1
        b = 0
        while b < min(len(old), len(new)) - a and old[-1 - b] == new[-1 - b]:
            b += 1
        mid_old, mid_new = old[a:len(old) - b], new[a:len(new) - b]
        lpart, rpart = old[:a], old[len(old) - b:]
        with SAVE_LOCK:
            text = p.read_text()
            starts = [0] + [m.end() for m in re.finditer("\n", text)]
            lo = starts[max(0, line - 1 - 40)]
            hi = starts[min(len(starts) - 1, line - 1 + 40)] if line - 1 + 40 < len(starts) else len(text)
            window = text[lo:hi]
            best = None
            for k in (1, 2, 3, 5, 8, 0):
                if k == 0 and not mid_old:
                    break
                rx = (_pat(_ctx_left(lpart, k)) + _MARKUP + "(" + _pat(mid_old) + ")" + _MARKUP
                      + _pat(_ctx_right(rpart, k)))
                hits = list(re.finditer(rx, window))
                if not hits:
                    continue
                def lineof(m):
                    return text.count("\n", 0, lo + m.start(1)) + 1
                best = min(hits, key=lambda m: abs(lineof(m) - line))
                if len(hits) == 1:
                    break
            if best is None:
                return dict(res, ok=False, reason="could not find that text in the source "
                                                  "(probably formatted by a macro) - edit it in the editor")
            s, e = lo + best.start(1), lo + best.end(1)
            text = text[:s] + tex_escape(mid_new) + text[e:]
            write_atomic(p, text.encode())
            res["line"] = text.count("\n", 0, s) + 1
        print(f"visual edit {p}:{res['line']}: {mid_old!r} -> {mid_new!r}", flush=True)
        return dict(res, ok=True)

    def resolve(self, rel):
        """Editable file path inside the project, or None."""
        if not rel:
            return None
        p = (self.proj / rel).resolve()
        if not p.is_relative_to(self.proj) or any(x.startswith(".") and x not in EDIT_NAMES
                                                   for x in p.relative_to(self.proj).parts):
            return None
        return p if editable(p) else None


class Registry:
    def __init__(self, root: Path, engine):
        self.root, self.engine = root, engine
        self.builders, self.lock = {}, threading.Lock()

    def get(self, rel):
        """Builder for a doc path relative to root; None if invalid. First access starts a build."""
        if not rel:
            return None
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root) or p.suffix != ".tex" or not p.is_file():
            return None
        key = str(p.relative_to(self.root))
        with self.lock:
            b = self.builders.get(key)
            if b is None:
                b = self.builders[key] = Builder(p, self.engine)
                b.request()
        b.last_seen = time.time()
        return b

    def watch(self):
        while True:
            time.sleep(1)
            for b in list(self.builders.values()):
                if time.time() - b.last_seen > ACTIVE_SECS:
                    b.snap = None
                    continue
                cur = b.snapshot()
                if b.snap is None:
                    b.snap = cur
                elif cur != b.snap:
                    time.sleep(0.4)  # debounce editor save bursts
                    b.snap = b.snapshot()
                    print("changed:", b.main, flush=True)
                    b.request()


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LaTeX editor</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/material-darker.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf_viewer.min.css">
<style>
:root{--bg:#525659;--bar:#1f2328;--fg:#e6e6e6;--ok:#3fb950;--err:#f85149;--busy:#d29922;--btn:#30363d;--acc:#388bfd;--side:#1b1e23}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
body{display:flex;flex-direction:column}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 12px;background:var(--bar)}
#dot{width:10px;height:10px;border-radius:50%;background:var(--busy);flex:none}
.bar a,.bar button,.bar select{color:var(--fg);background:var(--btn);border:0;border-radius:4px;padding:4px 8px;
 font:inherit;cursor:pointer;text-decoration:none}
.bar button.on{background:var(--acc)}
.bar button:disabled{opacity:.45;cursor:default}
.bar select{max-width:min(50vw,340px)}
.seg{display:flex;gap:2px}.seg button{border-radius:0}.seg button:first-child{border-radius:4px 0 0 4px}.seg button:last-child{border-radius:0 4px 4px 0}
#errs{display:none;background:#3d1214;color:#ffd7d5;font:12px ui-monospace,monospace;padding:6px 12px;
 max-height:22vh;overflow:auto}
#errs div{white-space:pre-wrap;padding:1px 0}
#errs div.jump{cursor:pointer;text-decoration:underline dotted}
#main{flex:1;display:flex;min-height:0}
#edpane{flex:1;display:flex;flex-direction:column;min-width:0;border-right:2px solid #000}
#edbar{background:#262a30;padding:4px 12px}
#edbody{flex:1;display:flex;min-height:0}
#tree{width:210px;flex:none;overflow:auto;background:var(--side);border-right:1px solid #000;padding:4px 0;font-size:13px}
#tree .dir{padding:6px 10px 2px;opacity:.55;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
#tree .f{padding:3px 10px 3px 16px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#tree .f:hover{background:#2a2f37}#tree .f.on{background:#1f3a5f;color:#fff}
#tree .f.main::after{content:" ★";opacity:.6}
body.notree #tree{display:none}
#ed{flex:1;min-width:0}
.CodeMirror{height:100%;font:13.5px/1.45 ui-monospace,"DejaVu Sans Mono",monospace}
#pvpane{flex:1;overflow:auto;min-width:0;position:relative}
#pages{display:flex;flex-direction:column;align-items:center;gap:10px;padding:12px}
#empty{padding:40px 16px;text-align:center;opacity:.8}
.page{position:relative;flex:none;box-shadow:0 2px 8px #0008;background:#fff}
.page canvas{display:block;width:100%;height:100%}
.textLayer{opacity:1}
body.v-vis .textLayer span{cursor:text;border-radius:2px}
body.v-vis .textLayer span:hover{background:rgba(56,139,253,.18);outline:1px solid rgba(56,139,253,.5)}
.textLayer span.pending{color:#000;background:#fff6c2!important}
.vedit{position:absolute;z-index:5;background:#fff;color:#000;border:2px solid var(--acc);border-radius:3px;
 padding:0 3px;font-family:"TeX Gyre Pagella","Palatino Linotype",Palatino,Georgia,serif;outline:none;box-shadow:0 4px 14px #0006}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#111;color:#fff;padding:8px 14px;
 border-radius:6px;box-shadow:0 4px 14px #0008;display:none;z-index:20;max-width:min(90vw,640px)}
#toast.err{background:#5a1418}
#hint{display:none;background:#14304f;padding:5px 12px;font-size:13px}
body.v-vis #hint{display:block}
body.v-edit #pvpane,body.v-pv #edpane,body.v-vis #edpane{display:none}
body.v-edit #edpane{border:0}
#saved{opacity:.8}#saved.dirty{color:var(--busy);opacity:1}#saved.bad{color:var(--err);opacity:1}
.sp{flex:1}
</style></head><body>
<div class="bar"><span id="dot"></span><select id="doc"><option value="">— choose document —</option></select>
<span id="st"></span><span class="sp"></span>
<span class="seg"><button id="vEd" title="source editor only">Edit</button><button id="vBoth" title="source + preview (double-click PDF = jump to source)">Both</button><button id="vPv" title="preview only">Preview</button><button id="vVis" title="click text in the PDF and edit it in place">Visual</button></span>
<button id="zo">−</button><span id="zv">100%</span><button id="zi">+</button>
<button id="rb">Rebuild</button><a id="lp" href="#" target="_blank">PDF</a><a id="ll" href="#" target="_blank">Log</a></div>
<div id="errs"></div>
<div id="hint">Visual mode: click any text in the document to edit it, Enter = save, Esc = cancel. The page rebuilds in ~10 s; edited text is shown highlighted until then.</div>
<div id="main">
 <div id="edpane"><div class="bar" id="edbar"><button id="tt" title="show/hide file list">Files</button><select id="file"></select>
  <button id="save" disabled>Save</button><label><input type="checkbox" id="auto"> autosave</label>
  <span id="saved"></span><span class="sp"></span><span style="opacity:.55;font-size:12px">Ctrl+click \input{…} to open</span></div>
  <div id="edbody"><div id="tree"></div><div id="ed"></div></div></div>
 <div id="pvpane"><div id="pages"><div id="empty">Choose a document above.</div></div></div>
</div>
<div id="toast"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/stex/stex.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/search.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
<script>
pdfjsLib.GlobalWorkerOptions.workerSrc="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
const $=id=>document.getElementById(id),store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},
 set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
let doc=new URLSearchParams(location.search).get("doc")||store.get("lp-doc")||"",ver=-1,rendering=false,again=false,
 scale=parseFloat(store.get("lp-scale")||"0"),timer=null,file="",mtime="0",clean=0,autoT=null,saving=false,files=[],mode="both";
const q=()=>"doc="+encodeURIComponent(doc),fq=f=>q()+"&path="+encodeURIComponent(f);
const cm=CodeMirror($("ed"),{mode:"stex",theme:"material-darker",lineNumbers:true,lineWrapping:true,
 matchBrackets:true,indentUnit:2,extraKeys:{"Ctrl-S":()=>save(),"Cmd-S":()=>save()}});
cm.setOption("readOnly","nocursor");
const dirty=()=>!cm.isClean(clean);
let toastT=null;
function toast(msg,err,ms){const t=$("toast");t.textContent=msg;t.className=err?"err":"";t.style.display="block";
 clearTimeout(toastT);toastT=setTimeout(()=>t.style.display="none",ms||(err?7000:2500))}

// ---- view / zoom ----
function view(v){const prev=mode;mode=v;document.body.className="v-"+v+(store.get("lp-tree")==="0"||innerWidth<700?" notree":"");store.set("lp-view",v);
 for(const [id,n] of [["vEd","edit"],["vBoth","both"],["vPv","pv"],["vVis","vis"]])$(id).classList.toggle("on",n===v);
 cm.refresh();if(ver>0&&v!=="edit"&&(prev==="edit"||(prev==="vis")!==(v==="vis")))render(ver)}
$("vEd").onclick=()=>view("edit");$("vBoth").onclick=()=>view("both");$("vPv").onclick=()=>view("pv");$("vVis").onclick=()=>view("vis");
$("tt").onclick=()=>{store.set("lp-tree",document.body.classList.toggle("notree")?"0":"1");cm.refresh()};
view(store.get("lp-view")||(innerWidth<800?"pv":"both"));
function zoom(f){scale=Math.max(.4,Math.min(4,(scale||1.3)*f));store.set("lp-scale",scale);if(ver>0)render(ver)}
$("zi").onclick=()=>zoom(1.15);$("zo").onclick=()=>zoom(1/1.15);
$("rb").onclick=()=>doc&&fetch("/rebuild?"+q(),{method:"POST"});

// ---- editor ----
$("auto").checked=store.get("lp-auto")!=="0";
$("auto").onchange=e=>{store.set("lp-auto",e.target.checked?"1":"0");if(e.target.checked&&dirty())save()};
function mark(){const d=dirty();$("save").disabled=!d||!file;
 if(!$("saved").classList.contains("bad")){$("saved").textContent=d?"unsaved changes":(file?"saved":"");$("saved").className=d?"dirty":""}}
cm.on("change",()=>{$("saved").classList.remove("bad");mark();clearTimeout(autoT);
 if($("auto").checked)autoT=setTimeout(save,1500)});
$("save").onclick=()=>save();
async function save(force){
 if(!file||saving||(!dirty()&&!force))return;saving=true;clearTimeout(autoT);
 const gen=cm.changeGeneration(),text=cm.getValue();
 try{
  const r=await fetch("/file?"+fq(file),{method:"PUT",body:text,headers:{"X-Base-Mtime":force?"0":mtime}});
  if(r.status===409){
   $("saved").textContent="file changed on disk!";$("saved").className="bad";
   if(confirm(file+" was changed outside the editor (another person, visual edit, Claude or git).\n\nOK = overwrite with your version\nCancel = keep the disk version (reload)")){saving=false;return save(true)}
   saving=false;return openFile(file,true);
  }
  if(!r.ok)throw await r.text();
  mtime=(await r.json()).mtime;clean=gen;
 }catch(e){$("saved").textContent="save failed: "+e;$("saved").className="bad"}
 saving=false;mark();if(dirty()&&$("auto").checked)autoT=setTimeout(save,1500);
}
async function openFile(f,force,line){
 if(f===file&&!force){if(line)jump(line);return}
 if(!force&&dirty()){await save();if(dirty()&&!confirm("Unsaved changes in "+file+" will be lost. Continue?")){$("file").value=file;return}}
 const r=await fetch("/file?"+fq(f));
 if(!r.ok){toast("cannot open "+f,true);$("file").value=file;return}
 const j=await r.json();file=f;mtime=j.mtime;$("file").value=f;store.set("lp-file:"+doc,f);
 for(const el of $("tree").querySelectorAll(".f"))el.classList.toggle("on",el.dataset.f===f);
 cm.setOption("mode",/\.(tex|cls|sty|bib|bst)$/.test(f)?"stex":null);
 cm.setOption("readOnly",false);cm.setValue(j.text);cm.clearHistory();clean=cm.changeGeneration();
 $("saved").className="";mark();if(line)jump(line);
}
function jump(line){cm.focus();cm.setCursor({line:line-1,ch:0});cm.scrollIntoView({line:line-1,ch:0},120);
 const h=cm.addLineClass(line-1,"background","CodeMirror-selected");setTimeout(()=>cm.removeLineClass(h,"background","CodeMirror-selected"),1500)}
$("file").onchange=e=>openFile(e.target.value);
function buildTree(){
 const t=$("tree");t.replaceChildren();let lastDir=null;
 const sorted=[files[0],...files.slice(1).sort((a,b)=>{const da=a.includes("/"),db=b.includes("/");return da!==db?(da?1:-1):a.localeCompare(b)})];
 for(const f of sorted){const i=f.lastIndexOf("/"),dir=i<0?"":f.slice(0,i);
  if(dir!==lastDir&&dir){const d=document.createElement("div");d.className="dir";d.textContent=dir+"/";t.appendChild(d)}lastDir=dir;
  const el=document.createElement("div");el.className="f"+(f===files[0]?" main":"")+(f===file?" on":"");el.dataset.f=f;el.title=f;
  el.textContent=f.slice(i+1);el.onclick=()=>openFile(f);t.appendChild(el)}
}
// Ctrl/Cmd+click on \input{x} \include{x} \usepackage{x} ... opens that file
cm.on("mousedown",(c,e)=>{
 if(!(e.ctrlKey||e.metaKey))return;const pos=c.coordsChar({left:e.clientX,top:e.clientY},"window"),ln=c.getLine(pos.line);
 const re=/\\(?:input|include|subfile|usepackage|RequirePackage|addbibresource|bibliography|documentclass)\*?(?:\[[^\]]*\])?\{([^}]*)\}/g;
 for(const m of ln.matchAll(re)){if(pos.ch<m.index||pos.ch>m.index+m[0].length)continue;
  for(let n of m[1].split(",")){n=n.trim().replace(/^\.\//,"");
   const hit=[n,n+".tex",n+".sty",n+".cls",n+".bib"].find(x=>files.includes(x));
   if(hit){e.preventDefault();openFile(hit);return}}
  toast("not an editable file in this project: "+m[1],true);e.preventDefault();return}
});
async function loadFiles(){
 const sel=$("file");sel.replaceChildren();file="";files=[];$("tree").replaceChildren();
 cm.setValue("");cm.setOption("readOnly","nocursor");clean=cm.changeGeneration();mark();
 if(!doc)return;files=await (await fetch("/files?"+q())).json();
 for(const f of files){const o=document.createElement("option");o.value=o.textContent=f;sel.appendChild(o)}
 buildTree();const last=store.get("lp-file:"+doc);openFile(files.includes(last)?last:files[0],true);
}
// pick up edits made on disk by others while the buffer is clean
setInterval(async()=>{
 if(!file||dirty()||saving||document.hidden)return;
 try{const j=await (await fetch("/file?"+fq(file)+"&stat=1")).json();
  if(j.mtime!==mtime&&!dirty()){const c=cm.getCursor(),s=cm.getScrollInfo();await openFile(file,true);cm.setCursor(c);cm.scrollTo(s.left,s.top)}}catch(e){}
},3000);
addEventListener("beforeunload",e=>{if(dirty()){e.preventDefault();e.returnValue=""}});

// ---- preview ----
function select(d,user){
 doc=d;ver=-1;store.set("lp-doc",d);history.replaceState(null,"",d?"?"+q():"/");
 $("lp").href="/pdf?"+q();$("ll").href="/log?"+q();document.title=d?d.split("/").pop()+" — LaTeX":"LaTeX editor";
 if(user){$("pvpane").scrollTop=0;$("pages").innerHTML='<div id="empty">'+(d?"Building…":"Choose a document above.")+'</div>'}
 clearTimeout(timer);poll();loadFiles();
}
async function render(v){
 if(mode==="edit")return;if(rendering){again=true;return}rendering=true;const d=doc,pane=$("pvpane"),vis=mode==="vis";
 try{
  const pdf=await pdfjsLib.getDocument({url:"/pdf?"+q()+"&v="+v,isEvalSupported:false}).promise;
  if(!scale){const vp1=(await pdf.getPage(1)).getViewport({scale:1});scale=Math.max(.5,Math.min(1.6,(pane.clientWidth-40)/vp1.width))}
  const y=pane.scrollTop,frag=document.createDocumentFragment(),dpr=window.devicePixelRatio||1;
  for(let i=1;i<=pdf.numPages;i++){
   const pg=await pdf.getPage(i),vp=pg.getViewport({scale}),hi=pg.getViewport({scale:scale*dpr});
   const box=document.createElement("div");box.className="page";box.style.width=vp.width+"px";box.style.height=vp.height+"px";
   box.dataset.page=i;box.dataset.scale=scale;
   const c=document.createElement("canvas");c.width=hi.width;c.height=hi.height;box.appendChild(c);
   await pg.render({canvasContext:c.getContext("2d"),viewport:hi}).promise;
   if(vis){const tl=document.createElement("div");tl.className="textLayer";tl.style.setProperty("--scale-factor",scale);box.appendChild(tl);
    await pdfjsLib.renderTextLayer({textContentSource:await pg.getTextContent(),container:tl,viewport:vp,textDivs:[]}).promise}
   frag.appendChild(box);
  }
  if(d===doc){$("pages").replaceChildren(frag);pane.scrollTop=y}
  $("zv").textContent=Math.round(scale/1.3*100)+"%";
 }catch(e){$("st").textContent="render error: "+e}
 rendering=false;if(again){again=false;render(ver)}
}
function pdfPoint(box,cx,cy){const r=box.getBoundingClientRect(),s=parseFloat(box.dataset.scale);
 return {page:+box.dataset.page,x:(cx-r.left)/s,y:(cy-r.top)/s}}
// double-click the PDF (Both/Preview) -> open source at that spot
$("pages").addEventListener("dblclick",async e=>{
 if(mode==="vis")return;const box=e.target.closest(".page");if(!box)return;const p=pdfPoint(box,e.clientX,e.clientY);
 const j=await (await fetch(`/synctex?${q()}&page=${p.page}&x=${p.x}&y=${p.y}`)).json();
 if(!j.file){toast("no source found for that spot",true);return}
 if(mode==="pv")view("both");openFile(j.file,false,j.line);
});
// Visual mode: click a text run -> inline editor
let editing=null;
$("pages").addEventListener("click",e=>{
 if(mode!=="vis"||editing)return;const span=e.target.closest(".textLayer span");if(!span||!span.textContent.trim())return;
 e.preventDefault();startEdit(span);
});
function startEdit(span){
 const box=span.closest(".page"),br=box.getBoundingClientRect(),r=span.getBoundingClientRect();
 const inp=document.createElement("input");inp.className="vedit";inp.value=span.textContent;inp.spellcheck=true;
 const fs=Math.max(11,r.height*.82);
 Object.assign(inp.style,{left:(r.left-br.left-5)+"px",top:(r.top-br.top-3)+"px",height:(r.height+6)+"px",fontSize:fs+"px",
  width:Math.min(br.width-(r.left-br.left)+5,Math.max(r.width+60,120))+"px"});
 box.appendChild(inp);inp.focus();
 editing={span,inp,box,old:span.textContent,pt:pdfPoint(box,r.left+r.width/2,r.top+r.height/2)};
 let done=false;
 const finish=async commit=>{if(done)return;done=true;const ed=editing;editing=null;inp.remove();
  if(commit&&inp.value!==ed.old)await visualSave(ed,inp.value)};
 inp.addEventListener("keydown",ev=>{if(ev.key==="Enter"){ev.preventDefault();finish(true)}else if(ev.key==="Escape"){finish(false)}});
 inp.addEventListener("blur",()=>finish(true));
}
async function visualSave(ed,val){
 if(dirty())await save();
 try{
  const r=await fetch("/visual-edit?"+q(),{method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({page:ed.pt.page,x:ed.pt.x,y:ed.pt.y,old:ed.old,new:val})});
  const j=r.ok?await r.json():{ok:false,reason:await r.text()};
  if(j.ok){ed.span.textContent=val;ed.span.classList.add("pending");toast("saved to "+j.file+":"+j.line+" — rebuilding…")}
  else{toast((j.reason||"failed")+(j.file?"  ("+j.file+":"+j.line+")":""),true);
   if(j.file&&confirm("Could not apply this edit automatically:\n"+j.reason+"\n\nOpen "+j.file+" at line "+j.line+" in the source editor?")){view("both");openFile(j.file,false,j.line)}}
 }catch(e){toast("visual edit failed: "+e,true)}
}
$("doc").onchange=async e=>{if(dirty())await save();select(e.target.value,true)};
async function loadDocs(){
 const list=await (await fetch("/docs")).json(),sel=$("doc");
 for(const d of list){const o=document.createElement("option");o.value=o.textContent=d;sel.appendChild(o)}
 if(doc&&!list.includes(doc))doc="";sel.value=doc;select(doc,false);
}
let shownErrs="";
async function poll(){
 if(!doc){$("st").textContent="";$("dot").style.background="var(--btn)";$("errs").style.display="none";return}
 try{
  const r=await fetch("/status?"+q());if(!r.ok)throw r.status;const s=await r.json();
  $("dot").style.background={ok:"var(--ok)",error:"var(--err)"}[s.status]||"var(--busy)";
  $("st").textContent=s.status+(s.finished?" · "+s.finished+" ("+s.duration+"s)":"")+" · "+s.engine;
  const key=JSON.stringify(s.errors);
  if(key!==shownErrs){shownErrs=key;const box=$("errs");box.replaceChildren();box.style.display=s.errors.length?"block":"none";
   for(const e of s.errors){const div=document.createElement("div");div.textContent=e.msg;
    if(e.file){div.className="jump";div.title="open "+e.file+":"+e.line;div.onclick=()=>{if(mode!=="both"&&mode!=="edit")view("both");openFile(e.file,false,e.line)}}
    box.appendChild(div)}}
  if(s.version!==ver&&s.version>0&&!editing){ver=s.version;await render(ver)}
 }catch(e){$("st").textContent="server unreachable";$("dot").style.background="var(--err)"}
 timer=setTimeout(poll,1000);
}
loadDocs();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def authed(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode().split(":", 1)[1]
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        self.send(401, b"password required", "text/plain", [("WWW-Authenticate", 'Basic realm="latex-preview"')])
        return False

    def route(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        return u.path, qs.get("doc", [""])[0], qs

    def do_GET(self):
        if not self.authed():
            return
        path, rel, qs = self.route()
        if path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/docs":
            return self.json(find_docs(REG.root))
        b = REG.get(rel)
        if b is None:
            return self.send(404, b"unknown document", "text/plain")
        if path == "/pdf":
            if b.preview.exists():
                self.send(200, b.preview.read_bytes(), "application/pdf")
            else:
                self.send(404, b"no pdf yet", "text/plain")
        elif path == "/status":
            self.json(b.state)
        elif path == "/log":
            log = b.out / (b.main.stem + ".log")
            txt = b.buildlog.read_text(errors="ignore") if b.buildlog.exists() else ""
            if log.exists():
                txt += "\n\n===== " + log.name + " =====\n" + log.read_text(errors="ignore")
            self.send(200, txt.encode(), "text/plain; charset=utf-8")
        elif path == "/files":
            self.json(b.files())
        elif path == "/synctex":
            try:
                loc = b.synctex(qs["page"][0], qs["x"][0], qs["y"][0])
            except (KeyError, ValueError):
                return self.send(400, b"page, x, y required", "text/plain")
            self.json({"file": loc[0], "line": loc[1]} if loc else {})
        elif path == "/file":
            p = b.resolve(qs.get("path", [""])[0])
            if p is None or not p.is_file():
                return self.send(404, b"no such editable file", "text/plain")
            st = p.stat()
            if "stat" in qs:
                return self.json({"mtime": str(st.st_mtime_ns)})
            if st.st_size > MAX_EDIT_BYTES:
                return self.send(413, b"file too large", "text/plain")
            self.json({"text": p.read_text(errors="replace"), "mtime": str(st.st_mtime_ns)})
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self.authed():
            return
        path, rel, _ = self.route()
        b = REG.get(rel)
        if path == "/rebuild" and b:
            b.request()
            self.send(202, b"ok", "text/plain")
        elif path == "/visual-edit" and b:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                j = json.loads(self.rfile.read(min(n, 100_000)))
                self.json(b.visual_edit(j["page"], j["x"], j["y"], str(j["old"]), str(j["new"])))
            except (KeyError, ValueError, TypeError) as e:
                self.send(400, f"bad request: {e}".encode(), "text/plain")
        else:
            self.send(404, b"not found", "text/plain")

    def do_PUT(self):
        if not self.authed():
            return
        path, rel, qs = self.route()
        b = REG.get(rel)
        p = b.resolve(qs.get("path", [""])[0]) if b else None
        if path != "/file" or p is None:
            return self.send(404, b"no such editable file", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_EDIT_BYTES:
            return self.send(413, b"too large", "text/plain")
        data = self.rfile.read(n)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return self.send(400, b"not utf-8", "text/plain")
        with SAVE_LOCK:
            base = self.headers.get("X-Base-Mtime", "0")  # str: ns exceeds JS safe ints
            if base != "0" and p.exists() and str(p.stat().st_mtime_ns) != base:
                return self.json({"error": "conflict", "mtime": str(p.stat().st_mtime_ns)}, 409)
            write_atomic(p, data)
            mt = str(p.stat().st_mtime_ns)
        print(f"saved {p} ({len(data)} B) from {self.client_address[0]}", flush=True)
        self.json({"mtime": mt})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--engine", default="xelatex", choices=ENGINES)
    ap.add_argument("--once", action="store_true", help="build one document (dir or .tex) and exit")
    a = ap.parse_args()
    target = Path(a.root).resolve()
    if a.once:
        b = Builder(target if target.suffix == ".tex" else find_main(target), a.engine)
        ok = b.build()
        print("\n".join(e["msg"] for e in b.state["errors"]))
        print(b.preview)
        sys.exit(0 if ok else 1)
    PASSWORD = os.environ.get("LATEX_PREVIEW_PASSWORD", "")
    SAVE_LOCK = threading.Lock()
    REG = Registry(target, a.engine)
    threading.Thread(target=REG.watch, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"latex-preview: root {target} -> http://{a.host}:{a.port}/ (auth {'on' if PASSWORD else 'OFF'})", flush=True)
    srv.serve_forever()

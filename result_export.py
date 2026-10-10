"""Export the latest discount-backtest / optimizer / validation results and push them to a dedicated git
branch so they can be analysed remotely.

Only RESULTS are exported (parameters, summaries, trades, optimizer tables) — never the raw intraday data.

The push uses git plumbing on a temporary index, so the user's working tree, index, HEAD and current branch
are never touched.  Each push is one commit on ``BRANCH`` (fast-forward only; history = every export).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

BRANCH = "analysis-results"
LOCAL_DIR = "analysis_results"
MAX_TRADES = 6000
MAX_CURVE = 600


def _thin(seq, n):
    if not isinstance(seq, list) or len(seq) <= n:
        return seq
    step = len(seq) / float(n)
    return [seq[int(i * step)] for i in range(n)] + [seq[-1]]


def _slim_backtest(res: dict) -> dict:
    out = dict(res)
    tr = out.get("trades") or []
    if len(tr) > MAX_TRADES:
        out["trades"] = tr[:MAX_TRADES]
        out["trades_truncated"] = len(tr)
    if isinstance(out.get("exposure"), dict):
        ex = dict(out["exposure"])
        ex["curve"] = _thin(ex.get("curve"), MAX_CURVE)
        out["exposure"] = ex
    if isinstance(out.get("bubble_index"), dict):
        bi = dict(out["bubble_index"])
        bi["curve"] = _thin(bi.get("curve"), MAX_CURVE)
        out["bubble_index"] = bi
    out["equity_curve"] = _thin(out.get("equity_curve"), MAX_CURVE)
    return out


def _git(repo: Path, *args, env=None, input_bytes: bytes | None = None, timeout=120):
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        e.update(env)
    return subprocess.run(["git", *args], cwd=str(repo), env=e, input=input_bytes, capture_output=True,
                          timeout=timeout)


def _txt(cp) -> str:
    return (cp.stdout.decode("utf-8", "replace") + cp.stderr.decode("utf-8", "replace")).strip()


def build_files(state: dict, code_version: dict | None = None) -> dict[str, bytes]:
    """``state`` = {"backtest": res, "study": res, "validation": res, "stats": res} (any may be missing)."""
    files: dict[str, bytes] = {}
    meta = {"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "code": code_version or {}, "contents": []}

    def put(name: str, obj):
        files[f"results/{name}.json"] = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                                                  default=str).encode("utf-8")
        meta["contents"].append(name)

    if state.get("backtest"):
        put("backtest", _slim_backtest(state["backtest"]))
    if state.get("study"):
        put("optimizer", state["study"])
    if state.get("validation"):
        put("validation", state["validation"])
    if state.get("stats"):
        put("stats", state["stats"])
    files["results/meta.json"] = json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8")
    return files


def save_local(files: dict[str, bytes], root: Path) -> Path:
    d = root / LOCAL_DIR
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    for path, data in files.items():
        p = d / Path(path).name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def code_version(repo: Path) -> dict:
    try:
        sha = _txt(_git(repo, "rev-parse", "HEAD"))
        br = _txt(_git(repo, "rev-parse", "--abbrev-ref", "HEAD"))
        dirty = bool(_txt(_git(repo, "status", "--porcelain", "--untracked-files=no")))
        return {"commit": sha, "branch": br, "uncommitted_changes": dirty}
    except Exception:
        return {}


def push_files(repo: Path, files: dict[str, bytes], branch: str = BRANCH, remote: str = "origin",
               message: str | None = None) -> dict:
    """Commit ``files`` on top of ``remote/branch`` (or as its first commit) and push. Never touches the
    working tree / index / HEAD.  Returns {"ok", "commit", "message"}."""
    chk = _git(repo, "rev-parse", "--git-dir")
    if chk.returncode != 0:
        return {"ok": False, "message": "پوشهٔ برنامه یک مخزن git نیست."}
    ls = _git(repo, "ls-remote", "--heads", remote, branch, timeout=60)
    if ls.returncode != 0:
        return {"ok": False, "message": "دسترسی به مخزن راه دور ممکن نشد (اینترنت یا دسترسی/رمز git):\n" + _txt(ls)[-600:]}
    parent = None
    if ls.stdout.strip():
        f = _git(repo, "fetch", "--quiet", remote, branch, timeout=120)
        if f.returncode != 0:
            return {"ok": False, "message": "دریافت شاخهٔ نتایج ناموفق بود:\n" + _txt(f)[-600:]}
        parent = _txt(_git(repo, "rev-parse", "FETCH_HEAD"))
    tmp = tempfile.mkdtemp(prefix="disc_export_")
    try:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        if parent:
            r = _git(repo, "read-tree", parent, env=env)
            if r.returncode != 0:
                return {"ok": False, "message": "read-tree: " + _txt(r)}
        for path, data in files.items():
            h = _git(repo, "hash-object", "-w", "--stdin", input_bytes=data)
            if h.returncode != 0:
                return {"ok": False, "message": "hash-object: " + _txt(h)}
            sha = h.stdout.decode().strip()
            u = _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{path}", env=env)
            if u.returncode != 0:
                return {"ok": False, "message": "update-index: " + _txt(u)}
        w = _git(repo, "write-tree", env=env)
        if w.returncode != 0:
            return {"ok": False, "message": "write-tree: " + _txt(w)}
        tree = w.stdout.decode().strip()
        who = {"GIT_AUTHOR_NAME": "arbitrage-dashboard", "GIT_AUTHOR_EMAIL": "dashboard@localhost",
               "GIT_COMMITTER_NAME": "arbitrage-dashboard", "GIT_COMMITTER_EMAIL": "dashboard@localhost"}
        have = _git(repo, "config", "user.name").stdout.strip() and _git(repo, "config", "user.email").stdout.strip()
        cmd = ["commit-tree", tree, "-m", message or ("export results " + time.strftime("%Y-%m-%d %H:%M:%S"))]
        if parent:
            cmd[2:2] = ["-p", parent]
        c = _git(repo, *cmd, env=None if have else who)
        if c.returncode != 0:
            return {"ok": False, "message": "commit-tree: " + _txt(c)}
        commit = c.stdout.decode().strip()
        p = _git(repo, "push", remote, f"{commit}:refs/heads/{branch}", timeout=180)
        if p.returncode != 0:
            return {"ok": False, "commit": commit, "message": "push ناموفق بود:\n" + _txt(p)[-700:]}
        return {"ok": True, "commit": commit, "message": f"روی شاخهٔ «{branch}» پوش شد."}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

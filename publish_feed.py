#!/usr/bin/env python3
"""Publish one day's episode of your show to a PRIVATE podcast feed (Apple Podcasts: Library > ... > Follow a Show by URL).

What it does (idempotent: re-running for the same date/guid updates that item, never duplicates it):
  1. Picks the episode mp3 for a date (default: <date>/<file_prefix>-<date>-mobile.mp3, then <file_prefix>-<date>.mp3).
     Mono files at <= 64 kbps are used as-is; otherwise a mono copy at 64k (or lower, to fit 25 MiB) is made.
  2. Copies it into the local site folder  feed-site/<slug>/episodes/<file_prefix>-<date>[-variant].mp3
     (<slug> = long random token from feed-config.json, so URLs are unguessable).
  3. Updates episodes.json (state) and rewrites feed-site/<slug>/feed.xml: RSS 2.0 + iTunes tags,
     <itunes:block>Yes</itunes:block> (never listed in Apple's directory), enclosure length/type,
     stable GUID, pubDate in utc_offset_hours from show.json, duration, show notes (from --notes file, else the script's teaser
     lines), square artwork art/cover-3000.jpg.
  4. Uploads the site with the configured backend:
       stage   - local only (default until a host is configured); builds and validates, uploads nothing.
       cloudflare-pages - (live) Cloudflare Pages via `npx -y wrangler@3`, token from env CLOUDFLARE_API_TOKEN
                 (never printed/stored). Creates project podcast-<6 random> on first run, records it +
                 base_url in feed-config.json, deploys feed-site/<slug>/ + _headers + robots.txt + the
                 pages-functions/ range Function (Pages static assets ignore Range; Apple needs 206).
       github  - GitHub Pages via the gh CLI (needs `gh auth login` on the box). Pushes the site as a
                 single orphan commit to branch gh-pages of config.github_repo (force-push of that
                 dedicated branch only, so old audio does not pile up in git history).
  5. --check: curl -I + a byte-range request against feed / cover / newest enclosure on base_url.

Usage:
  python3 publish_feed.py 2026-10-12                         # full episode (mobile mp3)
  python3 publish_feed.py 2026-10-12 --mp3 2026-10-12/preview-v2-5min.mp3 --variant preview \
        --title "Preview: ..." --notes notes.md
  python3 publish_feed.py --remove episode-2026-10-12-preview   # drop an item
  python3 publish_feed.py --rebuild    # rewrite feed + re-upload without adding anything
  python3 publish_feed.py --check
Options: --keep N (keep newest N episodes, default 30), --backend stage|github, --dry-run.
"""
import argparse, datetime as dt, email.utils, html, json, os, re, secrets, shutil, subprocess, sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
from show_config import cover_path, state_paths
CFG_PATH, SITE = state_paths()          # one private state file + site folder per show (file_prefix)
COVER_SRC = cover_path()
from show_config import SHOW, TZ, CFG

def load_cfg():
    if os.path.exists(CFG_PATH):
        return json.load(open(CFG_PATH))
    cfg = {"backend": "stage", "slug": secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"),
           "base_url": "", "github_repo": "", "keep": 30}
    json.dump(cfg, open(CFG_PATH, "w"), indent=2); os.chmod(CFG_PATH, 0o600)
    return cfg

def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout

def probe(path):
    out = json.loads(sh(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path]))
    st = out["streams"][0]
    return float(out["format"]["duration"]), int(st.get("channels", 0)), int(st.get("bit_rate") or out["format"].get("bit_rate", 0))

MAX_BYTES = 25 * 1024 * 1024 - 64 * 1024   # Cloudflare Pages: 25 MiB per file (small margin)

def ensure_mobile(src):
    """Anything already mono and <= 64 kbps is used as-is (never re-encoded up). Otherwise make a mono copy at
    64k, or lower (56/48/40/32k) if needed so the file fits the 25 MiB per-file limit of Cloudflare Pages."""
    dur, ch, br = probe(src)
    if ch == 1 and br <= 66000 and os.path.getsize(src) <= MAX_BYTES:
        return src
    for kbps in (64, 56, 48, 40, 32):
        if dur * kbps * 1000 / 8 * 1.01 <= MAX_BYTES or kbps == 32:
            break
    dst = re.sub(r"\.mp3$", "", src) + f"-{kbps}k.mp3"
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        sh(["ffmpeg", "-y", "-v", "error", "-i", src, "-ac", "1", "-ar", "44100", "-b:a", f"{kbps}k", "-map_metadata", "-1", dst])
    if os.path.getsize(dst) > MAX_BYTES:
        sys.exit(f"{dst} is {os.path.getsize(dst)} bytes, over the 25 MiB Pages limit")
    return dst

def strip_glosses(s):
    return re.sub(r"\{[^}]*\}", "", s).strip()

def auto_notes(date):
    """Show notes from the episode script: the teaser lines after "Here's what's coming today"."""
    for name in ("script-v2.md", "script.md", "preview-v2-script.md"):
        p = os.path.join(HERE, date, name)
        if not os.path.exists(p): continue
        lines = [l.strip() for l in open(p, encoding="utf-8")]
        out, on = [], False
        for l in lines:
            m = re.match(r"^[A-Z]+:\s*(.+)$", l)
            if not m: 
                if on and l.startswith("["): break
                continue
            t = strip_glosses(m.group(1))
            if not on and re.search(r"\bOne:", t):
                on = True; t = t[t.index("One:"):]
            if on:
                if t.startswith(("Nice.", "Okay.")) or "let's get into it" in t.lower(): break
                out.append(t)
        if out:
            return "\n".join(out)
    return f"Today's episode of {SHOW['title']}."

def fmt_dur(sec):
    sec = int(round(sec)); return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)

def build_feed(cfg, eps):
    base = cfg["base_url"].rstrip("/") + "/" + cfg["slug"]
    now = email.utils.format_datetime(dt.datetime.now(TZ))
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
         'xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom" '
         'xmlns:podcast="https://podcastindex.org/namespace/1.0">',
         "<channel>",
         f'<atom:link href="{escape(base)}/feed.xml" rel="self" type="application/rss+xml"/>',
         f"<title>{escape(SHOW['title'])}</title>",
         f"<link>{escape(base)}/feed.xml</link>",
         f"<language>{SHOW['language']}</language>",
         f"<description>{escape(SHOW['description'])}</description>",
         f"<lastBuildDate>{now}</lastBuildDate>",
         f"<itunes:author>{escape(SHOW['author'])}</itunes:author>",
         f"<itunes:subtitle>{escape(SHOW['subtitle'])}</itunes:subtitle>",
         f"<itunes:summary>{escape(SHOW['description'])}</itunes:summary>",
         f"<itunes:owner><itunes:name>{escape(SHOW['owner_name'])}</itunes:name>"
         f"<itunes:email>{escape(SHOW['owner_email'])}</itunes:email></itunes:owner>",
         f'<itunes:image href="{escape(base)}/cover.jpg"/>',
         f"<image><url>{escape(base)}/cover.jpg</url><title>{escape(SHOW['title'])}</title><link>{escape(base)}/feed.xml</link></image>",
         f'<itunes:category text="{SHOW["category"]}"/>',
         "<itunes:explicit>false</itunes:explicit>",
         "<itunes:type>episodic</itunes:type>",
         "<itunes:block>Yes</itunes:block>",
         "<itunes:complete>No</itunes:complete>"]
    for e in sorted(eps, key=lambda e: e["pub_ts"], reverse=True):
        url = f"{base}/episodes/{e['file']}"
        notes = e["notes"][:3999]
        notes_html = e.get("notes_html") or "<p>" + "</p><p>".join(html.escape(l) for l in notes.splitlines() if l.strip()) + "</p>"
        extra = []
        if e.get("vtt"): extra.append(f'<podcast:transcript url="{escape(base)}/tx/{e["vtt"]}" type="text/vtt" language="en" rel="captions"/>')
        if e.get("srt"): extra.append(f'<podcast:transcript url="{escape(base)}/tx/{e["srt"]}" type="application/x-subrip" language="en" rel="captions"/>')
        if e.get("chapters"): extra.append(f'<podcast:chapters url="{escape(base)}/tx/{e["chapters"]}" type="application/json+chapters"/>')
        x += ["<item>",
              f"<title>{escape(e['title'])}</title>",
              f'<guid isPermaLink="false">{escape(e["guid"])}</guid>',
              f"<pubDate>{email.utils.format_datetime(dt.datetime.fromtimestamp(e['pub_ts'], TZ))}</pubDate>",
              f"<description>{escape(notes)}</description>",
              f"<content:encoded><![CDATA[{notes_html.replace("]]>", "]]&gt;")}]]></content:encoded>",
              f"<itunes:summary>{escape(notes)}</itunes:summary>",
              f'<enclosure url="{escape(url)}" length="{e["length"]}" type="audio/mpeg"/>',
              f"<itunes:duration>{fmt_dur(e['duration'])}</itunes:duration>",
              f"<itunes:episodeType>{'bonus' if e.get('variant') else 'full'}</itunes:episodeType>",
              "<itunes:explicit>false</itunes:explicit>"] + extra + ["</item>"]
    x += ["</channel>", "</rss>", ""]
    return "\n".join(x)

GITHUB_PUBLIC_WARNING = """WARNING: GitHub Pages sites are PUBLIC. Anyone who finds the URL can download every episode,
and the repo itself is visible unless you pay for private Pages. Use cloudflare-pages for a private feed.
To go ahead anyway set "output": {"github_public_ok": true} in show.json."""

def upload_github(cfg, root):
    print(GITHUB_PUBLIC_WARNING, file=sys.stderr)
    if not CFG["output"].get("github_public_ok"): sys.exit("github backend refused: output.github_public_ok is false")
    repo = cfg.get("github_repo")
    if not repo: sys.exit("config.github_repo is empty (owner/name of the dedicated Pages repo)")
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0: sys.exit("gh is not logged in on the box: run `gh auth login` first")
    work = os.path.join(HERE, ".pages-push")
    shutil.rmtree(work, ignore_errors=True); shutil.copytree(root, work)
    open(os.path.join(work, ".nojekyll"), "w").close()
    open(os.path.join(work, "index.html"), "w").write("<!doctype html><title>404</title>")  # no listing
    sh(["git", "init", "-q", "-b", "gh-pages"], cwd=work)
    sh(["git", "-c", "user.name=podcast-bot", "-c", "user.email=podcast-bot@localhost", "add", "-A"], cwd=work)
    sh(["git", "-c", "user.name=podcast-bot", "-c", "user.email=podcast-bot@localhost", "commit", "-q", "-m", "publish"], cwd=work)
    sh(["gh", "auth", "setup-git"])
    sh(["git", "push", "-q", "--force", f"https://github.com/{repo}.git", "gh-pages:gh-pages"], cwd=work)
    shutil.rmtree(work, ignore_errors=True)
    print("pushed to", repo, "(Pages may take ~1 min to refresh)")

HEADERS = """/*
  X-Robots-Tag: noindex, nofollow
  Access-Control-Allow-Origin: *
/{slug}/tx/*.vtt
  Content-Type: text/vtt; charset=utf-8
  Cache-Control: public, max-age=300
/{slug}/tx/*.srt
  Content-Type: application/x-subrip; charset=utf-8
  Cache-Control: public, max-age=300
/{slug}/tx/*.json
  Content-Type: application/json+chapters; charset=utf-8
  Cache-Control: public, max-age=300
/{slug}/ch/*
  Content-Type: image/jpeg
/*.mp3
  Content-Type: audio/mpeg
  Cache-Control: public, max-age=86400
/*/feed.xml
  Content-Type: application/rss+xml; charset=utf-8
  Cache-Control: public, max-age=300
/*/cover.jpg
  Content-Type: image/jpeg
"""

def cf_env(cfg):
    """Environment for wrangler. The token is only read from the environment (CLOUDFLARE_API_TOKEN), never
    written to config, logs or stdout."""
    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        sys.exit("CLOUDFLARE_API_TOKEN is not set in the environment")
    env = dict(os.environ); acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or cfg.get("cloudflare_account_id", "")
    if not acct: sys.exit("set CLOUDFLARE_ACCOUNT_ID in the environment (or cloudflare_account_id in feed-config.json)")
    env["CLOUDFLARE_ACCOUNT_ID"] = acct
    env["WRANGLER_SEND_METRICS"] = "false"; env["CI"] = "1"
    return env

WR_DIR = os.path.expanduser("~/.cache/podcast-wrangler")  # own cwd with node_modules/, so wrangler's cache dir is writable

def wrangler(args, env):
    os.makedirs(os.path.join(WR_DIR, "node_modules"), exist_ok=True)
    if not os.path.exists(os.path.join(WR_DIR, "package.json")):
        open(os.path.join(WR_DIR, "package.json"), "w").write('{"name":"podcast-wrangler","private":true}')
    r = subprocess.run(["npx", "-y", "wrangler@3"] + args, capture_output=True, text=True, env=env, cwd=WR_DIR)
    tok = env.get("CLOUDFLARE_API_TOKEN") or "\0"
    out = (r.stdout + r.stderr).replace(tok, "***")
    return r.returncode, out

def ensure_cf_project(cfg):
    env = cf_env(cfg)
    proj = cfg.get("cloudflare_project")
    if not proj:
        proj = "podcast-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6))
        rc, out = wrangler(["pages", "project", "create", proj, "--production-branch", "main"], env)
        if rc != 0: sys.exit("project create failed:\n" + out[-2000:])
        cfg["cloudflare_project"] = proj; cfg["base_url"] = f"https://{proj}.pages.dev"; cfg["backend"] = "cloudflare-pages"
        json.dump(cfg, open(CFG_PATH, "w"), indent=2); os.chmod(CFG_PATH, 0o600)
        print("created Pages project", proj)
    return proj

def upload_cloudflare(cfg, root):
    env = cf_env(cfg)
    proj = ensure_cf_project(cfg)
    # deploy dir = feed-site/ minus local state (episodes.json); slug folder kept inside it
    work = os.path.join(HERE, ".pages-deploy")
    shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
    shutil.copytree(root, os.path.join(work, cfg["slug"]))
    open(os.path.join(work, "_headers"), "w").write(HEADERS.replace("{slug}", cfg["slug"]))  # Pages: max one * per rule
    open(os.path.join(work, "robots.txt"), "w").write("User-agent: *\nDisallow: /\n")
    for f in os.listdir(os.path.join(work, cfg["slug"], "episodes")):
        if os.path.getsize(os.path.join(work, cfg["slug"], "episodes", f)) > 25 * 1024 * 1024:
            sys.exit(f"{f} is over the 25 MiB Pages limit")
    # Pages Function for mp3 byte ranges (Pages static assets answer Range with 200). wrangler compiles ./functions
    fdir = os.path.join(WR_DIR, "functions"); shutil.rmtree(fdir, ignore_errors=True)
    shutil.copytree(os.path.join(HERE, "pages-functions"), fdir)
    rc, out = wrangler(["pages", "deploy", work, "--project-name", proj, "--branch", "main", "--commit-dirty=true"], env)
    shutil.rmtree(work, ignore_errors=True)
    if rc != 0: sys.exit("deploy failed:\n" + out[-2000:])
    m = re.search(r"https://\S+\.pages\.dev", out)
    print("deployed to Pages project", proj, "(preview:", (m.group(0) if m else "?") + ")")

def check(cfg, eps):
    base = cfg["base_url"].rstrip("/") + "/" + cfg["slug"]
    if not cfg["base_url"]: sys.exit("no base_url configured (backend=stage); nothing public to check")
    urls = [base + "/feed.xml", base + "/cover.jpg"]
    if eps:
        last = sorted(eps, key=lambda e: e['pub_ts'])[-1]
        urls.append(f"{base}/episodes/{last['file']}")
        for k in ("vtt", "srt", "chapters"):
            if last.get(k): urls.append(f"{base}/tx/{last[k]}")
        if last.get("chapters"): urls.append(f"{base}/ch/{last['guid']}/00.jpg")
    ok = True
    for u in urls:
        head = subprocess.run(["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code} %{content_type} %{size_header}", u], capture_output=True, text=True).stdout
        rng = subprocess.run(["curl", "-sL", "-r", "0-1023", "-o", "/dev/null", "-w", "%{http_code} %{size_download}", u], capture_output=True, text=True).stdout
        print(f"{u}\n  HEAD: {head}\n  RANGE 0-1023: {rng}")
        good = head.startswith("200")
        if u.endswith(".mp3"):
            good = good and "audio/mpeg" in head and rng.startswith("206")
        if u.endswith(".xml"):
            good = good and "xml" in head
        for ext, ct in ((".vtt", "text/vtt"), (".srt", "application/x-subrip"), (".json", "application/json"), (".jpg", "image/jpeg")):
            if u.endswith(ext): good = good and ct in head
        if not u.endswith(".mp3"):
            cors = subprocess.run(["curl", "-sI", u], capture_output=True, text=True).stdout.lower()
            good = good and "access-control-allow-origin: *" in cors
        ok = ok and good
    print("CHECK", "OK" if ok else "FAILED"); return ok

def episode_extras(date, guid, src_mp3, dst_mp3, root, cfg, notes_override=None, ep_kind="content"):
    """Captions, chapters (ID3 + JSON, with per-story art) and HTML show notes for one episode.
    Returns dict of item fields. Missing pieces are skipped with a note, never fatal."""
    import episode_extras as X
    out, base = {}, cfg.get("base_url", "").rstrip("/") + "/" + cfg["slug"]
    stem = re.sub(r"(-\d+k)?\.mp3$", "", src_mp3)
    if not os.path.exists(stem + ".chapters.json"):     # captions may be named after the full mp3 (no -mobile)
        stem = re.sub(r"-mobile$", "", stem)
    tl_p = stem + ".chapters.json"
    tx = os.path.join(root, "tx"); os.makedirs(tx, exist_ok=True)
    for ext in ("vtt", "srt"):
        if os.path.exists(f"{stem}.{ext}"):
            shutil.copyfile(f"{stem}.{ext}", os.path.join(tx, f"{guid}.{ext}")); out[ext] = f"{guid}.{ext}"
        else:
            print(f"note: no {stem}.{ext} (build with make_podcast.py, or backfill with --timings-only)")
    sections = X.load_daily(date) if ep_kind == "content" else []
    roles = X.load_job_roles(date) if ep_kind == "jobs" else None
    script = next((os.path.join(HERE, date, n) for n in ("script-v2.md", "script.md") if os.path.exists(os.path.join(HERE, date, n))), None)
    chapters = []
    if os.path.exists(tl_p):
        tl = json.load(open(tl_p, encoding="utf-8"))
        dur_ms = int(probe(dst_mp3)[0] * 1000)
        stories = X.match_stories(tl, sections) if ep_kind == "content" else []
        for c in tl["chapters"]:
            t = c["title"]; low = t.lower()
            kind = "segment" if low.startswith("segment") else "break" if "break" in low else "sign-off" if "sign" in low else "intro"
            ch = {"start": c["start_sec"], "title": t, "kind": kind, "url": "", "item": None}
            if roles:   # role chapters link to the original posting
                r = X.role_for_chapter(t, roles)
                if r: ch["url"] = r["url"]; ch["kind"] = "role"
            chapters.append(ch)
        for st in stories:
            it = st["item"]; url = it.get("video") or it.get("url") or ""
            seg = next((c for c in chapters if 0 <= st["start"] - c["start"] < 8 and c["item"] is None), None)
            if seg:   # story starts right at a segment start: the segment chapter carries its art + link
                seg.update(item=it, url=url); continue
            title = re.sub(r"\s*\((?:from|HN|guest)[^)]*\)", "", it["title"])
            short = title if len(title) <= 45 else title[:44].rsplit(" ", 1)[0].rstrip(" ,:;—–-") + "…"  # Apple: <=45 chars
            chapters.append({"start": st["start"], "title": short, "art_title": title, "kind": "story", "url": url, "item": it})
        chapters.sort(key=lambda c: c["start"])
        # art: fetch sources in parallel (cached), then fit all images into the 25 MiB budget
        urls = sorted({c["item"]["url"] for c in chapters if c["item"] and c["item"].get("url")})
        with __import__("concurrent.futures").futures.ThreadPoolExecutor(6) as ex:
            srcs = dict(zip(urls, ex.map(X.source_image, urls)))
        chd = os.path.join(root, "ch", guid); shutil.rmtree(chd, ignore_errors=True); os.makedirs(chd)
        jobs, hows = [], {}
        for i, c in enumerate(chapters):
            im, how = srcs.get(c["item"]["url"], (None, "cover")) if c["item"] and c["item"].get("url") else (None, "cover")
            c["img_name"] = f"ch/{guid}/{i:02d}.jpg"; c["img_path"] = os.path.join(root, c["img_name"])
            hows[how] = hows.get(how, 0) + 1
            jobs.append((c["img_path"], im, c.get("art_title", c["title"]), "story" if c["kind"] == "story" else c["kind"]))
        budget = X.MAX_BYTES - os.path.getsize(dst_mp3) - 300_000
        per = max(40_000, min(150_000, budget // max(1, len(jobs))))
        X.fit_jpegs(jobs, per)
        X.embed_id3(dst_mp3, chapters, dur_ms)
        if os.path.getsize(dst_mp3) > X.MAX_BYTES: sys.exit(f"{dst_mp3} over 25 MiB after chapter art")
        cj = X.chapters_json(chapters, base); json.dump(cj, open(os.path.join(tx, f"{guid}.chapters.json"), "w"), ensure_ascii=False, indent=1)
        out["chapters"] = f"{guid}.chapters.json"
        print(f"chapters: {len(chapters)} ({sum(c['kind']=='story' for c in chapters)} stories); art sources {hows}; ~{per//1000} KB/img budget")
    else:
        print(f"note: no {tl_p}: no chapters/art (build with the current make_podcast.py)")
    if notes_override:
        return out
    if ep_kind == "jobs":
        jscript = os.path.join(HERE, date, f"jobs-script-{date}.md")
        html_notes, plain = X.jobs_notes(date, roles, jscript, chapters,
                                         transcript_url=f"{base}/tx/{out['vtt']}" if out.get("vtt") else "")
        out["notes_html"], out["notes"] = html_notes, plain
        return out
    teaser = auto_notes(date)
    html_notes, plain = X.show_notes(date, sections, chapters, X.feishu_links(date), X.zh_keypoints(script),
                                     "" if teaser.startswith("Today's episode") else teaser,
                                     transcript_url=f"{base}/tx/{out['vtt']}" if out.get("vtt") else "")
    out["notes_html"], out["notes"] = html_notes, plain
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?")
    ap.add_argument("--mp3"); ap.add_argument("--variant", default="")
    ap.add_argument("--title"); ap.add_argument("--notes", help="file with show notes (plain text/markdown)")
    ap.add_argument("--pubtime", default=None, help="HH:MM local time (utc_offset_hours) used as pubDate (defaults from show.json)")
    ap.add_argument("--kind", choices=["content", "jobs"], default="content",
                    help="content = main episode; jobs = the single-host Jobs Briefing (separate item, GUID <file_prefix>-jobs-<date>)")
    ap.add_argument("--remove"); ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--check", action="store_true"); ap.add_argument("--keep", type=int)
    ap.add_argument("--config", help="show.json to use"); ap.add_argument("--backend", choices=["stage", "github", "cloudflare-pages"]); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg()
    backend = a.backend or {"local": "stage"}.get(CFG["output"]["backend"], CFG["output"]["backend"])
    if backend == "github": print(GITHUB_PUBLIC_WARNING, file=sys.stderr)
    root = os.path.join(SITE, cfg["slug"]); os.makedirs(os.path.join(root, "episodes"), exist_ok=True)
    state_p = os.path.join(SITE, "episodes.json")
    eps = json.load(open(state_p)) if os.path.exists(state_p) else []
    if a.check: sys.exit(0 if check(cfg, eps) else 1)
    if a.remove:
        eps = [e for e in eps if e["guid"] != a.remove]
    elif a.date:
        d = dt.date.fromisoformat(a.date)
        src = a.mp3
        if not src:
            names = (f"jobs-briefing-{a.date}-mobile.mp3", f"jobs-briefing-{a.date}.mp3", f"jobs-{a.date}-mobile.mp3",
                     f"jobs-{a.date}.mp3") if a.kind == "jobs" else ()
            for n in names or (f"{SHOW['file_prefix']}-{a.date}-mobile.mp3", f"{SHOW['file_prefix']}-{a.date}.mp3"):
                if os.path.exists(os.path.join(HERE, a.date, n)): src = os.path.join(a.date, n); break
            else:
                sys.exit(f"no episode mp3 found in {a.date}/")
        if not os.path.isabs(src): src = os.path.join(HERE, src)
        if not os.path.exists(src): sys.exit(f"no mp3: {src}")
        mob = ensure_mobile(src)
        guid = (SHOW["file_prefix"] + "-jobs-" if a.kind == "jobs" else SHOW["file_prefix"] + "-") + a.date + (f"-{a.variant}" if a.variant else "")
        fname = guid + ".mp3"
        dst = os.path.join(root, "episodes", fname)
        shutil.copyfile(mob, dst)   # fresh copy every run; chapters/ID3 are (re)embedded into this copy only
        guid_extras = {} if a.variant else episode_extras(a.date, guid, mob, dst, root, cfg, notes_override=a.notes, ep_kind=a.kind)
        dur, _, _ = probe(dst)
        hh, mm = map(int, (a.pubtime or (SHOW["jobs_pubtime"] if a.kind == "jobs" else SHOW["default_pubtime"])).split(":"))
        pub = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ) + dt.timedelta(minutes=-1 if a.variant else 0)
        notes = open(a.notes, encoding="utf-8").read().strip() if a.notes else guid_extras.get("notes") or auto_notes(a.date)
        wk = d.strftime("%a %b %-d")
        title = a.title or (f"Jobs Briefing - {d.strftime('%b %-d, %Y')}" if a.kind == "jobs" else f"{SHOW['title']} - {d.strftime('%b %-d, %Y')}")
        item = {"guid": guid, "file": fname, "title": title, "notes": notes, "variant": a.variant,
                "length": os.path.getsize(dst), "duration": dur, "pub_ts": int(pub.timestamp()), "date": a.date}
        item.update({k: v for k, v in guid_extras.items() if k != "notes"})
        eps = [e for e in eps if e["guid"] != guid] + [item]
    elif not a.rebuild:
        ap.error("give a date, --remove, --rebuild or --check")
    keep = a.keep or CFG["output"].get("keep", 30)
    eps = sorted(eps, key=lambda e: e["pub_ts"])[-keep:]
    live = {e["file"] for e in eps}
    for f in os.listdir(os.path.join(root, "episodes")):
        if f not in live: os.remove(os.path.join(root, "episodes", f))
    live_g = {e["guid"] for e in eps}
    for sub in ("tx", "ch"):
        dd = os.path.join(root, sub); os.makedirs(dd, exist_ok=True)
        for f in os.listdir(dd):
            g = f.split(".")[0]
            if g not in live_g:
                pth = os.path.join(dd, f); shutil.rmtree(pth) if os.path.isdir(pth) else os.remove(pth)
    shutil.copyfile(COVER_SRC, os.path.join(root, "cover.jpg"))
    if backend == "cloudflare-pages" and not cfg.get("cloudflare_project") and not a.dry_run:
        ensure_cf_project(cfg)                # creates the project (and sets base_url) before the feed is written
    feed = build_feed(cfg, eps)
    import xml.dom.minidom; xml.dom.minidom.parseString(feed.encode())  # well-formed or raise
    open(os.path.join(root, "feed.xml"), "w", encoding="utf-8").write(feed)
    json.dump(eps, open(state_p, "w"), indent=2, ensure_ascii=False)
    print(f"feed: {len(eps)} episode(s) -> {os.path.join(root, 'feed.xml')}")
    if backend in ("github", "cloudflare-pages") and not a.dry_run:
        (upload_github if backend == "github" else upload_cloudflare)(cfg, root)
        print("feed URL:", cfg["base_url"].rstrip("/") + "/" + cfg["slug"] + "/feed.xml")
    else:
        print("backend=stage: built locally only, nothing uploaded")

if __name__ == "__main__":
    main()

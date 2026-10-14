#!/usr/bin/env python3
"""Per-episode extras for the podcast feed (used by publish_feed.py; can also run alone):
  - parse <picks_dir>/<date>.md (+ <date>-new-sections*.md) into sections/items (title, source URL, video URL, takeaway)
  - chapters: one per script segment ("## " headings, timed by make_podcast.py -> <ep>.chapters.json) and one per
    major story (explicit [STORY 12] / [STORY new:17] markers in the script if present, else the first line in the
    segments that names the story's key subject; at least 25 s apart)
  - chapter art: og:image of the source page (public fetch) or a headless-Chrome screenshot (fresh temp profile, never
    logged in; Xiaohongshu / X / LinkedIn / WeChat are never screenshotted), cropped to 1400x1400 with show branding + a
    title strip; fallback = show cover + title strip
  - ID3v2.3 CHAP/CTOC frames (TIT2 + APIC + WXXX per chapter) embedded into the episode mp3 (Apple Podcasts reads these)
  - Podcasting 2.0 chapters JSON (version 1.2.0, startTime/title/img/url)
  - show notes: HTML (content:encoded) + plain text (description, < 4000 chars)
Usage: python3 episode_extras.py <date> --mp3 <file> --out-dir <site episode dir> --base-url <public dir url>
"""
import urllib.parse, concurrent.futures as cf, glob, hashlib, html, io, json, os, re, shutil, subprocess, sys, tempfile, textwrap, urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
from show_config import SHOW, CFG
PLUG = CFG["plugins"]
PICKS = SHOW["picks_dir"]
from show_config import cover_path
COVER = cover_path()
IMG_CACHE = os.path.join(HERE, ".img-cache")
FB = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
NO_FETCH = ("xiaohongshu.com", "xhslink.com", "xhscdn.com")                       # never touched at all
NO_SHOT = NO_FETCH + ("x.com", "twitter.com", "linkedin.com", "mp.weixin.qq.com", "weixin.qq.com")  # login walls
VIDEO_RE = re.compile(r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts|bilibili\.com/video|vimeo\.com/\d|/video/\d)", re.I)
SKIP_SECTIONS = tuple(s.lower() for s in CFG["input"].get("skip_sections", []))
MAX_BYTES = 25 * 1024 * 1024 - 64 * 1024

# ---------------------------------------------------------------- daily parsing
def _first_sentence(t, n=220):
    t = re.sub(r"\s+", " ", re.sub(r"\*\*|`", "", t)).strip()
    m = re.match(r"(.+?[.!?。！？])(\s|$)", t)
    s = m.group(1) if m else t
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"

def parse_notes_markdown(path, tag):
    """Generic input (input.format = "markdown"). Any Markdown file:
         ## Section            -> section
         - [Title](url) - note -> one item (a bullet without a link is fine too)
           Takeaway: ...       -> optional indented line under a bullet
    """
    secs, sec, item, n = [], None, None, 0
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line.startswith("## "):
            name = line[3:].strip()
            sec = None if any(k in name.lower() for k in SKIP_SECTIONS) else {"name": name, "items": []}
            if sec: secs.append(sec)
            item = None; continue
        m = re.match(r"^[-*]\s+(.*)$", line)
        if m:
            if sec is None: sec = {"name": "Notes", "items": []}; secs.append(sec)
            body = m.group(1).strip(); n += 1
            links = re.findall(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", body)
            links += [("link", u) for u in re.findall(r"(?<!\()https?://[^\s)]+", body)]
            title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
            title, _, rest = title.partition(" - ")
            item = {"n": n, "key": f"{tag}:{n}" if tag else str(n), "title": title.strip()[:120], "borderline": False,
                    "links": links, "takeaway": "", "what": _first_sentence(rest) if rest else "", "section": sec["name"]}
            sec["items"].append(item); continue
        m = re.match(r"^\s+(?:takeaway|why)\s*[:：]\s*(.+)$", line, re.I)
        if m and item: item["takeaway"] = _first_sentence(m.group(1))
    for s in secs:
        for it in s["items"]:
            it["url"] = it["links"][0][1] if it["links"] else ""
            vids = [u for _, u in it["links"] if VIDEO_RE.search(u)]
            it["video"] = vids[0] if vids else ""
            if not it["takeaway"]: it["takeaway"] = it["what"]
    return [s for s in secs if s["items"]]

def parse_daily_file(path, tag):
    """Detailed format (input.format = "detailed"): '### 1. Title' items with **Links:** / **What it is:** /
    **Why it matters to you:** lines. Generic Markdown is handled by parse_notes_markdown."""
    if CFG["input"].get("format", "markdown") != "detailed": return parse_notes_markdown(path, tag)
    secs, sec, item = [], None, None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line.startswith("## "):
            name = line[3:].strip()
            sec = None if any(k in name.lower() for k in SKIP_SECTIONS) else {"name": name, "items": []}
            if sec: secs.append(sec)
            item = None; continue
        m = re.match(r"^### (\d+)\.\s+(.*)$", line)
        if m and sec is not None:
            title = m.group(2).strip()
            border = "borderline" in title.lower()
            title = re.sub(r"\s*`\[[^\]]*\]`\s*|\s*★.*$", "", title).strip()
            item = {"n": int(m.group(1)), "key": f"{tag}:{m.group(1)}" if tag else m.group(1), "title": title,
                    "borderline": border, "links": [], "takeaway": "", "what": "", "section": sec["name"]}
            sec["items"].append(item); continue
        if item is None: continue
        if line.startswith("**Links:**"):
            item["links"] = re.findall(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", line)
        elif line.startswith("**Why it matters to you:**"):
            item["takeaway"] = _first_sentence(line.split(":**", 1)[1])
        elif line.startswith("**What it is:**"):
            item["what"] = _first_sentence(line.split(":**", 1)[1])
    for s in secs:
        for it in s["items"]:
            it["url"] = it["links"][0][1] if it["links"] else ""
            vids = [u for lbl, u in it["links"] if VIDEO_RE.search(u) or ("video" in lbl.lower() and re.search(r"(x|twitter)\.com/", u))]
            it["video"] = vids[0] if vids else ""
            if not it["takeaway"]: it["takeaway"] = it["what"]
    return [s for s in secs if s["items"]]

def load_daily(date):
    main = os.path.join(PICKS, CFG["input"].get("file_pattern", "{date}.md").format(date=date))
    out = parse_daily_file(main, "") if os.path.exists(main) else []
    news = sorted(p for p in glob.glob(os.path.join(PICKS, f"{date}-new-sections*.md")) if "backup" not in p)
    # prefer the final file over a "-trial" one if both exist
    news = [p for p in news if "trial" not in p] or news
    for p in news[:1]:
        out += parse_daily_file(p, "new")
    return out

def feishu_links(date):
    """Plugin plugins.doc_links (off): pick Feishu/Lark doc links from <date>-summary.txt into the notes."""
    if not PLUG.get("doc_links"): return []
    p = os.path.join(PICKS, f"{date}-summary.txt")
    if not os.path.exists(p): return []
    t = open(p, encoding="utf-8").read()
    return re.findall(r"https://[\w.]*(?:feishu\.cn|larksuite\.com)/\S+?(?=[\s)）。,，]|$)", t)

# ---------------------------------------------------------------- story chapters
STOP = set("the a an and or of for to in on with how why what when new your you is are now ai its it's from by this that "
           "after before every one two three free big still more than vs at as".split())

def story_keys(it):
    t = it["title"]
    head = re.split(r"[:：—–]| - ", t)[0]
    head = re.sub(r"\(.*?\)|（.*?）", "", head).strip()
    keys = []
    if 2 <= len(head) <= 40 and not re.match(r"^(A\d|B\d|Reply|Quote|①|②|③)", head): keys.append(head)
    for w in re.findall(r"[A-Z][A-Za-z0-9’'\.-]{2,}|[\u4e00-\u9fff]{2,6}", t):
        w = re.sub(r"[’']s$", "", w.strip(".’'"))
        if w.lower() not in STOP and w not in keys and len(keys) < 5: keys.append(w)
    # GitHub "owner/repo" -> repo, "Product Hunt #2 · kōdlo" -> kōdlo
    m = re.search(r"·\s*([^\s—–]+)", t)
    if m: keys.insert(0, m.group(1).split("/")[-1])
    bad = ("github", "product hunt", "x post", "infoq", "hn", "hf", "reply", "quote", "google", "openai", "meta",
           "microsoft", "anthropic", "contrarian", "lenny", "show", "podcast", "chinese", "agent", "agents")
    return [k for k in keys if k.lower() not in bad]

def _norm(s): return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s.lower())

WEAK = re.compile(r"^([A-Z]{2,5}s?|Use|Product|Hunt|Customers|President|Dev|Builds|Show|Indie|Bot|Market|AI-search)$")

def _stem(w): return re.sub(r"(ies|y|s)$", "", w.lower())

def _affinity(section, seg_title):
    stop = {"update", "your", "with", "what", "this", "week", "from", "about", "they", "say", "and", "the"}
    sw = {_stem(w) for w in re.findall(r"[A-Za-z]{4,}", section)} - stop
    tw = {_stem(w) for w in re.findall(r"[A-Za-z]{4,}", seg_title)}
    return bool(sw & tw)

def match_stories(timeline, sections, min_gap=20.0):
    """timeline = <ep>.chapters.json. Returns [{start, key, item}] for story chapters.
    Explicit [STORY key] markers in the script win. Otherwise: first spoken line (between the first segment and the
    sign-off) that names one of the story's specific keys, searching first in the segment(s) whose title shares a word
    with the daily section (e.g. "New tools" -> "Segment 4 - New tools and hot projects"), then anywhere."""
    items = {it["key"]: it for s in sections for it in s["items"]}
    if timeline.get("stories"):
        return [{"start": st["start_sec"], "key": st["key"], "item": items[st["key"]]} for st in timeline["stories"] if st["key"] in items]
    lines, segs = timeline["lines"], timeline["chapters"]
    seg_start = next((c["start_sec"] for c in segs if c["title"].lower().startswith("segment")), 0)
    signoff = next((c["start_sec"] for c in segs if "sign" in c["title"].lower()), 1e9)
    bounds = [(c["start_sec"], (segs[i + 1]["start_sec"] if i + 1 < len(segs) else 1e9), c["title"]) for i, c in enumerate(segs)]
    norm = [_norm(l["text"]) for l in lines]
    cands = []
    for key, it in items.items():
        if it["section"].lower().startswith(("content seeds", "your own x")) or re.match(r"^(Reply|Quote)\b", it["title"]): continue
        ks = [k for k in story_keys(it) if len(_norm(k).strip()) >= 3 and not WEAK.match(k)]
        head = _norm(re.sub(r"\(.*?\)|（.*?）", "", re.split(r"[:：—–]| - ", it["title"])[0])).strip()
        has_head = bool(ks) and _norm(ks[0]).strip() in head and len(head) <= 40
        if ks: cands.append((key, it, has_head, [re.compile(r"(^| )" + re.escape(_norm(k).strip()) + r"( |$)") for k in ks]))
    used, seen_urls, picks, done = set(), set(), [], set()
    for pass_ in ("affinity", "global"):
        for key, it, has_head, pats in cands:
            if key in done or (it["url"] and it["url"] in seen_urls): continue
            rngs = [(a_, b_) for a_, b_, t in bounds if _affinity(it["section"], t)] if pass_ == "affinity" else [(seg_start, signoff)]
            ok = lambda i: (i not in used and seg_start <= lines[i]["start"] < signoff
                            and any(a_ <= lines[i]["start"] < b_ for a_, b_ in rngs))
            hit = None
            for pt in pats:   # keys in priority order: the story's own name first
                hit = next((i for i in range(len(lines)) if ok(i) and pt.search(norm[i])), None)
                if hit is not None: break
            if pass_ == "global" and not (has_head and hit is not None and pats[0].search(norm[hit])):
                # no named subject found: earliest mention of any key
                hits = [next((i for i in range(len(lines)) if ok(i) and pt.search(norm[i])), None) for pt in pats]
                hits = [h for h in hits if h is not None]; hit = min(hits) if hits else None
            if hit is not None:
                used.add(hit); done.add(key); seen_urls.add(it["url"])
                picks.append({"start": lines[hit]["start"], "key": key, "item": it})
    picks.sort(key=lambda p: p["start"])
    out = []
    for p in picks:
        if out and p["start"] - out[-1]["start"] < min_gap: continue
        out.append(p)
    return out

# ---------------------------------------------------------------- images
def _get(url, limit, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,zh;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit), r.geturl(), r.headers.get_content_type()

def og_image(url):
    if not url or any(d in url for d in NO_FETCH): return None
    try:
        body, final, ctype = _get(url, 600_000)
        if "html" not in ctype: return None
        h = body.decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*content=["\']([^"\']+)', h, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)', h, re.I))
        if not m: return None
        iu = urllib.parse.urljoin(final, html.unescape(m.group(1)))
        if any(d in iu for d in NO_FETCH): return None
        data, _, ict = _get(iu, 8_000_000)
        im = Image.open(io.BytesIO(data)); im.load()
        return im if min(im.size) >= 200 else None
    except Exception:
        return None

def screenshot(url):
    if not url or any(d in url for d in NO_SHOT): return None
    with tempfile.TemporaryDirectory() as td:   # fresh profile: no cookies, never logged in
        out = os.path.join(td, "s.png")
        try:
            subprocess.run(["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                            "--mute-audio", f"--user-data-dir={td}/prof", "--window-size=1400,1400",
                            "--virtual-time-budget=8000", f"--screenshot={out}", url],
                           capture_output=True, timeout=45)
            if not os.path.exists(out): return None
            im = Image.open(out); im.load()
            # reject near-blank captures
            if im.convert("L").resize((50, 50)).getextrema()[1] - im.convert("L").resize((50, 50)).getextrema()[0] < 30: return None
            return im
        except Exception:
            return None

def source_image(url):
    """og:image, else screenshot, cached per URL. Returns (PIL image or None, how)."""
    os.makedirs(IMG_CACHE, exist_ok=True)
    k = os.path.join(IMG_CACHE, hashlib.sha1(url.encode()).hexdigest()[:16])
    for ext, how in ((".og.jpg", "og:image"), (".shot.jpg", "screenshot")):
        if os.path.exists(k + ext): return Image.open(k + ext), how
    if os.path.exists(k + ".none"): return None, "cover"
    im = og_image(url)
    if im is not None:
        im.convert("RGB").save(k + ".og.jpg", quality=90); return im, "og:image"
    im = screenshot(url)
    if im is not None:
        im.convert("RGB").save(k + ".shot.jpg", quality=90); return im, "screenshot"
    open(k + ".none", "w").close(); return None, "cover"

def _square(im, size=1400):
    im = im.convert("RGB"); w, h = im.size; s = min(w, h)
    box = ((w - s) // 2, 0 if h > w else (h - s) // 2, (w - s) // 2 + s, (0 if h > w else (h - s) // 2) + s)  # top-crop tall pages
    return im.crop(box).resize((size, size), Image.LANCZOS)

def chapter_art(src, title, kind, out, quality=80, size=1400):
    base = _square(src if src is not None else Image.open(COVER), size)
    if src is not None:   # gentle darkening for legibility
        base = Image.blend(base, Image.new("RGB", base.size, (10, 16, 40)), 0.18)
    d = ImageDraw.Draw(base, "RGBA")
    # brand badge
    d.rounded_rectangle([40, 40, 560, 130], radius=24, fill=(10, 16, 40, 225))
    d.ellipse([62, 66, 100, 104], fill=(0, 226, 205, 255))
    d.text((118, 58), SHOW["title"].upper(), font=ImageFont.truetype(FB, 46), fill=(255, 255, 255))
    # title strip
    f = ImageFont.truetype(FB, 64); fk = ImageFont.truetype(FB, 34)
    t = re.sub(r"\s+", " ", title).strip()
    lines, cur = [], ""
    for tok in re.findall(r"[\u4e00-\u9fff]|[^\s\u4e00-\u9fff]+\s*|\s+", t):
        if d.textlength(cur + tok, font=f) > size - 140:
            lines.append(cur.rstrip()); cur = tok.lstrip()
            if len(lines) == 3: break
        else: cur += tok
    if len(lines) < 3 and cur.strip(): lines.append(cur.strip())
    elif len(lines) == 3 and cur: lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    hgt = 70 + 82 * len(lines) + 40
    d.rectangle([0, size - hgt, size, size], fill=(10, 16, 40, 232))
    d.rectangle([0, size - hgt, size, size - hgt + 10], fill=(0, 226, 205, 255))
    d.text((70, size - hgt + 28), kind.upper(), font=fk, fill=(255, 196, 61))
    for i, l in enumerate(lines):
        d.text((70, size - hgt + 74 + 82 * i), l, font=f, fill=(255, 255, 255))
    base.save(out, "JPEG", quality=quality, optimize=True)
    return out

def fit_jpegs(paths_titles, per_img_budget):
    """Render each (path, src, title, kind) at 1400x1400 and lower JPEG quality until <= budget. Busy photos that still
    don't fit at q30 get a light blur of the background photo (the title strip stays sharp)."""
    for p, src, title, kind in paths_titles:
        q, soft = 82, src
        while True:
            chapter_art(soft, title, kind, p, quality=q)
            if os.path.getsize(p) <= per_img_budget: break
            if q > 30: q -= 8; continue
            if soft is None or soft is not src: break
            soft = src.convert("RGB").filter(ImageFilter.GaussianBlur(1.6)); q = 60

# ---------------------------------------------------------------- ID3 + JSON
def embed_id3(mp3, chapters, duration_ms):
    from mutagen.id3 import ID3, ID3NoHeaderError, CHAP, CTOC, CTOCFlags, TIT2, APIC, WXXX, TPE1, TALB, COMM
    try: tags = ID3(mp3)
    except ID3NoHeaderError: tags = ID3()
    for k in [k for k in tags.keys() if k.startswith(("CHAP", "CTOC"))]: del tags[k]
    ids = []
    for i, c in enumerate(chapters):
        cid = f"chp{i:03d}"; ids.append(cid)
        st = int(c["start"] * 1000); en = int(chapters[i + 1]["start"] * 1000) if i + 1 < len(chapters) else duration_ms
        sub = [TIT2(encoding=3, text=[c["title"]])]
        if c.get("img_path"):
            sub.append(APIC(encoding=0, mime="image/jpeg", type=3, desc="", data=open(c["img_path"], "rb").read()))
        if c.get("url"): sub.append(WXXX(encoding=0, desc="", url=c["url"]))
        tags.add(CHAP(element_id=cid, start_time=st, end_time=en, start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF, sub_frames=sub))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED, child_element_ids=ids,
                  sub_frames=[TIT2(encoding=3, text=["Chapters"])]))
    tags.setall("TALB", [TALB(encoding=3, text=[SHOW["title"]])]); tags.setall("TPE1", [TPE1(encoding=3, text=[SHOW["author"]])])
    # mutagen orders non-APIC frames by size on save; keep CHAP frames in time order (ffprobe and some players list
    # chapters in frame order) by injecting a sort wrapper into mutagen's tag writer for this one save.
    import builtins, mutagen.id3._tags as _T
    def _sorted(it, key=None, reverse=False):
        items = list(it)
        if key and items and isinstance(items[0], tuple) and len(items[0]) == 2 and isinstance(items[0][1], tuple) \
                and hasattr(items[0][1][0], "FrameID"):
            def k2(x):
                k = key(x); fr = x[1][0]
                return (k[0], fr.start_time if fr.FrameID == "CHAP" else k[1], k[2])
            return builtins.sorted(items, key=k2, reverse=reverse)
        return builtins.sorted(items, key=key, reverse=reverse)
    _T.sorted = _sorted
    try: tags.save(mp3, v2_version=3)
    finally: del _T.sorted

def chapters_json(chapters, base_url):
    out = {"version": "1.2.0", "chapters": []}
    for c in chapters:
        e = {"startTime": round(c["start"], 2), "title": c["title"]}
        if c.get("img_name"): e["img"] = f"{base_url}/{c['img_name']}"
        if c.get("url"): e["url"] = c["url"]
        out["chapters"].append(e)
    return out

# ---------------------------------------------------------------- show notes
def _mmss(s): s = int(s); return f"{s//3600}:{s%3600//60:02d}:{s%60:02d}" if s >= 3600 else f"{s//60}:{s%60:02d}"

def zh_keypoints(script_path):
    """Plugin plugins.zh_recap: a host line starting with 今天 becomes a 要点 summary in the notes."""
    if not PLUG.get("zh_recap"): return ""
    if not script_path or not os.path.exists(script_path): return ""
    for l in open(script_path, encoding="utf-8"):
        m = re.match(r"^[A-Z]+:\s*(今天.*)$", l.strip())
        if m: return re.sub(r"\{[^}]*\}", "", m.group(1)).strip()
    return ""

def show_notes(date, sections, chapters, feishu, zh, teaser, transcript_url=""):
    H, P = [], []
    if zh: H.append(f"<p><b>要点：</b>{html.escape(zh)}</p>"); P.append(f"要点：{zh}")
    if zh and teaser:   # the teaser's own Chinese recap line duplicates the 要点 line
        teaser = "\n".join(l for l in teaser.splitlines() if l.strip() != zh.strip())
    if teaser: H.append("<p>" + "<br>".join(html.escape(t) for t in teaser.splitlines()) + "</p>"); P.append(teaser)
    if feishu:
        H.append("<p>Full daily (Feishu): " + " · ".join(f'<a href="{html.escape(u)}">{"full version" if i == 0 else "new sections"}</a>' for i, u in enumerate(feishu)) + "</p>")
        P.append("Full daily (Feishu): " + feishu[0])
    if transcript_url:
        H.append(f'<p>Transcript (time-synced, VTT): <a href="{html.escape(transcript_url)}">open</a></p>')
    seg = [c for c in chapters if c["kind"] in ("segment", "intro", "break", "sign-off")]
    if seg:
        H.append("<p><b>Chapters</b><br>" + "<br>".join(f"{_mmss(c['start'])} {html.escape(c['title'])}" for c in seg) + "</p>")
        P.append("Chapters:\n" + "\n".join(f"{_mmss(c['start'])} {c['title']}" for c in seg))
    plain_items = []
    for s in sections:
        H.append(f"<p><b>{html.escape(s['name'])}</b></p><ul>")
        for it in s["items"]:
            t = html.escape(it["title"]) + (" <i>(borderline)</i>" if it["borderline"] else "")
            link = f'<a href="{html.escape(it["url"])}">{t}</a>' if it["url"] else t
            vid = f' · <a href="{html.escape(it["video"])}">▶ video</a>' if it.get("video") else ""
            H.append(f"<li>{link}{vid} — {html.escape(it['takeaway'])}</li>")
            plain_items.append((s["name"], it["title"]))
        H.append("</ul>")
    H.append(f"<p>{html.escape(SHOW['notes_footer'])}</p>")
    # plain text < 4000 chars: header + item titles grouped by section, truncated with a pointer to the full notes
    head = "\n\n".join(P)
    body, cur_sec, budget = [], None, 3900 - len(head) - 80
    for sec, t in plain_items:
        add = ([f"\n{sec}:"] if sec != cur_sec else []) + [f"- {t}"]
        if sum(len(x) + 1 for x in body + add) > budget:
            body.append("\n… more items with links in the full show notes."); break
        body += add; cur_sec = sec
    plain = (head + "\n" + "\n".join(body)).strip()
    return "".join(H), plain[:3999]

# ---------------------------------------------------------------- jobs briefing (kind=jobs)
MONITOR = SHOW["jobs_dir"]

def load_job_roles(date):
    """Jobs roles for the briefing notes. Primary: <jobs_dir>/daily/items-<date>.json (your own job tracker exports this): status push -> priority, verify -> to check, skip -> excluded.
    Fallback: <jobs_dir>/candidates-<date>.md (load_candidates)."""
    p = os.path.join(MONITOR, "daily", f"items-{date}.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            pri, cond, excl = [], [], []
            for it in d.get("items", []):
                st = (it.get("status") or "").lower()
                posted = it.get("posted", "")
                is_new = bool(re.search(r"首次|new", posted, re.I))
                comp = re.sub(r"[（(].*$", "", it.get("company", "")).strip()
                prod = re.search(r"产品[：:]\s*([^\s，,）)]+)", it.get("company", ""))
                if prod and prod.group(1) not in comp: comp += " " + prod.group(1)
                r = {"id": "", "name": it.get("title", ""), "company": comp, "url": it.get("url", ""),
                     "source": it.get("source", ""), "where": " · ".join(x for x in [it.get("location", ""), it.get("mode", "")] if x),
                     "pay": it.get("pay", ""), "checked": ("checked " + it["checked"]) if it.get("checked") else "",
                     "match": it.get("reason", ""), "next": it.get("next", ""), "new": is_new}
                if st == "push": pri.append(r)
                elif st == "verify": cond.append(r)
                elif st == "skip" and comp: excl.append(comp)
            for i, r in enumerate(pri, 1):
                r["id"] = f"P{i}"
                if r["new"]: r["company"] += " (new)"
            if pri or cond:
                return {"priority": pri, "conditional": cond, "excluded": excl, "path": p,
                        "cond_label": "To check (not a priority yet)"}
        except Exception as e:
            print(f"items json unreadable ({e}); falling back to candidates", file=sys.stderr)
    return load_candidates(date) if PLUG.get("zh_jobs_parser") else {"priority": [], "conditional": [], "excluded": [], "path": None}

def load_candidates(date):
    """Plugin plugins.zh_jobs_parser (off by default): Chinese-headed job list. Priority + conditional roles from <jobs_dir>/candidates-<date>.md (your job tracker's daily file)."""
    p = os.path.join(MONITOR, f"candidates-{date}.md")
    if not os.path.exists(p): return {"priority": [], "conditional": [], "excluded": [], "path": None}
    txt = open(p, encoding="utf-8").read()
    secs = re.split(r"^## ", txt, flags=re.M)
    pri, cond, excl = [], [], []
    for sec in secs:
        head = sec.split("\n", 1)[0]
        if head.startswith("一"):
            for row in re.findall(r"^\|\s*(P\d+)\s*\|(.+)$", sec, re.M):
                cols = [c.strip() for c in row[1].strip().strip("|").split("|")]
                url = (re.findall(r"https?://\S+", cols[1]) or [""])[0]
                pri.append({"id": row[0], "name": re.sub(r"\*\*", "", cols[0]), "url": url,
                            "source": re.sub(r"\s*https?://\S+", "", cols[1]).strip(), "checked": re.sub(r"\*\*", "", cols[2]),
                            "where": cols[3], "pay": cols[4], "fresh": cols[5], "stage": cols[6] if len(cols) > 6 else ""})
        elif head.startswith("二"):
            for blk in re.split(r"^### ", sec, flags=re.M)[1:]:
                g = lambda k: (re.search(r"^- " + k + r"[^：:]*[：:]\s*(.+)$", blk, re.M) or [None, ""])[1].strip()
                comp = re.search(r"公司：([^（(；;\n]+)", blk)
                cond.append({"id": blk.split("\n", 1)[0].strip(), "name": g("角色"), "company": comp.group(1).strip() if comp else "",
                             "url": (re.findall(r"https?://\S+", g("原始页")) or [""])[0], "match": g("匹配"),
                             "next": g("下一步"), "where": g("薪资/工时/形式")})
        elif head.startswith("三"):
            for row in re.findall(r"^\|(?!\s*ID|\s*-)(.+)$", sec, re.M):
                cols = [c.strip() for c in row.strip().strip("|").split("|")]
                if len(cols) >= 3 and cols[2] and cols[2] not in excl: excl.append(cols[2])
    return {"priority": pri, "conditional": cond, "excluded": excl, "path": p}

def role_for_chapter(title, roles):
    key = re.split(r"[—–-]\s*", title, 1)[-1].strip().lower() if re.search(r"[—–]", title) else ""
    if not key or not re.match(r"(priority|new conditional|conditional)", title.lower()): return None
    for r in roles["priority"] + roles["conditional"]:
        hay = " ".join([r.get("name", ""), r.get("company", ""), r.get("url", ""), r.get("id", "")]).lower()
        if key in hay or key.replace(" ", "") in hay.replace(" ", ""): return r
    return None

def _script_chapters(path):
    out, cur = {}, None
    if not path or not os.path.exists(path): return out
    for l in open(path, encoding="utf-8"):
        if l.startswith("## "): cur = l[3:].strip(); out[cur] = []; continue
        m = re.match(r"^[A-Z]+:\s*(.+)$", l.strip())
        if m and cur:
            t = re.sub(r"\s*\{[^}]*\}", "", m.group(1)).strip()
            t = re.sub(r"\b[A-Z](?: [A-Z]\b)+", lambda k: k.group(0).replace(" ", ""), t)   # spoken "A I" -> "AI"
            out[cur].append(t)
    return out

def _pick(lines, pats):
    for l in lines:
        for p in pats:
            m = re.search(r"(?:^|(?<=[.!?] ))" + p, l, re.I)
            if m: return l[m.end():].strip() or l
    return ""

def jobs_notes(date, roles, script_path, chapters, transcript_url=""):
    """Show notes for the Jobs Briefing: facts + links from <jobs_dir>/candidates-<date>.md; the English one-line
    'why' / 'next step' come from the briefing script (written from the same file)."""
    sc = _script_chapters(script_path)
    zh = next((l for ls in sc.values() for l in ls if l.startswith(("简单说", "今天"))), "") if PLUG.get("zh_recap") else ""
    zh = re.sub(r"^简单说[：:]\s*", "", zh)
    H, P = [], []
    if zh: H.append(f"<p><b>要点：</b>{html.escape(zh)}</p>"); P.append(f"要点：{zh}")
    bl = sc.get("Bottom line", [])
    if bl: H.append(f"<p>{html.escape(' '.join(bl[:2]))}</p>"); P.append(" ".join(bl[:2]))
    if transcript_url: H.append(f'<p>Transcript (time-synced, VTT): <a href="{html.escape(transcript_url)}">open</a></p>')
    segs = [c for c in chapters]
    if segs:
        H.append("<p><b>Chapters</b><br>" + "<br>".join(f"{_mmss(c['start'])} {html.escape(c['title'])}" for c in segs) + "</p>")
        P.append("Chapters:\n" + "\n".join(f"{_mmss(c['start'])} {c['title']}" for c in segs))
    def chap_lines(r):
        for t, ls in sc.items():
            if role_for_chapter(t, {"priority": [r], "conditional": []} if r in roles["priority"] else {"priority": [], "conditional": [r]}) is r:
                return ls
        return []
    def block(label, rs, is_cond):
        if not rs: return
        H.append(f"<p><b>{label}</b></p><ul>"); P.append(f"\n{label}:")
        for r in rs:
            ls = chap_lines(r)
            why = _pick(ls, [r"why it (?:fits|could fit|matters)[^:]*:\s*", r"why[^:]*:\s*"]) or (r.get("match") or "")
            nxt = _pick(ls, [r"(?:the )?next step[^:]*:\s*", r"(?:the )?next step is yours\.\s*"]) or (r.get("next") or r.get("stage", ""))
            facts = " · ".join(x for x in [r.get("source", ""), r.get("where", ""), r.get("pay", ""), r.get("checked", "")] if x)
            nm = r["name"] + (f" ({r['company']})" if r.get("company") else "")
            link = f'<a href="{html.escape(r["url"])}">{html.escape(nm)}</a>' if r.get("url") else html.escape(nm)
            H.append(f"<li>{'' if is_cond else r['id'] + ' '}{link}"
                     + (f"<br><small>{html.escape(facts)}</small>" if facts else "")
                     + f"<br>Why: {html.escape(why)}<br>Next: {html.escape(nxt)}</li>")
            P.append(f"- {'' if is_cond else r['id'] + ' '}{nm} {r.get('url','')}\n  Why: {why}\n  Next: {nxt}")
        H.append("</ul>")
    block("Priority roles", roles["priority"], False)
    block(roles.get("cond_label", "Conditional (not a priority yet)"), roles["conditional"], True)
    ex = " ".join(l for l in sc.get("Excluded and blocked", []) if not re.match(r"[\u4e00-\u9fff]", l))
    ex = ex if len(ex) <= 450 else ex[:449].rsplit(" ", 1)[0] + "…"
    if not ex and roles["excluded"]: ex = "Excluded (location/onsite rules): " + ", ".join(roles["excluded"][:6]) + "."
    if ex: H.append(f"<p><b>Excluded / blocked:</b> {html.escape(ex)}</p>"); P.append(f"\nExcluded / blocked: {ex}")
    H.append(f"<p>{html.escape(SHOW['notes_footer'])}</p>")
    return "".join(H), "\n".join(P)[:3999]

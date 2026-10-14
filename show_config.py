"""One config file for the whole show: show.json (copy one of examples/configs/*.json).
Nothing secret lives here. The Cloudflare token is only read from the environment (CLOUDFLARE_API_TOKEN).
Pick a different file with --config path or the PODCAST_SHOW_CONFIG env var."""
import copy, datetime as dt, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "show": {"title": "My Daily Podcast", "tagline": "A daily audio briefing made with AI.", "cover_subtitle": "",
             "description": "A private daily briefing. Not listed in any directory.", "author": "",
             "owner_name": "", "owner_email": "", "category": "Technology", "listener_name": "friend",
             "notes_footer": "Private feed. Nothing in this episode was posted or sent.", "file_prefix": "episode"},
    "language": {"code": "en", "script_language": "English", "glosses": False},
    "hosts": [{"id": "ALEX", "name": "Alex", "voice": "en-US-AndrewMultilingualNeural", "rate": ""},
              {"id": "MAYA", "name": "Maya", "voice": "en-US-AvaMultilingualNeural", "rate": ""}],
    "voice_defaults": {"rate": "-10%", "gap_extra": 0.2, "lufs": -16.0, "bitrate": "128k"},
    "segments": [{"title": "Cold open", "music": "intro", "brief": "One-line hook for today."},
                 {"title": "Main stories", "music": "bed", "brief": "Walk through each note or link."},
                 {"title": "Wrap", "music": "outro", "brief": "One takeaway and goodbye."}],
    "music": {"enabled": True, "dir": "music", "intro": "intro.wav", "outro": "outro.wav",
              "stinger": "stinger.wav", "bed": "bed.wav", "bed_db": -26},
    "input": {"format": "markdown", "dir": "examples/notes", "file_pattern": "{date}.md"},
    "plugins": {"doc_links": False, "zh_recap": False, "jobs_briefing": False, "zh_jobs_parser": False},
    "output": {"backend": "local", "github_public_ok": False, "utc_offset_hours": 0,
               "pubtime": "07:30", "jobs_pubtime": "10:00", "keep": 30},
}

def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict): _merge(a[k], v)
        else: a[k] = v
    return a

def config_path():
    for i, x in enumerate(sys.argv):              # --config works for every script
        if x == "--config" and i + 1 < len(sys.argv): return sys.argv[i + 1]
        if x.startswith("--config="): return x.split("=", 1)[1]
    p = os.environ.get("PODCAST_SHOW_CONFIG") or os.path.join(HERE, "show.json")
    return p if os.path.exists(p) else os.path.join(HERE, "examples", "configs", "en-two-hosts.json")

def validate(c):
    for h in c["hosts"]:
        if not re.fullmatch(r"[A-Z][A-Z ]{1,20}", h["id"]):
            sys.exit(f"host id {h['id']!r}: use 2-21 capital letters A-Z (it's the label in the script, e.g. ALEX:)")
    if c["output"]["backend"] not in ("local", "cloudflare-pages", "github"):
        sys.exit("output.backend must be local, cloudflare-pages or github")
    if not 1 <= len(c["hosts"]) <= 6: sys.exit("hosts: 1 to 6")

def load(path=None):
    c = copy.deepcopy(DEFAULTS)
    p = path or config_path()
    if os.path.exists(p): _merge(c, json.load(open(p, encoding="utf-8")))
    for k, env in (("owner_name", "PODCAST_OWNER_NAME"), ("owner_email", "PODCAST_OWNER_EMAIL"), ("title", "PODCAST_TITLE")):
        if os.environ.get(env): c["show"][k] = os.environ[env]
    c["show"]["author"] = c["show"]["author"] or c["show"]["title"]
    for sect, key in (("input", "dir"), ("music", "dir")):
        if not os.path.isabs(c[sect][key]): c[sect][key] = os.path.join(HERE, c[sect][key])
    validate(c); c["_path"] = p
    return c

CFG = load()
# flat view used by publish_feed / episode_extras
SHOW = dict(CFG["show"], subtitle=CFG["show"]["tagline"], language=CFG["language"]["code"],
            picks_dir=CFG["input"]["dir"], jobs_dir=os.path.join(CFG["input"]["dir"], "jobs"),
            default_pubtime=CFG["output"]["pubtime"], jobs_pubtime=CFG["output"]["jobs_pubtime"])
TZ = dt.timezone(dt.timedelta(hours=float(CFG["output"]["utc_offset_hours"])))

def _prefix(): return re.sub(r"[^A-Za-z0-9_-]", "", CFG["show"]["file_prefix"]) or "episode"
def cover_path(): return os.path.join(HERE, "art", f"cover-{_prefix()}.jpg")
def state_paths():
    """(private state json, local site dir) for this show. The state file holds the secret slug: never commit it."""
    return os.path.join(HERE, f"feed-config.{_prefix()}.json"), os.path.join(HERE, "feed-site", _prefix())

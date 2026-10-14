#!/usr/bin/env python3
"""One command for everything. Run from this folder.

  python3 podcast.py init                       # questions -> show.json (or: --example en|zh)
  python3 podcast.py skeleton -o script.md      # empty script with your hosts + segments, fill it in
  python3 podcast.py prompt notes.md > p.txt    # LLM prompt: paste into ChatGPT/Claude/Grok, save the answer as script.md
  python3 podcast.py build script.md            # -> out/<prefix>-<date>.mp3 + captions + chapters
  python3 podcast.py publish [--date D]         # -> private RSS feed (backend from show.json output.backend)

Add --config path/to/show.json to any command to use another show.
"""
import datetime as dt, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = {"en": "en-two-hosts.json", "zh": "zh-single-host.json"}

def ask(q, default):
    try: a = input(f"{q} [{default}]: ").strip()
    except EOFError: a = ""
    return a or default

def init(args):
    dst = os.path.join(HERE, "show.json")
    if os.path.exists(dst) and "--force" not in args: sys.exit("show.json already exists (add --force to overwrite)")
    if "--example" in args:
        src = EXAMPLES[args[args.index("--example") + 1]]
        shutil.copy(os.path.join(HERE, "examples", "configs", src), dst); print("wrote show.json from", src); return
    lang = ask("Language: en or zh", "en")
    c = json.load(open(os.path.join(HERE, "examples", "configs", EXAMPLES["zh" if lang.startswith("zh") else "en"])))
    s = c["show"]
    s["title"] = ask("Show name", s["title"]); s["tagline"] = ask("Tagline", s["tagline"])
    s["owner_name"] = ask("Your name (shown in the feed)", s["owner_name"])
    s["owner_email"] = ask("Email for the feed (optional, visible to anyone with the feed URL)", s["owner_email"])
    s["listener_name"] = ask("What should the hosts call the listener", s["listener_name"])
    n = int(ask("How many hosts (1-6)", str(len(c["hosts"]))))
    print("Voices: run  edge-tts --list-voices  for the full list (any language works).")
    hosts = []
    for i in range(n):
        d = c["hosts"][i] if i < len(c["hosts"]) else {"id": f"HOST{'ABCDEF'[i]}", "name": f"Host {i+1}", "voice": c["hosts"][0]["voice"], "rate": ""}
        hid = ask(f"Host {i+1} label in the script (capital letters)", d["id"]).upper()
        hosts.append({"id": hid, "name": ask(f"  display name", d["name"]), "voice": ask("  voice", d["voice"]),
                      "rate": ask("  speed, e.g. -10% or +5% (empty = default)", d.get("rate", ""))})
    c["hosts"] = hosts
    c["music"]["enabled"] = ask("Music on? y/n", "y" if c["music"].get("enabled", True) else "n").lower().startswith("y")
    c["output"]["utc_offset_hours"] = float(ask("Your time zone offset from UTC in hours", str(c["output"]["utc_offset_hours"])))
    c["output"]["backend"] = ask("Publish to: local / cloudflare-pages / github (github is PUBLIC)", "local")
    json.dump(c, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2); print("wrote show.json")

def cfg():
    from show_config import CFG; return CFG

def skeleton(args):
    c = cfg(); hosts = c["hosts"]; out = args[args.index("-o") + 1] if "-o" in args else "script.md"
    L = [f"voice {h['id']} = {h['voice']}" for h in hosts] + [""]
    for i, seg in enumerate(c["segments"]):
        L.append(f"## {seg['title']}")
        for k, h in enumerate(hosts[:2] if len(hosts) > 1 else hosts):
            L.append(f"{h['id']}: ({seg.get('brief', '')}) ...write here...")
        if seg.get("music") == "intro": L += ["[INTRO]", "[BED ON]"]
        elif seg.get("music") == "outro": L += ["[BED OFF]", "[OUTRO]"]
        elif i < len(c["segments"]) - 1: L.append("[STINGER]")
    open(out, "w", encoding="utf-8").write("\n".join(L) + "\n"); print("wrote", out)

def prompt(args):
    c = cfg(); files = [a for a in args if not a.startswith("-") and os.path.exists(a)]
    if not files: sys.exit("usage: podcast.py prompt notes.md")
    notes = "\n\n".join(open(f, encoding="utf-8").read() for f in files)
    t = open(os.path.join(HERE, "prompts", "notes-to-script.md"), encoding="utf-8").read()
    hosts = "\n".join(f"- {h['id']}: {h['name']}" for h in c["hosts"])
    segs = "\n".join(f"{i+1}. ## {s['title']} - {s.get('brief','')}" for i, s in enumerate(c["segments"]))
    rule = ("One host only, talking straight to the listener." if len(c["hosts"]) == 1 else
            "A real conversation: hosts react, ask short questions, take turns. Nobody talks for more than 4 sentences in a row.")
    print(t.format(title=c["show"]["title"], tagline=c["show"]["tagline"], script_language=c["language"]["script_language"],
                   hosts=hosts, segments=segs, length_rule=rule + " Aim for 600-1500 words.",
                   listener_name=c["show"]["listener_name"], notes=notes))

def run(cmd): print("+", " ".join(cmd)); subprocess.run(cmd, check=True, cwd=HERE)

def passthru(args):
    return (["--config", args[args.index("--config") + 1]] if "--config" in args else [])

def build(args):
    c = cfg(); script = next(a for a in args if a.endswith(".md"))
    date = args[args.index("--date") + 1] if "--date" in args else dt.date.today().isoformat()
    if c["music"].get("enabled", True) and not os.path.exists(os.path.join(c["music"]["dir"], "bed.wav")):
        run([sys.executable, "make_music.py"] + passthru(args))
    out = os.path.join("out", f"{c['show']['file_prefix']}-{date}.mp3")
    run([sys.executable, "make_podcast.py", os.path.abspath(script), "-o", out,
         "--mode", "briefing" if len(c["hosts"]) == 1 else "conversation"] + passthru(args))

def publish(args):
    c = cfg(); date = args[args.index("--date") + 1] if "--date" in args else dt.date.today().isoformat()
    if not os.path.exists(__import__("show_config").cover_path()): run([sys.executable, "make_cover.py"] + passthru(args))
    run([sys.executable, "publish_feed.py", date, "--mp3", os.path.join("out", f"{c['show']['file_prefix']}-{date}.mp3")]
        + passthru(args) + [a for a in args if a in ("--dry-run",)])

if __name__ == "__main__":
    sys.path.insert(0, HERE)
    if len(sys.argv) < 2 or sys.argv[1] not in ("init", "skeleton", "prompt", "build", "publish"): sys.exit(__doc__)
    {"init": init, "skeleton": skeleton, "prompt": prompt, "build": build, "publish": publish}[sys.argv[1]](sys.argv[2:])

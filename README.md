# ai-daily-podcast

Turn a text script into a podcast episode, and publish it to your own private podcast feed.

I listen to one of these every morning. It's a short English show about the things I want to keep up with (AI, growth, building products). I'm not a programmer. I built this with AI assistants, step by step, for my own use. It works for me every day, and I'm sharing it in case it's useful to you.

## What it does

- **Script in, MP3 out.** Write a plain text script. Each line is `NAME: what they say`.
- **One host or two.** Free Microsoft neural voices through [edge-tts](https://github.com/rany2/edge-tts). No API key.
- **Original music.** `make_music.py` makes an intro, outro, transition sound and a soft background loop from code. No samples, nothing copyrighted.
- **Captions.** Every episode gets a `.vtt` and `.srt` file with speaker names, plus a timed transcript.
- **Chapters with pictures.** `## Heading` lines in the script become chapters. They are written into the MP3 (Apple Podcasts shows them) and into a Podcasting 2.0 chapters file.
- **Private RSS feed.** `publish_feed.py` builds a normal podcast feed under a long random URL and can upload it to Cloudflare Pages (free plan). You add the URL to Apple Podcasts or any podcast app. The feed is marked "do not list" so it stays out of directories.
- **Loudness and pacing.** Audio is normalized to -16 LUFS. Voices are a bit slower by default (change it in show.json), which helps if English isn't your first language.

## Quick start (no coding needed)

You will type a few commands into a terminal. Copy and paste them exactly.

**1. Install** (once). You need Python 3.10+, ffmpeg and a font.

```bash
# Mac (with Homebrew):    brew install python ffmpeg && brew install --cask font-noto-sans-cjk
# Ubuntu/Debian:          sudo apt install python3-pip ffmpeg fonts-noto-cjk
pip install -r requirements.txt
```

**2. Make your config.** Either answer a few questions:

```bash
python3 podcast.py init
```

or copy an example: `python3 podcast.py init --example en` (two hosts, English) or `--example zh` (one host, Chinese, no music). Then open `show.json` in any text editor if you want to change more.

**3. Get a script.** Two ways:

- *Let an AI write it.* Put your notes or links in a Markdown file (see `examples/notes/2026-10-14.md`), then run
  `python3 podcast.py prompt my-notes.md > prompt.txt`. Paste `prompt.txt` into ChatGPT, Claude, Grok or any chat AI. Save its answer as `script.md`. The prompt template is `prompts/notes-to-script.md`. Edit it if you want a different style.
- *Write it yourself.* `python3 podcast.py skeleton -o script.md` gives you an empty script with your hosts and segments. Fill in the lines.

**4. Build the episode.**

```bash
python3 podcast.py build script.md
```

You get `out/<name>-<date>.mp3`, captions (`.vtt`, `.srt`), chapters and a transcript. The first run also makes the music.

**5. Publish** (optional).

```bash
python3 podcast.py publish
```

With `"backend": "local"` this only builds the feed folder on your computer. To put it online, see "Publish a private feed" below.

Every command takes `--config other.json` if you run more than one show.

## What you can change in show.json

| Section | What it controls |
|---|---|
| `show` | Name, tagline (also on the cover), description, your name and email, what hosts call the listener, file name prefix, show-notes footer |
| `language` | Feed language code (`en`, `zh-cn`, ...), the language the AI prompt asks for, and optional `{gloss}` notes in transcripts |
| `hosts` | 1 to 6 hosts. Each has a script label (capital letters, e.g. `ALEX`), a display name for captions, any edge-tts voice, and a speed |
| `voice_defaults` | Default speed, gap between turns, loudness, MP3 bitrate |
| `segments` | The parts of each episode, in order. Used by `skeleton` and `prompt` |
| `music` | `enabled` on/off, your own files for `intro`/`outro`/`stinger`/`bed`, background level |
| `input` | Where your notes live and the file name pattern. `format`: `markdown` (any list of links/notes) or `detailed` |
| `plugins` | Extras that are off by default (see below) |
| `output` | `local`, `cloudflare-pages` or `github`, your time zone, publish time, how many episodes to keep |

Any edge-tts voice works, in any language: run `edge-tts --list-voices`. Mixed English and Chinese works best with the "Multilingual" voices.

**Notes file format** (`input.format = "markdown"`):

```markdown
## Section name
- [Title](https://link) - one sentence about it
  Takeaway: why it matters (optional)
- A note without a link is fine too
```

**Plugins** (all off by default). They exist because I use this for a Chinese-English show and a job-search briefing:
`zh_recap` turns a host line starting with 今天 into a summary line in the show notes; `jobs_briefing` and `zh_jobs_parser` read a daily job list for a second "jobs" episode (`publish_feed.py --kind jobs`, template in `templates/`).

## Script format

```
voice ALEX = en-US-AndrewMultilingualNeural
voice MAYA = en-US-AvaMultilingualNeural

## Cold open
ALEX: Hey, welcome back.
[INTRO]
[BED ON]
## Story one
MAYA: First up...
[STINGER]
[PAUSE 1.5]
[BED OFF]
[OUTRO]
```

- `[INTRO]`, `[OUTRO]`, `[STINGER]`: music.
- `[BED ON]` / `[BED OFF]`: quiet music under the talking.
- `[PAUSE 2]`, `[BEAT]`: silence.
- `(text in brackets)` is not read out.
- `word{meaning}` is not read out, but the meaning shows in the transcript. I use it for Chinese glosses of harder English words.

List voices with `edge-tts --list-voices`. Change speed with `--rate ALEX=+5%` or `--default-rate`.

## Publish a private feed

```bash
python3 publish_feed.py 2026-10-14 --mp3 out/demo.mp3            # builds feed-site/ locally
```

The first run creates `feed-config.<prefix>.json` with a random `slug`. That slug is your "password". Anyone with the feed URL can listen, so don't share it and don't commit that file (it's in `.gitignore`).

To put it online with Cloudflare Pages (free):

1. Make a Cloudflare account and an API token with "Cloudflare Pages: Edit".
2. Set `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in your environment (see `.env.example`). The script never stores or prints the token.
3. Set `"output": {"backend": "cloudflare-pages"}` in show.json (or add `--backend cloudflare-pages`). The first time it creates a project and saves the `base_url`. It needs Node.js for `npx wrangler`.
4. `python3 publish_feed.py --check` tests the feed, cover and audio links.
5. In Apple Podcasts: Library → ⋯ → Follow a Show by URL, and paste `<base_url>/<slug>/feed.xml`.

`pages-functions/` adds byte-range support for the MP3 files, which podcast apps need for seeking.

There is also a GitHub Pages backend. **GitHub Pages sites are public.** The script refuses to use it until you set `"github_public_ok": true` in `output`, and you put the repo name in `github_repo` in the feed-config file. It needs the `gh` command logged in.

## Optional: chapter art and show notes from a daily list

If you keep a notes file for the day in `input.dir` (named by `input.file_pattern`), `episode_extras.py` reads it to make show notes and one chapter per story, with the story's preview image as chapter art. Without that list you still get chapters from your `##` headings, using the show cover.

`--kind jobs` publishes a second, separate daily item (I use it for a short job-search briefing). See `templates/job-briefing-template.md`.

## Files

| File | What it does |
|---|---|
| `make_podcast.py` | Script → MP3, captions, chapters, transcript |
| `make_music.py` | Makes the original music |
| `make_cover.py` | Makes the square show cover |
| `episode_extras.py` | Chapter art, ID3 chapters, show notes |
| `publish_feed.py` | Builds and uploads the private RSS feed |
| `podcast.py` | The one command: init, skeleton, prompt, build, publish |
| `show_config.py` | Reads show.json |
| `examples/configs/` | English two-host and Chinese one-host configs |
| `prompts/notes-to-script.md` | Prompt template for turning notes into a script |
| `pages-functions/` | Cloudflare Pages function for audio seeking |

## Notes

- edge-tts uses Microsoft's online read-aloud service. It's free but unofficial and could change.
- Some podcast apps only show captions for paid or public shows. Apple shows transcripts only for shows in its directory, so for a private feed you may only see the captions in the show notes link.

## License

MIT

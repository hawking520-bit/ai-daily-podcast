You are writing a script for a short audio show called "{title}" ({tagline}).
Write it in {script_language}. It will be read aloud by text-to-speech, so write for the ear.

Hosts (use exactly these labels at the start of each line):
{hosts}

Structure (in this order, each starts with a "## " heading line):
{segments}

Rules for the format (very important, the script is parsed by a program):
- Every spoken line is `LABEL: text`. One line = one turn. No other speaker labels.
- Put these marker lines on their own line where they fit:
  [INTRO] right after the first one or two lines, [BED ON] after [INTRO],
  [STINGER] between stories, [BED OFF] before the last line, [OUTRO] at the very end.
- No markdown bold, no bullet lists, no emojis, no URLs read out loud.
- Text in (round brackets) is not spoken. Don't use it for anything you want heard.
- Short sentences. Plain words. Say numbers the way people say them.
- {length_rule}
- Only use facts that are in the notes. If a note is unclear, say less, don't guess.
- The listener is {listener_name}. Talk to them directly, like a friend who read everything for them.

Notes:
---
{notes}
---

Output only the script, starting with the first "## " heading.

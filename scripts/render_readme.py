#!/usr/bin/env python3
"""Render a README with a language map and the latest English list."""

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

from fetch_new_articles import DEFAULT_PROJECTS

LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "zh": "Chinese",
    "fr": "French",
    "de": "German",
    "ru": "Russian",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "ar": "Arabic",
    "fa": "Persian",
    "tr": "Turkish",
    "he": "Hebrew",
    "sv": "Swedish",
    "nl": "Dutch",
    "ko": "Korean",
    "id": "Indonesian",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
}

INTRO = """\
# New Wikipedia Articles

Hourly lists of articles newly created on Wikipedia, taken from the
[MediaWiki recent changes API](https://www.mediawiki.org/wiki/API:RecentChanges).
A GitHub Actions workflow runs every hour, fetches the articles created since
the previous list and commits one CSV per language to [`data/`](data/), e.g.
[`data/en/new-articles-<timestamp>.csv`](data/en/).

Pick a language below to open its latest CSV, or read the latest English list
further down.
"""

EN_SECTION = """\
## {name} ({code}) \u2014 {end}

{lead}

[Full CSV]({csv_path})

{body}
"""

TABLE_HEADER = """\
| Created (UTC) | Article | Creator | Bytes |
| :------------ | :------ | :------ | ----: |"""


def parse_iso(value):
    text = str(value)
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H-%M-%SZ"):
        try:
            return dt.datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def display_timestamp(value):
    moment = parse_iso(value)
    if moment is None:
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def display_time(value):
    moment = parse_iso(value)
    if moment is None:
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def languages_from_files(data_dir):
    languages = {}
    for path in sorted(Path(data_dir).glob("*/new-articles-*.csv")):
        code = path.parent.name
        moment = parse_iso(path.stem.removeprefix("new-articles-"))
        if moment is None:
            continue
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        current = languages.get(code)
        if current is None or stamp > current["to"]:
            languages[code] = {"path": path.as_posix(), "to": stamp}
    return languages


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_languages(manifest_path, data_dir):
    text = read_manifest_text(manifest_path)
    try:
        data = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        data = None
    languages = data.get("languages") if isinstance(data, dict) else None
    if not isinstance(languages, dict):
        return languages_from_files(data_dir)
    return languages


def ordered_codes(languages):
    known = list(DEFAULT_PROJECTS)
    extra = sorted(code for code in languages if code not in known)
    return known + extra


def read_csv_text(path):
    file = Path(path)
    if file.exists():
        return file.read_text(encoding="utf-8")
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def article_link(code, article):
    url = f"https://{code}.wikipedia.org/wiki/" + urllib.parse.quote(article)
    label = article.replace("_", " ").replace("|", "\\|")
    return f"[{label}]({url})"


def user_link(code, user):
    url = (
        f"https://{code}.wikipedia.org/wiki/User:"
        + urllib.parse.quote(user.replace(" ", "_"))
    )
    label = user.replace("|", "\\|")
    return f"[{label}]({url})"


def display_bytes(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def render_rows(code, rows):
    return "\n".join(
        f"| {display_time(row['created_at'])} | {article_link(code, row['article'])} "
        f"| {user_link(code, row['user'])} | {display_bytes(row['bytes'])} |"
        for row in rows
    )


def render_map(languages, codes):
    lines = [
        "| Language | Code | Latest list | Articles |",
        "| :------- | :--- | :---------- | -------: |",
    ]
    for code in codes:
        name = LANGUAGE_NAMES.get(code, code)
        entry = languages.get(code) or {}
        if entry.get("path"):
            latest = f"[{display_timestamp(entry.get('to'))}]({entry['path']})"
            count = entry.get("count")
            count = f"{count:,}" if isinstance(count, int) else ""
        else:
            latest, count = "\u2014", ""
        lines.append(f"| {name} | `{code}` | {latest} | {count} |")
    return "\n".join(lines)


def render_section(code, entry, rows, limit):
    end = display_timestamp(entry.get("to"))
    if entry.get("from"):
        lead = (
            f"New articles created between {display_timestamp(entry['from'])} "
            f"and {end}."
        )
    else:
        lead = f"New articles created up to {end}."
    if rows is None:
        body = "_The latest CSV could not be read; open it for the full list._"
    elif not rows:
        body = "_No articles were created in this window._"
    else:
        body = TABLE_HEADER + "\n" + render_rows(code, rows[:limit])
        if len(rows) > limit:
            body += (
                f"\n\n_Showing the first {limit:,} of {len(rows):,} articles; "
                f"see the [full CSV]({entry['path']})._"
            )
    return EN_SECTION.format(
        name=LANGUAGE_NAMES.get(code, code),
        code=code,
        end=end,
        lead=lead,
        csv_path=entry["path"],
        body=body,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)

    languages = load_languages(args.manifest, args.data_dir)
    if not languages:
        print("no lists found; rendering a map without any links", file=sys.stderr)

    codes = ordered_codes(languages)
    content = INTRO + "\n## Languages\n\n" + render_map(languages, codes) + "\n\n"

    entry = languages.get("en")
    if entry and entry.get("path"):
        text = read_csv_text(entry["path"])
        rows = list(csv.DictReader(text.splitlines())) if text is not None else None
        content += render_section("en", entry, rows, args.limit)
    else:
        print("no English list found; map rendered without the article table", file=sys.stderr)

    Path(args.output).write_text(content, encoding="utf-8")
    print(f"wrote language map ({len(codes)} languages) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

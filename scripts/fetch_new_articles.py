#!/usr/bin/env python3
"""Fetch Wikipedia articles created between the previous list and now."""

import argparse
import csv
import datetime as dt
import http.client
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://{domain}/w/api.php"
DEFAULT_USER_AGENT = (
    "new-wikipedia-articles/1.0 "
    "(https://github.com/GHLists/new-wikipedia-articles)"
)

DEFAULT_PROJECTS = (
    "en", "ja", "zh", "fr", "de", "ru", "es", "it", "pt", "pl",
    "ar", "fa", "tr", "he", "sv", "nl", "ko", "id", "uk", "vi",
)

COMMENT_LIMIT = 200

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


def to_domain(code):
    return code if "." in code else f"{code}.wikipedia.org"


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0)


def timestamp_filename(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def fetch_page(domain, params, user_agent, retries=3, backoff=5.0):
    url = f"{API_URL.format(domain=domain)}?{urllib.parse.urlencode(params)}"
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
        except TRANSIENT_ERRORS as error:
            last_error = error
        else:
            if "error" in payload:
                last_error = RuntimeError(
                    payload["error"].get("info", "unknown API error")
                )
            else:
                return payload
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_window(domain, start, end, user_agent, retries):
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "list": "recentchanges",
        "rcnamespace": 0,
        "rctype": "new",
        "rcshow": "!redirect",
        "rcdir": "newer",
        "rcstart": iso(start),
        "rcend": iso(end),
        "rclimit": "max",
        "rcprop": "title|timestamp|user|comment|sizes|ids",
    }
    entries = []
    seen = set()
    while True:
        payload = fetch_page(domain, params, user_agent, retries=retries)
        for entry in payload.get("query", {}).get("recentchanges") or []:
            key = entry.get("rcid")
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
        cont = payload.get("continue") or {}
        if "rccontinue" not in cont:
            break
        params["rccontinue"] = cont["rccontinue"]
    entries.sort(key=lambda entry: entry.get("timestamp", ""))
    return entries


def clean_comment(comment):
    text = " ".join(comment.split())
    if len(text) > COMMENT_LIMIT:
        text = text[: COMMENT_LIMIT - 1].rstrip() + "\u2026"
    return text


def write_csv(path, project, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["created_at", "article", "pageid", "user", "bytes", "comment", "project"]
        )
        for entry in entries:
            writer.writerow(
                [
                    entry.get("timestamp", ""),
                    (entry.get("title") or "").replace(" ", "_"),
                    entry.get("pageid", ""),
                    entry.get("user", ""),
                    entry.get("newlen", ""),
                    clean_comment(entry.get("comment") or ""),
                    project,
                ]
            )


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


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {"languages": {}}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"languages": {}}
    if not isinstance(data, dict) or not isinstance(data.get("languages"), dict):
        return {"languages": {}}
    return data


def save_manifest(path, manifest):
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    Path(path).write_text(text, encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of each language's last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument(
        "--projects",
        default=",".join(DEFAULT_PROJECTS),
        help="comma-separated language codes (default: the 20 most-read wikis)",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length for languages without a previous list (default: 1)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    until = parse_timestamp(args.until) if args.until else now
    override_since = parse_timestamp(args.since) if args.since else None
    manifest = load_manifest(args.manifest)
    languages = manifest.setdefault("languages", {})
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    codes = [code.strip() for code in args.projects.split(",") if code.strip()]
    failed = []
    updated = 0
    for code in codes:
        domain = to_domain(code)
        previous = languages.get(code) or {}
        if override_since is not None:
            start = override_since
        else:
            try:
                start = parse_timestamp(previous["to"])
            except (KeyError, TypeError, ValueError):
                start = until - dt.timedelta(hours=args.lookback_hours)
        if start >= until:
            print(
                f"{domain}: nothing to do ({iso(start)} >= {iso(until)})",
                file=sys.stderr,
            )
            continue
        try:
            entries = fetch_window(domain, start, until, args.user_agent, args.retries)
        except RuntimeError as error:
            print(f"{domain}: {error}", file=sys.stderr)
            failed.append(code)
            continue
        if not entries:
            print(f"{domain}: no new articles between {iso(start)} and {iso(until)}")
            continue
        output = (
            output_dir / code / f"new-articles-{timestamp_filename(until)}.csv"
        )
        write_csv(output, domain, entries)
        languages[code] = {
            "path": output.as_posix(),
            "from": iso(start),
            "to": iso(until),
            "count": len(entries),
        }
        updated += 1
        print(f"wrote {len(entries)} articles for {iso(start)}..{iso(until)} to {output}")
        time.sleep(1)

    if updated or not Path(args.manifest).exists():
        save_manifest(args.manifest, manifest)

    if failed:
        print(f"failed wikis: {', '.join(failed)}", file=sys.stderr)
        if len(failed) == len(codes):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

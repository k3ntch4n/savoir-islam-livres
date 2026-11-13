#!/usr/bin/env python3
"""Construit assets/data/playlists_catalog.json : les vraies playlists YouTube
(séries de cours) des chaînes de confiance, avec leurs vidéos DANS L'ORDRE.

Même principe que build_catalog.py : tourne hors de l'app, l'app lit le JSON.

Pour rester léger, chaque playlist ne stocke que les IDs de ses vidéos ; les
titres sont retrouvés dans videos_catalog.json. Seules les vidéos absentes de ce
catalogue (autre chaîne, ancienne vidéo...) ont leur titre dans "titles".

Quota : channels.list 1 + playlists.list 1/page de 50 + playlistItems.list
1/page de 50 vidéos. Jamais search.list.

Usage :
  export YT_API_KEY=...
  python tools/build_playlists.py
"""

from __future__ import annotations

import json
import re
import os
import sys
from datetime import datetime, timezone

from build_catalog import (CATALOG_JSON, ROOT, HttpError, build,
                           fetch_playlist_videos, load_channels)

PLAYLISTS_JSON = ROOT / "playlists_catalog.json"
MIN_VIDEOS = 3  # en dessous, ce n'est pas une série
UNAVAILABLE = {"private video", "deleted video"}


def list_channel_playlists(yt, handle: str):
    """Retourne (channel_title, [{id, title, count}])."""
    resp = yt.channels().list(part="id,snippet", forHandle=handle,
                              maxResults=1).execute()
    items = resp.get("items", [])
    if not items:
        return None, []
    channel_id = items[0]["id"]
    title = items[0]["snippet"]["title"]
    out, token = [], None
    while True:
        r = yt.playlists().list(part="snippet,contentDetails",
                                channelId=channel_id, maxResults=50,
                                pageToken=token).execute()
        for it in r.get("items", []):
            out.append({
                "id": it["id"],
                "title": it["snippet"]["title"].strip(),
                "count": it["contentDetails"]["itemCount"],
            })
        token = r.get("nextPageToken")
        if not token:
            break
    return title, out


# Playlists dont l'ordre YouTube est faux : trié d'après « Notion n°N »
# (introduction = 0, « Dernière vidéo » = fin).
NOTION_ORDER_FIX = {"PLd1k0s80xtuiiteUs0RKaA1BcgcNrAX3Z"}  # Cours de Tajwid


def _notion_key(title: str) -> int:
    t = title.lower()
    m = re.search(r"notion\s*n?\s*°?\s*(\d+)", t)
    if m:
        return int(m.group(1))
    return 999 if "dernière" in t else 0


def main() -> int:
    api_key = os.environ.get("YT_API_KEY")
    if not api_key:
        sys.exit("Variable d'environnement YT_API_KEY absente (voir README).")

    known_ids: set[str] = set()
    if CATALOG_JSON.exists():
        cat = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
        for ch in cat.get("channels", []):
            known_ids.update(v["id"] for v in ch.get("videos", []))

    yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    channels = load_channels()
    playlists, titles, seen = [], {}, set()
    for i, ch in enumerate(channels, 1):
        handle = ch["handle"]
        print(f"[{i}/{len(channels)}] @{handle} …", end=" ", flush=True)
        try:
            ch_title, pls = list_channel_playlists(yt, handle)
            kept = 0
            for pl in pls:
                if pl["count"] < MIN_VIDEOS or pl["id"] in seen:
                    continue
                vids = [v for v in fetch_playlist_videos(yt, pl["id"])
                        if v["title"].lower() not in UNAVAILABLE]
                if len(vids) < MIN_VIDEOS:
                    continue
                if pl["id"] in NOTION_ORDER_FIX:
                    vids.sort(key=lambda v: _notion_key(v["title"]))
                seen.add(pl["id"])
                for v in vids:
                    if v["id"] not in known_ids:
                        titles[v["id"]] = v["title"]
                playlists.append({
                    "id": pl["id"],
                    "title": pl["title"],
                    "channel": ch_title or handle,
                    "videos": [v["id"] for v in vids],
                })
                kept += 1
        except HttpError as e:
            print(f"erreur API ({e.status_code}), sautée.")
            continue
        print(f"{kept} playlist(s).")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "playlists": playlists,
        "titles": titles,
    }
    PLAYLISTS_JSON.write_text(json.dumps(payload, ensure_ascii=False),
                              encoding="utf-8")
    size_mb = PLAYLISTS_JSON.stat().st_size / 1_048_576
    print(f"\nÉcrit {PLAYLISTS_JSON.relative_to(ROOT)} — {len(playlists)} "
          f"playlists, {len(titles)} titres hors catalogue, {size_mb:.2f} Mo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

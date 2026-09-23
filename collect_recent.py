#!/usr/bin/env python3
"""Collecte du soir : dernières vidéos des chaînes fiables -> videos_recent.json.

Tourne 1x/jour vers 18h (heure de Paris) via GitHub Actions (voir
.github/workflows/evening-recent.yml). Pour chaque chaîne listée dans
content.json, lit la 1re page (50 vidéos les plus récentes) de sa playlist
« uploads », puis garde les RECENT_COUNT vidéos les plus récentes toutes
chaînes confondues.

videos_recent.json a le même format que videos_catalog.json (en plus petit) :
l'app le télécharge pour la section « Nouvelles vidéos » de l'accueil. Le
catalogue complet (thèmes, recherche, vidéo à la une) n'est PAS touché.

S'il y a de nouvelles vidéos par rapport à la veille, envoie UNE notification
« 🎬 De nouvelles vidéos t'attendent ! » (via notify.py). Une vidéo n'est
annoncée que si elle a moins de FRESH_DAYS jours : ajouter une chaîne ne
déclenche pas une avalanche d'anciennes vidéos.

Quota YouTube : 2 unités par chaîne (channels.list + playlistItems.list),
~40-50 unités/jour sur 10 000 gratuites.

Usage : YT_API_KEY=... python3 collect_recent.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).parent
CONTENT = ROOT / "content.json"
RECENT = ROOT / "videos_recent.json"

RECENT_COUNT = 40
FRESH_DAYS = 3


def handle_from_url(url: str) -> str | None:
    m = re.search(r"/@([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/c/([^/?#]+)", url)
    return unquote(m.group(1)) if m else None


def parse_date(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def collect(api_key: str) -> list[dict]:
    """Liste de chaînes {handle, title, videos:[{id,title,date}]}."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    content = json.loads(CONTENT.read_text(encoding="utf-8"))
    out = []
    for ch in content.get("channels", []):
        handle = handle_from_url(ch.get("url", ""))
        if not handle:
            continue
        try:
            resp = yt.channels().list(
                part="contentDetails,snippet", forHandle=handle, maxResults=1,
            ).execute()
            items = resp.get("items", [])
            if not items:
                print(f"  ! @{handle} introuvable, sauté.")
                continue
            title = items[0]["snippet"]["title"]
            uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            page = yt.playlistItems().list(
                part="snippet,contentDetails", playlistId=uploads, maxResults=50,
            ).execute()
        except HttpError as e:
            print(f"  ! @{handle} erreur API ({e.status_code}), sauté.")
            continue
        videos = []
        for it in page.get("items", []):
            sn, cd = it["snippet"], it.get("contentDetails", {})
            vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            videos.append({
                "id": vid,
                "title": sn.get("title", "").strip(),
                "date": cd.get("videoPublishedAt") or sn.get("publishedAt", ""),
            })
        out.append({"handle": handle, "title": title, "videos": videos})
        print(f"  @{handle} : {len(videos)} vidéos lues.")
    return out


def keep_most_recent(channels: list[dict]) -> list[dict]:
    """Garde les RECENT_COUNT vidéos les plus récentes, regroupées par chaîne."""
    flat = [
        (v, ch) for ch in channels for v in ch["videos"]
        if v["title"] and parse_date(v["date"])
        # vidéos privées/supprimées : titre générique, on les écarte
        and v["title"] not in ("Private video", "Deleted video")
    ]
    flat.sort(key=lambda p: p[0]["date"], reverse=True)
    kept: dict[str, dict] = {}
    for v, ch in flat[:RECENT_COUNT]:
        kept.setdefault(ch["handle"], {
            "handle": ch["handle"], "title": ch["title"], "videos": [],
        })["videos"].append(v)
    return list(kept.values())


def ids(channels: list[dict]) -> set[str]:
    return {v["id"] for ch in channels for v in ch["videos"]}


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    api_key = os.environ.get("YT_API_KEY", "").strip()
    if not api_key:
        sys.exit("YT_API_KEY absente (secret GitHub).")

    previous = None
    if RECENT.exists():
        previous = json.loads(RECENT.read_text(encoding="utf-8"))

    channels = keep_most_recent(collect(api_key))
    if not channels:
        sys.exit("Aucune vidéo récupérée : fichier inchangé.")

    RECENT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channels": channels,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{RECENT.name} : {len(ids(channels))} vidéos.")

    if previous is None:
        print("Premier passage : pas de comparaison, pas de notification.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
    old_ids = ids(previous.get("channels", []))
    added = [
        v for ch in channels for v in ch["videos"]
        if v["id"] not in old_ids and parse_date(v["date"]) >= cutoff
    ]
    if not added:
        print("Pas de nouvelle vidéo depuis hier : pas de notification.")
        return

    from notify import send_all, summary

    n = len(added)
    data = {"type": "video"}
    if n == 1:
        data |= {"videoId": added[0]["id"], "title": added[0]["title"]}
    send_all([{
        "title": "🎬 Une nouvelle vidéo t'attend !" if n == 1
        else "🎬 De nouvelles vidéos t'attendent !",
        "body": summary([v["title"] for v in added]),
        "data": data,
    }], dry_run=dry_run)


if __name__ == "__main__":
    main()

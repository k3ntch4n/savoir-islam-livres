#!/usr/bin/env python3
"""Construit assets/data/videos_catalog.json : un index local (titre + description +
id + date) de toutes les vidéos des chaînes de confiance listées dans
assets/data/content.json.

Ce script tourne EN DEHORS de l'app, ~1x par semaine. L'app, elle, ne fait
qu'une recherche 100 % hors ligne dans le JSON produit ici.

Quota YouTube Data API v3 :
  - channels.list        -> 1 unité par chaîne
  - playlistItems.list   -> 1 unité par page de 50 vidéos
  On n'utilise JAMAIS search.list (100 unités/appel).
  Ordre de grandeur : ~22 chaînes, playlists de 200-2000 vidéos
  => quelques centaines d'unités par rafraîchissement complet,
     très loin du quota gratuit de 10 000 unités/jour.

Usage :
  set YT_API_KEY=...        (Windows)  / export YT_API_KEY=...  (bash)
  python tools/build_catalog.py            # ne fait rien si le fichier a < 7 jours
  python tools/build_catalog.py --refresh  # force la régénération
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.exit(
        "Dépendance manquante. Installe-la avec :\n"
        "    pip install -r tools/requirements.txt"
    )

ROOT = Path(__file__).resolve().parent.parent
CONTENT_JSON = ROOT / "content.json"
CATALOG_JSON = ROOT / "videos_catalog.json"

MAX_DESC = 300           # on tronque les descriptions
STALE_AFTER_DAYS = 7     # en dessous, on ne régénère pas sans --refresh
PAGE_SIZE = 50           # maximum autorisé par l'API


def handle_from_url(url: str) -> str | None:
    """Extrait le @handle d'une URL de chaîne YouTube.

    https://www.youtube.com/@SalimMCHICH            -> SalimMCHICH
    https://youtube.com/@omradusavoir/playlists     -> omradusavoir
    https://www.youtube.com/c/MosqueeAbouBakr...     -> MosqueeAbouBakr... (best effort)
    """
    m = re.search(r"/@([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/c/([^/?#]+)", url)
    if m:
        # Pas de @handle : on tente le nom d'URL personnalisé comme handle.
        # channels.list(forHandle=...) l'accepte parfois ; sinon on log et on saute.
        from urllib.parse import unquote
        return unquote(m.group(1))
    return None


def load_channels() -> list[dict]:
    data = json.loads(CONTENT_JSON.read_text(encoding="utf-8"))
    out = []
    for ch in data.get("channels", []):
        handle = handle_from_url(ch.get("url", ""))
        if not handle:
            print(f"  ! ignoré (pas de handle) : {ch.get('name')} — {ch.get('url')}")
            continue
        out.append({"name": ch.get("name", handle), "handle": handle})
    return out


def is_fresh() -> bool:
    if not CATALOG_JSON.exists():
        return False
    try:
        data = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
        gen = datetime.fromisoformat(data["generated_at"])
    except Exception:
        return False
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - gen).total_seconds() / 86400
    return age_days < STALE_AFTER_DAYS


def resolve_uploads_playlist(yt, handle: str):
    """Retourne (channel_title, uploads_playlist_id) ou (None, None)."""
    resp = yt.channels().list(
        part="contentDetails,snippet",
        forHandle=handle,
        maxResults=1,
    ).execute()
    items = resp.get("items", [])
    if not items:
        return None, None
    it = items[0]
    title = it["snippet"]["title"]
    uploads = it["contentDetails"]["relatedPlaylists"]["uploads"]
    return title, uploads


def fetch_playlist_videos(yt, playlist_id: str) -> list[dict]:
    videos = []
    page_token = None
    while True:
        resp = yt.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=PAGE_SIZE,
            pageToken=page_token,
        ).execute()
        for it in resp.get("items", []):
            sn = it["snippet"]
            cd = it.get("contentDetails", {})
            vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            desc = (sn.get("description") or "").strip().replace("\r", "")
            if len(desc) > MAX_DESC:
                desc = desc[:MAX_DESC].rstrip() + "…"
            videos.append({
                "id": vid,
                "title": sn.get("title", "").strip(),
                "desc": desc,
                "date": cd.get("videoPublishedAt") or sn.get("publishedAt", ""),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.1)  # courtoisie
    return videos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="force la régénération même si le fichier est récent")
    args = ap.parse_args()

    if not args.refresh and is_fresh():
        print(f"{CATALOG_JSON.name} a moins de {STALE_AFTER_DAYS} jours — rien à faire "
              f"(utilise --refresh pour forcer).")
        return 0

    api_key = os.environ.get("YT_API_KEY")
    if not api_key:
        sys.exit("Variable d'environnement YT_API_KEY absente. "
                 "Voir tools/README.md pour créer une clé YouTube Data API v3.")

    yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    channels_in = load_channels()
    print(f"{len(channels_in)} chaîne(s) à indexer.")

    result_channels = []
    total_videos = 0
    for i, ch in enumerate(channels_in, 1):
        handle = ch["handle"]
        print(f"[{i}/{len(channels_in)}] @{handle} …", end=" ", flush=True)
        try:
            title, uploads = resolve_uploads_playlist(yt, handle)
            if not uploads:
                print("introuvable, sauté.")
                continue
            vids = fetch_playlist_videos(yt, uploads)
        except HttpError as e:
            print(f"erreur API ({e.status_code}), sauté.")
            continue
        vids.sort(key=lambda v: v["date"], reverse=True)
        result_channels.append({
            "handle": handle,
            "title": title,
            "videos": vids,
        })
        total_videos += len(vids)
        print(f"{len(vids)} vidéos.")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channels": result_channels,
    }
    CATALOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    size_mb = CATALOG_JSON.stat().st_size / 1_048_576
    print(f"\nÉcrit {CATALOG_JSON.relative_to(ROOT)} — "
          f"{len(result_channels)} chaînes, {total_videos} vidéos, {size_mb:.2f} Mo.")
    if size_mb > 5:
        print("⚠  L'asset dépasse 5 Mo : envisage un hébergement distant du JSON "
              "(voir TODO dans lib/main.dart).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Choisit la vidéo « à la une » du jour et met à jour content.json.

Tourne 1x/jour via GitHub Actions (voir .github/workflows/daily-featured.yml).
Aucune clé API requise. On pioche EN PRIORITÉ dans videos_recent.json (les
40 dernières vidéos des chaînes, rafraîchies chaque soir par
collect_recent.py). Repli sur videos_catalog.json (construit à la main par
tools/build_catalog.py côté app, vidéos de moins de MAX_AGE_DAYS) si le
fichier récent est absent ou vide.

Choix déterministe par date (UTC) : même jour -> même vidéo pour tout le
monde, et la sélection change automatiquement le lendemain.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_AGE_DAYS = 60
ROOT = Path(__file__).parent
CATALOG = ROOT / "videos_catalog.json"
RECENT = ROOT / "videos_recent.json"
CONTENT = ROOT / "content.json"


def load_candidates(path: Path, max_age_days: int | None) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days is not None
        else None
    )
    out = []
    for chan in data.get("channels", []):
        for v in chan.get("videos", []):
            date_str = v.get("date")
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if cutoff is not None and dt < cutoff:
                continue
            out.append({
                "videoId": v["id"],
                "title": v.get("title", "").strip(),
                "source": chan.get("title") or chan.get("handle", ""),
            })
    return out


def pick_for_today(candidates: list[dict]) -> dict:
    # Ordre stable (indépendant de l'ordre du JSON) avant de piocher, pour que
    # le hash du jour retombe toujours sur la même vidéo pour une même liste.
    candidates = sorted(candidates, key=lambda c: c["videoId"])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hashlib.sha256(today.encode()).hexdigest()
    idx = int(digest, 16) % len(candidates)
    return candidates[idx]


def main() -> None:
    # Priorité aux vidéos récentes (collecte du soir), repli sur le catalogue.
    candidates = [c for c in load_candidates(RECENT, None) if c["title"]]
    if not candidates:
        candidates = [
            c for c in load_candidates(CATALOG, MAX_AGE_DAYS) if c["title"]
        ]
    if not candidates:
        print("Aucune vidéo candidate (catalogue vide ou trop ancien) -> inchangé.")
        return

    featured = pick_for_today(candidates)
    content = json.loads(CONTENT.read_text(encoding="utf-8"))
    if content.get("featured") == featured:
        print("Vidéo à la une déjà à jour pour aujourd'hui.")
        return

    content["featured"] = featured
    CONTENT.write_text(
        json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Nouvelle vidéo à la une : {featured['videoId']} — {featured['title']}")


if __name__ == "__main__":
    main()

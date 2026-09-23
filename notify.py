#!/usr/bin/env python3
"""Envoie une notification push quand du nouveau contenu arrive dans l'app.

Tourne via GitHub Actions (voir .github/workflows/notify.yml) à chaque push
qui touche content.json ou books.json. Compare la version d'avant le push
avec la nouvelle et ne notifie que les AJOUTS :

  - nouveau livre (nouvel `id` dans books.json)
      -> « 📚 Un nouveau livre est disponible ! »
  - nouvelle vidéo (nouveau `videoId` dans content.json : thèmes, piliers,
    biographies, FAQ...)
      -> « 🎬 Une nouvelle vidéo t'attend ! »

Ne notifie PAS : les retraits, corrections de texte, réordonnancements, ni
la vidéo « à la une » du jour (bloc `featured`, changé chaque matin par
pick_featured.py — on ne veut pas une notification par jour). Les nouvelles
vidéos des chaînes sont annoncées par la collecte du soir (collect_recent.py,
qui réutilise send_all ci-dessous).

Envoi via Firebase Cloud Messaging (API HTTP v1) au topic « nouveautes »,
auquel toutes les installations de l'app sont abonnées. La clé du compte de
service Firebase est lue dans la variable d'environnement
FIREBASE_SERVICE_ACCOUNT (secret GitHub). Sans clé : simulation (affiche ce
qui serait envoyé, n'envoie rien).

Usage : python3 notify.py <sha_avant> <sha_apres> [--dry-run]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PROJECT_ID = "savoir-islam"
TOPIC = "nouveautes"
FCM_URL = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"


def load_at(sha: str, path: str) -> dict | None:
    """Contenu JSON de `path` au commit `sha` (None si absent/illisible)."""
    try:
        raw = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            capture_output=True, check=True,
        ).stdout
        return json.loads(raw.decode("utf-8"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def books_by_id(data: dict | None) -> dict[str, dict]:
    if not data:
        return {}
    return {b["id"]: b for b in data.get("books", []) if b.get("id")}


def videos_by_id(data: dict | None) -> dict[str, str]:
    """videoId -> titre, pour toutes les vidéos de content.json sauf la une."""
    out: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            vid = node.get("videoId")
            if isinstance(vid, str) and vid:
                out.setdefault(vid, (node.get("title") or "").strip())
            for key, child in node.items():
                if key != "featured":
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    if data:
        walk({k: v for k, v in data.items() if k != "featured"})
    return out


def plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


def summary(titles: list[str]) -> str:
    titles = [t for t in titles if t]
    if len(titles) <= 3:
        return " · ".join(titles)
    return " · ".join(titles[:3]) + f" et {len(titles) - 3} autre(s)"


def build_messages(before: str, after: str) -> list[dict]:
    messages = []

    old_books = books_by_id(load_at(before, "books.json"))
    new_books = books_by_id(load_at(after, "books.json"))
    added_books = [b for i, b in new_books.items() if i not in old_books]
    if added_books:
        n = len(added_books)
        messages.append({
            "title": plural(n, "📚 Un nouveau livre est disponible !",
                            "📚 {n} nouveaux livres sont disponibles !"),
            "body": summary([b.get("title", "") for b in added_books]),
            "data": {"type": "book"},
        })

    old_videos = videos_by_id(load_at(before, "content.json"))
    new_videos = videos_by_id(load_at(after, "content.json"))
    added_videos = [(v, t) for v, t in new_videos.items() if v not in old_videos]
    if added_videos:
        n = len(added_videos)
        data = {"type": "video"}
        if n == 1:
            # Une seule vidéo : le tap l'ouvre directement dans le lecteur.
            data |= {"videoId": added_videos[0][0], "title": added_videos[0][1]}
        messages.append({
            "title": plural(n, "🎬 Une nouvelle vidéo t'attend !",
                            "🎬 {n} nouvelles vidéos t'attendent !"),
            "body": summary([t for _, t in added_videos]),
            "data": data,
        })

    return messages


def access_token(service_account: dict) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account as sa

    creds = sa.Credentials.from_service_account_info(
        service_account,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"],
    )
    creds.refresh(Request())
    return creds.token


def send(msg: dict, token: str) -> None:
    import requests

    payload = {
        "message": {
            "topic": TOPIC,
            "notification": {"title": msg["title"], "body": msg["body"]},
            "data": msg["data"],
            "android": {
                "priority": "normal",
                "notification": {
                    "channel_id": "nouveautes",
                    "icon": "ic_stat_notify",
                    "color": "#8FD8BD",
                },
            },
        }
    }
    res = requests.post(
        FCM_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=30,
    )
    if res.status_code != 200:
        sys.exit(f"Échec FCM {res.status_code} : {res.text}")
    print(f"Envoyé : {res.json().get('name')}")


def send_all(messages: list[dict], dry_run: bool = False) -> None:
    """Affiche puis envoie les messages (simulation sans clé ou en dry-run)."""
    for m in messages:
        print(f"-> {m['title']}\n   {m['body']}\n   data={m['data']}")

    raw_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if dry_run or not raw_key:
        print("Simulation (pas de clé ou --dry-run) : rien n'a été envoyé.")
        return

    token = access_token(json.loads(raw_key))
    for m in messages:
        send(m, token)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)
    before, after = args

    if set(before) == {"0"}:
        print("Premier push de la branche : rien à comparer.")
        return

    messages = build_messages(before, after)
    if not messages:
        print("Aucun nouveau livre ni nouvelle vidéo : pas de notification.")
        return
    send_all(messages, dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()

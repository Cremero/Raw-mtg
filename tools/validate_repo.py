#!/usr/bin/env python3
"""Valida el repo de datos (v2.97). Solo stdlib. Lo ejecuta .github/workflows/validate.yml en cada push y la Action
semanal antes de commitear.   python tools/validate_repo.py [--root .] [--lenient]
Falla (código 1) si: una lista no cumple el esquema, el manifiesto no coincide con los archivos (sha git), hay
archivos de más de 90 MB, un JSON no parsea, un precon del índice falta, la watchlist tiene líneas absurdas o un mazo
de decks/ pesa más de 200 KB."""
import argparse, hashlib, json, os, re, sys

MAX_FILE = 90_000_000
MAX_DECK = 200_000


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def validate(root, lenient=False):
    errs = []

    def err(m):
        errs.append(m)

    def load(rel):
        try:
            return json.load(open(os.path.join(root, rel), encoding="utf-8"))
        except (OSError, ValueError) as ex:
            err(f"{rel}: no se puede leer ({ex})")

    for dp, dirs, fns in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.getsize(p) > MAX_FILE:
                err(f"{os.path.relpath(p, root)}: pesa más de 90 MB")
    for rel, key, mn in (("data/commander-banlist.json", "banned_cards", 20), ("data/game-changers.json", "game_changers", 10)):
        if not os.path.exists(os.path.join(root, rel)):
            continue
        d = load(rel)
        if not isinstance(d, dict):
            continue
        names = d.get(key)
        if not (isinstance(names, list) and all(isinstance(x, str) and 0 < len(x) <= 200 for x in names)):
            err(f"{rel}: '{key}' no es una lista de nombres")
            continue
        if len(names) < (1 if lenient else mn) or len(set(n.lower() for n in names)) != len(names):
            err(f"{rel}: {len(names)} entradas (mínimo {mn}, sin repetidas)")
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", str(d.get("last_verified", ""))):
            err(f"{rel}: last_verified inválido")
    if os.path.exists(os.path.join(root, "manifest.json")):
        m = load("manifest.json")
        if isinstance(m, dict) and isinstance(m.get("files"), dict):
            for rel, ent in m["files"].items():
                p = os.path.join(root, rel)
                if ".." in rel.split("/") or not os.path.exists(p):
                    err(f"manifest: {rel} no existe")
                elif git_blob_sha(open(p, "rb").read()) != ent.get("sha"):
                    err(f"manifest: {rel} no coincide con su sha (¿editado a mano sin regenerar el manifiesto?)")
        elif m is not None:
            err("manifest.json sin 'files'")
    if os.path.exists(os.path.join(root, "precons", "index.json")):
        idx = load("precons/index.json")
        for slug in (idx or {}):
            if not os.path.exists(os.path.join(root, "precons", slug + ".txt")):
                err(f"precons/index.json: falta precons/{slug}.txt")
    for sub in ("stats", "combos"):
        for dp, _, fns in os.walk(os.path.join(root, sub)):
            for fn in fns:
                if fn.endswith(".json"):
                    try:
                        json.load(open(os.path.join(dp, fn), encoding="utf-8"))
                    except (OSError, ValueError):
                        err(f"{os.path.relpath(os.path.join(dp, fn), root)}: JSON inválido")
    dd = os.path.join(root, "decks")
    if os.path.isdir(dd):
        for fn in os.listdir(dd):
            if fn.endswith(".txt") and os.path.getsize(os.path.join(dd, fn)) > MAX_DECK:
                err(f"decks/{fn}: más de 200 KB, no parece una lista")
    wp = os.path.join(root, "watchlist.txt")
    if os.path.exists(wp):
        lines = [l.strip() for l in open(wp, encoding="utf-8") if l.strip() and not l.strip().startswith("#")]
        if len(lines) > 60:
            err("watchlist.txt: más de 60 comandantes")
        for l in lines:
            if len(l) > 200 or re.search(r"[\x00-\x1f]", l):
                err(f"watchlist.txt: línea no válida: {l[:40]!r}")
    return errs


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--lenient", action="store_true")
    a = ap.parse_args()
    e = validate(a.root, a.lenient)
    for x in e:
        print("✗", x, file=sys.stderr)
    print("OK: repo válido" if not e else f"{len(e)} problema(s)")
    sys.exit(1 if e else 0)

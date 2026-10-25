#!/usr/bin/env python3
"""Sincroniza los datos del repo (v2.97). Solo biblioteca estándar; lo ejecuta la GitHub Action semanal
(.github/workflows/sync-data.yml) y también sirve en tu PC.

  python tools/sync_data.py [--out .] [--dist dist] [--only cache,lists,rulings,precons] [--lenient]
                            [--bulk-dir DIR]      # tests: usa archivos locales en vez de Scryfall/MTGJSON

Qué produce (rutas relativas a --out):
  dist/card-cache.json.gz     TODAS las cartas jugables (oracle) + mapa set/número → oracle_id, formato de la skill (v2)
  dist/rulings.json.gz        {"_meta":…, "by_oracle": {oracle_id: [[fecha, texto], …]}}
  data/commander-banlist.json y data/game-changers.json   sacados de la propia caché (legalities.commander, game_changer)
  precons/<slug>.txt + precons/index.json                  mazos Commander de MTGJSON, en el formato stock.txt de la skill
  manifest.json               sha git de los archivos versionados + sha256 y tamaño de los assets de dist/

Los assets de dist/ se publican como *release* (tag data-latest) para no engordar el historial de git.
Seguridad: solo https a api.scryfall.com, data.scryfall.io y mtgjson.com; tope de tamaño; si Scryfall/MTGJSON devuelven
algo absurdo (muy pocas cartas, listas fuera de rango) NO se escribe nada de esa pieza y el proceso termina con error.
"""
import argparse, datetime, gzip, hashlib, json, os, re, sys, tempfile, time, urllib.parse, urllib.request

UA = {"User-Agent": "mtg-deck-advisor-sync/1.0", "Accept": "application/json;q=0.9,*/*;q=0.8"}
ALLOWED = {"api.scryfall.com", "data.scryfall.io", "mtgjson.com"}
KEEP = ["name", "mana_cost", "cmc", "type_line", "oracle_text", "colors", "color_identity", "keywords", "produced_mana",
        "power", "toughness", "loyalty", "rarity", "layout", "edhrec_rank", "released_at"]
SKIP_LAYOUTS = {"token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar", "host", "augment"}
MAX_BULK = 900_000_000
REAL = {"cards": 20000, "banned": 20, "gc": 10, "rulings": 5000}
LENIENT = {"cards": 1, "banned": 1, "gc": 1, "rulings": 1}


# ------------------------------------------------------------------ red ----
def check_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED or u.username or u.password or u.port not in (None, 443):
        raise ValueError(f"URL no permitida: {url!r}")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    return urllib.request.build_opener(SafeRedirect).open(urllib.request.Request(check_url(url), headers=UA), timeout=120)


def get_json(url, retries=4):
    for i in range(retries):
        try:
            with _open(url) as r:
                return json.loads(r.read(60_000_000))
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)


def download(url, dest, max_bytes=MAX_BULK):
    n = 0
    with _open(url) as r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
            if n > max_bytes:
                raise ValueError("descarga demasiado grande")
            f.write(b)
    return dest


_WS = re.compile(r"[ \n\r\t,]*")


def iter_array(path):
    """Recorre un array JSON enorme objeto a objeto sin cargarlo entero (solo stdlib)."""
    dec = json.JSONDecoder()
    with open(path, "r", encoding="utf-8") as f:
        buf, pos, started, eof = "", 0, False, False
        while True:
            if not eof:
                chunk = f.read(1 << 20)
                if chunk:
                    buf = buf[pos:] + chunk
                    pos = 0
                else:
                    eof = True
            if not started:
                s = buf[pos:].lstrip()
                if not s:
                    if eof:
                        return
                    continue
                if s[0] != "[":
                    raise ValueError("se esperaba un array JSON")
                buf, pos, started = s[1:], 0, True
            while True:
                pos = _WS.match(buf, pos).end()
                if pos >= len(buf):
                    break
                if buf[pos] == "]":
                    return
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except ValueError:
                    if eof:
                        raise
                    break
                yield obj
                pos = end
            if eof:
                return


def source_file(kind, bulk_dir, tmp):
    """(ruta local, updated_at) del volcado `kind` (oracle_cards | default_cards | rulings)."""
    if bulk_dir:
        p = os.path.join(bulk_dir, kind + ".json")
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        return p, None
    idx = get_json("https://api.scryfall.com/bulk-data")
    ent = next((d for d in idx.get("data", []) if d.get("type") == kind), None)
    if not ent:
        raise ValueError(f"Scryfall no lista el volcado {kind}")
    return download(ent["download_uri"], os.path.join(tmp, kind + ".json")), ent.get("updated_at")


# ----------------------------------------------------------------- piezas ----
def slim(card, today):
    out = {k: card[k] for k in KEEP if k in card}
    fs = card.get("card_faces")
    out["oracle_id"] = card.get("oracle_id") or (fs[0].get("oracle_id") if fs else None) or "name:" + card["name"].lower()
    if fs:
        out["card_faces"] = [{"name": f.get("name"), "mana_cost": f.get("mana_cost"), "type_line": f.get("type_line"),
                              "oracle_text": f.get("oracle_text")} for f in fs]
        out.setdefault("oracle_text", "")
    out["legalities"] = card.get("legalities") or {}
    out["game_changer"] = bool(card.get("game_changer"))
    pr = card.get("prices") or {}
    out["prices"] = {k: pr[k] for k in ("eur", "eur_foil", "usd") if pr.get(k)}
    out["retrieved_at"] = today
    return out


def build_cache(bulk_dir, tmp, today, minimum):
    op, upd = source_file("oracle_cards", bulk_dir, tmp)
    cards = {}
    for c in iter_array(op):
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("layout") in SKIP_LAYOUTS:
            continue
        s = slim(c, today)
        cards[s["oracle_id"]] = s
    if len(cards) < minimum:
        raise ValueError(f"oracle_cards trae solo {len(cards)} cartas (mínimo {minimum}): no escribo la caché")
    printings = {}
    try:
        dp, _ = source_file("default_cards", bulk_dir, tmp)
    except FileNotFoundError:
        dp = None
    if dp:
        for c in iter_array(dp):
            if not isinstance(c, dict) or not c.get("set") or c.get("collector_number") is None:
                continue
            fs = c.get("card_faces")
            oid = c.get("oracle_id") or (fs[0].get("oracle_id") if fs else None)
            if oid in cards:
                printings[f"{c['set']}/{str(c['collector_number']).lower()}"] = oid
    meta = {"schema_version": 2, "generated_at": today, "source": "Scryfall bulk (GitHub Action)",
            "scryfall_bulk_updated_at": upd, "n_cards": len(cards), "n_printings": len(printings)}
    return {"_meta": meta, "cards": cards, "printings": printings}


def build_lists(cache, today, minimum):
    banned, gc = set(), set()
    for c in cache["cards"].values():
        if (c.get("legalities") or {}).get("commander") == "banned":
            banned.add(c["name"])
            if " // " in c["name"]:
                banned.add(c["name"].split(" // ")[0].strip())
        if c.get("game_changer"):
            gc.add(c["name"])
            if " // " in c["name"]:
                gc.add(c["name"].split(" // ")[0].strip())
    if len(banned) < minimum["banned"] or len(gc) < minimum["gc"]:
        raise ValueError(f"listas absurdas: {len(banned)} baneadas, {len(gc)} Game Changers: no las escribo")
    key = lambda s: sorted(s, key=str.lower)
    return ({"format": "Commander", "source": "Scryfall legalities.commander (Action)", "last_verified": today, "banned_cards": key(banned)},
            {"format": "Commander", "source": "Scryfall game_changer (Action)", "last_verified": today, "game_changers": key(gc)})


def build_rulings(cache, bulk_dir, tmp, today, minimum):
    rp, _ = source_file("rulings", bulk_dir, tmp)
    by = {}
    n = 0
    for r in iter_array(rp):
        if not isinstance(r, dict) or r.get("oracle_id") not in cache["cards"] or not isinstance(r.get("comment"), str):
            continue
        by.setdefault(r["oracle_id"], []).append([str(r.get("published_at", ""))[:10], r["comment"][:1500]])
        n += 1
    if n < minimum:
        raise ValueError(f"rulings: solo {n} (mínimo {minimum}): no escribo")
    for v in by.values():
        v.sort()
    return {"_meta": {"generated_at": today, "source": "Scryfall rulings (Action)", "n_rulings": n, "n_cards": len(by)}, "by_oracle": by}


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def deck_to_stock(deck, code, today):
    """MTGJSON deck → texto stock.txt de la skill y nombres de comandantes."""
    d = deck.get("data", deck)
    lines = [f"# Stock del precon {d.get('name', '?')} ({code}, {str(d.get('releaseDate', ''))[:10]}). Fuente: MTGJSON, bajado el {today}.",
             "# Generado automáticamente: contrástalo con tu caja antes de fiarte de una carta concreta.", "", "Commander"]
    cmd = d.get("commander") or []
    for c in cmd:
        lines.append(f"{int(c.get('count', 1))} {c['name']}")
    lines += ["", "Deck"]
    for c in d.get("mainBoard") or []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            lines.append(f"{int(c.get('count', 1))} {c['name']}")
    return "\n".join(lines) + "\n", [c["name"] for c in cmd]


def build_precons(out, bulk_dir, today, max_new, min_lines=60):
    """Añade (idempotente) los precons de MTGJSON que aún no están. Devuelve cuántos nuevos."""
    idx_path = os.path.join(out, "precons", "index.json")
    index = json.load(open(idx_path, encoding="utf-8")) if os.path.exists(idx_path) else {}
    if bulk_dir:
        dl = json.load(open(os.path.join(bulk_dir, "decklist.json"), encoding="utf-8"))
        getdeck = lambda fn: json.load(open(os.path.join(bulk_dir, "deck-" + fn + ".json"), encoding="utf-8"))
    else:
        dl = get_json("https://mtgjson.com/api/v5/DeckList.json")
        getdeck = lambda fn: get_json("https://mtgjson.com/api/v5/decks/" + urllib.parse.quote(fn) + ".json")
    decks = [d for d in dl.get("data", []) if isinstance(d, dict) and d.get("type") == "Commander Deck" and isinstance(d.get("fileName"), str)]
    decks.sort(key=lambda d: str(d.get("releaseDate", "")), reverse=True)
    os.makedirs(os.path.join(out, "precons"), exist_ok=True)
    new = 0
    for d in decks:
        slug = slugify(str(d.get("name", "")) + "-" + str(d.get("code", "")))
        if not slug or slug in index:
            continue
        if new >= max_new:
            break
        try:
            text, cmds = deck_to_stock(getdeck(d["fileName"]), d.get("code", "?"), today)
        except Exception as ex:
            print(f"  ⚠ precon {d.get('name')}: {ex}", file=sys.stderr)
            continue
        if len(text.splitlines()) < min_lines or not cmds:      # un precon Commander tiene ~100 cartas y comandante
            print(f"  ⚠ precon {d.get('name')}: lista incompleta, no se guarda", file=sys.stderr)
            continue
        with open(os.path.join(out, "precons", slug + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)
        index[slug] = {"name": d.get("name"), "code": d.get("code"), "released": str(d.get("releaseDate", ""))[:10], "commanders": cmds}
        new += 1
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(index.items())), f, ensure_ascii=False, indent=1)
        f.write("\n")
    return new


# --------------------------------------------------------------- escritura ----
def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def write_json(path, obj, indent=1):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.write("\n")


def write_gz(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(path, "wb") as raw_f, gzip.GzipFile(fileobj=raw_f, mode="wb", compresslevel=9, mtime=0) as g:
        g.write(raw)
    return len(raw)


def build_manifest(out, dist, today, counts):
    files = {}
    for sub in ("data", "precons"):      # solo lo que escribe esta Action; stats/, combos/ y decks/ los escriben n8n y tú
        for dp, _, fns in os.walk(os.path.join(out, sub)):
            for fn in fns:
                p = os.path.join(dp, fn)
                if fn.startswith(".") or fn == "README.md":
                    continue
                b = open(p, "rb").read()
                files[os.path.relpath(p, out).replace(os.sep, "/")] = {"sha": git_blob_sha(b), "bytes": len(b)}
    assets = {}
    if os.path.isdir(dist):
        for fn in sorted(os.listdir(dist)):
            b = open(os.path.join(dist, fn), "rb").read()
            assets[fn] = {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
    old = {}
    mp = os.path.join(out, "manifest.json")
    if os.path.exists(mp):
        try:
            old = json.load(open(mp, encoding="utf-8")).get("synced") or {}
        except ValueError:
            pass
    synced = dict(old)
    for k in counts:
        synced[k] = today
    return {"schema": 2, "generated_at": today, "synced": synced, "counts": counts,
            "files": dict(sorted(files.items())), "assets": assets}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=".")
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--only", default="cache,lists,rulings,precons")
    ap.add_argument("--lenient", action="store_true", help="solo tests: sin mínimos de tamaño")
    ap.add_argument("--bulk-dir")
    ap.add_argument("--today", default=datetime.date.today().isoformat())
    ap.add_argument("--max-new-precons", type=int, default=40)
    a = ap.parse_args()
    only = set(a.only.split(","))
    mn = LENIENT if a.lenient else REAL
    counts, errors = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        cache = None
        if only & {"cache", "lists", "rulings"}:
            try:
                cache = build_cache(a.bulk_dir, tmp, a.today, mn["cards"])
            except Exception as ex:
                errors.append(f"cache: {ex}")
        if cache and "cache" in only:
            raw = write_gz(os.path.join(a.dist, "card-cache.json.gz"), cache)
            counts["cache"] = cache["_meta"]["n_cards"]
            print(f"✓ caché: {len(cache['cards'])} cartas, {len(cache['printings'])} impresiones, {raw // 1_000_000} MB sin comprimir")
        if cache and "lists" in only:
            try:
                bl, gc = build_lists(cache, a.today, mn)
                write_json(os.path.join(a.out, "data", "commander-banlist.json"), bl)
                write_json(os.path.join(a.out, "data", "game-changers.json"), gc)
                counts["lists"] = len(bl["banned_cards"])
                print(f"✓ listas: {len(bl['banned_cards'])} baneadas, {len(gc['game_changers'])} Game Changers")
            except Exception as ex:
                errors.append(f"listas: {ex}")
        if cache and "rulings" in only:
            try:
                ru = build_rulings(cache, a.bulk_dir, tmp, a.today, mn["rulings"])
                write_gz(os.path.join(a.dist, "rulings.json.gz"), ru)
                counts["rulings"] = ru["_meta"]["n_rulings"]
                print(f"✓ rulings: {ru['_meta']['n_rulings']} de {ru['_meta']['n_cards']} cartas")
            except Exception as ex:
                errors.append(f"rulings: {ex}")
        if "precons" in only:
            try:
                n = build_precons(a.out, a.bulk_dir, a.today, a.max_new_precons, 3 if a.lenient else 60)
                counts["precons"] = n
                print(f"✓ precons nuevos: {n}")
            except Exception as ex:      # los precons son opcionales: un fallo aquí no debe frenar la caché ni la release
                print(f"::warning::precons: {ex}", file=sys.stderr)
                print(f"⚠ precons: {ex}")
    if counts:
        write_json(os.path.join(a.out, "manifest.json"), build_manifest(a.out, a.dist, a.today, counts))
    for e in errors:
        print("✗ " + e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

"""
Polymarket — Partidos MLB del día
- Gamma API: partidos y mercados del día
- CLOB API:  precios reales en paralelo (ThreadPoolExecutor)
"""

import requests
import json
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
MLB_SERIES_ID  = 10062
HEADERS        = {"User-Agent": "Mozilla/5.0"}
SESSION        = requests.Session()
SESSION.headers.update(HEADERS)

# ── helpers ────────────────────────────────────────────────────────────────────

def hora_et() -> str:
    return datetime.utcnow().strftime("%H:%M UTC")

def centavos(p: float) -> str:
    return f"{p*100:+.1f}¢" if p else "n/a"

# ── Gamma API ──────────────────────────────────────────────────────────────────

def obtener_partidos_hoy() -> list[dict]:
    """
    Devuelve lista de partidos MLB de HOY con sus mercados clasificados.
    Cada entry: { "titulo", "evento_id", "moneyline", "runline", "totals" }
    Cada mercado: { "market_id", "pregunta", "tokens": [{"token_id","equipo"}] }
    """
    hoy     = date.today().isoformat()          # "2025-04-01"
    manana  = (date.today() + timedelta(days=1)).isoformat()

    # 1) Traer el evento diario MLB de hoy
    params = {
        "series_id": MLB_SERIES_ID,
        "start_date_min": hoy,
        "start_date_max": manana,
        "limit": 10,
        "active": "true",
        "closed": "false",
    }
    resp = SESSION.get(f"{GAMMA_API}/events", params=params, timeout=15)
    resp.raise_for_status()
    eventos = resp.json()

    if not eventos:
        return []

    partidos = []

    for evento in eventos:
        evento_id = evento.get("id")
        titulo_ev = evento.get("title", "MLB")
        markets   = evento.get("markets", [])

        # Agrupar por nombre de partido (detectamos equipos en el título del mercado)
        juegos = {}   # key = frozenset({equipo_a, equipo_b})

        for m in markets:
            if m.get("closed") or not m.get("active", True):
                continue

            pregunta = m.get("question", "")
            cat      = clasificar_mercado(pregunta)
            if cat is None:
                continue

            tokens_raw = m.get("tokens") or m.get("outcomePrices") or []
            tokens = _parse_tokens(tokens_raw, pregunta)
            if not tokens:
                continue

            # Extraer nombres de equipos del mercado
            equipos = tuple(t["equipo"] for t in tokens)
            llave   = frozenset(equipos)

            if llave not in juegos:
                juegos[llave] = {
                    "titulo":     _titulo_partido(pregunta),
                    "evento_id":  evento_id,
                    "moneyline":  None,
                    "runline":    None,
                    "totals":     None,
                }

            mercado_entry = {
                "market_id": m.get("id"),
                "pregunta":  pregunta,
                "volumen":   float(m.get("volume", 0) or 0),
                "tokens":    tokens,
            }

            # Guardar el de mayor volumen por categoría
            existente = juegos[llave][cat]
            if existente is None or mercado_entry["volumen"] > existente["volumen"]:
                juegos[llave][cat] = mercado_entry

        for partido in juegos.values():
            if partido["moneyline"]:   # solo si hay moneyline
                partidos.append(partido)

    return partidos


def clasificar_mercado(pregunta: str) -> str | None:
    """Clasifica un mercado MLB en: 'moneyline', 'runline' o 'totals'."""
    p = pregunta.lower()

    # Run line (equivalente al spread en NBA)
    if any(x in p for x in ["run line", "runline", "-1.5", "+1.5", "cover"]):
        return "runline"

    # Totales (over/under)
    if any(x in p for x in ["over", "under", "total runs", "total"]):
        return "totals"

    # Moneyline: "will X win?" o "X to win" o solo el nombre del equipo
    if any(x in p for x in ["win", "moneyline", "money line"]):
        return "moneyline"

    # Si la pregunta es corta y tiene "vs" probablemente sea moneyline
    if " vs " in p or " @ " in p:
        return "moneyline"

    return None


def _parse_tokens(tokens_raw: list, pregunta: str) -> list[dict]:
    """Convierte la lista cruda de tokens en [{token_id, equipo}]."""
    result = []
    for t in tokens_raw:
        if isinstance(t, dict):
            tid    = t.get("token_id") or t.get("id") or t.get("tokenId")
            equipo = t.get("outcome") or t.get("name") or t.get("title", "")
            if tid:
                result.append({"token_id": str(tid), "equipo": equipo})
    return result


def _titulo_partido(pregunta: str) -> str:
    """Extrae algo legible como título del partido desde la pregunta."""
    p = pregunta
    for rem in ["Will ", " win?", " to win", " Win?", "Will the ", " cover?",
                "Will there be over", "Will there be under"]:
        p = p.replace(rem, "")
    return p.strip()[:60]


# ── CLOB API — precios en paralelo ─────────────────────────────────────────────

def precio_clob(token_id: str) -> float | None:
    """Devuelve mid-price (0.0–1.0) de un token en Polymarket CLOB."""
    try:
        url  = f"{CLOB_API}/midpoints"
        resp = SESSION.get(url, params={"token_id": token_id}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        mid  = data.get("mid") or data.get("midpoint") or data.get(token_id)
        if mid is not None:
            return float(mid)
        # fallback: book endpoint
        resp2 = SESSION.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=8)
        resp2.raise_for_status()
        book = resp2.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    except Exception:
        pass
    return None


def obtener_precios_paralelo(token_ids: list[str], workers: int = 25) -> dict[str, float]:
    """Trae los precios de múltiples tokens en paralelo."""
    precios = {}
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futuros = {exe.submit(precio_clob, tid): tid for tid in token_ids}
        for fut in as_completed(futuros):
            tid = futuros[fut]
            try:
                p = fut.result()
                if p is not None:
                    precios[tid] = p
            except Exception:
                pass
    return precios


# ── Ejecución directa ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{hora_et()}]  Buscando partidos MLB de hoy en Polymarket...\n")

    partidos = obtener_partidos_hoy()

    if not partidos:
        print("Sin partidos MLB encontrados para hoy.")
    else:
        # Recolectar todos los token_ids
        all_tokens = []
        for p in partidos:
            for cat in ("moneyline", "runline", "totals"):
                m = p.get(cat)
                if m:
                    all_tokens += [t["token_id"] for t in m["tokens"]]

        print(f"Trayendo precios para {len(all_tokens)} tokens...\n")
        precios = obtener_precios_paralelo(list(set(all_tokens)))

        for p in partidos:
            print(f"  ⚾  {p['titulo']}")
            for cat in ("moneyline", "runline", "totals"):
                m = p.get(cat)
                if not m:
                    continue
                print(f"     [{cat.upper()}] {m['pregunta']}")
                for t in m["tokens"]:
                    precio = precios.get(t["token_id"])
                    print(f"       {t['equipo']:30s}  {centavos(precio) if precio else 'sin precio'}")
            print()

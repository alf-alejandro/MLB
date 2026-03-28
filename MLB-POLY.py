"""
Polymarket — Partidos MLB del día
- Gamma API: eventos MLB via tag_id=100381 (funciona para 2026+)
- CLOB API:  precios reales en paralelo (ThreadPoolExecutor)

Estructura real de los mercados MLB 2026 en Polymarket:
  Evento: { "id", "title", "markets": [...] }
  Mercado por partido: {
      "question":       "Will the Rays beat the Cardinals?",
      "outcomes":       "[\"Tampa Bay Rays\", \"St. Louis Cardinals\"]",  ← JSON string
      "outcomePrices":  "[\"0.215\", \"0.785\"]",                         ← JSON string
      "clobTokenIds":   "[\"...\", \"...\"]",                              ← JSON string
      "sportsMarketType": "moneyline",
      "gameStartTime":  "2026-03-28 18:15:00+00",
      "active": true, "closed": false
  }
"""

import requests
import json
from datetime import datetime, date, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
MLB_TAG_ID  = 100381    # tag "MLB" en Polymarket (válido 2025 y 2026+)
HEADERS     = {"User-Agent": "Mozilla/5.0"}
SESSION     = requests.Session()
SESSION.headers.update(HEADERS)

# ── helpers ────────────────────────────────────────────────────────────────────

def hora_et() -> str:
    return datetime.utcnow().strftime("%H:%M UTC")

def centavos(p: float) -> str:
    return f"{p*100:+.1f}¢" if p is not None else "n/a"

def _parse_json_str(s) -> list:
    """Convierte JSON string o lista nativa en lista Python."""
    if isinstance(s, list):
        return s
    if isinstance(s, str):
        try:
            return json.loads(s)
        except Exception:
            pass
    return []

def _es_hoy(gameStartTime: str) -> bool:
    """True si gameStartTime corresponde al día de hoy (UTC)."""
    if not gameStartTime:
        return False
    try:
        # Formato: "2026-03-28 18:15:00+00" o ISO 8601
        ts = gameStartTime.replace(" ", "T")
        if not ts.endswith("Z") and "+" not in ts[-6:]:
            ts += "Z"
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.date() == date.today()
    except Exception:
        # Fallback: buscar la fecha como string
        hoy = date.today().isoformat()  # "2026-03-28"
        return hoy in gameStartTime

# ── Gamma API ──────────────────────────────────────────────────────────────────

def obtener_partidos_hoy() -> list[dict]:
    """
    Devuelve lista de partidos MLB moneyline de HOY.
    Cada entry: {
        "titulo":    str,
        "evento_id": str,
        "moneyline": {
            "market_id": str,
            "pregunta":  str,
            "volumen":   float,
            "tokens":    [{"token_id": str, "equipo": str}, ...]
        }
    }
    """
    eventos = _fetch_eventos_mlb()
    partidos = []

    for evento in eventos:
        mercados = evento.get("markets") or []
        for m in mercados:
            if not isinstance(m, dict):
                continue
            # Solo moneyline activos de hoy
            if m.get("sportsMarketType") != "moneyline":
                continue
            if m.get("closed") or not m.get("active", True):
                continue
            if not _es_hoy(m.get("gameStartTime", "")):
                continue

            token_ids = _parse_json_str(m.get("clobTokenIds", "[]"))
            outcomes  = _parse_json_str(m.get("outcomes",    "[]"))

            if len(token_ids) < 2 or len(outcomes) < 2:
                continue

            tokens = [
                {"token_id": str(token_ids[i]), "equipo": str(outcomes[i])}
                for i in range(min(len(token_ids), len(outcomes)))
            ]

            partidos.append({
                "titulo":     m.get("question", f"{outcomes[0]} vs {outcomes[1]}"),
                "evento_id":  evento.get("id"),
                "moneyline": {
                    "market_id": str(m.get("id", "")),
                    "pregunta":  m.get("question", ""),
                    "volumen":   float(m.get("volumeNum") or m.get("volume") or 0),
                    "tokens":    tokens,
                    "game_time": m.get("gameStartTime", ""),
                },
            })

    return partidos


def _fetch_eventos_mlb() -> list[dict]:
    """Trae todos los eventos MLB activos desde Polymarket usando tag_id."""
    params = {
        "tag_id":    MLB_TAG_ID,
        "active":    "true",
        "closed":    "false",
        "limit":     50,
        "order":     "startDate",
        "ascending": "true",
    }
    try:
        resp = SESSION.get(f"{GAMMA_API}/events", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  ⚠ Error consultando Gamma API: {e}")
        return []


# ── CLOB API — precios en paralelo ─────────────────────────────────────────────

def precio_clob(token_id: str) -> float | None:
    """Devuelve mid-price (0.0–1.0) de un token en Polymarket CLOB."""
    try:
        resp = SESSION.get(f"{CLOB_API}/midpoints",
                           params={"token_id": token_id}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        mid  = data.get("mid") or data.get("midpoint") or data.get(token_id)
        if mid is not None:
            return float(mid)
        # fallback: book
        resp2 = SESSION.get(f"{CLOB_API}/book",
                            params={"token_id": token_id}, timeout=8)
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
    """Trae precios de múltiples tokens en paralelo."""
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


# ── Diagnóstico ────────────────────────────────────────────────────────────────

def diagnosticar_api():
    """Muestra info de diagnóstico sobre los mercados MLB disponibles."""
    print(f"\n[DIAGNÓSTICO] Consultando Polymarket (tag_id={MLB_TAG_ID})...\n")

    eventos = _fetch_eventos_mlb()
    juegos_total = 0
    juegos_hoy   = 0

    for evento in eventos:
        for m in (evento.get("markets") or []):
            if isinstance(m, dict) and m.get("sportsMarketType") == "moneyline":
                juegos_total += 1
                if _es_hoy(m.get("gameStartTime", "")):
                    juegos_hoy += 1
                    print(f"  HOY  ⚾ {m.get('question','?')[:55]}  "
                          f"({m.get('gameStartTime','?')[:16]})")

    print(f"\n  Total partidos MLB abiertos: {juegos_total}")
    print(f"  Partidos de HOY ({date.today()}): {juegos_hoy}")

    if juegos_hoy == 0 and juegos_total > 0:
        print("\n  Próximos partidos disponibles:")
        mostrados = 0
        for evento in eventos:
            for m in (evento.get("markets") or []):
                if isinstance(m, dict) and m.get("sportsMarketType") == "moneyline" and not m.get("closed"):
                    print(f"    ⚾ {m.get('question','?')[:55]}  ({m.get('gameStartTime','?')[:16]})")
                    mostrados += 1
                    if mostrados >= 5:
                        break
            if mostrados >= 5:
                break

    if juegos_total == 0:
        print("  Sin partidos MLB activos. Opening Day posiblemente no está listado aún.")


# ── Ejecución directa ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{hora_et()}]  Buscando partidos MLB de hoy en Polymarket...\n")

    partidos = obtener_partidos_hoy()

    if not partidos:
        diagnosticar_api()
    else:
        all_tokens = [t["token_id"] for p in partidos for t in p["moneyline"]["tokens"]]
        print(f"Trayendo precios para {len(all_tokens)} tokens...\n")
        precios = obtener_precios_paralelo(list(set(all_tokens)))

        for p in partidos:
            m = p["moneyline"]
            gt = m.get("game_time", "")[:16]
            print(f"  ⚾  {p['titulo']}  [{gt}]")
            for t in m["tokens"]:
                precio = precios.get(t["token_id"])
                print(f"     {t['equipo']:30s}  {centavos(precio) if precio is not None else 'sin precio'}")
            print()

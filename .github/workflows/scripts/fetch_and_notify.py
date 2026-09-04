#!/usr/bin/env python3
"""
Trading News Bot
-----------------
1. Lee varios feeds RSS de noticias financieras.
2. Le pide a Google Gemini (nivel gratuito) que puntúe cada noticia por relevancia (1-10) y la resuma.
3. Consulta los movimientos recientes de trading del Congreso de EE.UU. (Quiver Quantitative, endpoint abierto).
4. Envía una notificación push por ntfy.sh solo con lo que de verdad importa.
5. Guarda un data.json en /docs para que el dashboard (PWA) lo muestre.

Todas las claves se leen de variables de entorno (configúralas como
"Secrets" en GitHub Actions, nunca las escribas aquí).
"""

import os
import json
import time
import datetime
import urllib.request
import urllib.error

import feedparser

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets
    "https://www.investing.com/rss/news_301.rss",              # Investing.com - Economy
    "https://news.google.com/rss/search?q=mercados+financieros+OR+bolsa+OR+inflaci%C3%B3n&hl=es&gl=ES&ceid=ES:es",
]

# Tickers / temas que te interesan para el tracker de trading congresional.
# Déjalo vacío ["*"] para ver todo, o pon tus tickers, ej: ["NVDA", "AAPL", "TSLA"]
WATCHLIST = ["*"]

# Umbral mínimo de relevancia (1-10) para que una noticia te llegue por notificación
RELEVANCE_THRESHOLD = 7

# Cuántas noticias como máximo evaluar por ejecución
MAX_ITEMS_PER_RUN = 15

QUIVER_CONGRESS_URL = "https://api.quiverquant.com/beta/live/congresstrading"

DATA_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")

# ---------------------------------------------------------------------------
# Credenciales (desde variables de entorno / GitHub Secrets)
# ---------------------------------------------------------------------------

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
QUIVER_API_TOKEN = os.environ.get("QUIVER_API_TOKEN")  # opcional


# ---------------------------------------------------------------------------
# Paso 1: recoger noticias
# ---------------------------------------------------------------------------

def fetch_news():
    items = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                items.append({
                    "title": entry.get("title", "").strip(),
                    "summary": entry.get("summary", "")[:500],
                    "link": entry.get("link", ""),
                    "source": feed.feed.get("title", url),
                })
        except Exception as e:
            print(f"[aviso] no se pudo leer el feed {url}: {e}")
    return items[:MAX_ITEMS_PER_RUN]


# ---------------------------------------------------------------------------
# Paso 2: filtrar con Google Gemini (gratis)
# ---------------------------------------------------------------------------

def score_news_with_gemini(items):
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY en las variables de entorno.")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    scored = []

    for item in items:
        prompt = f"""Eres un analista financiero conciso. Evalúa esta noticia para un inversor
retail con poco capital que quiere saber solo lo que de verdad importa.

Título: {item['title']}
Resumen: {item['summary']}

Responde ÚNICAMENTE con un JSON válido, sin texto adicional, con este formato exacto:
{{"relevancia": <entero 1-10>, "resumen": "<máximo 2 frases en español, directo al grano>"}}

Puntúa alto (8-10) solo si afecta a tipos de interés, inflación, decisiones de bancos
centrales, geopolítica con impacto de mercado real, o resultados de empresas muy grandes.
Puntúa bajo (1-4) si es ruido, opinión, o no tiene impacto claro en mercados."""

        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 200},
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
            item["relevancia"] = int(parsed.get("relevancia", 0))
            item["resumen_claude"] = parsed.get("resumen", "")
            scored.append(item)
        except Exception as e:
            print(f"[aviso] fallo evaluando '{item['title']}': {e}")
        time.sleep(1.5)  # el nivel gratuito de Gemini tiene límite de peticiones por minuto

    return scored


# ---------------------------------------------------------------------------
# Paso 3: trading congresional (Quiver Quantitative)
# ---------------------------------------------------------------------------

def fetch_congress_trades():
    req = urllib.request.Request(QUIVER_CONGRESS_URL)
    if QUIVER_API_TOKEN:
        req.add_header("Authorization", f"Token {QUIVER_API_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"[aviso] no se pudo consultar Quiver: {e}")
        return []
    except Exception as e:
        print(f"[aviso] error inesperado con Quiver: {e}")
        return []

    if "*" in WATCHLIST:
        return data[:10]
    return [t for t in data if t.get("Ticker") in WATCHLIST][:10]


# ---------------------------------------------------------------------------
# Paso 4: notificar por ntfy.sh
# ---------------------------------------------------------------------------

def send_ntfy_message(title, text):
    if not NTFY_TOPIC:
        print("[aviso] falta NTFY_TOPIC, no se envía notificación.")
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url,
        data=text.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": "default",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[error] no se pudo enviar la notificación de ntfy: {e}")


def build_notification_text(top_news, trades):
    lines = []
    if top_news:
        for n in top_news:
            lines.append(f"[{n['relevancia']}/10] {n['resumen_claude']}")
            lines.append(n['link'])
            lines.append("")
    else:
        lines.append("Sin noticias que superen el umbral hoy.")

    if trades:
        lines.append("--- Movimientos del Congreso (EE.UU.) ---")
        for t in trades[:5]:
            rep = t.get("Representative", "?")
            ticker = t.get("Ticker", "?")
            tx = t.get("Transaction", "?")
            lines.append(f"{rep}: {tx} {ticker}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Paso 5: guardar data.json para el dashboard
# ---------------------------------------------------------------------------

def save_dashboard_data(all_news, top_news, trades):
    os.makedirs(os.path.dirname(DATA_JSON_PATH), exist_ok=True)
    payload = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "top_news": top_news,
        "all_news_evaluated": all_news,
        "congress_trades": trades,
    }
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Recogiendo noticias...")
    raw_news = fetch_news()

    print(f"Evaluando {len(raw_news)} noticias con Gemini...")
    scored_news = score_news_with_gemini(raw_news)

    top_news = [n for n in scored_news if n.get("relevancia", 0) >= RELEVANCE_THRESHOLD]
    top_news.sort(key=lambda n: n["relevancia"], reverse=True)

    print("Consultando trading congresional...")
    trades = fetch_congress_trades()

    if top_news or trades:
        message = build_notification_text(top_news, trades)
        send_ntfy_message("Radar de mercado - resumen", message)
        print("Notificación enviada.")
    else:
        print("Nada relevante esta vez, no se envía notificación.")

    save_dashboard_data(scored_news, top_news, trades)
    print("data.json actualizado.")


if __name__ == "__main__":
    main()

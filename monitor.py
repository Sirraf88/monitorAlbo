#!/usr/bin/env python3
"""
Monitor giornaliero degli albi pretori delle aziende del SSR Sardegna.

Per ogni azienda in config.json:
  1. legge la pagina indice dell'albo e scopre in automatico sezioni e sotto-sezioni
     (Delibere, Determine, Bandi e Gare, Concorsi, Avvisi, ecc.);
  2. scorre le pagine di ogni sezione ed estrae gli atti in pubblicazione;
  3. confronta con gli atti già visti (seen.json) e isola le novità;
  4. aggiorna l'archivio CSV e rigenera il sito (sito/index.html).
"""
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE_DIR / "seen.json"
ARCHIVE_FILE = BASE_DIR / "archivio_atti.csv"
SITO_TEMPLATE = BASE_DIR / "sito_template.html"
SITO_DIR = BASE_DIR / "sito"
ROMA = ZoneInfo("Europe/Rome")


def adesso():
    return datetime.now(ROMA)

PAUSA = CONFIG.get("pausa_secondi", 1.5)
MAX_PAGINE = CONFIG.get("max_pagine_per_sezione", 30)
MAX_PROFONDITA = CONFIG.get("max_profondita_sezioni", 3)
GIORNI_CONSERVAZIONE = CONFIG.get("giorni_conservazione_stato", 120)
# Parola intera per default; con "*" finale vale come prefisso (es. "assunzion*").
PAROLE_CHIAVE = [(p.rstrip("*"), re.compile(r"\b" + re.escape(p.rstrip("*").lower()) + ("" if p.endswith("*") else r"\b")))
                 for p in CONFIG.get("parole_chiave", [])]

RE_DATE = re.compile(r"dal:?\s*(\d{2}[./-]\d{2}[./-]\d{4}).*?al:?\s*(\d{2}[./-]\d{2}[./-]\d{4})", re.I | re.S)
RE_ALLEGATO = re.compile(r"\.(pdf|xlsx?|docx?|zip|p7m)$", re.I)

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
})

# Ogni azienda viene letta in un thread separato (server diversi, quindi in parallelo);
# il browser di riserva e il limite di tempo sono gestiti per thread.
_locale = threading.local()
SCADENZA = [None]   # istante oltre il quale le scansioni si interrompono (fissato in main)


class TempoScaduto(Exception):
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def scarica_con_browser(url, attendi=None):
    """Fallback per i portali che rifiutano le richieste non-browser (es. errore 428):
    apre la pagina in Chromium headless (Playwright) e ne restituisce l'HTML.
    Con `attendi` (selettore CSS) aspetta che il contenuto reale compaia, superando
    le eventuali pagine di verifica anti-bot che si risolvono da sole via JavaScript."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("accesso bloccato dal portale (428/403) e Playwright non installato")
    if getattr(_locale, "pagina", None) is None:
        _locale.pw = sync_playwright().start()
        _locale.browser = _locale.pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        contesto = _locale.browser.new_context(
            locale="it-IT", timezone_id="Europe/Rome", viewport={"width": 1366, "height": 900},
            user_agent=session.headers["User-Agent"])
        _locale.pagina = contesto.new_page()
    _locale.pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
    if attendi:
        try:
            _locale.pagina.wait_for_selector(attendi, timeout=45000)
        except Exception:
            pass   # si restituisce comunque la pagina: la diagnostica la registra nel log
    time.sleep(PAUSA)
    return BeautifulSoup(_locale.pagina.content(), "html.parser")


def chiudi_browser():
    if getattr(_locale, "pagina", None) is not None:
        _locale.browser.close()
        _locale.pw.stop()
        _locale.pagina = None


# ---------------------------------------------------------------- rete e parsing
def scarica(url, attendi=None):
    """GET con 3 tentativi; None se la pagina non esiste."""
    if SCADENZA[0] and time.time() > SCADENZA[0]:
        raise TempoScaduto()
    if getattr(_locale, "solo_browser", False):
        return scarica_con_browser(url, attendi)
    for tentativo in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code in (403, 428):
                _locale.solo_browser = True   # da qui in poi, per questo sito, solo browser
                return scarica_con_browser(url, attendi)
            r.raise_for_status()
            time.sleep(PAUSA)
            return BeautifulSoup(r.content, "html.parser")  # charset letto dalla pagina
        except requests.RequestException:
            if tentativo == 2:
                raise
            time.sleep(5 * (tentativo + 1))


def normalizza(url):
    return url.split("#")[0].split("?")[0].rstrip("/") + "/"


def area_principale(soup):
    return soup.find(id="main_container") or soup.find("main") or soup


def link_sezioni(soup, url_pagina, radice):
    """Sotto-sezioni dell'albo linkate nei titoli della pagina (h2-h6: ARNAS usa h6, gli altri h3)."""
    trovate, viste = [], set()
    for a in area_principale(soup).select("h2 a[href], h3 a[href], h4 a[href], h5 a[href], h6 a[href]"):
        href = normalizza(urljoin(url_pagina, a["href"]))
        if (href.startswith(radice) and href != normalizza(url_pagina)
                and "/page/" not in href and "archivio" not in href.lower()
                and href not in viste):
            viste.add(href)
            trovate.append((href, a.get_text(" ", strip=True)))
    return trovate


def estrai_atti(soup, url_pagina, radice):
    """Atti elencati nella pagina: titolo (h5), oggetto, date di pubblicazione, ambito."""
    area = area_principale(soup)
    dominio = urlparse(url_pagina).netloc
    atti = []
    for a in area.select("h5 a[href]"):
        href = urljoin(url_pagina, a["href"])
        # esclude link esterni e link a sezioni dell'albo (che non sono atti)
        if urlparse(href).netloc != dominio or normalizza(href).startswith(radice):
            continue
        titolo_tag = a.find_parent("h5")
        ul = titolo_tag.find_next_sibling("ul") if titolo_tag else None
        voci = [li.get_text(" ", strip=True) for li in ul.find_all("li", recursive=False)] if ul else []
        testo = " ".join(voci)
        oggetto = next((v for v in voci if not re.match(r"(in pubblicazione|ambito|visualizza)", v, re.I)), "")
        ambito = next((v.split(":", 1)[1].strip() for v in voci if v.lower().startswith("ambito") and ":" in v), "")
        m = RE_DATE.search(testo)
        atti.append({
            "url": href, "titolo": a.get_text(" ", strip=True), "oggetto": oggetto,
            "dal": m.group(1) if m else "", "al": m.group(2) if m else "", "ambito": ambito,
        })
    # Fallback: sezioni che elencano atti senza titoli h5
    if not atti:
        for a in area.select('a[href*="/ap/"]'):
            href = urljoin(url_pagina, a["href"])
            if urlparse(href).netloc == dominio and a.get_text(strip=True):
                atti.append({"url": href, "titolo": a.get_text(" ", strip=True), "oggetto": "",
                             "dal": "", "al": "", "ambito": ""})
    return atti


def estrai_allegati_sezione(soup, url_pagina):
    """Documenti caricati direttamente nel testo della sezione (es. tabelle riepilogative concorsi)."""
    out = []
    for a in area_principale(soup).select("a[href]"):
        href = urljoin(url_pagina, a["href"])
        if RE_ALLEGATO.search(urlparse(href).path) and a.get_text(strip=True):
            out.append({"url": href, "titolo": a.get_text(" ", strip=True), "oggetto": "Documento allegato alla sezione",
                        "dal": "", "al": "", "ambito": ""})
    return out


def pagina_successiva(soup, url_pagina):
    for a in soup.find_all("a", href=True):
        if "pagina successiva" in a.get_text(" ", strip=True).lower() or "next" in (a.get("rel") or []):
            return urljoin(url_pagina, a["href"])
    return None


# ---------------------------------------------------------------- scansione
def scansiona_azienda(azienda, visti, primo_avvio):
    radice = normalizza(azienda["albo"])
    coda = [(radice, "Albo Pretorio", 0)]
    visitate, atti = set(), {}
    while coda:
        url, nome_sezione, profondita = coda.pop(0)
        if url in visitate:
            continue
        visitate.add(url)
        log(f"{azienda['nome']}: sezione {nome_sezione}")
        pagina, n = url, 0
        while pagina and n < MAX_PAGINE:
            soup = scarica(pagina)
            if soup is None:
                break
            if n == 0:
                if profondita < MAX_PROFONDITA:
                    for sub_url, sub_nome in link_sezioni(soup, url, radice):
                        if sub_url not in visitate:
                            etichetta = sub_nome if profondita == 0 else f"{nome_sezione} › {sub_nome}"
                            coda.append((sub_url, etichetta, profondita + 1))
                if CONFIG.get("traccia_allegati_sezioni", True):
                    for x in estrai_allegati_sezione(soup, pagina):
                        atti.setdefault(x["url"], {**x, "sezione": nome_sezione})
            trovati = estrai_atti(soup, pagina, radice)
            for x in trovati:
                atti.setdefault(x["url"], {**x, "sezione": nome_sezione})
            n += 1
            # Gli elenchi sono in ordine di pubblicazione decrescente: se una pagina
            # contiene solo atti già noti, le successive sono già state viste.
            if not trovati or (not primo_avvio and all(x["url"] in visti for x in trovati)):
                break
            pagina = pagina_successiva(soup, pagina)
    if not atti:
        diagnostica(azienda["nome"], scarica(radice))
    return list(atti.values())


def estrai_atti_albotelematico(soup, url_pagina):
    """Schede 'card-icona' del portale albotelematico: un unico elenco con tutte le tipologie."""
    atti = []
    for card in soup.select("div.card-icona"):
        a = card.select_one("h3 a[href]")
        if not a:
            continue
        campi = {}
        for box in card.select("div.box-icona"):
            etichetta, valore = box.find("strong"), box.select_one("span.grigio")
            if etichetta and valore:
                campi[etichetta.get_text(strip=True).rstrip(":").lower()] = " ".join(valore.get_text(" ", strip=True).split())
        intestazione = card.select_one("div.icona span")
        intestazione = " ".join(intestazione.get_text(" ", strip=True).split()) if intestazione else ""
        tipo = campi.get("tipo", "Altro")
        titolo = intestazione.replace("Atto ", "", 1) if " n. " in intestazione else f"{tipo} del {intestazione}".strip()
        m = RE_DATE.search(campi.get("pubblicazione", ""))
        atti.append({
            "url": urljoin(url_pagina, a["href"]), "titolo": titolo,
            "oggetto": " ".join(a.get_text(" ", strip=True).split()),
            "dal": m.group(1) if m else "", "al": m.group(2) if m else "",
            "ambito": campi.get("servizio/ufficio", ""), "sezione": tipo,
        })
    return atti


def diagnostica(nome, soup):
    """Scrive nel log cosa ha restituito il portale quando non si trovano atti."""
    titolo = soup.title.get_text(strip=True) if soup and soup.title else "(nessun titolo)"
    testo = " ".join(soup.get_text(" ", strip=True).split())[:300] if soup else ""
    log(f"{nome}: DIAGNOSTICA pagina senza atti. Titolo: {titolo!r}. Testo: {testo!r}")


def scansiona_albotelematico(azienda, visti, primo_avvio):
    base = azienda["albo"].split("?")[0]
    atti, n = {}, 1
    if azienda.get("usa_browser"):
        _locale.solo_browser = True
    while n <= azienda.get("max_pagine", 60):
        url = f"{base}?p={n}"
        soup = scarica(url, attendi="div.card-icona")
        if soup is None:
            break
        trovati = estrai_atti_albotelematico(soup, url)
        if not trovati and n == 1:
            diagnostica(azienda["nome"], soup)
            if not getattr(_locale, "solo_browser", False):
                # risposta "vuota" senza errore: si riprova una volta con il browser
                _locale.solo_browser = True
                soup = scarica(url, attendi="div.card-icona")
                trovati = estrai_atti_albotelematico(soup, url) if soup else []
                if not trovati:
                    diagnostica(azienda["nome"] + " (browser)", soup)
        for x in trovati:
            atti.setdefault(x["url"], x)
        # elenco ordinato per inizio pubblicazione decrescente
        if not trovati or (not primo_avvio and all(x["url"] in visti for x in trovati)):
            break
        if not soup.select_one(f'a.page-link[href$="?p={n + 1}"]'):
            break
        n += 1
    return list(atti.values())


SCANSIONI = {"wordpress": scansiona_azienda, "albotelematico": scansiona_albotelematico}


def in_evidenza(atto):
    testo = f"{atto['titolo']} {atto['oggetto']}".lower()
    return [etichetta for etichetta, rx in PAROLE_CHIAVE if rx.search(testo)]


# ---------------------------------------------------------------- output
def aggiorna_archivio(novita):
    campi = ["rilevato_il", "azienda", "sezione", "titolo", "oggetto", "dal", "al", "ambito", "parole", "url"]
    nuovo = not ARCHIVE_FILE.exists()
    with ARCHIVE_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campi, delimiter=";", extrasaction="ignore")
        if nuovo:
            w.writeheader()
        for x in novita:
            w.writerow({**x, "parole": ", ".join(x["parole"])})


def genera_sito(correnti, errori, baseline_per_azienda, visti, url_nuovi, momento):
    """Pagina web statica: atti in pubblicazione + archivio delle novità degli ultimi 365 giorni."""
    oggi = momento.date().isoformat()
    atti, visti_url = [], set()
    for x in correnti:
        r = visti.get(x["url"], momento.strftime("%Y-%m-%dT%H:%M"))
        atti.append({"a": x["azienda"], "s": x["sezione"], "t": x["titolo"], "o": x["oggetto"],
                     "d": x["dal"], "f": x["al"], "m": x["ambito"], "u": x["url"], "r": r,
                     "k": in_evidenza(x), "p": True,
                     "b": r[:10] == baseline_per_azienda.get(x["azienda"]) and x["url"] not in url_nuovi})
        visti_url.add(x["url"])
    guaste = {e.split(": ", 1)[0] for e in errori}

    def data_iso(s):
        m = re.match(r"(\d{2})[./-](\d{2})[./-](\d{4})", s or "")
        return f"{m[3]}-{m[2]}-{m[1]}" if m else ""

    for a in atti:   # atti letti ora: "in pubblicazione" se la fine pubblicazione non è passata
        fine = data_iso(a["f"])
        a["p"] = not fine or fine >= oggi
        a["i"] = data_iso(a["d"])   # inizio pubblicazione, per l'ordinamento

    def ancora_in_pubblicazione(row):
        # per le aziende non lette in questa esecuzione si usa la data di fine pubblicazione
        if row["azienda"] not in guaste:
            return False
        m = re.match(r"(\d{2})[./-](\d{2})[./-](\d{4})", row["al"] or "")
        return bool(m) and f"{m[3]}-{m[2]}-{m[1]}" >= oggi

    if ARCHIVE_FILE.exists():
        limite = (momento - timedelta(days=365)).date().isoformat()
        with ARCHIVE_FILE.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f, delimiter=";"):
                if row["url"] in visti_url or row["rilevato_il"] < limite:
                    continue
                visti_url.add(row["url"])
                atti.append({"a": row["azienda"], "s": row["sezione"], "t": row["titolo"], "o": row["oggetto"],
                             "d": row["dal"], "f": row["al"], "m": row["ambito"], "u": row["url"],
                             "r": row["rilevato_il"], "k": [k for k in row["parole"].split(", ") if k],
                             "p": ancora_in_pubblicazione(row), "b": False, "i": data_iso(row["dal"])})
    dati = {"generato": momento.strftime("%d/%m/%Y alle %H:%M"), "ts": momento.strftime("%Y-%m-%dT%H:%M"),
            "oggi": oggi,
            "aziende": [a["nome"] for a in CONFIG["aziende"] if a.get("attivo", True)],
            "anomalie": [{"azienda": e.split(": ", 1)[0], "errore": e.split(": ", 1)[-1][:160]} for e in errori],
            "atti": atti}
    js = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = SITO_TEMPLATE.read_text(encoding="utf-8").replace("/*__DATI__*/null", js)
    SITO_DIR.mkdir(exist_ok=True)
    (SITO_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"Sito generato: {len(atti)} atti")


# ---------------------------------------------------------------- main
def spiega_errore(e):
    """Messaggio comprensibile per il riquadro anomalie del sito."""
    if isinstance(e, TempoScaduto):
        return "lettura non completata nel tempo disponibile: riprende al prossimo aggiornamento"
    if isinstance(e, requests.Timeout):
        return "il sito non ha risposto in tempo"
    if isinstance(e, requests.ConnectionError):
        return "sito non raggiungibile"
    if isinstance(e, requests.HTTPError):
        return f"il sito ha restituito un errore ({e.response.status_code})"
    if "bloccato" in str(e):
        return "accesso bloccato dal portale"
    return f"errore imprevisto ({type(e).__name__})"


def main():
    stato = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    visti = stato.setdefault("visti", {})
    inizializzate = set(stato.setdefault("aziende_inizializzate", []))
    data_baseline = stato.setdefault("baseline", {})
    momento = adesso()
    oggi, ts = momento.date().isoformat(), momento.strftime("%Y-%m-%dT%H:%M")
    novita, errori, correnti = [], [], []

    attive = [az for az in CONFIG["aziende"] if az.get("attivo", True)]
    SCADENZA[0] = time.time() + CONFIG.get("tempo_massimo_minuti", 80) * 60

    def leggi_azienda(az):
        _locale.solo_browser = False
        inizio = time.time()
        try:
            atti = SCANSIONI[az.get("piattaforma", "wordpress")](az, visti, az["nome"] not in inizializzate)
            log(f"{az['nome']}: {len(atti)} atti in {time.time() - inizio:.0f} s")
            return atti, None
        except Exception as e:  # un sito irraggiungibile non deve bloccare gli altri
            log(f"{az['nome']}: ERRORE {type(e).__name__}: {e}")
            return None, e
        finally:
            chiudi_browser()

    with ThreadPoolExecutor(max_workers=len(attive)) as pool:
        risultati = list(pool.map(leggi_azienda, attive))

    for az, (atti, errore) in zip(attive, risultati):
        nome = az["nome"]
        primo_avvio = nome not in inizializzate
        if errore is not None:
            errori.append(f"{nome}: {spiega_errore(errore)}")
            continue
        if not atti:
            errori.append(f"{nome}: nessun atto rilevato — verificare URL o struttura della pagina")
            continue
        correnti.extend({**x, "azienda": nome} for x in atti)
        for x in atti:
            if x["url"] not in visti:
                visti[x["url"]] = ts          # data e ora della prima rilevazione
                if not primo_avvio:
                    novita.append({**x, "azienda": nome, "rilevato_il": oggi, "parole": in_evidenza(x)})
        if primo_avvio:
            inizializzate.add(nome)
            data_baseline[nome] = oggi

    limite = (momento - timedelta(days=GIORNI_CONSERVAZIONE)).date().isoformat()
    stato["visti"] = {u: d for u, d in visti.items() if d >= limite}
    stato["aziende_inizializzate"] = sorted(inizializzate)
    STATE_FILE.write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")
    aggiorna_archivio(novita)
    genera_sito(correnti, errori, data_baseline, visti, {x["url"] for x in novita}, momento)
    print(f"Novità: {len(novita)} · Anomalie: {len(errori)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

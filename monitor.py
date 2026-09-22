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
REGISTRO_FILE = BASE_DIR / "registro_atti.json"   # tutti gli atti conosciuti, con i loro dati
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


def scarica_con_browser(url, attendi=None, attesa_ms=45000):
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
            _locale.pagina.wait_for_selector(attendi, timeout=attesa_ms)
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
        # l'elenco dei dati deve essere l'elemento immediatamente successivo al titolo,
        # altrimenti si rischia di attribuire all'atto i dati dell'atto seguente
        succ = titolo_tag.find_next_sibling() if titolo_tag else None
        ul = succ if succ is not None and succ.name == "ul" else None
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
        prima = len(atti)
        pagina, n = url, 0
        con_browser = False   # diventa vero se la sezione risponde solo a un browser vero
        while pagina and n < MAX_PAGINE:
            soup = scarica_con_browser(pagina, "h5 a[href]") if con_browser else scarica(pagina)
            if soup is None:
                break
            if (n == 0 and profondita > 0 and not estrai_atti(soup, pagina, radice)
                    and not link_sezioni(soup, url, radice)):   # le pagine-indice di sottosezioni non contano
                # Sezione apparentemente vuota: se la pagina dichiara dei risultati (o non dice nulla)
                # si registra cosa è arrivato e si riprova con il browser.
                m = RE_RISULTATI.search(" ".join(soup.get_text(" ", strip=True).split()))
                if m is None or int(m.group(1)) > 0:
                    diagnostica(f"{azienda['nome']} [{nome_sezione}]", soup)
                    try:
                        prova = scarica_con_browser(pagina, "h5 a[href]", 20000)
                        if estrai_atti(prova, pagina, radice):
                            soup, con_browser = prova, True
                            log(f"{azienda['nome']}: sezione {nome_sezione} letta con il browser")
                    except TempoScaduto:
                        raise
                    except Exception as e:
                        log(f"{azienda['nome']}: tentativo con browser non riuscito ({type(e).__name__})")
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
        log(f"{azienda['nome']}: sezione {nome_sezione}: {len(atti) - prima} atti in {n} pagine")
    if not atti:
        diagnostica(azienda["nome"], scarica(radice))
    return list(atti.values())


RE_RISULTATI = re.compile(r"Risultati trovati\s*(\d+)", re.I)
RE_PUB = re.compile(r"Data di pubblicazione\s*(\d{2}[./-]\d{2}[./-]\d{4})", re.I)
RE_SCAD = re.compile(r"Data e ora di scadenza\s*(\d{2}[./-]\d{2}[./-]\d{4})", re.I)
TITOLI = ["h2", "h3", "h4", "h5", "h6"]


VERSIONE_DETTAGLIO = 2   # se cambia, gli atti rimasti senza oggetto vengono ritentati
SEL_OGGETTO = ('[id*="oggetto"], h2:has-text("Oggetto"), h3:has-text("Oggetto"), '
               'h4:has-text("Oggetto"), h5:has-text("Oggetto")')
RE_OGGETTO_TESTO = re.compile(r"\bOggetto\s+(.{15,}?)\s+(?:Pubblicazione|Documenti|Ulteriori informazioni)\b", re.S)


def estrai_oggetto(soup):
    """Cerca l'oggetto dell'atto nella pagina di dettaglio con tre metodi, dal più preciso al più tollerante."""
    # 1. sezione con identificativo "…oggetto…" (es. <section id="articolo-oggetto">)
    for el in soup.find_all(id=re.compile("oggetto", re.I)):
        t = " ".join(el.get_text(" ", strip=True).split())
        t = re.sub(r"^Oggetto\s*", "", t, flags=re.I)
        if len(t) > 15:
            return t
    # 2. titolo "Oggetto" seguito dal testo
    intestazione = soup.find(lambda t: t.name in TITOLI and t.get_text(strip=True).lower() == "oggetto")
    if intestazione:
        parti = []
        for el in intestazione.find_next_siblings():
            if el.name in TITOLI:
                break
            parti.append(el.get_text(" ", strip=True))
        if not any(parti) and intestazione.parent:
            parti = [intestazione.parent.get_text(" ", strip=True)[len(intestazione.get_text(strip=True)):]]
        t = " ".join(" ".join(parti).split())
        if len(t) > 15:
            return t
    # 3. testo della pagina compreso tra "Oggetto" e la sezione successiva (si sceglie il tratto più lungo,
    #    per scartare l'indice della pagina "Stato Oggetto Pubblicazione …")
    testo = " ".join(area_principale(soup).get_text(" ", strip=True).split())
    trovati = RE_OGGETTO_TESTO.findall(testo)
    return max(trovati, key=len) if trovati else ""


def analizza_dettaglio(soup):
    testo = " ".join(area_principale(soup).get_text(" ", strip=True).split())
    pub, scad = RE_PUB.search(testo), RE_SCAD.search(testo)
    return {"oggetto": estrai_oggetto(soup), "dal": pub.group(1) if pub else "",
            "al": scad.group(1) if scad else "", "ver": VERSIONE_DETTAGLIO}


def leggi_dettaglio(url, nome_azienda=""):
    """Oggetto e date dalla pagina del singolo atto (usata quando l'elenco non le riporta).
    Se la pagina scaricata direttamente non contiene l'oggetto (es. contenuto generato via JavaScript),
    si usa il browser; dopo il primo successo così, per quell'azienda si passa subito al browser."""
    if not getattr(_locale, "dettaglio_browser", False):
        soup = scarica(url)
        if soup is None:
            return {"ver": VERSIONE_DETTAGLIO}
        risultato = analizza_dettaglio(soup)
        if risultato["oggetto"]:
            return risultato
    soup = scarica_con_browser(url, SEL_OGGETTO, 12000)
    risultato = analizza_dettaglio(soup)
    if risultato["oggetto"]:
        if not getattr(_locale, "dettaglio_browser", False):
            _locale.dettaglio_browser = True
            log(f"{nome_azienda}: le pagine di dettaglio si leggono solo con il browser")
    elif not getattr(_locale, "diagnostica_dettaglio", False):
        _locale.diagnostica_dettaglio = True
        diagnostica(f"{nome_azienda} [dettaglio {url}]", soup)
    return risultato


def data_atto(x):
    """Data per l'ordinamento: inizio pubblicazione o, in mancanza, la data nel titolo."""
    m = re.search(r"(\d{2})[./-](\d{2})[./-](\d{4})", x["dal"] or x["titolo"])
    return f"{m[3]}-{m[2]}-{m[1]}" if m else ""


def arricchisci(azienda, atti, cache):
    """Completa gli atti privi di oggetto leggendo la pagina di dettaglio (una sola volta per atto).
    Si parte dai più recenti; il limite per esecuzione evita di sovraccaricare i siti."""
    def da_rileggere(u):
        c = cache.get(u)
        return c is None or (not c.get("oggetto") and c.get("ver", 0) < VERSIONE_DETTAGLIO)

    da_fare = sorted((x for x in atti if not x["oggetto"] and "/ap/" in x["url"]), key=data_atto, reverse=True)
    fatti, vuoti, esempio = 0, 0, ""
    for x in da_fare:
        if da_rileggere(x["url"]):
            if fatti >= CONFIG.get("max_dettagli_per_esecuzione", 400):
                continue   # gli altri al prossimo aggiornamento
            try:
                cache[x["url"]] = leggi_dettaglio(x["url"], azienda["nome"])
            except TempoScaduto:
                raise
            except Exception as e:
                cache[x["url"]] = {"ver": VERSIONE_DETTAGLIO}
                log(f"{azienda['nome']}: dettaglio non leggibile {x['url']} ({type(e).__name__})")
            fatti += 1
            if not cache[x["url"]].get("oggetto"):
                vuoti += 1
                esempio = esempio or x["url"]
        d = cache.get(x["url"], {})
        x["oggetto"] = d.get("oggetto") or x["oggetto"]
        x["dal"] = x["dal"] or d.get("dal", "")
        x["al"] = x["al"] or d.get("al", "")
    restanti = sum(1 for x in da_fare if da_rileggere(x["url"]))
    if da_fare:
        log(f"{azienda['nome']}: atti senza oggetto nell'elenco {len(da_fare)}; completati ora {fatti - vuoti}; "
            f"senza oggetto anche nel dettaglio {vuoti}{' (es. ' + esempio + ')' if esempio else ''}; "
            f"rimandati al prossimo aggiornamento {restanti}")


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


def data_iso(s):
    m = re.match(r"(\d{2})[./-](\d{2})[./-](\d{4})", s or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else ""


def carica_registro():
    if REGISTRO_FILE.exists():
        return json.loads(REGISTRO_FILE.read_text(encoding="utf-8"))
    registro = {}
    # prima volta: si recupera lo storico delle novità già archiviate nel CSV
    if ARCHIVE_FILE.exists():
        with ARCHIVE_FILE.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f, delimiter=";"):
                registro[row["url"]] = {"a": row["azienda"], "s": row["sezione"], "t": row["titolo"],
                                        "o": row["oggetto"], "d": row["dal"], "f": row["al"],
                                        "m": row["ambito"], "r": row["rilevato_il"], "v": row["rilevato_il"]}
    return registro


def salva_registro(registro, momento):
    """Una riga per atto: i salvataggi successivi producono differenze piccole nello storico."""
    limite = (momento - timedelta(days=365)).date().isoformat()
    tenuti = {u: x for u, x in registro.items() if x["r"][:10] >= limite}
    righe = [json.dumps(u, ensure_ascii=False) + ":" + json.dumps(x, ensure_ascii=False, separators=(",", ":"))
             for u, x in sorted(tenuti.items())]
    REGISTRO_FILE.write_text("{\n" + ",\n".join(righe) + "\n}\n", encoding="utf-8")


def genera_sito(registro, errori, baseline_per_azienda, url_nuovi, momento):
    """Pagina web statica costruita dal registro di tutti gli atti conosciuti (ultimi 365 giorni)."""
    oggi, ts = momento.date().isoformat(), momento.strftime("%Y-%m-%dT%H:%M")
    recenti = (momento - timedelta(days=30)).date().isoformat()
    aziende = {a["nome"] for a in CONFIG["aziende"] if a.get("attivo", True) and not a.get("manuale")}
    atti = []
    for u, x in registro.items():
        if x["a"] not in aziende:
            continue
        fine = data_iso(x["f"])
        # in pubblicazione: fino alla data di fine; se l'atto non la riporta, se visto nell'ultimo mese
        p = fine >= oggi if fine else (x.get("v", "") >= recenti or x.get("v") == ts)
        atti.append({"a": x["a"], "s": x["s"], "t": x["t"], "o": x["o"], "d": x["d"], "f": x["f"],
                     "m": x["m"], "u": u, "r": x["r"], "k": in_evidenza({"titolo": x["t"], "oggetto": x["o"]}),
                     "p": p, "i": data_iso(x["d"]),
                     "b": x["r"][:10] == baseline_per_azienda.get(x["a"]) and u not in url_nuovi})
    dati = {"generato": momento.strftime("%d/%m/%Y alle %H:%M"), "ts": ts, "oggi": oggi,
            "aziende": [a["nome"] for a in CONFIG["aziende"] if a.get("attivo", True)],
            "manuali": {a["nome"]: a["albo"] for a in CONFIG["aziende"] if a.get("attivo", True) and a.get("manuale")},
            "anomalie": [{"azienda": e.split(": ", 1)[0], "errore": e.split(": ", 1)[-1][:160]} for e in errori],
            "atti": atti}
    js = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = SITO_TEMPLATE.read_text(encoding="utf-8").replace("/*__DATI__*/null", js)
    SITO_DIR.mkdir(exist_ok=True)
    (SITO_DIR / "index.html").write_text(html, encoding="utf-8")
    log(f"Sito generato: {len(atti)} atti ({sum(1 for a in atti if a['p'])} in pubblicazione)")


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
    dettagli = stato.setdefault("dettagli", {})   # oggetti letti dalle pagine di dettaglio
    nuovo_registro = not REGISTRO_FILE.exists()
    registro = carica_registro()
    momento = adesso()
    oggi, ts = momento.date().isoformat(), momento.strftime("%Y-%m-%dT%H:%M")
    novita, errori = [], []

    # le aziende "manuali" compaiono nel sito solo come collegamento diretto all'albo
    attive = [az for az in CONFIG["aziende"] if az.get("attivo", True) and not az.get("manuale")]
    SCADENZA[0] = time.time() + CONFIG.get("tempo_massimo_minuti", 80) * 60
    if nuovo_registro:
        log("Registro atti assente: lettura completa di tutte le pagine")

    def leggi_azienda(az):
        _locale.solo_browser = False
        _locale.dettaglio_browser = False
        _locale.diagnostica_dettaglio = False
        inizio = time.time()
        # lettura completa (senza fermarsi alle pagine già note) al primo avvio o senza registro
        completa = nuovo_registro or az["nome"] not in inizializzate
        try:
            atti = SCANSIONI[az.get("piattaforma", "wordpress")](az, visti, completa)
            arricchisci(az, atti, dettagli)
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
        for x in atti:
            if x["url"] not in visti:
                visti[x["url"]] = ts          # data e ora della prima rilevazione
                if not primo_avvio:
                    novita.append({**x, "azienda": nome, "rilevato_il": oggi, "parole": in_evidenza(x)})
            prec = registro.get(x["url"], {})
            registro[x["url"]] = {
                "a": nome, "s": x["sezione"], "t": x["titolo"],
                "o": x["oggetto"] or prec.get("o", ""), "d": x["dal"] or prec.get("d", ""),
                "f": x["al"] or prec.get("f", ""), "m": x["ambito"] or prec.get("m", ""),
                "r": prec.get("r") or visti[x["url"]], "v": ts}
        if primo_avvio:
            inizializzate.add(nome)
            data_baseline[nome] = oggi

    limite = (momento - timedelta(days=GIORNI_CONSERVAZIONE)).date().isoformat()
    stato["visti"] = {u: d for u, d in visti.items() if d >= limite}
    stato["dettagli"] = {u: d for u, d in dettagli.items() if u in stato["visti"]}
    stato["aziende_inizializzate"] = sorted(inizializzate)
    STATE_FILE.write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")
    salva_registro(registro, momento)
    aggiorna_archivio(novita)
    genera_sito(registro, errori, data_baseline, {x["url"] for x in novita}, momento)
    log(f"Novità: {len(novita)} · Anomalie: {len(errori)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

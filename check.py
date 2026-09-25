"""
Tjekker Godsbanens keramikværksted (Halbooking) for ledige tider på drejeskiverne
og sender push-besked via ntfy, når en tid skifter fra optaget til ledig.

Kør lokalt:  NTFY_TOPIC=dit-emne FORCE_RUN=1 python check.py
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

URL = os.environ.get("HALBOOKING_URL", "https://godsbanen.halbooking.dk/newlook/proc_baner.asp")
OMRAADE_TEKST = "Keramik"          # matcher "Bestil plads i Keramikværkstedet"
TZ = ZoneInfo("Europe/Copenhagen")
AKTIV_FRA, AKTIV_TIL = 7, 23      # tjek kun mellem kl. 7 og 23 dansk tid
FEJL_FOER_BESKED = 3              # antal fejl i træk før der sendes fejlbesked

ROOT = Path(__file__).parent
CONFIG_FIL = ROOT / "config.json"
STATE_FIL = ROOT / "state.json"

UGEDAGE = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MAANEDER = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
            "august", "september", "oktober", "november", "december"]


# ---------------------------------------------------------------- hjælpere

def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def parse_dato(s: str) -> date:
    return datetime.strptime(s, "%d-%m-%Y").date()


def pæn_dato(d: date) -> str:
    return f"{UGEDAGE[d.weekday()]} {d.day}. {MAANEDER[d.month - 1]}"


def blokke(timer):
    """[10, 11, 13] -> ['kl. 10 til 12', 'kl. 13 til 14']"""
    timer = sorted(timer)
    ud, start = [], None
    for i, t in enumerate(timer):
        if start is None:
            start = t
        if i == len(timer) - 1 or timer[i + 1] != t + 1:
            ud.append(f"kl. {start} til {t + 1}")
            start = None
    return ud


def ntfy(titel, besked, prioritet=4, tags=None):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    if not topic:
        log("NTFY_TOPIC mangler, ville have sendt:", titel, "|", besked)
        return
    data = json.dumps({
        "topic": topic,
        "title": titel,
        "message": besked,
        "priority": prioritet,
        "tags": tags or [],
        "click": URL,
    }).encode("utf-8")
    req = urllib.request.Request(server, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        log("ntfy:", r.status, titel)


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


# ---------------------------------------------------------------- scraping

UDTRÆK_JS = """
() => [...document.querySelectorAll('.bane')]
  .filter(b => b.querySelector('.baneheadtxt'))
  .map(b => ({
    navn: b.querySelector('.baneheadtxt').innerText.replace(/\\s+/g, ' ').trim(),
    felter: [...b.querySelectorAll(':scope > .banefelt:not(.banehead)')].map(s => ({
      tekst: s.innerText.replace(/\\s+/g, ' ').trim(),
      ledig: s.classList.contains('btn_ledig'),
    })),
  }))
"""

OVERSKRIFT_RE = re.compile(
    r"(Mandag|Tirsdag|Onsdag|Torsdag|Fredag|Lørdag|Søndag) (\d{1,2})\. (\w+) (\d{4})")
TID_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def ledige_timer(baner, prefix, fra, til):
    """Returnerer {'Drejeskive 1': [10, 11], ...} for alle baner der starter med prefix."""
    res = {}
    for b in baner:
        if not b["navn"].startswith(prefix):
            continue
        timer = set()
        for f in b["felter"]:
            if not f["ledig"]:
                continue
            m = TID_RE.search(f["tekst"])
            if not m:
                continue
            h1, h2 = int(m.group(1)), int(m.group(3))
            if int(m.group(4)) > 0:          # fx 14:30 -> tæl timen med
                h2 += 1
            timer.update(h for h in range(h1, h2) if fra <= h < til)
        res[b["navn"]] = sorted(timer)
    return res


def vis_dato(page, dato_str):
    """Vælg keramikområdet og datoen på siden (siden bruger POST-formularer)."""
    # Område
    omr = page.evaluate(f"""() => {{
        const s = document.getElementById('soeg_omraede');
        const o = [...s.options].find(o => o.text.includes('{OMRAADE_TEKST}'));
        return o ? [o.value, s.value] : null;
    }}""")
    if not omr:
        raise RuntimeError("Kan ikke finde keramikværkstedet i dropdown'en")
    if omr[0] != omr[1]:
        with page.expect_navigation():
            page.evaluate(f"""() => {{
                document.getElementById('soeg_omraede').value = '{omr[0]}';
                sende('proc_baner.asp','omr_soeg','','','','');
            }}""")
        page.wait_for_load_state("domcontentloaded")

    # Dato
    with page.expect_navigation():
        page.evaluate(f"""() => {{
            document.getElementById('banedato').value = '{dato_str}';
            sende('proc_baner.asp','soegdato','','','','');
        }}""")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector(".bane .baneheadtxt", timeout=20000)

    # Kontrollér at siden faktisk viser den rigtige dato og det rigtige område
    tekst = page.inner_text("body")
    m = OVERSKRIFT_RE.search(tekst)
    ønsket = parse_dato(dato_str)
    if not m or (int(m.group(2)), m.group(3).lower(), int(m.group(4))) != (
            ønsket.day, MAANEDER[ønsket.month - 1], ønsket.year):
        raise RuntimeError(f"Siden viser ikke {dato_str} (fandt: {m.group(0) if m else 'ingen dato'})")
    valgt = page.evaluate("() => { const s = document.getElementById('soeg_omraede'); "
                          "return s.options[s.selectedIndex].text }")
    if OMRAADE_TEKST not in valgt:
        raise RuntimeError(f"Forkert område valgt: {valgt}")


def vindue(cfg, dato_str):
    """(fra_time, til_time) for en dato. Ugedage i "tider_pr_ugedag" overstyrer standarden."""
    dag = UGEDAGE[parse_dato(dato_str).weekday()]
    særlig = cfg.get("tider_pr_ugedag", {}).get(dag)
    if særlig:
        return særlig["fra_time"], særlig["til_time"]
    return cfg["fra_time"], cfg["til_time"]


def hent_alle(datoer, cfg):
    resultat = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="da-DK")
        page.set_default_timeout(30000)
        page.goto(URL, wait_until="domcontentloaded")
        for d in datoer:
            for forsøg in (1, 2):
                try:
                    vis_dato(page, d)
                    baner = page.evaluate(UDTRÆK_JS)
                    ledige = ledige_timer(baner, cfg["ressource_prefix"], *vindue(cfg, d))
                    if not ledige:
                        raise RuntimeError(f"Fandt ingen '{cfg['ressource_prefix']}' på siden")
                    resultat[d] = ledige
                    log(d, json.dumps(ledige, ensure_ascii=False))
                    break
                except Exception:
                    if forsøg == 2:
                        raise
                    log(f"{d}: fejl, prøver igen")
                    time.sleep(5)
                    page.goto(URL, wait_until="domcontentloaded")
        browser.close()
    return resultat


# ---------------------------------------------------------------- logik

def blokliste(timer):
    """[10, 11, 13] -> [[10, 11], [13]]"""
    ud = []
    for t in sorted(timer):
        if ud and ud[-1][-1] == t - 1:
            ud[-1].append(t)
        else:
            ud.append([t])
    return ud


def relevante(ledige, min_timer, til, sidste_altid):
    """Behold kun sammenhængende blokke på mindst min_timer timer,
    eller blokke der slutter ved lukketid (dagens sidste tid)."""
    ud = {}
    for skive, timer in ledige.items():
        ud[skive] = sorted(t for b in blokliste(timer)
                           if len(b) >= min_timer or (sidste_altid and b[-1] == til - 1)
                           for t in b)
    return ud


def sammenlign(forrige, nu):
    """Returnerer (nye_ledige, taget_igen) som {skive: [timer]}.
    Nye ledige angives som hele blokke, så beskeden viser den samlede ledige tid."""
    nye, taget = {}, {}
    for skive in sorted(set(forrige) | set(nu)):
        f, n = set(forrige.get(skive, [])), set(nu.get(skive, []))
        if n - f:
            nye[skive] = sorted(t for b in blokliste(n) if set(b) - f for t in b)
        if f - n:
            taget[skive] = sorted(f - n)
    return nye, taget


def alt_ledigt(ledige, fra, til):
    fuld = list(range(fra, til))
    return all(t == fuld for t in ledige.values())


def formater(d: date, pr_skive):
    linjer = [f"{skive}: {', '.join(blokke(t))}" for skive, t in pr_skive.items()]
    return "\n".join(linjer)


def kør():
    cfg = load_json(CONFIG_FIL, {})
    state = load_json(STATE_FIL, {"datoer": {}, "fejl_i_traek": 0, "fejl_meldt": False})
    nu = datetime.now(TZ)

    if not os.environ.get("FORCE_RUN") and not (AKTIV_FRA <= nu.hour < AKTIV_TIL):
        log("Uden for aktiv tid, springer over")
        return

    idag = nu.date()
    horisont = idag + timedelta(days=cfg.get("booking_horisont_dage", 31))
    datoer = []
    for s in cfg["datoer"]:
        d = parse_dato(s)
        if d < idag:
            log(s, "er passeret, springer over")
        elif d > horisont:
            log(s, "ligger uden for bookingvinduet endnu, springer over")
        else:
            datoer.append(s)

    # Ryd gamle datoer ud af state
    state["datoer"] = {k: v for k, v in state["datoer"].items() if k in cfg["datoer"]
                       and parse_dato(k) >= idag}

    try:
        data = hent_alle(datoer, cfg)
    except Exception as e:
        traceback.print_exc()
        state["fejl_i_traek"] = state.get("fejl_i_traek", 0) + 1
        log("Fejl nr.", state["fejl_i_traek"], "i træk")
        if state["fejl_i_traek"] >= FEJL_FOER_BESKED and not state.get("fejl_meldt"):
            ntfy("Drejeskive-tjekkeren virker ikke",
                 f"Tjekket er fejlet {state['fejl_i_traek']} gange i træk. "
                 f"Seneste fejl: {(str(e).splitlines() or ['ukendt'])[0][:200]}\nSe loggen under Actions på GitHub.",
                 prioritet=3, tags=["warning"])
            state["fejl_meldt"] = True
        gem(state)
        return

    if state.get("fejl_meldt"):
        ntfy("Drejeskive-tjekkeren virker igen", "Tjekket kører normalt igen.",
             prioritet=2, tags=["white_check_mark"])
    state["fejl_i_traek"] = 0
    state["fejl_meldt"] = False

    regel = f"min{cfg.get('min_timer', 2)}_sidste{int(cfg.get('sidste_tid_altid', True))}"
    regel_skiftet = state.get("regel") != regel
    state["regel"] = regel

    for s, ledige in data.items():
        d = parse_dato(s)
        fra, til = vindue(cfg, s)
        if alt_ledigt(ledige, fra, til):
            # Siden viser alt som ledigt for dage der ikke er åbnet for booking endnu
            log(s, "alt står som ledigt, formentlig ikke åbnet endnu, venter")
            continue

        ledige = relevante(ledige, cfg.get("min_timer", 2), til,
                           cfg.get("sidste_tid_altid", True))
        første_gang = s not in state["datoer"]
        forrige = {} if første_gang else state["datoer"][s]
        nye, taget = sammenlign(forrige, ledige)
        if regel_skiftet:
            taget = {}   # undgå falske "taget igen" lige efter en regelændring

        if nye:
            titel = (f"Ledig drejeskive {pæn_dato(d)}" if not første_gang
                     else f"Allerede ledigt {pæn_dato(d)}")
            ntfy(titel, formater(d, nye) + "\nTryk her for at åbne bookingsiden.", prioritet=5 if not første_gang else 4,
                 tags=["tada"])
        if taget and not første_gang:
            ntfy(f"Taget igen {pæn_dato(d)}",
                 formater(d, taget) + "\ner ikke længere ledig.", prioritet=2)
        if første_gang and not nye:
            log(s, "første tjek, alt optaget")

        state["datoer"][s] = ledige

    gem(state)


def gem(state):
    STATE_FIL.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")


if __name__ == "__main__":
    kør()
    sys.exit(0)

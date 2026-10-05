import html as html_lib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path


BASIS = "https://laren.bestuurlijkeinformatie.nl"

CATEGORIEEN = {
    "Raadsvergadering": "/Calendar/OpenCategory/10002003",
    "Commissie R&I": "/Calendar/OpenCategory/10002008",
    "Commissie M&F": "/Calendar/OpenCategory/10002007",
}

MAANDEN = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VergaderstukkenLaren/1.0)"
}

STATE_BESTAND = Path("state.json")
OVERZICHT_BESTAND = Path("overzicht.md")

# Teksten die nooit als documenttitel mogen gelden.
OVERGESLAGEN_TEKSTEN = {
    "bijlagen", "download", "downloaden", "open", "openen",
    "document", "documenten", "vergaderstukken", "bekijken",
}


def haal_pagina(url):
    """Haalt een webpagina op; geeft (uiteindelijke URL, HTML) terug."""
    verzoek = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(verzoek, timeout=60) as antwoord:
        uiteindelijke_url = antwoord.geturl()
        inhoud = antwoord.read()
    return uiteindelijke_url, inhoud.decode("utf-8", errors="replace")


def schoon_tekst(tekst):
    """Verwijdert HTML en maakt tekst netjes leesbaar."""
    tekst = re.sub(r"<[^>]+>", " ", tekst)
    tekst = html_lib.unescape(tekst)
    tekst = tekst.replace("\xa0", " ")
    return re.sub(r"\s+", " ", tekst).strip()


def markdown_veilig(tekst):
    """Voorkomt dat vierkante haken de Markdown-link beschadigen."""
    return (
        tekst.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    )


def zonder_bestandsgrootte(titel):
    """Verwijdert bestandsgroottes zoals '149 KB' of '2,4 MB' aan het eind."""
    return re.sub(
        r"\s+\d+(?:[.,]\d+)?\s*(?:bytes?|kB|MB|GB)\s*$",
        "",
        titel,
        flags=re.IGNORECASE,
    ).strip()


def bruikbare_titel(kandidaat):
    """Geeft de opgeschoonde titel terug, of None als hij onbruikbaar is."""
    kandidaat = zonder_bestandsgrootte(schoon_tekst(kandidaat))
    if len(kandidaat) < 5:
        return None
    if kandidaat.lower() in OVERGESLAGEN_TEKSTEN:
        return None
    if re.fullmatch(r"[\d.,]+\s*(?:bytes?|kB|MB|GB)?", kandidaat, flags=re.I):
        return None
    return kandidaat


def lees_datum(tekst):
    """Herkent Nederlandse datums zoals '23 september 2026'."""
    match = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", tekst.lower())
    if not match:
        return None
    maand = MAANDEN.get(match.group(2))
    if not maand:
        return None
    try:
        return date(int(match.group(3)), maand, int(match.group(1)))
    except ValueError:
        return None


def haal_titel(html):
    """Haalt de titel van de iBabs-pagina uit het title-element."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if not match:
        return ""
    titel = schoon_tekst(match.group(1))
    return re.split(r"(?i)\s*[-|]\s*iBabs", titel)[0].strip()


def titel_voor_link(html, positie):
    """
    VANGNET (alleen als de link zelf geen titel bevat):
    kijkt maximaal 800 tekens terug voor bruikbare tekst.
    """
    fragment = html[max(0, positie - 800):positie]
    # Een afgeknipte tag aan het begin weghalen
    # (dat gaf eerder rommel als 'ass="panel-title-label" >').
    eerste_open = fragment.find("<")
    eerste_sluit = fragment.find(">")
    if eerste_sluit != -1 and (eerste_open == -1 or eerste_sluit < eerste_open):
        fragment = fragment[eerste_sluit + 1:]
    for kandidaat in reversed(re.split(r"<[^>]+>", fragment)):
        titel = bruikbare_titel(kandidaat)
        if titel:
            return titel
    return None


def vind_toekomstige_vergaderingen(html):
    """Vangnet: zoekt toekomstige vergaderlinks op de pagina."""
    kandidaten = []
    patroon = re.compile(
        r'href=["\']([^"\']*?/Agenda/Index/[^"\']+)["\'][^>]*>(.*?)</a>',
        flags=re.I | re.S,
    )
    for match in patroon.finditer(html):
        vergaderdatum = lees_datum(schoon_tekst(match.group(2)))
        if not vergaderdatum or vergaderdatum < date.today():
            continue
        url = urllib.parse.urljoin(BASIS, html_lib.unescape(match.group(1)))
        kandidaten.append((vergaderdatum, url))
    kandidaten.sort(key=lambda k: k[0])
    return kandidaten


def kies_vergadering(start_url):
    """OpenCategory primair; bij een vergadering in het verleden de eerstvolgende."""
    eind_url, pagina = haal_pagina(start_url)
    huidige_datum = lees_datum(haal_titel(pagina))
    if huidige_datum and huidige_datum >= date.today():
        return eind_url, pagina
    kandidaten = vind_toekomstige_vergaderingen(pagina)
    if kandidaten:
        return haal_pagina(kandidaten[0][1])
    return eind_url, pagina


def vind_agendapunten(html):
    """
    Zoekt de koppen van agendapunten (elementen met class 'panel-title-label').
    Geeft een lijst van (positie, titel) terug. Leeg als de structuur anders is;
    dan valt het overzicht terug op een platte lijst.
    """
    punten = []
    patroon = re.compile(
        r'<(h\d|div|span|a)\b[^>]*class=["\'][^"\']*panel-title-label'
        r'[^"\']*["\'][^>]*>(.*?)</\1>',
        flags=re.I | re.S,
    )
    for match in patroon.finditer(html):
        titel = schoon_tekst(match.group(2))
        if titel:
            punten.append((match.start(), titel))
    return punten


def vind_documenten(html):
    """
    Vindt per document het ID, de titel, het agendapunt en de leeslink.

    WIJZIGING: de titel komt nu uit de tekst ÍN de link. Het portaal heeft per
    document twee links (een icoon en de titel). Voorheen werd de tekst vóór
    de eerste link gepakt; dat was de titel van het vórige document, waardoor
    alle titels één plek opschoven.
    """
    ankerpatroon = re.compile(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        flags=re.I | re.S,
    )
    per_id = {}
    volgorde = []

    for match in ankerpatroon.finditer(html):
        url = html_lib.unescape(match.group(1))
        if "/Agenda/Document/" not in url:
            continue
        id_match = re.search(r"[?&]documentId=([0-9a-fA-F-]+)", url)
        if not id_match:
            continue
        document_id = id_match.group(1)
        if document_id not in per_id:
            per_id[document_id] = {"positie": match.start(), "titels": []}
            volgorde.append(document_id)
        titel = bruikbare_titel(match.group(2))
        if titel:
            per_id[document_id]["titels"].append(titel)

    agendapunten = vind_agendapunten(html)
    documenten = []

    for document_id in volgorde:
        gegevens = per_id[document_id]
        if gegevens["titels"]:
            titel = max(gegevens["titels"], key=len)
        else:
            titel = titel_voor_link(html, gegevens["positie"]) or "Document"

        agendapunt = None
        for positie, punt_titel in agendapunten:
            if positie < gegevens["positie"]:
                agendapunt = punt_titel
            else:
                break

        documenten.append({
            "id": document_id,
            "titel": titel,
            "agendapunt": agendapunt,
            "leeslink": f"{BASIS}/Document/View/{document_id}",
        })

    return documenten


def controleer_leeslink(url):
    """Lichte controle of de leeslink een bestand is (True/False/None)."""
    verzoek = urllib.request.Request(
        url, headers={**HEADERS, "Range": "bytes=0-1023"}
    )
    try:
        with urllib.request.urlopen(verzoek, timeout=20) as antwoord:
            content_type = (
                antwoord.headers.get("Content-Type", "")
                .split(";")[0].strip().lower()
            )
            eerste_bytes = antwoord.read(16)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    if content_type in {"text/html", "application/xhtml+xml"}:
        return False
    if eerste_bytes.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return False
    return True


def lees_vorige_staat():
    """Leest state.json; bij problemen een lege state."""
    try:
        data = json.loads(STATE_BESTAND.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def schrijf_overzicht():
    """Haalt alle categorieën op en schrijft overzicht.md en state.json."""
    vorige_staat = lees_vorige_staat()
    nieuwe_staat = dict(vorige_staat)

    regels = [
        "# Vergaderstukken Laren",
        "",
        f"_Laatst gecontroleerd op {date.today().isoformat()}._",
        "",
    ]
    succesvol = 0

    for onderdeel, pad in CATEGORIEEN.items():
        start_url = urllib.parse.urljoin(BASIS, pad)
        try:
            eind_url, pagina = kies_vergadering(start_url)
            documenten = vind_documenten(pagina)
        except Exception as fout:
            regels.extend([
                f"## {onderdeel}",
                "",
                f"_Kon deze vergadering niet ophalen: {fout}. "
                "De vorige stand is behouden._",
                "",
            ])
            continue

        succesvol += 1
        paginatitel = haal_titel(pagina)
        kop = onderdeel + (f" - {paginatitel}" if paginatitel else "")
        regels.extend([
            f"## {markdown_veilig(kop)}",
            "",
            f"[Open de volledige agenda]({eind_url})",
            "",
        ])

        oude_documenten = vorige_staat.get(onderdeel, {})
        huidige_documenten = {}

        if not documenten:
            regels.extend(["_Nog geen documenten gepubliceerd._", ""])
            continue

        vorig_agendapunt = None
        for document in documenten:
            agendapunt = document["agendapunt"]
            if agendapunt and agendapunt != vorig_agendapunt:
                regels.extend(["", f"### {markdown_veilig(agendapunt)}", ""])
                vorig_agendapunt = agendapunt

            document_id = document["id"]
            huidige_documenten[document_id] = document["titel"]

            nieuw = ""
            if oude_documenten and document_id not in oude_documenten:
                nieuw = " **(nieuw)**"

            waarschuwing = ""
            if controleer_leeslink(document["leeslink"]) is False:
                waarschuwing = " **WAARSCHUWING: geen direct bestand**"

            regels.append(
                f"- [{markdown_veilig(document['titel'])}]"
                f"({document['leeslink']}){nieuw}{waarschuwing}"
            )

        regels.append("")
        nieuwe_staat[onderdeel] = huidige_documenten

    if succesvol == 0:
        raise RuntimeError(
            "Geen enkele categorie kon worden opgehaald. "
            "Overzicht en state worden niet overschreven."
        )

    OVERZICHT_BESTAND.write_text(
        "\n".join(regels).rstrip() + "\n", encoding="utf-8"
    )
    STATE_BESTAND.write_text(
        json.dumps(nieuwe_staat, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("Klaar: overzicht.md en state.json zijn bijgewerkt.")


if __name__ == "__main__":
    schrijf_overzicht()

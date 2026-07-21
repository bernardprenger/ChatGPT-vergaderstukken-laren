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
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; VergaderstukkenLaren/1.0)"
    )
}

STATE_BESTAND = Path("state.json")
OVERZICHT_BESTAND = Path("overzicht.md")


def haal_pagina(url):
    """
    Haalt een webpagina op.

    Geeft terug:
    - de uiteindelijke URL na redirects;
    - de HTML van de pagina.
    """
    verzoek = urllib.request.Request(
        url,
        headers=HEADERS,
    )

    with urllib.request.urlopen(
        verzoek,
        timeout=60,
    ) as antwoord:
        uiteindelijke_url = antwoord.geturl()
        inhoud = antwoord.read()

    return (
        uiteindelijke_url,
        inhoud.decode("utf-8", errors="replace"),
    )


def schoon_tekst(tekst):
    """
    Verwijdert HTML en maakt tekst netjes leesbaar.
    """
    tekst = re.sub(r"<[^>]+>", "", tekst)
    tekst = html_lib.unescape(tekst)
    tekst = tekst.replace("\xa0", " ")

    return re.sub(r"\s+", " ", tekst).strip()


def markdown_veilig(tekst):
    """
    Voorkomt dat vierkante haken in een titel
    de Markdown-link beschadigen.
    """
    return (
        tekst
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def zonder_bestandsgrootte(titel):
    """
    Verwijdert bestandsgroottes zoals:
    500 KB
    2,4 MB
    1.2 GB
    """
    return re.sub(
        r"\s+\d+(?:[.,]\d+)?\s*"
        r"(?:bytes?|kB|MB|GB)\s*$",
        "",
        titel,
        flags=re.IGNORECASE,
    ).strip()


def lees_datum(tekst):
    """
    Herkent Nederlandse datums zoals:
    23 september 2026
    """
    match = re.search(
        r"(\d{1,2})\s+([a-z]+)\s+(\d{4})",
        tekst.lower(),
    )

    if not match:
        return None

    dag = int(match.group(1))
    maand = MAANDEN.get(match.group(2))
    jaar = int(match.group(3))

    if not maand:
        return None

    try:
        return date(jaar, maand, dag)
    except ValueError:
        return None


def haal_titel(html):
    """
    Haalt de titel van de iBabs-pagina uit het title-element.
    """
    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return ""

    titel = schoon_tekst(match.group(1))

    return re.split(
        r"(?i)\s*[-|]\s*iBabs",
        titel,
    )[0].strip()


def titel_voor_link(html, positie):
    """
    Op het Laren-portaal staat de zichtbare titel
    vlak vóór de hyperlink. De hyperlink zelf kan
    uitsluitend een icoon bevatten.

    Daarom wordt vanaf de link maximaal 800 tekens
    teruggekeken.
    """
    begin = max(0, positie - 800)
    fragment = html[begin:positie]

    losse_fragmenten = re.split(
        r"<[^>]+>",
        fragment,
    )

    overgeslagen_teksten = {
        "bijlagen",
        "download",
        "open",
        "document",
        "documenten",
        "vergaderstukken",
    }

    for kandidaat in reversed(losse_fragmenten):
        kandidaat = schoon_tekst(kandidaat)
        kandidaat = zonder_bestandsgrootte(kandidaat)

        if len(kandidaat) < 5:
            continue

        if kandidaat.lower() in overgeslagen_teksten:
            continue

        if re.fullmatch(
            r"[\d.,]+\s*(?:bytes?|kB|MB|GB)?",
            kandidaat,
            flags=re.IGNORECASE,
        ):
            continue

        return kandidaat

    return None


def vind_toekomstige_vergaderingen(html):
    """
    Zoekt toekomstige vergaderlinks op de pagina.

    Dit wordt alleen als vangnet gebruikt wanneer
    OpenCategory naar een oude vergadering verwijst.
    """
    kandidaten = []

    patroon = re.compile(
        r'href=["\']'
        r'([^"\']*?/Agenda/Index/[^"\']+)'
        r'["\'][^>]*>'
        r'(.*?)'
        r"</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    for match in patroon.finditer(html):
        zichtbare_tekst = schoon_tekst(
            match.group(2)
        )

        vergaderdatum = lees_datum(
            zichtbare_tekst
        )

        if not vergaderdatum:
            continue

        if vergaderdatum < date.today():
            continue

        url = urllib.parse.urljoin(
            BASIS,
            html_lib.unescape(match.group(1)),
        )

        kandidaten.append(
            (vergaderdatum, url)
        )

    kandidaten.sort(
        key=lambda kandidaat: kandidaat[0]
    )

    return kandidaten


def kies_vergadering(start_url):
    """
    Gebruikt OpenCategory als primaire ingang.

    Alleen wanneer de geselecteerde vergadering
    in het verleden ligt, wordt op dezelfde pagina
    gezocht naar de eerstvolgende toekomstige
    vergadering.
    """
    eind_url, pagina = haal_pagina(start_url)

    huidige_datum = lees_datum(
        haal_titel(pagina)
    )

    if (
        huidige_datum
        and huidige_datum >= date.today()
    ):
        return eind_url, pagina

    kandidaten = vind_toekomstige_vergaderingen(
        pagina
    )

    if kandidaten:
        eerstvolgende_url = kandidaten[0][1]

        return haal_pagina(
            eerstvolgende_url
        )

    return eind_url, pagina


def vind_documenten(html):
    """
    Vindt de document-ID's, titels en directe leeslinks.

    De zichtbare titel staat op het Laren-portaal
    vlak vóór de hyperlink met het documenticoon.
    """
    documenten = []
    gezien = set()

    linkpatroon = re.compile(
        r'href=["\']([^"\']+)["\']',
        flags=re.IGNORECASE,
    )

    for match in linkpatroon.finditer(html):
        url = html_lib.unescape(
            match.group(1)
        )

        if "/Agenda/Document/" not in url:
            continue

        id_match = re.search(
            r"[?&]documentId="
            r"([0-9a-fA-F-]+)",
            url,
            flags=re.IGNORECASE,
        )

        if not id_match:
            continue

        document_id = id_match.group(1)

        if document_id in gezien:
            continue

        gezien.add(document_id)

        titel = (
            titel_voor_link(
                html,
                match.start(),
            )
            or "Document"
        )

        leeslink = (
            f"{BASIS}/Document/View/"
            f"{document_id}"
        )

        documenten.append(
            {
                "id": document_id,
                "titel": titel,
                "leeslink": leeslink,
            }
        )

    return documenten


def controleer_leeslink(url):
    """
    Controleert licht of de directe leeslink
    een bestand teruggeeft en geen HTML-kijkpagina.

    Mogelijke uitkomsten:
    - True: lijkt een bestand;
    - False: is waarschijnlijk HTML;
    - None: controle kon niet worden uitgevoerd.
    """
    headers = {
        **HEADERS,
        "Range": "bytes=0-1023",
    }

    verzoek = urllib.request.Request(
        url,
        headers=headers,
    )

    try:
        with urllib.request.urlopen(
            verzoek,
            timeout=20,
        ) as antwoord:
            content_type = (
                antwoord.headers.get(
                    "Content-Type",
                    "",
                )
                .split(";")[0]
                .strip()
                .lower()
            )

            eerste_bytes = antwoord.read(16)

    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ):
        return None

    if content_type in {
        "text/html",
        "application/xhtml+xml",
    }:
        return False

    if eerste_bytes.lstrip().lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        return False

    return True


def lees_vorige_staat():
    """
    Leest state.json.

    Bij een ontbrekend of beschadigd bestand
    wordt gestart met een lege state.
    """
    try:
        inhoud = STATE_BESTAND.read_text(
            encoding="utf-8"
        )

        data = json.loads(inhoud)

    except (
        OSError,
        json.JSONDecodeError,
    ):
        return {}

    if not isinstance(data, dict):
        return {}

    return {
        onderdeel: documenten
        for onderdeel, documenten in data.items()
        if isinstance(documenten, dict)
    }


def schrijf_overzicht():
    """
    Haalt alle categorieën op en schrijft overzicht.md
    en state.json.
    """
    vorige_staat = lees_vorige_staat()

    # Begin met de vorige staat.
    # Een tijdelijke storing verwijdert zo niets.
    nieuwe_staat = dict(vorige_staat)

    regels = [
        "# Vergaderstukken Laren",
        "",
        (
            f"_Laatst gecontroleerd op "
            f"{date.today().isoformat()}._"
        ),
        "",
    ]

    succesvol = 0

    for onderdeel, pad in CATEGORIEEN.items():
        start_url = urllib.parse.urljoin(
            BASIS,
            pad,
        )

        try:
            eind_url, pagina = kies_vergadering(
                start_url
            )

            documenten = vind_documenten(
                pagina
            )

        except Exception as fout:
            regels.extend(
                [
                    f"## {onderdeel}",
                    "",
                    (
                        "_Kon deze vergadering niet "
                        f"ophalen: {fout}. "
                        "De vorige stand is behouden._"
                    ),
                    "",
                ]
            )

            continue

        succesvol += 1

        paginatitel = haal_titel(
            pagina
        )

        kop = onderdeel

        if paginatitel:
            kop += f" — {paginatitel}"

        regels.extend(
            [
                f"## {markdown_veilig(kop)}",
                "",
                (
                    f"[Open de volledige agenda]"
                    f"({eind_url})"
                ),
                "",
            ]
        )

        oude_documenten = vorige_staat.get(
            onderdeel,
            {},
        )

        huidige_documenten = {}

        if not documenten:
            regels.extend(
                [
                    "_Nog geen documenten gepubliceerd._",
                    "",
                ]
            )

            # Belangrijk:
            # bij nul resultaten wissen we de oude state niet.
            # Dit voorkomt dat een gewijzigde HTML-structuur
            # alle documenten stilletjes uit state.json haalt.
            continue

        for document in documenten:
            document_id = document["id"]
            titel = document["titel"]
            leeslink = document["leeslink"]

            huidige_documenten[
                document_id
            ] = titel

            nieuw_markering = ""

            if (
                oude_documenten
                and document_id
                not in oude_documenten
            ):
                nieuw_markering = " **(nieuw)**"

            bereikbaarheid = controleer_leeslink(
                leeslink
            )

            waarschuwing = ""

            if bereikbaarheid is False:
                waarschuwing = (
                    " ⚠️ **geeft geen direct bestand terug**"
                )

            regels.append(
                f"- [{markdown_veilig(titel)}]"
                f"({leeslink})"
                f"{nieuw_markering}"
                f"{waarschuwing}"
            )

        regels.append("")

        # Alleen vervangen als er werkelijk documenten
        # zijn gevonden.
        nieuwe_staat[
            onderdeel
        ] = huidige_documenten

    if succesvol == 0:
        raise RuntimeError(
            "Geen enkele categorie kon worden opgehaald. "
            "Overzicht en state worden niet overschreven."
        )

    OVERZICHT_BESTAND.write_text(
        "\n".join(regels).rstrip() + "\n",
        encoding="utf-8",
    )

    STATE_BESTAND.write_text(
        json.dumps(
            nieuwe_staat,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        "Klaar: overzicht.md en state.json "
        "zijn bijgewerkt."
    )


if __name__ == "__main__":
    schrijf_overzicht()

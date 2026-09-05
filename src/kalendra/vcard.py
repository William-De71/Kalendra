"""Analyseur vCard (RFC 6350), sans dépendance externe.

Même parti pris que `ics.py` : on n'extrait que les métadonnées d'index (UID,
nom affiché, adresse mail) et la carte déposée par le client est conservée
octet pour octet. Un serveur de stockage n'a pas à avoir d'opinion sur les
propriétés qu'il ne comprend pas — les réécrire, ce serait les perdre à chaque
synchronisation.

La syntaxe des lignes de contenu est celle d'iCalendar (`NOM;PARAM=x:valeur`,
pliage par espace en début de ligne), d'où la réutilisation de `unfold` et
`parse_content_line` plutôt qu'un second analyseur.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ics import Property, parse_content_line, unfold


class InvalidCardData(ValueError):
    """Le corps fourni n'est pas une carte de visite exploitable."""


@dataclass(slots=True)
class VCard:
    """Une carte, réduite à ce que le serveur indexe."""

    uid: str
    fn: str
    email: str
    version: str
    props: dict[str, list[Property]]

    def get(self, name: str) -> Property | None:
        values = self.props.get(name.upper())
        return values[0] if values else None

    def all(self, name: str) -> list[Property]:
        return self.props.get(name.upper(), [])


def parse_vcard(text: str) -> VCard:
    """Analyse une carte et en extrait les métadonnées d'index.

    Lève `InvalidCardData` si le corps n'est pas encadré par BEGIN/END:VCARD.
    On tolère en revanche l'absence d'UID ou de FN : certains clients anciens
    n'en mettent pas, et refuser la carte ferait échouer la synchronisation
    entière pour une propriété que le serveur peut suppléer.
    """
    props: dict[str, list[Property]] = {}
    ouvert = False
    ferme = False

    for line in unfold(text):
        prop = parse_content_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN" and prop.value.strip().upper() == "VCARD":
            ouvert = True
            continue
        if prop.name == "END" and prop.value.strip().upper() == "VCARD":
            ferme = True
            break
        if ouvert:
            props.setdefault(prop.name, []).append(prop)

    if not ouvert or not ferme:
        raise InvalidCardData("carte vCard incomplète : BEGIN/END:VCARD attendus")

    def _premier(nom: str) -> str:
        valeurs = props.get(nom)
        return valeurs[0].text.strip() if valeurs else ""

    fn = _premier("FN")
    if not fn:
        # À défaut de FN, reconstituer un nom depuis N (famille;prénom;…) donne
        # une liste lisible dans l'interface plutôt qu'une ligne vide.
        n = props.get("N")
        if n:
            morceaux = [m.strip() for m in n[0].text.split(";")]
            prenom = morceaux[1] if len(morceaux) > 1 else ""
            famille = morceaux[0] if morceaux else ""
            fn = " ".join(m for m in (prenom, famille) if m)

    return VCard(
        uid=_premier("UID"),
        fn=fn,
        email=_premier("EMAIL"),
        version=_premier("VERSION") or "3.0",
        props=props,
    )


def card_matches(card: VCard, texte: str) -> bool:
    """Recherche plein texte naïve sur les propriétés indexées.

    Sert au REPORT `addressbook-query` avec un filtre non exprimable en SQL :
    en cas de doute on renvoie la carte plutôt que de la masquer, comme pour
    les récurrences non expansées côté calendrier.
    """
    if not texte:
        return True
    besoin = texte.lower()
    return any(
        besoin in valeur.lower()
        for valeur in (card.fn, card.email, card.uid)
        if valeur
    )

"""vCard parser (RFC 6350), with no external dependency.

Same stance as `ics.py`: only index metadata is extracted (UID, display name,
email address) and the card the client uploads is kept byte for byte. A storage
server has no business holding opinions about properties it does not
understand — rewriting them would mean losing them on every sync.

Content-line syntax is iCalendar's (`NAME;PARAM=x:value`, folding by a leading
space), hence the reuse of `unfold` and `parse_content_line` rather than a
second parser.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ics import Property, parse_content_line, unfold


class InvalidCardData(ValueError):
    """The supplied body is not a usable vCard."""


@dataclass(slots=True)
class VCard:
    """A card, reduced to what the server indexes."""

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
    """Parse a card and extract its index metadata.

    Raises `InvalidCardData` when the body is not framed by BEGIN/END:VCARD.
    A missing UID or FN is tolerated, however: some older clients omit them, and
    rejecting the card would fail the whole sync over a property the server can
    supply itself.
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
        # Without FN, rebuilding a name from N (family;given;…) keeps the list
        # readable in the interface rather than showing an empty row.
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
    """Naive full-text search over the indexed properties.

    Serves the `addressbook-query` REPORT when the filter cannot be expressed in
    SQL: when in doubt the card is returned rather than hidden, as with
    unexpanded recurrences on the calendar side.
    """
    if not texte:
        return True
    besoin = texte.lower()
    return any(
        besoin in valeur.lower()
        for valeur in (card.fn, card.email, card.uid)
        if valeur
    )

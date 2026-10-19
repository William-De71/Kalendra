"""Import of a `.vcf` file into an existing address book.

Address-book exports (Google Contacts, Thunderbird, a phone) put every card
end to end in one file, whereas CardDAV stores one resource per card. The file
is split on BEGIN:VCARD / END:VCARD and each card stored as it came, folding
included: nothing is parsed back into text, so properties `vcard.py` ignores
reach the address book untouched.

Like the calendar import, this makes no network request: the file arrives
through the form.
"""

from __future__ import annotations

import hashlib

from .importics import MAX_COMPOSANTS, Rapport
from .security import etag_for
from .vcard import InvalidCardData, parse_vcard


def split_vcards(texte: str) -> list[str]:
    """Raw cards of a file, CRLF-terminated, in order.

    Works on physical lines so a folded property stays folded. Anything
    outside a BEGIN/END pair — blank lines, a stray header — is dropped.
    """
    cartes: list[str] = []
    courante: list[str] | None = None
    for ligne in texte.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        etiquette = ligne.strip().upper()
        if etiquette == "BEGIN:VCARD":
            courante = [ligne.strip()]
            continue
        if courante is None:
            continue
        if etiquette == "END:VCARD":
            courante.append(ligne.strip())
            cartes.append("\r\n".join(courante) + "\r\n")
            courante = None
        elif ligne:
            courante.append(ligne)
    return cartes


def importer_cartes(db, carnet_row, contenu: str, *, max_taille: int, quota: int = 0) -> Rapport:
    """Store every card of `contenu` into `carnet_row`.

    The resource name derives from the UID, so re-importing the same file
    updates cards instead of duplicating them. A card without a UID — older
    exports omit it — is named after a digest of its content, which keeps a
    re-import of the unchanged file idempotent too.
    """
    rapport = Rapport(unite="contact")
    cartes = split_vcards(contenu)
    if not cartes:
        rapport.erreurs.append("Aucune carte de visite trouvée dans ce fichier.")
        return rapport
    if len(cartes) > MAX_COMPOSANTS:
        rapport.erreurs.append(
            f"Fichier trop volumineux : {len(cartes)} cartes, maximum {MAX_COMPOSANTS}."
        )
        return rapport

    usage = db.account_usage(carnet_row["user_id"]) if quota > 0 else 0

    for index, carte in enumerate(cartes, start=1):
        taille = len(carte.encode("utf-8"))
        if taille > max_taille:
            rapport.ignores += 1
            rapport.erreurs.append(f"Contact {index} : trop volumineux.")
            continue
        try:
            card = parse_vcard(carte)
        except InvalidCardData as exc:
            rapport.ignores += 1
            rapport.erreurs.append(f"Contact {index} : {exc}")
            continue

        if card.uid:
            href = _href_pour(card.uid)
            existant = db.get_object_by_uid(carnet_row["id"], card.uid)
            if existant is not None:
                href = existant["href"]
            else:
                existant = db.get_object(carnet_row["id"], href)
        else:
            empreinte = hashlib.sha256(carte.encode("utf-8")).hexdigest()[:32]
            href = f"import-{empreinte}.vcf"
            existant = db.get_object(carnet_row["id"], href)
        # Same rule as a CardDAV PUT without UID: the resource name stands in.
        uid = card.uid or href

        if quota > 0:
            ancienne = int(existant["size"]) if existant is not None else 0
            if usage - ancienne + taille > quota:
                rapport.ignores += 1
                rapport.erreurs.append(f"Contact {index} : quota du compte atteint.")
                continue
            usage += taille - ancienne

        db.put_object(
            carnet_row["id"],
            href,
            carte,
            uid=uid,
            component="VCARD",
            dtstart=None,
            dtend=None,
            recurring=False,
            summary=card.fn[:200],
            etag=etag_for(carte),
        )
        if existant is None:
            rapport.crees += 1
        else:
            rapport.remplaces += 1
    return rapport


def _href_pour(uid: str) -> str:
    """Resource name from the UID, restricted to the `SAFE_HREF` set."""
    base = "".join(c if c.isalnum() or c in "._-" else "-" for c in uid).strip("-")
    if not base:
        base = "carte-" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
    return f"{base[:200]}.vcf"

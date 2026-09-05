#!/usr/bin/env python3
"""Compute the next version and update the files that carry it.

    scripts/bump-version.py patch|minor|major [--dry-run]

Serves the "Prepare a release" workflow: GitHub only knows the chosen level
(patch, minor, major), and this is where the number is derived and
`__init__.py`, `pyproject.toml` and `CHANGELOG.md` are rewritten.

A script rather than shell inside the workflow: reshaping the changelog means
moving a whole section, which is unreadable in `sed` and impossible to test
outside GitHub.

The script never calls git: it prepares the files, and it is up to the workflow
(or the user) to commit and tag.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys

RACINE = pathlib.Path(__file__).resolve().parents[1]
INIT = RACINE / "src" / "kalendra" / "__init__.py"
CHANGELOG = RACINE / "CHANGELOG.md"
PYPROJECT = RACINE / "pyproject.toml"

VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
#: `pyproject.toml` carries the version hard-coded, separately from
#: `__init__.py`: both must move together, otherwise the published wheel would
#: announce a different number from the one the server reports.
PYPROJECT_RE = re.compile(r'^version = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


def version_courante() -> tuple[int, int, int]:
    trouve = VERSION_RE.search(INIT.read_text(encoding="utf-8"))
    if trouve is None:
        raise SystemExit(f"__version__ introuvable ou mal formé dans {INIT}")

    # Both files must already agree: starting from an inconsistent repository
    # would only carry the discrepancy into the next version.
    dans_pyproject = PYPROJECT_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    courante = ".".join(trouve.groups())
    if dans_pyproject is not None and dans_pyproject.group(1) != courante:
        raise SystemExit(
            f"__init__.py annonce {courante} mais pyproject.toml "
            f"{dans_pyproject.group(1)} : accordez-les avant de publier."
        )
    return tuple(int(g) for g in trouve.groups())  # type: ignore[return-value]


def version_suivante(actuelle: tuple[int, int, int], niveau: str) -> tuple[int, int, int]:
    majeur, mineur, correctif = actuelle
    if niveau == "major":
        return majeur + 1, 0, 0
    if niveau == "minor":
        return majeur, mineur + 1, 0
    return majeur, mineur, correctif + 1


def _section_non_publiee(texte: str) -> str:
    """Contents of the "Unreleased" section, without its heading."""
    debut = texte.find("## [Non publié]")
    if debut < 0:
        return ""
    apres = texte.index("\n", debut) + 1
    suivante = texte.find("\n## [", apres)
    fin = suivante + 1 if suivante >= 0 else len(texte)
    return texte[apres:fin].strip("\n")


def maj_changelog(texte: str, version: str, aujourdhui: str) -> str:
    """Move "Unreleased" into a dated section and update the links.

    An empty "Unreleased" section is refused: publishing a version without
    describing what it changes would leave the release notes empty, and
    `release.yml` extracts them verbatim.
    """
    contenu = _section_non_publiee(texte)
    if not contenu.strip():
        raise SystemExit(
            "La section « Non publié » du CHANGELOG est vide : "
            "décrivez les changements avant de publier."
        )

    debut = texte.find("## [Non publié]")
    apres_titre = texte.index("\n", debut) + 1
    suivante = texte.find("\n## [", apres_titre)
    fin = suivante + 1 if suivante >= 0 else len(texte)

    remplacement = (
        "## [Non publié]\n\n"
        f"## [{version}] — {aujourdhui}\n\n"
        f"{contenu}\n\n"
    )
    texte = texte[:debut] + remplacement + texte[fin:]

    # Comparison links at the foot of the file: "Unreleased" now starts from
    # the version just frozen, and the new one points at the previous one.
    lien_non_publie = re.search(
        r"^\[Non publié\]: (\S+)/compare/v(\d+\.\d+\.\d+)\.\.\.HEAD$", texte, re.MULTILINE
    )
    if lien_non_publie is not None:
        base, precedente = lien_non_publie.group(1), lien_non_publie.group(2)
        texte = texte.replace(
            lien_non_publie.group(0),
            f"[Non publié]: {base}/compare/v{version}...HEAD\n"
            f"[{version}]: {base}/compare/v{precedente}...v{version}",
        )
    return texte


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "niveau", nargs="?", choices=["patch", "minor", "major"],
        help="omis avec --show",
    )
    parser.add_argument(
        "--show", action="store_true", help="affiche la version courante et sort"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="affiche la version sans rien écrire"
    )
    args = parser.parse_args(argv)

    actuelle = version_courante()
    if args.show:
        print(".".join(str(n) for n in actuelle))
        return 0
    if args.niveau is None:
        parser.error("indiquez un niveau (patch, minor, major) ou --show")
    suivante = version_suivante(actuelle, args.niveau)
    version = ".".join(str(n) for n in suivante)

    if args.dry_run:
        print(version)
        return 0

    # Compute everything before writing anything: `maj_changelog` rejects an
    # empty "Unreleased" section, and a partial write would leave `__version__`
    # bumped without a matching section — exactly the inconsistency
    # `release.yml` then refuses to publish.
    aujourdhui = datetime.date.today().isoformat()
    nouveau_init = VERSION_RE.sub(
        f'__version__ = "{version}"', INIT.read_text(encoding="utf-8"), count=1
    )
    nouveau_changelog = maj_changelog(
        CHANGELOG.read_text(encoding="utf-8"), version, aujourdhui
    )
    texte_pyproject = PYPROJECT.read_text(encoding="utf-8")
    if PYPROJECT_RE.search(texte_pyproject) is None:
        raise SystemExit(f"version introuvable ou mal formée dans {PYPROJECT}")
    nouveau_pyproject = PYPROJECT_RE.sub(f'version = "{version}"', texte_pyproject, count=1)

    INIT.write_text(nouveau_init, encoding="utf-8")
    CHANGELOG.write_text(nouveau_changelog, encoding="utf-8")
    PYPROJECT.write_text(nouveau_pyproject, encoding="utf-8")

    ancienne = ".".join(str(n) for n in actuelle)
    print(f"{ancienne} -> {version}", file=sys.stderr)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

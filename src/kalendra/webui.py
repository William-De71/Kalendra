"""Shared chrome for the two web interfaces: sidebar, shell, common styles.

The admin UI and the calendar view are separate modules but the same site to
the person using them. Each used to build its own `<header>`, which drifted:
links styled as buttons on one side and as plain links on the other, and a bar
whose height changed from page to page because its contents did.

A fixed sidebar fixes both: navigation lives in one place, always the same
width and the same order, so only the panel on the right changes when moving
between pages.
"""

from __future__ import annotations

from html import escape

# Palette and primitives shared by both interfaces. Page-specific rules stay in
# their own module; anything here is what makes the two look like one site.
BASE_STYLE = """
:root { color-scheme: light dark; --bg:#fbfbfd; --fg:#16161d; --muted:#606070;
  --line:#dcdce4; --card:#fff; --accent:#3054c8; --danger:#b3261e;
  --rail:#f4f4f8; --rail-fg:#3a3a48; --rail-active:#e9e9f2; --nav:248px; }
@media (prefers-color-scheme: dark) { :root { --bg:#15151a; --fg:#e9e9ef;
  --muted:#a0a0b0; --line:#2c2c36; --card:#1d1d24; --accent:#8aa6ff;
  --danger:#f2836b; --rail:#111117; --rail-fg:#c9c9d6; --rail-active:#23232e; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 ui-sans-serif,
  system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color:var(--accent); }

/* The rail is fixed so that a long month grid scrolls under it rather than
   pushing the navigation off-screen. */
.rail { position:fixed; inset:0 auto 0 0; width:var(--nav); background:var(--rail);
  border-right:1px solid var(--line); display:flex; flex-direction:column;
  padding:18px 12px; gap:4px; overflow-y:auto; }
.rail .marque { display:flex; align-items:center; gap:10px; padding:6px 10px 18px;
  font-weight:650; font-size:16px; color:var(--fg); text-decoration:none; }
.rail .marque .point { width:22px; height:22px; border-radius:7px; flex:none;
  background:var(--accent); }
.rail a.item { display:flex; align-items:center; gap:11px; padding:9px 11px;
  border-radius:9px; color:var(--rail-fg); text-decoration:none; font-size:14.5px; }
.rail a.item:hover { background:var(--rail-active); color:var(--fg); }
.rail a.item[aria-current] { background:var(--rail-active); color:var(--fg);
  font-weight:600; }
.rail a.item svg { width:18px; height:18px; flex:none; stroke:currentColor;
  fill:none; stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round; }
.rail .groupe { color:var(--muted); font-size:11.5px; text-transform:uppercase;
  letter-spacing:.07em; padding:16px 11px 6px; }
.rail .compte { margin-top:auto; display:flex; align-items:center; gap:10px;
  padding:12px 10px 4px; border-top:1px solid var(--line); }
/* Initial in a disc rather than a photo: Kalendra stores no avatar, and a
   letter identifies the account well enough to catch a wrong login. */
.rail .compte .jeton { width:30px; height:30px; border-radius:50%; flex:none;
  background:var(--accent); color:#fff; display:flex; align-items:center;
  justify-content:center; font-size:13px; font-weight:650; text-transform:uppercase; }
.rail .compte .qui { min-width:0; line-height:1.25; }
.rail .compte .nom { color:var(--fg); font-size:13.5px; font-weight:600;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rail .compte .role { color:var(--muted); font-size:11.5px; }
.rail .compte .version { margin-left:auto; color:var(--muted); font-size:11px; }

.zone { margin-left:var(--nav); min-height:100vh; }
/* Fixed height regardless of what a page puts in it: the bar must not jump
   when moving between pages. */
.titre { height:60px; display:flex; align-items:center; gap:12px; padding:0 24px;
  border-bottom:1px solid var(--line); background:var(--card); }
.titre h1 { margin:0; font-size:17px; letter-spacing:-.01em; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.titre .meta { color:var(--muted); font-size:13px; white-space:nowrap; }
.titre .actions { margin-left:auto; display:flex; gap:8px; align-items:center; }
.titre .actions a { text-decoration:none; border:1px solid var(--line);
  border-radius:8px; padding:5px 11px; color:var(--fg); background:var(--bg);
  font-size:13.5px; white-space:nowrap; }
.titre .actions a:hover { border-color:var(--accent); color:var(--accent); }
.pastille { width:12px; height:12px; border-radius:3px; display:inline-block;
  flex:none; }

/* Below the rail's width the sidebar becomes a horizontal strip: a phone has
   no room for a 248px column next to a seven-day grid. */
@media (max-width: 720px) {
  .rail { position:static; width:auto; flex-direction:row; flex-wrap:wrap;
    padding:10px 12px; border-right:none; border-bottom:1px solid var(--line); }
  .rail .marque { padding:0 10px 0 0; }
  .rail .groupe { display:none; }
  .rail .compte { margin-top:0; border-top:none; padding:0 0 0 6px; }
  .zone { margin-left:0; }
  .titre { height:auto; padding:12px 16px; flex-wrap:wrap; }
}
"""

# Inline SVG rather than an icon font or emoji: no network request, and the
# glyphs inherit the link colour so the active state needs no second rule.
_ICONES = {
    "agenda": "<rect x='3' y='4.5' width='18' height='16' rx='2.5'/>"
    "<path d='M8 2.5v4M16 2.5v4M3 9.5h18'/>",
    "admin": "<path d='M12 2.5 4 6v5.5c0 4.6 3.2 8.6 8 10 4.8-1.4 8-5.4 8-10V6l-8-3.5Z'/>",
}


def _icone(nom: str) -> str:
    return f"<svg viewBox='0 0 24 24' aria-hidden='true'>{_ICONES[nom]}</svg>"


def _item(href: str, icone: str, texte: str, actif: bool) -> str:
    marque = " aria-current='page'" if actif else ""
    return f"<a class=item href='{href}'{marque}>{_icone(icone)}<span>{texte}</span></a>"


def rail(base: str, actif: str, admin: bool, username: str = "", version: str = "") -> str:
    """Sidebar shared by both interfaces.

    `actif` names the current section ("agendas" or "admin") so the entry is
    highlighted. Administration is omitted for a plain user: /admin
    would answer 403, and showing a door that does not open is worse than
    showing none.
    """
    # /view/ lists calendars and address books on one page, so a separate
    # "Contacts" entry would point at the same place as "Agendas".
    items = [
        f"<a class=marque href='{base}/view/'><span class=point></span>Kalendra</a>",
        _item(f"{base}/view/", "agenda", "Agendas et contacts", actif == "agendas"),
    ]
    if admin:
        items.append(_item(f"{base}/admin", "admin", "Administration", actif == "admin"))
    if username:
        role = "Administrateur" if admin else "Utilisateur"
        items.append(
            "<div class=compte>"
            f"<span class=jeton aria-hidden='true'>{escape(username[:1])}</span>"
            f"<span class=qui><span class=nom>{escape(username)}</span>"
            f"<div class=role>{role}</div></span>"
            + (f"<span class=version>v{escape(version)}</span>" if version else "")
            + "</div>"
        )
    return f"<nav class=rail>{''.join(items)}</nav>"


def titre(contenu: str, actions: str = "") -> str:
    """Fixed-height page header, to the right of the rail."""
    bloc = f"<div class=actions>{actions}</div>" if actions else ""
    return f"<div class=titre>{contenu}{bloc}</div>"

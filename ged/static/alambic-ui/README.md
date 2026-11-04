# Alambic UI — charte commune d'Alambic Search et d'Alambic Light

Maquette de référence : « Charte Alambic — Light & Search » (bandeau, menu, couleurs, typographie,
composants). Ce dossier est la source de vérité : copié tel quel dans chaque application.

| Fichier | Rôle |
|---|---|
| `alambic.css` | Jetons (`--al-*`), bandeau, sélecteur d'applications, menu latéral, boutons, statuts, aides |
| `alambic.js` | Facultatif : un seul menu ouvert, fermeture au clic extérieur et sur Échap |
| `bandeau.html` | Bandeau et menu à reprendre dans chaque page |
| `img/arondor.png`, `img/arondor-blanc.png` | Logo d'Arondor : original (fonds clairs : pied du menu, connexion, portail) et version claire (bandeau sombre) |
| `fonts/` | IBM Plex Sans/Mono, Comfortaa (marque) — licence OFL, aucune ressource externe |

## Règles

- Logo de la société : `img/arondor-blanc.png` dans le bandeau (classe `al-company`, à droite, avant le
  client) ; `img/arondor.png` sur fond clair : pied du menu (`al-signature`), pages de connexion et de
  portail (`<p class="al-powered">Une solution <img …></p>`).
- Bandeau identique partout : marque « alambic » + produit (search, light), « Applications »,
  client, aide, compte. Seul le nom du produit change.
- « Applications » : liens groupés par produit, affichés selon les rôles Keycloak
  (`alambic-search:*`, `alambic-light:*`) ; portail = page d'accueil d'Alambic Search.
- Menu latéral propre à chaque application, mêmes classes (`al-nav`, `al-nav-group`, `aria-current`).
- Couleurs : cuivre (`--al-accent`) pour l'action principale et l'élément actif ; statuts
  `al-ok` (indexé, validé), `al-warn` (à vérifier), `al-bad` (erreur), `al-info` (en cours),
  `al-muted` (non pris en charge) — jamais une couleur seule pour porter un sens : toujours un libellé.
- Un seul bouton primaire (`al-btn`) par écran ; les autres en `al-secondary`.
- Aides : `al-help` (bulle au survol, au clavier et au toucher), 340 px au plus.

## Intégration

```html
<link rel="stylesheet" href="/alambic-ui/alambic.css">
<script src="/alambic-ui/alambic.js" defer></script>
<body class="al-app"> bandeau.html … </body>
```

Content-Security-Policy : `style-src 'self'` (en plus de l'existant), `font-src 'self'`,
`script-src 'self'` pour alambic.js (ou ne pas l'inclure : les menus restent utilisables).

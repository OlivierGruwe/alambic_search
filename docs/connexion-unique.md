# Connexion unique (Keycloak) et portail Alambic

Une seule connexion pour Alambic Search, Alambic Light et le portail. Keycloak vérifie
l'identité ; chaque application reçoit un jeton signé et en déduit les droits.

```
navigateur ──► portail / Alambic Search ──┐
          └──► Alambic Light ─────────────┼──► Keycloak ──► AD du client (LDAP) ou Entra ID
                                          │
            jetons signés (OIDC) ◄────────┘
```

## Essai sur le poste

1. `make sso` : Keycloak sur http://localhost:8180, royaume `alambic` importé
   (`keycloak/realm-alambic.json`), comptes `olivier`, `alice`, `bob`, mot de passe `demo`.
2. `.env` :
   ```
   OIDC_ISSUER=http://localhost:8180/realms/alambic
   OIDC_CLIENT_SECRET=dev-alambic-search-a-changer
   PUBLIC_URL=http://localhost:8002
   ```
   avec `make run`, ajouter `OIDC_INTERNAL_URL=http://localhost:8180/realms/alambic`
   (dans Docker, `http://keycloak:8180/…` est déjà réglé).
3. `sources.yaml` : décommenter les sections `oidc` et `portail` de `sources.example.yaml`.
4. http://localhost:8002 → redirection vers Keycloak → portail.

Rôles du royaume de démonstration :

| Compte | Groupes | Rôles |
|---|---|---|
| olivier | direction, compta, rh, tous | alambic-search : admin ; alambic-light : admin, validation, lecture |
| alice | compta, tous | alambic-search : utilisateur ; alambic-light : validation |
| bob | rh, tous | alambic-search : utilisateur |

`alambic-search:admin` ouvre la console `/admin` sans mot de passe (ADMIN_PASSWORD reste un
secours). Les tuiles du portail suivent les rôles (`role:` dans la section `portail`).

## Chez un client : brancher Keycloak sur son annuaire

**AD sur site** — Keycloak → royaume `alambic` → *User federation* → *Add LDAP provider* :

- Vendor : *Active Directory* ; Connection URL : `ldaps://dc01.client.local` ;
- Bind DN : compte de service en lecture seule ; Users DN : `OU=Utilisateurs,DC=client,DC=local` ;
- Edit mode : **READ_ONLY** (Keycloak ne modifie jamais l'AD), Sync registrations : off ;
- *Mappers* → *Add mapper* → type `group-ldap-mapper`, Groups DN : `OU=Groupes,…`, mode READ_ONLY :
  les groupes AD arrivent dans le jeton (claim `groups`) ;
- facultatif : *Kerberos* (SPNEGO) pour la connexion Windows automatique sur les postes du domaine.

Alambic Search en `groupes: annuaire` relit de toute façon les groupes (et les SID) dans l'AD :
Keycloak ne sert alors qu'à authentifier. C'est le mode à utiliser avec les partages Windows
(droits NTFS = SID) et SharePoint.

**Entra ID (Microsoft 365)** — *Identity providers* → *Microsoft* (ou OpenID Connect v1.0), avec
une application déclarée dans Entra ID ; activer *Sync mode: force* et un mapper de groupes.
Les utilisateurs se connectent avec leur compte Microsoft, MFA comprise.

**Plusieurs clients sur une même plateforme (SaaS)** — un royaume par client, chacun branché sur
l'annuaire de ce client. Alambic Light retrouve le client à partir du royaume (émetteur du jeton).
Alambic Search hébergé sans accès à l'AD du client : `groupes: jeton`.

## Production

- Keycloak en mode `start` (pas `start-dev`), base PostgreSQL, HTTPS, `KC_HOSTNAME` = adresse
  publique ; changer les secrets des clients (`dev-…-a-changer`) et le mot de passe admin.
- Alambic Search : `url_publique` en HTTPS, `COOKIE_SECURE=true`, `SESSION_SECRET` fixe.
- Les adresses de retour (*Valid redirect URIs*) des clients doivent être exactes
  (`https://search.client.fr/auth/callback`), sans joker en production.
- Adresse de retour après déconnexion (*Valid post logout redirect URIs*) du client
  alambic-search : `https://search.client.fr/connexion*` (sinon « URI de redirection invalide »
  à la déconnexion). Le royaume n'est importé qu'au premier démarrage : une correction du fichier
  ne s'applique pas à un Keycloak déjà initialisé (la faire dans la console, ou `docker volume rm`
  du volume keycloak pour repartir de zéro).
- Déconnexion : « Se déconnecter » ferme la session Keycloak, donc toutes les applications.

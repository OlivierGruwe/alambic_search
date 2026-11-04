"""Ligne de commande : `python -m ged.cli sync <source>` (à planifier en cron) ou `ask`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict


def main(argv: list[str] | None = None) -> int:
    from ged.answer import ask
    from ged.config import Settings, load_analyzer, load_converter
    from ged.convert import OFFICE_EXTENSIONS
    from ged.embeddings import OpenAICompatEmbedder
    from ged.llm import load_llm
    from ged.store import PgStore
    from ged.sync import MassDeletionGuard, sync_source

    parser = argparse.ArgumentParser(prog="alambic-search")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="Synchronise une source avec l'index")
    p_sync.add_argument("source")
    sub.add_parser("light-check", help="Teste la connexion à Alambic Light (section alambic_light)")
    p_ask = sub.add_parser("ask", help="Pose une question")
    p_ask.add_argument("question")
    p_ask.add_argument("--groups", required=True, help="Groupes de l'utilisateur, séparés par des virgules")
    p_ask.add_argument("--extraits", action="store_true", help="Extraits pertinents, sans formulation par le LLM")
    p_re = sub.add_parser("revectoriser", help="Recalcule les vecteurs des passages indexés (sans relire les fichiers)")
    p_re.add_argument("--source", help="Limiter à une source")
    p_eval = sub.add_parser("eval", help="Mesure la qualité sur un jeu de questions de référence (YAML)")
    p_eval.add_argument("jeu", help="Fichier YAML du jeu de questions (voir ged/evaluation.py)")
    p_eval.add_argument("--reponses", action="store_true", help="Mesure aussi les réponses rédigées (LLM, plus lent)")
    p_eval.add_argument("--sortie", help="Enregistre la mesure (JSON), pour la comparer plus tard")
    p_eval.add_argument("--reference", help="Mesure précédente (JSON) : affiche écarts, régressions, progrès")
    p_eval.add_argument("--sans-reclassement", action="store_true",
                        help="Ignore RERANK_URL : mesure la recherche hybride seule (pour comparer)")
    p_gen = sub.add_parser("eval-generer", help="Génère des questions candidates (à relire) depuis l'index")
    p_gen.add_argument("--n", type=int, default=30, help="Nombre de questions (défaut 30)")
    p_gen.add_argument("--source", help="Limiter à une source")
    p_gen.add_argument("--sortie", required=True, help="Fichier YAML à écrire (brouillon)")
    p_acc = sub.add_parser("compte", help="Crée un compte, ou redéfinit son mot de passe (accès perdu à la console)")
    p_acc.add_argument("login")
    p_acc.add_argument("--role", choices=["utilisateur", "gestionnaire", "admin"],
                       help="Rôle (création : utilisateur par défaut ; compte existant : inchangé sauf si précisé)")
    p_acc.add_argument("--nom", default="", help="Nom affiché (création)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from ged.envfiles import load_env_files

    load_env_files()  # secrets Docker (NOM_FILE)
    s = Settings.from_env()
    if args.cmd == "light-check":
        from ged.config import load_analyzer

        analyzer = load_analyzer(s.sources_file)
        if analyzer is None:
            print("Pas de section alambic_light dans " + s.sources_file, file=sys.stderr)
            return 2
        result = analyzer.check()
        print(f"Alambic Light : {analyzer.url}")
        for label, ok, detail in result["etapes"]:
            print(f"  [{'ok' if ok else 'KO' if ok is False else '!!'}] {label} : {detail}")
        print("Connexion opérationnelle" if result["ok"] else "Connexion inutilisable")
        return 0 if result["ok"] else 1

    store = PgStore(s.database_url, s.embed_dim)
    store.init()
    if args.cmd == "compte":
        return _account(store, args)
    embedder = OpenAICompatEmbedder(s.embed_base_url, s.embed_model, s.embed_dim)

    if args.cmd == "sync":
        import os

        from ged.config import build_connector, read_sources, source_extensions
        from ged.secret_box import SecretBox
        from ged.sources import bootstrap, load_sources, runtime_config, tag

        converter = load_converter(s)
        extensions = source_extensions(s.sources_file, OFFICE_EXTENSIONS if converter else frozenset())
        # Sources en base (gérées dans la console) ; sources.yaml importé au tout premier lancement
        bootstrap(store, read_sources(s.sources_file) if os.path.isfile(s.sources_file) else [])
        box = SecretBox.from_env()  # secrets saisis dans la console (chiffrés en base)
        connectors, errors, _ = load_sources(
            store, lambda cfg: tag(build_connector(runtime_config(store, box, cfg), extensions), cfg))
        if args.source in errors:
            print(f"Source {args.source} inaccessible : {errors[args.source]}", file=sys.stderr)
            return 2
        if args.source not in connectors:
            print(f"Source inconnue : {args.source} (sources : {', '.join(sorted(connectors)) or 'aucune'})",
                  file=sys.stderr)
            return 2
        lock = store.sync_lock(args.source)  # partagé avec l'API et le worker (Swarm)
        if lock is None:
            print(f"Synchronisation de {args.source} déjà en cours (API, worker ou autre commande)", file=sys.stderr)
            return 4
        try:
            report = sync_source(connectors[args.source], store, embedder, analyzer=load_analyzer(s.sources_file),
                                 converter=converter)
        except MassDeletionGuard as exc:
            print(str(exc), file=sys.stderr)
            return 3
        finally:
            lock.release()
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 1 if report.errors else 0

    if args.cmd == "revectoriser":
        from ged.sync import reembed

        def step(n, path):
            if n % 50 == 0:
                print(f"  {n} documents… ({path})", file=sys.stderr)

        n = reembed(store, embedder, args.source, progress=step)
        print(f"{n} document(s) revectorisé(s)")
        return 0

    if args.cmd in ("eval", "eval-generer"):
        return _evaluation(args, s, store, embedder)

    groups = frozenset(g.strip() for g in args.groups.split(",") if g.strip())
    llm = load_llm(s)
    from ged.rerank import load_reranker

    answer = ask(args.question, groups, store, embedder, llm, generate=not args.extraits, reranker=load_reranker())
    print(answer.text)
    for c in answer.citations:
        print(f"  [{c.n}] {c.path} (page {c.page})")
        if not answer.generated:
            print("      " + answer.hits[c.n - 1].text[:300].replace("\n", " "))
    return 0


def _evaluation(args, s, store, embedder) -> int:
    import os

    from ged import evaluation as ev
    from ged.config import load_identity
    from ged.llm import load_llm

    llm = load_llm(s)
    if args.cmd == "eval-generer":
        if llm is None:
            print("Génération impossible sans LLM (LLM_MODEL vide)", file=sys.stderr)
            return 2
        stats: dict = {}
        items = ev.generate_candidates(store, llm, n=args.n, source=args.source, embedder=embedder, stats=stats)
        print(f"Écartés : {stats['copies']} passage(s) de documents déjà interrogés (ou de leurs copies), "
              f"{stats['texte_type']} texte(s) type (présents dans plus de {ev.MAX_EQUIVALENTS + 1} documents), "
              f"{stats['doublon']} question(s) en double, {stats['renvoi']} question(s) en « ce document », "
              f"{stats['inexploitable']} passage(s) inexploitable(s)", file=sys.stderr)
        if len(items) < args.n:
            print(f"Seulement {len(items)} question(s) sur {args.n} : l'index contient peu de documents distincts",
                  file=sys.stderr)
        if not items:
            print("Aucune question générée : index vide, passages trop courts (< 200 caractères) "
                  "ou LLM injoignable", file=sys.stderr)
            return 1
        with open(args.sortie, "w", encoding="utf-8") as f:
            f.write(ev.candidates_yaml(items))
        print(f"{len(items)} question(s) candidate(s) dans {args.sortie} : à relire avant usage")
        return 0
    if not os.path.isfile(args.jeu):
        print(f"Jeu de questions introuvable : {args.jeu}\n"
              "Le plus rapide : make eval-init N=30 (questions générées depuis l'index, à relire ensuite)\n"
              "Ou, en relisant d'abord :\n"
              "  1. make eval-generer N=30      → questions candidates tirées de l'index (eval/candidates.yaml)\n"
              "  2. les relire, garder les bonnes, ajouter les vôtres (modèle : eval/jeu.exemple.yaml)\n"
              f"  3. enregistrer le résultat sous {args.jeu}, puis make eval", file=sys.stderr)
        return 2
    try:
        cases = ev.load_cases(args.jeu)
    except (OSError, ev.EvalError) as exc:
        print(f"Jeu de questions invalide : {exc}", file=sys.stderr)
        return 2
    if args.reponses and llm is None:
        print("--reponses ignoré : pas de LLM (LLM_MODEL vide)", file=sys.stderr)
    identity = load_identity(s.sources_file) if os.path.isfile(s.sources_file) else None
    logging.getLogger().setLevel(logging.WARNING)

    def progress(i, n, res):
        mark = "ERREUR" if res.erreur else ("FUITE" if res.fuites else
                                             ("rang " + str(res.rang) if res.rang else "—" if res.trouve is None else
                                              f"absent (rang {res.rang_etendu})" if res.rang_etendu else
                                              "absent (au-delà de 50)"))
        print(f"  [{i}/{n}] {res.id} : {mark}", file=sys.stderr)

    from ged.rerank import load_reranker

    reranker = None if args.sans_reclassement else load_reranker()
    print(f"Reclassement : {reranker.describe() if reranker else 'aucun (recherche hybride seule)'}", file=sys.stderr)
    result = ev.evaluate(cases, store, embedder, llm, identity, with_answers=args.reponses, progress=progress,
                         reranker=reranker)
    comparison = None
    if args.reference:
        with open(args.reference, encoding="utf-8") as f:
            comparison = ev.compare(result, json.load(f))
    print(ev.format_report(result, comparison))
    if args.sortie:
        ev.save(result, args.sortie)
        print(f"\nMesure enregistrée : {args.sortie}")
    # code retour : 1 en cas de fuite de droits ou de régression (utilisable en intégration continue)
    return 1 if result["resume"]["fuites"] or (comparison and comparison["regressions"]) else 0


if __name__ == "__main__":
    sys.exit(main())


def _account(store, args, read_password=None) -> int:
    """Crée le compte ou redéfinit son mot de passe (saisi deux fois, jamais en argument)."""
    import getpass

    from ged.accounts import AccountError, Accounts
    from ged.models import Role

    read_password = read_password or getpass.getpass
    accounts = Accounts(store)
    password = read_password("Mot de passe (10 caractères au moins) : ")
    if password != read_password("Confirmation : "):
        print("Les deux saisies diffèrent.", file=sys.stderr)
        return 2
    try:
        user = store.get_user(args.login)
        if user is None:
            user = accounts.create(args.login, args.nom, args.role or Role.USER, password)
            print(f"Compte « {user.login} » créé ({Role.LABELS[user.role]}).")
            return 0
        accounts.set_password(user, password)
        if args.role and args.role != user.role:
            user.role = args.role
        user.active = True  # compte de secours : réactivé
        store.update_user(user)
        print(f"Compte « {user.login} » : mot de passe redéfini ({Role.LABELS[user.role]}, actif).")
        return 0
    except AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 2

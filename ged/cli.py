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
    from ged.llm import OpenAICompatLLM
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
    p_eval = sub.add_parser("eval", help="Mesure la qualité sur un jeu de questions de référence (YAML)")
    p_eval.add_argument("jeu", help="Fichier YAML du jeu de questions (voir ged/evaluation.py)")
    p_eval.add_argument("--reponses", action="store_true", help="Mesure aussi les réponses rédigées (LLM, plus lent)")
    p_eval.add_argument("--sortie", help="Enregistre la mesure (JSON), pour la comparer plus tard")
    p_eval.add_argument("--reference", help="Mesure précédente (JSON) : affiche écarts, régressions, progrès")
    p_gen = sub.add_parser("eval-generer", help="Génère des questions candidates (à relire) depuis l'index")
    p_gen.add_argument("--n", type=int, default=30, help="Nombre de questions (défaut 30)")
    p_gen.add_argument("--source", help="Limiter à une source")
    p_gen.add_argument("--sortie", required=True, help="Fichier YAML à écrire (brouillon)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
        try:
            report = sync_source(connectors[args.source], store, embedder, analyzer=load_analyzer(s.sources_file),
                                 converter=converter)
        except MassDeletionGuard as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 1 if report.errors else 0

    if args.cmd in ("eval", "eval-generer"):
        return _evaluation(args, s, store, embedder)

    groups = frozenset(g.strip() for g in args.groups.split(",") if g.strip())
    llm = OpenAICompatLLM(s.llm_base_url, s.llm_model) if s.llm_model else None
    answer = ask(args.question, groups, store, embedder, llm, generate=not args.extraits)
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
    from ged.llm import OpenAICompatLLM

    llm = OpenAICompatLLM(s.llm_base_url, s.llm_model) if s.llm_model else None
    if args.cmd == "eval-generer":
        if llm is None:
            print("Génération impossible sans LLM (LLM_MODEL vide)", file=sys.stderr)
            return 2
        items = ev.generate_candidates(store, llm, n=args.n, source=args.source)
        with open(args.sortie, "w", encoding="utf-8") as f:
            f.write(ev.candidates_yaml(items))
        print(f"{len(items)} question(s) candidate(s) dans {args.sortie} : à relire avant usage")
        return 0
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
                                             ("rang " + str(res.rang) if res.rang else "—" if res.trouve is None else "absent"))
        print(f"  [{i}/{n}] {res.id} : {mark}", file=sys.stderr)

    result = ev.evaluate(cases, store, embedder, llm, identity, with_answers=args.reponses, progress=progress)
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

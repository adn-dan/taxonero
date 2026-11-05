"""Ligne de commande.

    taxonero check                 vérifie la compatibilité avec le serveur Immich
    taxonero scan                  enregistre les photos nouvelles ou modifiées
    taxonero analyze [--limit N]   détecte et identifie les espèces
    taxonero sync [--apply]        écrit tags et descriptions dans Immich (dry-run sans --apply)
    taxonero uninstall [--apply]   retire d'Immich tout ce que l'outil y a posé
    taxonero calibrate             seuil d'auto-validation mesuré sur tes relectures
    taxonero reanalyze SCOPE       remet en file : failed (abandonnées), outdated (autre version des modèles), all
    taxonero serve                 dashboard + boucle automatique (édition serveur, mot de passe)
    taxonero desktop               édition de bureau : fenêtre locale, sans compte (voir desktop.py)
"""

import argparse
import json
import logging
import threading

from . import logbuffer
from .config import Settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="taxonero")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "scan", "calibrate", "serve", "desktop"):
        sub.add_parser(name)
    p = sub.add_parser("analyze")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("reanalyze", help="remettre des photos dans la file d'analyse (détections relues conservées)")
    p.add_argument("scope", choices=("failed", "outdated", "all"))
    for name in ("sync", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--apply", action="store_true", help="écrire réellement (sinon dry-run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # « unauthenticated requests to the HF Hub » à chaque lancement : les modèles publics n'ont pas besoin de jeton.
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
    s = Settings()
    if args.cmd == "desktop":
        from . import desktop
        desktop.run(s)   # dossier de données propre au bureau, journal branché ensuite
        return
    logbuffer.install(s)
    if args.cmd in ("sync", "uninstall"):
        s.dry_run = not args.apply

    from .autoconfirm import calibrate
    from .worker import Worker
    w = Worker(s)

    if args.cmd == "check":
        ok = w.check()
        print(json.dumps({"version": w.client.version, "writable": ok, "errors": w.client.errors,
                          "warnings": w.client.warnings}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if ok else 1)
    if args.cmd == "scan":
        w.check()
        print(f"{w.scan()} photos nouvelles ou modifiées")
    elif args.cmd == "analyze":
        w.check()
        print(f"{w.analyze(args.limit)} photos analysées")
    elif args.cmd == "sync":
        w.check()
        print(w.sync(), "(dry-run)" if s.dry_run else "")
    elif args.cmd == "uninstall":
        from .sync import Syncer
        if not w.check():
            raise SystemExit("serveur non validé : uninstall refusé")
        syncer = Syncer(w.db, w.client, s.tag_root, s.names_lang)
        if not syncer.same_server():
            raise SystemExit("aucun des tags écrits n'existe sur ce serveur (autre Immich ou autre compte ?) : uninstall refusé")
        print(syncer.uninstall(), f"photos restantes : {syncer.remaining}", "(dry-run)" if s.dry_run else "")
    elif args.cmd == "reanalyze":
        print(f"{w.requeue(args.scope)} photo(s) remise(s) dans la file d'analyse")
    elif args.cmd == "calibrate":
        print(json.dumps(calibrate(w.db, s.auto_confirm_target_precision, s.auto_confirm_min_reviews), indent=2))
    elif args.cmd == "serve":
        import uvicorn

        from .web import create_app
        threading.Thread(target=w.loop, daemon=True, name="worker").start()
        # log_config=None : les erreurs du serveur web passent par le journal commun (donc aussi par /api/logs).
        uvicorn.run(create_app(s, w), host=s.dashboard_host, port=s.dashboard_port, log_level="warning", log_config=None)


if __name__ == "__main__":
    main()

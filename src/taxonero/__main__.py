"""Command line.

    taxonero check                 checks compatibility with the Immich server
    taxonero scan                  records new or changed photos
    taxonero analyze [--limit N]   detects and identifies the species
    taxonero sync [--apply]        writes tags and descriptions to Immich (dry-run without --apply)
    taxonero uninstall [--apply]   removes from Immich everything the tool set there
    taxonero calibrate             auto-confirmation threshold measured on your reviews
    taxonero reanalyze SCOPE       puts back in the queue: failed (given up), outdated (other model version), all
    taxonero serve                 dashboard + automatic loop (server edition, password)
    taxonero desktop               desktop edition: local window, no account (see desktop.py)
    taxonero desktop --fetch-models  only downloads the models (desktop installation)
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
    for name in ("check", "scan", "calibrate", "serve"):
        sub.add_parser(name)
    p = sub.add_parser("desktop")
    p.add_argument("--fetch-models", action="store_true", help="download the models, then quit")
    p = sub.add_parser("analyze")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("reanalyze", help="put photos back in the analysis queue (reviewed detections kept)")
    p.add_argument("scope", choices=("failed", "outdated", "all"))
    for name in ("sync", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--apply", action="store_true", help="really write (otherwise dry-run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # "unauthenticated requests to the HF Hub" at every launch: public models need no token.
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
    s = Settings()
    if args.cmd == "desktop":
        from . import desktop
        if args.fetch_models:
            from .worker import fetch_models
            fetch_models(desktop.prepare(s))
            return
        desktop.run(s)   # data folder specific to the desktop, log plugged in afterwards
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
        print(f"{w.scan()} new or changed photos")
    elif args.cmd == "analyze":
        w.check()
        print(f"{w.analyze(args.limit)} photos analysed")
    elif args.cmd == "sync":
        w.check()
        print(w.sync(), "(dry-run)" if s.dry_run else "")
    elif args.cmd == "uninstall":
        from .sync import Syncer
        if not w.check():
            raise SystemExit("server not validated: uninstall refused")
        syncer = Syncer(w.db, w.client, s.tag_root, s.names_lang)
        if not syncer.same_server():
            raise SystemExit("none of the written tags exists on this server (another Immich or another account?): uninstall refused")
        print(syncer.uninstall(), f"remaining photos: {syncer.remaining}", "(dry-run)" if s.dry_run else "")
    elif args.cmd == "reanalyze":
        print(f"{w.requeue(args.scope)} photo(s) put back in the analysis queue")
    elif args.cmd == "calibrate":
        print(json.dumps(calibrate(w.db, s.auto_confirm_target_precision, s.auto_confirm_min_reviews), indent=2))
    elif args.cmd == "serve":
        import uvicorn

        from .web import create_app
        threading.Thread(target=w.loop, daemon=True, name="worker").start()
        # log_config=None: web server errors go through the common log (hence also through /api/logs).
        uvicorn.run(create_app(s, w), host=s.dashboard_host, port=s.dashboard_port, log_level="warning", log_config=None)


if __name__ == "__main__":
    main()

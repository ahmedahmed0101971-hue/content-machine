"""CLI:  python -m machine <command>

  web                 run the dashboard
  worker              run the background worker
  import FILE.json    add a package
  preview FILE.json   render a silent preview video (no Google needed)
  selftest            check the machine can render
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "web":
        from .web.app import main as web_main
        web_main()
    elif cmd == "worker":
        from .worker import main as worker_main
        worker_main()
    elif cmd == "import":
        from . import pipeline
        print(pipeline.import_local(argv[2]))
    elif cmd == "preview":
        from . import config, pipeline
        with open(argv[2], encoding="utf-8") as f:
            pkg = json.load(f)
        out = pipeline.render_package({"id": "preview-" + pkg["id"], "channel": pkg["channel"], "data": pkg}, with_voice=False)
        print(out["video"])
    elif cmd == "selftest":
        return selftest()
    else:
        print(__doc__)
    return 0


def selftest() -> int:
    import shutil
    import tempfile
    from . import config
    from .render.video import VideoRenderer, build_timeline
    ok = True
    for tool in ("ffmpeg", "ffprobe", "pdftotext"):
        found = shutil.which(tool) is not None
        print(f"{'OK ' if found else 'NO '} {tool}")
        ok &= found
    s = config.load_settings()
    pkg = {"id": "selftest", "channel": "money", "format": "short", "sources": [{"url": "https://www.ssa.gov"}],
           "scenes": [{"type": "headline", "vo": "Self test.", "lines": [{"text": "SELF TEST", "color": "accent"}]}],
           "youtube": {"title": "t"}}
    r = VideoRenderer(pkg, s["channels"]["money"]["theme"], build_timeline([1.0]))
    with tempfile.TemporaryDirectory() as d:
        try:
            r.render(f"{d}/t.mp4")
            print("OK  render")
        except Exception as e:
            print("NO  render:", e)
            ok = False
    print("ALL GOOD" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

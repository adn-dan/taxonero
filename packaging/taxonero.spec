# PyInstaller recipe of the desktop edition launcher (see bootstrap.py): a small executable that contains uv,
# the taxonero wheel, constraints.txt and ExifTool, and installs PyTorch for the machine's GPU at first
# launch.
#   python packaging/prepare.py && pyinstaller packaging/taxonero.spec --noconfirm
# Windows and Linux: a single file (taxonero.exe, taxonero). macOS: Taxonero.app (a folder, like any Mac
# application; PyInstaller advises against a single file inside an .app).
import sys
from pathlib import Path

BUILD = Path(SPECPATH) / "build"
ICONS = Path(SPECPATH) / "icons"
VERSION = next(line.split('"')[1] for line in (Path(SPECPATH).parent / "pyproject.toml").read_text().splitlines()
               if line.startswith("version"))
datas = [(str(BUILD / d), d) for d in ("uv", "wheel", "exiftool") if (BUILD / d).exists()]
datas.append((str(BUILD / "constraints.txt"), "."))

a = Analysis([str(Path(SPECPATH) / "bootstrap.py")], datas=datas)
pyz = PYZ(a.pure)
# Without a console: the first installation (a few minutes) is shown in a Tk window, then the application
# opens in its own.
if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, exclude_binaries=True, name="taxonero", console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="taxonero")
    app = BUNDLE(coll, name="Taxonero.app", icon=str(ICONS / "taxonero.icns"), bundle_identifier="io.github.adn-dan.taxonero",
                 version=VERSION, info_plist={"CFBundleDisplayName": "Taxonero", "CFBundleShortVersionString": VERSION,
                                              "NSHighResolutionCapable": True,
                                              # Apple Photos source: keywords and description written by Photos, through
                                              # AppleScript; macOS shows this reason when it asks for the permission.
                                              "NSAppleEventsUsageDescription":
                                                  "Taxonero writes the species found as keywords and description in Photos."})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="taxonero", console=False,
              icon=str(ICONS / "taxonero.ico") if sys.platform == "win32" else None)

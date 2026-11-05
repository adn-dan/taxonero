# Recette PyInstaller du lanceur de l'édition de bureau (voir bootstrap.py) : un petit exécutable qui contient uv,
# la roue de taxonero, constraints.txt et ExifTool, et installe PyTorch pour le GPU de la machine au premier
# lancement.
#   python packaging/prepare.py && pyinstaller packaging/taxonero.spec --noconfirm
# Windows et Linux : un seul fichier (taxonero.exe, taxonero). macOS : Taxonero.app (dossier, comme toute application
# Mac ; PyInstaller déconseille le fichier unique dans une .app).
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
# Sans console : la première installation (quelques minutes) s'affiche dans une fenêtre Tk, puis l'application
# s'ouvre dans la sienne.
if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, exclude_binaries=True, name="taxonero", console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="taxonero")
    app = BUNDLE(coll, name="Taxonero.app", icon=str(ICONS / "taxonero.icns"), bundle_identifier="io.github.adn-dan.taxonero",
                 version=VERSION, info_plist={"CFBundleDisplayName": "Taxonero", "CFBundleShortVersionString": VERSION,
                                              "NSHighResolutionCapable": True})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="taxonero", console=False,
              icon=str(ICONS / "taxonero.ico") if sys.platform == "win32" else None)

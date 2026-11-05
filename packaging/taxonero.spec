# Recette PyInstaller du lanceur de l'édition de bureau (voir bootstrap.py) : un petit exécutable qui contient uv,
# la roue de taxonero, constraints.txt et ExifTool, et installe PyTorch pour le GPU de la machine au premier
# lancement.
#   python packaging/prepare.py && pyinstaller packaging/taxonero.spec --noconfirm
from pathlib import Path

BUILD = Path(SPECPATH) / "build"
datas = [(str(BUILD / d), d) for d in ("uv", "wheel", "exiftool") if (BUILD / d).exists()]
datas.append((str(BUILD / "constraints.txt"), "."))

a = Analysis([str(Path(SPECPATH) / "bootstrap.py")], datas=datas)
pyz = PYZ(a.pure)
# Sans console : la première installation (quelques minutes) s'affiche dans une fenêtre Tk, puis l'application
# s'ouvre dans la sienne.
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="taxonero", console=False, icon=None)

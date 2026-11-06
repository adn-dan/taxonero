#!/bin/sh
# Linux packages of the desktop launcher: .deb (Debian, Ubuntu, Mint), .rpm (Fedora, openSUSE) and AppImage (any
# distribution). They contain the same launcher (dist/taxonero, made by PyInstaller), a menu entry and the icon.
#
#   sh packaging/linux-packages.sh dist/taxonero 0.1.0 out/
#
# Needs dpkg-deb, rpmbuild and file (Ubuntu: apt install rpm file). The architecture is that of the machine (x86_64 or aarch64).
# appimagetool and the AppImage runtime are downloaded at a pinned version, checksums verified.
set -eu
bin=$1; version=$2; out=$3
here=$(cd "$(dirname "$0")" && pwd)
arch=$(uname -m)
case "$arch" in
  x86_64)  deb_arch=amd64 ;;
  aarch64) deb_arch=arm64 ;;
  *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac
mkdir -p "$out"
out=$(cd "$out" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Common tree: /usr/bin/taxonero, menu entry, icon.
root=$work/root
install -Dm755 "$bin" "$root/usr/bin/taxonero"
install -Dm644 "$here/icons/taxonero.png" "$root/usr/share/icons/hicolor/512x512/apps/taxonero.png"
install -Dm644 "$here/icons/taxonero.svg" "$root/usr/share/icons/hicolor/scalable/apps/taxonero.svg"
install -Dm644 "$here/taxonero.desktop" "$root/usr/share/applications/taxonero.desktop"

# System libraries used by the application window (Qt WebEngine, installed at first launch) and
# sometimes missing on a desktop installation: recommended, not required.
maintainer="Dan <245003188+adn-dan@users.noreply.github.com>"
summary="Self-hosted AI species identification for your photos"

# .deb
mkdir -p "$work/deb"
cp -a "$root/." "$work/deb/"
mkdir -p "$work/deb/DEBIAN"
cat > "$work/deb/DEBIAN/control" <<EOF
Package: taxonero
Version: $version
Architecture: $deb_arch
Maintainer: $maintainer
Section: graphics
Priority: optional
Homepage: https://github.com/adn-dan/taxonero
Recommends: libxcb-cursor0, libxkbcommon-x11-0, libnss3, libegl1
Description: $summary
 Identifies the species in your photos (birds, mammals, insects, plants, fungi...)
 from Immich, a local folder or a photo application. On first launch, installs
 PyTorch for the GPU of the machine in the user folder.
EOF
dpkg-deb --root-owner-group --build "$work/deb" "$out/taxonero_${version}_${deb_arch}.deb" >/dev/null

# .rpm
mkdir -p "$work/rpm/SPECS"
cat > "$work/rpm/SPECS/taxonero.spec" <<EOF
Name: taxonero
Version: $version
Release: 1
Summary: $summary
License: AGPL-3.0-only
URL: https://github.com/adn-dan/taxonero
Recommends: xcb-util-cursor, libxkbcommon-x11, nss, mesa-libEGL
AutoReqProv: no
%global debug_package %{nil}
%global __strip /bin/true

%description
Identifies the species in your photos (birds, mammals, insects, plants, fungi...)
from Immich, a local folder or a photo application. On first launch, installs
PyTorch for the GPU of the machine in the user folder.

%install
cp -a $root/. %{buildroot}/

%files
/usr/bin/taxonero
/usr/share/applications/taxonero.desktop
/usr/share/icons/hicolor/512x512/apps/taxonero.png
/usr/share/icons/hicolor/scalable/apps/taxonero.svg
EOF
# PyInstaller launcher: do not touch it (strip or build-id would break it).
rpmbuild -bb --quiet --define "_topdir $work/rpm" --define "_build_id_links none" \
  --define "__brp_strip /bin/true" --define "__os_install_post %{nil}" "$work/rpm/SPECS/taxonero.spec"
cp "$work/rpm/RPMS/$arch/taxonero-$version-1.$arch.rpm" "$out/"

# AppImage
tool_sha=$(case "$arch" in x86_64) echo ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0 ;;
                           aarch64) echo f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158 ;; esac)
runtime_sha=$(case "$arch" in x86_64) echo 2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d ;;
                              aarch64) echo 00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444 ;; esac)
curl -fsSL -o "$work/appimagetool" "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-$arch.AppImage"
curl -fsSL -o "$work/runtime" "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-$arch"
echo "$tool_sha  $work/appimagetool" | sha256sum -c - >/dev/null
echo "$runtime_sha  $work/runtime" | sha256sum -c - >/dev/null
chmod +x "$work/appimagetool"
app=$work/Taxonero.AppDir
cp -a "$root/." "$app/"
cp "$here/taxonero.desktop" "$app/"
cp "$here/icons/taxonero.png" "$app/taxonero.png"
ln -s usr/bin/taxonero "$app/AppRun"
# --appimage-extract-and-run: without FUSE (containers, build machines).
ARCH=$arch "$work/appimagetool" --appimage-extract-and-run --no-appstream --runtime-file "$work/runtime" \
  "$app" "$out/Taxonero-$version-$arch.AppImage" >/dev/null 2>&1
ls -la "$out"

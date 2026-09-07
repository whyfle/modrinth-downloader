#!/bin/bash
# Build modrinth-downloader release assets: RPM, DEB, tarball + checksums.
# Output: dist/<version>/ (asset filenames use vista-friendly arch tokens)
set -e
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION=$(grep '^version' pyproject.toml | head -n1 | cut -d'"' -f2)
OUTDIR="$ROOT/dist/$VERSION"
RPMBUILD="$ROOT/build/rpmbuild"

echo "==> modrinth-downloader $VERSION"
python3 -m pytest tests/ -q 2>&1 | tail -n 2

echo "==> RPM..."
rm -rf "$RPMBUILD"
mkdir -p "$RPMBUILD"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp packaging/rpm/modrinth-downloader.spec "$RPMBUILD/SPECS/"
# Standard tarball source for %setup
rm -rf /tmp/mdown-src && mkdir -p "/tmp/mdown-src/modrinth-downloader-$VERSION"
tar --exclude=./build --exclude=./dist --exclude=./__pycache__ --exclude=./.pytest_cache -cf - . | tar -xf - -C "/tmp/mdown-src/modrinth-downloader-$VERSION"
tar -czf "$RPMBUILD/SOURCES/modrinth-downloader-$VERSION.tar.gz" -C /tmp/mdown-src "modrinth-downloader-$VERSION"
rpmbuild --define "_topdir $RPMBUILD" -bb "$RPMBUILD/SPECS/modrinth-downloader.spec" 2>&1 | tail -n 3

echo "==> Collecting assets..."
rm -rf "$OUTDIR"; mkdir -p "$OUTDIR"
RPM=$(find "$RPMBUILD/RPMS" -name "*.rpm")
cp "$RPM" "$OUTDIR/modrinth-downloader-${VERSION}-x86_64.rpm"

echo "==> DEB (ar, no dpkg-deb needed)..."
DEBWORK="/tmp/mdown-deb"; rm -rf "$DEBWORK"; mkdir -p "$DEBWORK/DEBIAN" "$DEBWORK/usr/bin" "$DEBWORK/usr/share/doc/modrinth-downloader"
cat > "$DEBWORK/DEBIAN/control" <<EOF
Package: modrinth-downloader
Version: ${VERSION}-1
Section: games
Priority: optional
Architecture: amd64
Maintainer: Whyfle <whyfle@local>
Homepage: https://github.com/whyfle/modrinth-downloader
Description: Install Modrinth modpacks into the official Minecraft Launcher
 Bridges Modrinth modpacks (.mrpack) into the official launcher
 with hash verification and Vista-based system dependency setup.
EOF
cp modpacker.py "$DEBWORK/usr/bin/modrinth-downloader"
chmod 0755 "$DEBWORK/usr/bin/modrinth-downloader"
(cd "$DEBWORK" && find usr -type f -exec md5sum {} + > DEBIAN/md5sums)
echo "2.0" > "$DEBWORK/debian-binary"
tar -czf "$DEBWORK/control.tar.gz" -C "$DEBWORK/DEBIAN" control md5sums
tar -czf "$DEBWORK/data.tar.gz" -C "$DEBWORK" usr
ar r "$OUTDIR/modrinth-downloader-${VERSION}-amd64.deb" "$DEBWORK/debian-binary" "$DEBWORK/control.tar.gz" "$DEBWORK/data.tar.gz"

echo "==> Tarball..."
tar -czf "$OUTDIR/modrinth-downloader-${VERSION}.tar.gz" \
  modpacker.py modrinth-downloader.py modrinth_downloader.py README.md USER_GUIDE.md LICENSE requirements.txt

echo "==> Checksums..."
(cd "$OUTDIR" && sha256sum modrinth-downloader-* > checksums.txt && cat checksums.txt)
ls -lh "$OUTDIR"

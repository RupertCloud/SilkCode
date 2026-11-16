#!/bin/bash
# Build "Silk Code.app" and a .dmg of it.
#
#   packaging/macos/build-dmg.sh [--arch arm64|x86_64] [--wheel FILE] [--out DIR] [--app-only]
#
# The app bundles its own Python (python-build-standalone, pinned below) with
# Silk Code and its dependencies installed into it, so a Mac needs nothing
# installed beforehand. The bundle is assembled with the host's pip using
# --platform, so this runs on any OS; only the .dmg itself needs macOS
# (hdiutil). --app-only stops after the .app, for checking the bundle
# elsewhere. The wheel is built from this checkout unless --wheel is given.
#
# Output: DIR/SilkCode-<version>-<arch>.dmg (default DIR: dist)

set -euo pipefail

PBS_RELEASE=20250818
PBS_PYTHON=3.12.11
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_RELEASE"

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
python="${PYTHON:-python3}"

arch="$(uname -m)"
wheel=""
out="$root/dist"
app_only=0
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) arch="$2"; shift 2 ;;
    --wheel) wheel="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --app-only) app_only=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$arch" in
  arm64|aarch64)
    arch=arm64
    pbs_triple=aarch64-apple-darwin
    pbs_sha256=bbf0c85d09a8173e50d18a0198f14d1de91eab17a593ccf9445f214fb0555547
    pip_platform=macosx_11_0_arm64 ;;
  x86_64)
    pbs_triple=x86_64-apple-darwin
    pbs_sha256=296af6b9dd16f16dca2503a9a1cfc8593e4cd79ed19ee20cb2557da0912cf6b2
    pip_platform=macosx_11_0_x86_64 ;;
  *) echo "unsupported --arch $arch (arm64 or x86_64)" >&2; exit 2 ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ -z "$wheel" ]; then
  "$python" -m build --wheel --outdir "$work/wheel" "$root" >/dev/null
  wheel="$(ls "$work"/wheel/silkcode-*.whl)"
fi
version="$(basename "$wheel" | cut -d- -f2)"
# CFBundleShortVersionString is major.minor.patch; a dev build keeps the
# full version in CFBundleVersion.
short_version="$(echo "$version" | grep -o '^[0-9]*\.[0-9]*\.[0-9]*')"
echo "Silk Code $version for macOS $arch"

app="$work/Silk Code.app"
contents="$app/Contents"
mkdir -p "$contents/MacOS" "$contents/Resources/bin"

echo "fetching Python $PBS_PYTHON ($pbs_triple)"
tarball="$work/python.tar.gz"
curl -fsSL -o "$tarball" \
  "$PBS_BASE/cpython-$PBS_PYTHON+$PBS_RELEASE-$pbs_triple-install_only_stripped.tar.gz"
echo "$pbs_sha256  $tarball" | shasum -a 256 -c - >/dev/null
tar -xzf "$tarball" -C "$contents/Resources"     # -> Resources/python

echo "installing $(basename "$wheel") and its dependencies"
site="$contents/Resources/python/lib/python${PBS_PYTHON%.*}/site-packages"
"$python" -m pip install --quiet --no-compile --only-binary=:all: \
  --platform "$pip_platform" --python-version "${PBS_PYTHON%.*}" \
  --implementation cp --target "$site" "$wheel"
# pip --target also writes entry-point scripts and headers; the scripts point
# at the build host's Python, so neither belongs in the bundle.
rm -rf "$site/bin" "$site/include"

cp "$here/launcher.sh" "$contents/MacOS/SilkCode"
chmod 755 "$contents/MacOS/SilkCode"
cat >"$contents/Resources/bin/silkcode" <<'SHIM'
#!/bin/bash
# The silkcode CLI from the app's own Python. Symlink this into your PATH.
exec "$(cd "$(dirname "$0")/../python/bin" && pwd)/python3" -m silkcode "$@"
SHIM
chmod 755 "$contents/Resources/bin/silkcode"
printf 'APPL????' >"$contents/PkgInfo"
cat >"$contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Silk Code</string>
  <key>CFBundleDisplayName</key><string>Silk Code</string>
  <key>CFBundleIdentifier</key><string>app.web.silkcode</string>
  <key>CFBundleExecutable</key><string>SilkCode</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$short_version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

mkdir -p "$out"
if [ "$app_only" = 1 ]; then
  rm -rf "$out/Silk Code.app"
  cp -R "$app" "$out/"
  echo "built $out/Silk Code.app"
  exit 0
fi

staging="$work/dmg"
mkdir -p "$staging"
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"
dmg="$out/SilkCode-$version-$arch.dmg"
rm -f "$dmg"
hdiutil create -quiet -volname "Silk Code" -srcfolder "$staging" -format UDZO "$dmg"
echo "built $dmg"

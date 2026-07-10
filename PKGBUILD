# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=python-mdcal
pkgver=$(grep '^version = ' pyproject.toml | head -1 | sed 's/.*= "\(.*\)"/\1/')
pkgrel=1
pkgdesc='An mddb-backed personal calendar'
arch=('any')
url='https://github.com/handley-lab/mdcal'
license=('MIT')
depends=('python' 'python-mddb>=0.0.17' 'python-icalendar' 'python-slugify' 'python-yaml' 'python-dateutil' 'python-google-api-python-client' 'python-google-auth' 'python-google-auth-httplib2' 'git')
install=python-mdcal.install

package() {
  cd "$startdir"
  local purelib
  purelib=$(env -u VIRTUAL_ENV PATH=/usr/bin:/bin \
    python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
  for f in src/mdcal/*.py; do
    install -Dm644 "$f" "$pkgdir/$purelib/mdcal/$(basename "$f")"
  done
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -dm755 "$pkgdir/usr/bin"
  printf '#!/usr/bin/python\nfrom mdcal.ics import main\nmain()\n' > "$pkgdir/usr/bin/mdcal-import"
  chmod 755 "$pkgdir/usr/bin/mdcal-import"
  printf '#!/usr/bin/python\nfrom mdcal.gcal import main\nmain()\n' > "$pkgdir/usr/bin/mdcal-pull"
  chmod 755 "$pkgdir/usr/bin/mdcal-pull"
  printf '#!/usr/bin/python\nfrom mdcal.imip import main\nmain()\n' > "$pkgdir/usr/bin/mdcal-reply"
  chmod 755 "$pkgdir/usr/bin/mdcal-reply"
}

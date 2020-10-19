#!/bin/sh

set -eux

trap 'if [ $? -ne 0 ]; then exec bash -i; fi' EXIT

yum install -y bison bzip2-devel flex lzo-devel snappy-devel xz xz-devel zlib-devel

# The manylinux image contains an upgraded autotools in /usr/local, but the
# pkg-config macros are not present for this upgraded package. See
# https://github.com/pypa/manylinux/issues/731.
ln -s /usr/share/aclocal/pkg.m4 /usr/local/share/aclocal/

libkdumpfile_commit=a83a6e528a779a8b85f55a12e6488b48f13d9abd
libkdumpfile_url=https://github.com/ptesarik/libkdumpfile/archive/$libkdumpfile_commit/libkdumpfile-$libkdumpfile_commit.tar.gz
mkdir /tmp/libkdumpfile
cd /tmp/libkdumpfile
curl -L "$libkdumpfile_url" | tar -xz --strip-components=1
autoreconf -fiv
./configure --with-lzo --with-snappy --with-zlib --without-python
make -j$(($(nproc) + 1))
make install

cd /io
mkdir /tmp/drgn
tar -xf "$drgn_tar" --strip-components=1 -C /tmp/drgn
cd /tmp/drgn

python_supported() {
	"$1" -c 'import sys; sys.exit(sys.version_info < (3, 6))'
}

for pybin in /opt/python/*/bin; do
	if python_supported "$pybin/python"; then
		"$pybin/python" setup.py bdist_wheel -d /tmp/wheels/
	fi
done

for wheel in /tmp/wheels/*.whl; do
	if auditwheel show "$wheel"; then
		auditwheel repair "$wheel" --plat "$PLAT" -w /tmp/manylinux_wheels/
	else
		echo "Skipping non-platform wheel $wheel"
	fi
done

for pybin in /opt/python/*/bin; do
	if python_supported "$pybin/python"; then
		"$pybin/pip" install drgn --no-index -f /tmp/manylinux_wheels
		"$pybin/drgn" --version
	fi
done

chown "$OWNER" /tmp/manylinux_wheels/*
mv /tmp/manylinux_wheels/* /io/dist/

#!/bin/sh

set -eux

: ${PYTHON:=python3}
drgn_tar=dist/drgn-"$("$PYTHON" setup.py --version)".tar.gz
if [ ! -e "$drgn_tar" ]; then
	"$PYTHON" setup.py sdist
fi

: ${DOCKER:=docker}
$DOCKER pull quay.io/pypa/manylinux2014_x86_64
$DOCKER run -it \
	--env OWNER="$(id -u):$(id -g)" \
	--env PLAT=manylinux2014_x86_64 \
	--env drgn_tar="$drgn_tar" \
	--volume "$(pwd)":/io:ro \
	--volume "$(pwd)/dist":/io/dist \
	--workdir /io \
	--hostname drgn \
	--rm \
	quay.io/pypa/manylinux2014_x86_64 \
	./scripts/build_manylinux_in_docker.sh

#!/bin/sh
# Builds the SDK wheel and the base image appext/python:<version>-py3.12.
#
#   docker/build.sh                          # tag appext/python:0.1.0-py3.12
#   APPEXT_IMAGE_REPO=registry.example.com/appext/python docker/build.sh
#   PYTHON=/path/to/python3.12 docker/build.sh
#
# The wheel is built outside the image and only the wheel enters the build
# context – not the source tree, not the tests, not any key lying around.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
sdk=$(cd "$here/.." && pwd)
python=${PYTHON:-python3}

version=$("$python" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$sdk/pyproject.toml")
repo=${APPEXT_IMAGE_REPO:-appext/python}
tag="$repo:$version-py3.12"

dist=$sdk/dist
rm -f "$dist"/appext-"$version"-*.whl
"$python" -m pip wheel --quiet --no-deps --wheel-dir "$dist" "$sdk"
wheel=$(basename "$(ls "$dist"/appext-"$version"-*.whl)")

context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
cp "$dist/$wheel" "$context/"

docker build --file "$here/Dockerfile" --build-arg APPEXT_WHEEL="$wheel" --tag "$tag" "$context"
echo "built $tag"

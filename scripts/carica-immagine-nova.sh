#!/usr/bin/env bash
#
# Build the Archivist image OFF the VPS "nova" and load it there, verifying that
# the image that arrived is the one that was built.
#
# nova has no swap and little free disk, so it must not build: the api and the
# embed-worker run a pre-built image (docker-compose.nova.yml, `image:` with
# pull_policy never; both services share the same Dockerfile and the same image).
#
# Usage:
#   ARCHIVIST_NOVA_SSH=user@host ./scripts/carica-immagine-nova.sh <tag>
#
#   ARCHIVIST_NOVA_SSH  ssh target of nova. Mandatory, no default: this script
#                       must never reach a host nobody named.
#   <tag>               image tag, mandatory. The same value goes in ARCHIVIST_TAG
#                       in the .env on nova, so `compose up` finds this image.
#
# Steps: build locally (docker-compose.nova.yml + the local-only override
# docker-compose.nova.build.yml, the only place with a `build:`), print the local
# image ID, stream `docker save` into `docker load` on nova, read ID and
# architecture back from nova and exit 1 if either differs.
# The script touches no secrets and does not run `up`.
#
# Assumption: the ID comparison holds only if both sides use the same Docker
# image store. With the containerd image store the ID is the manifest digest,
# with the classic store it is the config digest: the two are not comparable, and
# a mismatch can then be a false alarm (or a match, a coincidence) — check which
# store each side uses if the comparison ever looks wrong.
set -euo pipefail

ARCHIVIST_NOVA_SSH="${ARCHIVIST_NOVA_SSH:?ARCHIVIST_NOVA_SSH is required (ssh target of nova, no default)}"
TAG="${1:?usage: carica-immagine-nova.sh <tag> (the tag is required)}"
# The tag is interpolated into a remote shell string: only a closed alphabet.
if [[ ! "${TAG}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "ERROR: invalid tag '${TAG}' (allowed: letters, digits, _ . -)" >&2
  exit 1
fi
IMAGE="archivist-api:${TAG}"

# The compose reads the tag from the environment: the image built here carries
# exactly the name the stack on nova will look for.
export ARCHIVIST_TAG="${TAG}"

SSH_OPTS=(-o BatchMode=yes)

cd "$(dirname "$0")/.."

docker compose -p archivist -f docker-compose.nova.yml -f docker-compose.nova.build.yml build api

local_id="$(docker image inspect --format '{{.Id}}' "${IMAGE}")"
local_arch="$(docker image inspect --format '{{.Architecture}}' "${IMAGE}")"
echo "local image ID:  ${local_id} (${local_arch})"

docker save "${IMAGE}" | ssh "${SSH_OPTS[@]}" "${ARCHIVIST_NOVA_SSH}" docker load

remote_id="$(ssh "${SSH_OPTS[@]}" "${ARCHIVIST_NOVA_SSH}" "docker image inspect --format '{{.Id}}' '${IMAGE}'")"
remote_arch="$(ssh "${SSH_OPTS[@]}" "${ARCHIVIST_NOVA_SSH}" "docker image inspect --format '{{.Architecture}}' '${IMAGE}'")"
echo "remote image ID: ${remote_id} (${remote_arch})"

if [[ "${local_id}" != "${remote_id}" ]]; then
  echo "ERROR: image ID differs between local and nova; do not start the stack" >&2
  exit 1
fi
if [[ "${local_arch}" != "${remote_arch}" ]]; then
  echo "ERROR: image architecture differs between local and nova; do not start the stack" >&2
  exit 1
fi

echo "ok: ${IMAGE} loaded on nova, ID verified"

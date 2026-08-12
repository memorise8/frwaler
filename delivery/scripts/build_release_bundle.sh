#!/bin/sh
# Build a source-only, secret-free release archive from the committed revision.
set -eu
[ "${DELIVERY_RELEASE_CONFIRM:-}" = "BUILD_COMMITTED_SOURCE_BUNDLE" ] || {
  echo "refusing: set DELIVERY_RELEASE_CONFIRM=BUILD_COMMITTED_SOURCE_BUNDLE" >&2; exit 2;
}
[ -n "${RELEASE_DIR:-}" ] && [ -d "$RELEASE_DIR" ] || { echo "RELEASE_DIR must exist" >&2; exit 2; }
git diff --quiet && git diff --cached --quiet || { echo "refusing: worktree has tracked changes" >&2; exit 2; }
revision=$(git rev-parse --verify HEAD)
short=$(git rev-parse --short=12 HEAD)
archive="$RELEASE_DIR/libertree-delivery-$short.tar.gz"
manifest="$RELEASE_DIR/libertree-delivery-$short.manifest"
git archive --format=tar.gz --prefix="libertree-delivery-$short/" -o "$archive" HEAD
{
  echo "revision=$revision"
  echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sha256sum "$archive"
} > "$manifest"
chmod 600 "$archive" "$manifest"
echo "release bundle: $archive"
echo "release manifest: $manifest"

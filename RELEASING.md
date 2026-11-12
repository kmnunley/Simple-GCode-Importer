# Releasing

## Versions on main

The performance and progress merge (`1b26b6a`) is the versioning baseline at
`2.1.0`. It covers the changes since `v1.2.1`, including the new toolpath
hierarchy. Every subsequent commit on the first-parent history of `main`
counts, including the README update (`23cef3d`) at `2.1.1`:

| Commit message on `main` | Bump |
| --- | --- |
| A Conventional Commit header with `!`, or a `BREAKING CHANGE:` / `BREAKING-CHANGE:` note | Major |
| `feat:` or `feat(scope):` | Minor |
| Any other message, including docs and maintenance | Patch |

For squash merges, use the intended commit title and put any breaking change
note in the squash commit body. Git history determines the version, so keep
`main` append-only and leave the baseline in `scripts/release_build.py` fixed.
To calculate a version locally, run `python scripts/release_build.py version SHA`
with full Git history and the full commit SHA (or omit the SHA for `HEAD`).

**Build versioned extension** runs the Blender regression suite, stamps the
version into the packaged manifest and `bl_info`, and builds and validates the
ZIP with Blender 4.2.0. The source metadata in Git stays at its checked-in
values. Each build job posts the version, commit, and artifact ID in the run
summary. The artifact contains the installable ZIP and its SHA256 file.

## Publish a selected build

1. In **Actions → Build versioned extension**, choose the version to ship and
   copy its artifact ID from the run summary. If the artifact expired, run that
   workflow from `main` with the original full commit SHA to rebuild it. Use
   the new artifact ID and test the replacement ZIP.
2. Run **Actions → Draft extension release** from `main` and enter the artifact
   ID. This verifies the commit, version, and checksum, then attaches the
   existing ZIP and checksum to a draft targeting that commit. If an interrupted
   upload leaves an incomplete draft, inspect and remove that draft before
   retrying with the same artifact ID.
3. Review the generated notes against the previous public release and credit
   contributors. Install the attached ZIP in a clean Blender profile and check
   text and binary imports, arcs, tool materials, and drag-and-drop on the minimum
   and a current supported Blender version.
4. Publish the draft in GitHub. GitHub creates the `vX.Y.Z` tag at the selected
   commit when the draft is published. Upload the same tested ZIP to Blender
   Extensions.

If the candidate needs a code change, land it on `main` and select its new
build. Public versions can skip numbers that were used only for CI builds.

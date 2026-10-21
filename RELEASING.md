# Releasing

Keep source in Git and publish generated ZIPs as GitHub release assets. Existing
tracked archives under `releases/` remain available; new archives are ignored.

1. Merge the intended changes into `main` and pull the latest source.
2. Update `version` in `simple_gcode_importer/blender_manifest.toml` and
   `bl_info` in `simple_gcode_importer/__init__.py`. Commit and push the update.
3. Create and push an annotated tag, such as `v1.2.0`. The tag version must
   match the manifest version, with a leading `v` only in the tag.
4. Check out the tag in a clean directory. Build and validate with Blender:

   ```powershell
   blender --command extension build --source-dir ./simple_gcode_importer --output-dir ./releases
   blender --command extension validate ./releases/simple_gcode_importer-1.2.0.zip
   blender --background --factory-startup --python-exit-code 1 --python tests/run_blender.py
   ```

5. Create a draft GitHub release for that tag. Include release notes,
   contributor credit, and the built ZIP as an asset. Record the ZIP's SHA256.
6. Install that ZIP in a clean Blender profile, enable the extension, and check
   text and binary imports, arcs, tool materials, and drag-and-drop in the UI.
   Check the minimum supported Blender version and a current supported version.
7. Publish the release after the package checks pass. Upload the same tested ZIP
   to Blender Extensions; do not rebuild it between destinations.

If a source change is needed after tagging, resolve the tag before publishing
and repeat the build and package checks. Do not move an already published tag.

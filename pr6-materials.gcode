; Blender importer fixture for PR #6: materials, bevel, and tool changes.
; Intended for visualization testing only.
; Expected: left L uses material T0; right L uses material T1.
; Both paths should have the PR's default bevel depth of 0.2.
; No retraction at T1: this deliberately tests splitting a buffered path.
; The unmodified PR assigns the entire combined path to T1.
G21
G90
M83
G92 E0
T0
; Left L: (0,0) -> (20,0) -> (20,20).
G1 X0 Y0 Z0.2 E0 F1200
G1 X20 Y0 E1
G1 X20 Y20 E1
T1
; Right L: (20,20) -> (40,20) -> (40,0).
G1 X40 Y20 E1
G1 X40 Y0 E1
; Flush the final buffered path with a retraction.
G1 E-1

; Visual regression fixture: short R arcs, a long R arc, a circle and a helix.
G90
M83
G0 X10 Y0 Z0.2
G3 X0 Y10 R10 E1
G0 X30 Y0
G2 X20 Y10 R10 E1
G0 X50 Y0
G3 X40 Y10 R-10 E3
T1
G0 X10 Y30
G3 I-10 E4
G0 X40 Y30
G3 I-10 Z10.2 E4

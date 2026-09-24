# Architecture

NestingStudio separates the user interface from the geometry and optimization backend.

## Layers

```text
nesting_studio/
  app.py               Application entry point and logging
  main_window.py       Main window, actions, project persistence
  config_panel.py      Material, thickness, optimizer and process settings
  part_table.py        Searchable/editable part list
  canvas.py            Interactive DXF-like nesting canvas
  workers.py           Background loading and optimization threads
  project_io.py        Project and global-summary import
  logging_config.py    Rotating logs

nesting/
  geometry.py          Polygon cleanup, offset-like buffers, ring helpers
  inputs.py            DXF/SVG/JSON loading and thickness grouping
  occ.py               Low-level OpenCascade/OCP adapter
  step_input.py        STEP B-Rep and top-face projection
  engine.py            Bottom-left placement and hole-aware collision engine
  optimize.py          Greedy, SA and GA optimization
  pipeline.py          Thickness-grouped execution pipeline
  output.py            DXF/JSON/CSV export and validation
  common_line.py       Common-line cutting extension interface
```

## Thickness grouped pipeline

1. Inputs are normalized to 2D geometry.
2. STEP thickness is inferred from the broad planar face pair.
3. DXF/SVG/JSON require explicit `thickness_mm`.
4. Parts are clustered by thickness and never mixed across jobs.
5. Each group runs an independent nesting engine and optimizer.
6. Each group exports separate sheets and reports.

## Placement engine

- Parts are pre-expanded by half the effective clearance.
- Sheet margins include kerf and lead-in reserve.
- Bottom-left candidate generation uses boundaries, contact points and holes.
- STRtree is used for nearby-collision queries.
- Thermal density can reject locally overloaded placements.
- All final dimensions are checked again with unexpanded geometry.

## Interactive editing

- `PlacementState` contains actual and collision geometry.
- Move/rotate/flip operations are undoable Qt commands.
- Invalid edits are retained and marked red instead of being rejected.
- Mirroring is disabled for STEP parts with `face_up_locked`.
- Manual edits are saved in project and session files.

## Persistence

- `.neststudio.json` stores the full editable project.
- `runtime/last_session.json` restores the last session automatically.
- `QSettings` stores user material presets and the last configuration.
- Full WKT is stored only when geometry cannot be reconstructed from a rotation and offset, such as mirrored parts.

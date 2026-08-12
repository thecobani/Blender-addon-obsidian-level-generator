OBSIDIAN — Procedural Grey-Box Level Generator

A Python engine for procedurally generating DOOM-style grey-box level geometry, built on a custom BMesh construction pipeline.

Overview

OBSIDIAN procedurally generates grey-box (blockout) level geometry in the style of classic first-person shooter level design — rooms, corridors, and connections laid out algorithmically instead of by hand. The generation logic is built as a standalone Python engine, fully decoupled from any specific application, and was validated independently before being integrated into a full Blender addon.

This repository contains the core generation engine: the pure-Python logic layer, tested and validated on its own before any editor integration was added on top of it.

How it works
A rule-based procedural algorithm generates the level's overall layout — rooms, connections, and flow — before any 3D geometry exists.
That layout is then translated into real 3D grey-box geometry through a custom construction pipeline built on Blender's BMesh API.
Generation logic is fully separated from any UI or editor-integration code, so the core engine can be tested and run independently as plain Python — no editor required to validate correctness.
Tech stack
Python
Blender's bmesh / bpy APIs for geometry construction
Custom procedural / rule-based generation logic
About this repo

This showcases the core generation engine behind OBSIDIAN. A full-featured, packaged Blender addon built on top of this engine — with a UI panel, presets, and additional tooling — is available separately as a commercial product.

Running it
python main.py

(Update this section with the actual entry point and any setup steps once the files are in the repo.)

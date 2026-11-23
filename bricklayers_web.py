#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# SPDX-License-Identifier: GPL-3.0-only
#
# BrickLayers Web — interfaz web local para el postprocesador BrickLayers.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Original BrickLayers project by GeekDetour:
# https://github.com/GeekDetour/BrickLayers
# Original copyright and license notices must be preserved.

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import base64
import binascii
import math

from pathlib import Path

from flask import Flask, jsonify, request, render_template_string, send_file
from werkzeug.serving import make_server

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from thumbnail_fallback import generate_gcode_thumbnail

HERE = Path(__file__).resolve().parent
BRICKLAYERS = HERE / "bricklayers.py"

if not BRICKLAYERS.exists():
    print(
        "No se encontró bricklayers.py junto a bricklayers_web.py.",
        file=sys.stderr,
    )
    sys.exit(1)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

STATE = {
    "original_gcode": b"",
    "processed_gcode": b"",
    "params": {
        "startAtLayer": 3,
        "extrusionMultiplier": 1.05,
        "enabled": 1,
    },
    "temporary_dir": None,
    "processed_path": None,
}

READY_TO_CLOSE = threading.Event()
PROCESS_LOCK = threading.RLock()
SERVER = {"instance": None}


# ---------------------------------------------------------------------------
# Ejecución de BrickLayers 
# ---------------------------------------------------------------------------

def run_bricklayers(
    gcode_bytes: bytes,
    start_at_layer: int,
    extrusion_multiplier: float,
    enabled: int,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="bricklayers_run_") as temporary:
        temporary_path = Path(temporary)

        input_file = temporary_path / "input.gcode"
        output_file = temporary_path / "brick.gcode"

        input_file.write_bytes(gcode_bytes)

        command = [
            sys.executable,
            str(BRICKLAYERS),
            str(input_file),
            "-outputFile", str(output_file),
            "-startAtLayer", str(start_at_layer),
            "-extrusionMultiplier", str(extrusion_multiplier),
            "-enabled", str(enabled),
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            cwd=str(HERE),
        )

        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or "bricklayers.py devolvió un error.")

        if not output_file.exists():
            raise RuntimeError(
                "bricklayers.py terminó, pero no creó el archivo de salida."
            )

        return output_file.read_bytes()
    
# ---------------------------------------------------------------------------
# Parser mínimo para el visor 3D
# ---------------------------------------------------------------------------

NUMBER = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"
X_RE = re.compile(rf"\bX({NUMBER})\b", re.IGNORECASE)
Y_RE = re.compile(rf"\bY({NUMBER})\b", re.IGNORECASE)
E_RE = re.compile(rf"\bE({NUMBER})\b", re.IGNORECASE)
Z_RE = re.compile(rf"\bZ({NUMBER})\b", re.IGNORECASE)
TYPE_RE = re.compile(r";\s*TYPE:\s*(.+)", re.IGNORECASE)

TOTAL_LAYERS_RE = re.compile(
    r"^\s*;\s*total\s+layers\s+count\s*=\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

GCODE_WORD_RE = re.compile(
    rf"([A-Za-z])\s*({NUMBER})"
)

def read_declared_layer_count(gcode_bytes):
    text = gcode_bytes.decode("utf-8", errors="replace")
    match = TOTAL_LAYERS_RE.search(text)

    if not match:
        return None

    count = int(match.group(1))
    return count if count > 0 else None

def parse_gcode(gcode_bytes, max_layers=None, start_marker=None):
    # max_layers se conserva por compatibilidad.
    # No truncar: necesitamos contar todas las capas.
    text = gcode_bytes.decode("utf-8", errors="replace")
    def normalize_marker(line):
        return line.strip().lstrip(";").strip().upper()

    normalized_marker = (
        normalize_marker(start_marker)
        if start_marker
        else None
    )

    marker_found = (
        normalized_marker is not None
        and any(
            normalize_marker(line) == normalized_marker
            for line in text.splitlines()
        )
    )

    # Sin marcador solicitado, o si no existe, conservar
    # el comportamiento habitual.
    collect_geometry = not marker_found

    layers_by_z = {}

    position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    offsets = {"X": 0.0, "Y": 0.0, "Z": 0.0}

    absolute_xyz = True
    relative_e = False
    last_e = 0.0
    retraction_debt = 0.0

    current_kind = "other"
    current_role = "default"

    current_segment = None
    current_key = None

    stats = {
        "lines": 0,
        "moves": 0,
        "z_changes": 0,
        "type_comments": 0,
        "extrusions": 0,
        "layers": 0,
        "segments": 0,
    }

    for raw_line in text.splitlines():
        stats["lines"] += 1
        if (
            normalized_marker is not None
            and normalize_marker(raw_line) == normalized_marker
        ):
            collect_geometry = True

            # No conectar recorridos anteriores con la pieza.
            current_segment = None
            current_key = None

            continue
        
        # Leer TYPE antes de eliminar comentarios.
        type_match = TYPE_RE.search(raw_line)

        if type_match:
            label = type_match.group(1).strip()
            current_role = viewer_role_from_type(label)
            current_kind = (
                "inner"
                if current_role == "inner_wall"
                else "other"
            )

            current_segment = None
            current_key = None
            stats["type_comments"] += 1

        code = raw_line.split(";", 1)[0].strip()

        if not code:
            continue

        command_match = re.match(
            r"^(G|M)\s*(\d+)(?![\d.])",
            code,
            re.IGNORECASE,
        )

        if not command_match:
            continue

        command = (
            command_match.group(1).upper(),
            int(command_match.group(2)),
        )

        words = {
            letter.upper(): float(value)
            for letter, value in GCODE_WORD_RE.findall(
                code[command_match.end():]
            )
        }

        if command == ("M", 82):
            relative_e = False
            continue

        if command == ("M", 83):
            relative_e = True
            continue

        if command in {("G", 90), ("G", 91)}:
            absolute_xyz = command == ("G", 90)

            # E mantiene su modo M82/M83 independiente.
            # Adecuado para el sabor Klipper de tu perfil.
            continue

        if command == ("G", 92):
            for axis in position:
                if axis in words:
                    offsets[axis] = position[axis] - words[axis]

            if "E" in words:
                last_e = words["E"]

            continue

        if command not in {("G", 0), ("G", 1)}:
            continue

        stats["moves"] += 1

        start = [
            position["X"],
            position["Y"],
            position["Z"],
        ]

        for axis in position:
            if axis in words:
                if absolute_xyz:
                    position[axis] = words[axis] + offsets[axis]
                else:
                    position[axis] += words[axis]

        end = [
            position["X"],
            position["Y"],
            position["Z"],
        ]

        if abs(end[2] - start[2]) > 1e-6:
            stats["z_changes"] += 1

        delta_e = 0.0

        if "E" in words:
            value = words["E"]
            delta_e = value if relative_e else value - last_e
            last_e = value

        deposited_e = 0.0
        recovered = 0.0

        if delta_e < 0:
            retraction_debt += -delta_e

        elif delta_e > 0:
            recovered = min(retraction_debt, delta_e)
            retraction_debt -= recovered
            deposited_e = delta_e - recovered

        # La recuperación de retracción no crea un cordón.
        # Si comparte movimiento, aproximar dónde acaba.
        if deposited_e > 1e-9 and recovered > 0:
            fraction = recovered / delta_e
            start = [
                a + (b - a) * fraction
                for a, b in zip(start, end)
            ]

        xy_length = math.hypot(
            end[0] - start[0],
            end[1] - start[1],
        )

        if deposited_e <= 1e-9 or xy_length <= 1e-9:
            current_segment = None
            current_key = None
            continue

        if not collect_geometry:
            current_segment = None
            current_key = None
            continue

        stats["extrusions"] += 1

        # Agrupar pequeñas diferencias numéricas.
        # No usar la altura nominal: perderíamos las intermedias.
        z_key = round(end[2], 5)

        layer = layers_by_z.setdefault(
            z_key,
            {"z": z_key, "segments": []},
        )

        segment_key = (
            z_key,
            current_kind,
            current_role,
        )

        connected = (
            current_segment is not None
            and current_key == segment_key
            and all(
                abs(a - b) < 1e-6
                for a, b in zip(
                    current_segment["points"][-1],
                    start,
                )
            )
        )

        if not connected:
            current_segment = {
                "kind": current_kind,
                "role": current_role,
                "points": [start],
            }

            layer["segments"].append(current_segment)
            current_key = segment_key

        current_segment["points"].append(end)

    layers = [
        layers_by_z[z]
        for z in sorted(layers_by_z)
    ]

    stats["layers"] = len(layers)
    stats["segments"] = sum(
        len(layer["segments"])
        for layer in layers
    )

    return layers, stats

MAX_VIEWER_POINTS_PER_LAYER = 20000
MAX_VIEWER_LAYERS = 1000


def simplify_points(points, max_points):
    if len(points) <= max_points:
        return points

    step = max(1, len(points) // max_points)
    simplified = points[::step]

    if simplified[-1] != points[-1]:
        simplified.append(points[-1])

    return simplified


def build_viewer_data(original_layers, processed_layers):
    original_by_z = {
        round(layer["z"], 5): layer
        for layer in original_layers
    }

    viewer_layers = []

    for processed_layer in processed_layers:
        z = processed_layer["z"]

        original_layer = original_by_z.get(
            round(z, 5),
            {"segments": []},
        )

        segments = []

        for segment in original_layer["segments"]:
            if segment["kind"] == "inner":
                segments.append({
                    **segment,
                    "kind": "original_inner",
                })

        for segment in processed_layer["segments"]:
            segments.append({
                **segment,
                "kind": (
                    "brick_inner"
                    if segment["kind"] == "inner"
                    else "other"
                ),
            })

        viewer_layers.append({
            "z": z,
            "segments": segments,
        })

    return viewer_layers

# ---------------------------------------------------------------------------
# Interfaz web
# ---------------------------------------------------------------------------

PAGE = r"""
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>BrickLayers — Post-proceso</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; height: 100vh; overflow: hidden;
  font-family: system-ui, sans-serif;
  background: #101010; color: #e8e8e8;
}
#viewer {
  position: fixed; inset: 0;
}
#panel {
  position: fixed; top: 16px; left: 16px; bottom: 16px;
  width: 320px; padding: 18px;
  background: rgba(24, 24, 24, 0.82);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 14px;
  overflow-y: auto; z-index: 10;
}
h1 { margin: 0 0 4px; font-size: 19px; }
.sub { color: #999; font-size: 12px; margin-bottom: 14px; }
.info {
  padding: 10px; border-radius: 8px; background: rgba(255,255,255,0.05);
  font-size: 12px; line-height: 1.5;
}
.legend { 
  padding: 10px; border-radius: 8px; background: rgba(255,255,255,0.05);
  margin: 14px 0; font-size: 12px; color: #aaa; line-height: 1.7; }
.dot {
  display: inline-block; width: 10px; height: 10px;
  border-radius: 50%; margin-right: 6px;
}
label { display: block; margin: 12px 0 5px; font-size: 13px; }
input[type=number], select {
  width: 100%; padding: 8px; border-radius: 7px;
  border: 1px solid #3a3a3a; background: #222; color: #fff;
}
button {
  width: 100%; margin-top: 12px; padding: 10px; border: 0;
  border-radius: 8px; background: #2f81f7; color: #fff;
  font-weight: 600; cursor: pointer;
}
button.secondary { background: #333; }
button.danger { background: #b3550a; }
.toolbar {
  margin-top: 14px; font-size: 12px;
}
.toolbar input[type=range] { width: 100%; }
.checks {
  display: flex; gap: 10px; flex-wrap: wrap;
  margin-top: 10px; font-size: 12px;
}
.checks label { margin: 0; }
.status {
  margin-top: 14px; padding: 10px; border-radius: 8px;
  background: rgba(255,255,255,0.05); color: #bbb;
  font-size: 12px; white-space: pre-wrap;
}
#layerControl {
  --thumb-size: 18px;

  position: fixed;
  top: 24px;
  bottom: 24px;
  right: 10px;

  width: 24px;
  z-index: 20;

  overflow: visible;
}

#layer {
  position: absolute;
  inset: 0;

  width: 24px;
  height: 100%;
  margin: 0;
  padding: 0;

  writing-mode: vertical-lr;
  direction: rtl;

  appearance: none;
  -webkit-appearance: none;

  background: transparent;
  cursor: pointer;
  touch-action: none;
}

#layer::-webkit-slider-runnable-track {
  width: 5px;
  height: 100%;
  background: rgba(210, 215, 225, 0.35);
  border-radius: 999px;
}

#layer::-webkit-slider-thumb {
  appearance: none;
  -webkit-appearance: none;

  width: var(--thumb-size);
  height: var(--thumb-size);
  margin-left: -6.5px;

  border: 2px solid #fff;
  border-radius: 50%;
  background: #ffb52e;
  box-shadow: 0 1px 7px rgba(0, 0, 0, 0.5);
}

#layer::-moz-range-track {
  width: 5px;
  background: rgba(210, 215, 225, 0.35);
  border-radius: 999px;
}

#layer::-moz-range-thumb {
  width: var(--thumb-size);
  height: var(--thumb-size);

  box-sizing: border-box;
  border: 2px solid #fff;
  border-radius: 50%;
  background: #ffb52e;
}

#layer:focus-visible {
  outline: 1px solid rgba(255, 255, 255, 0.65);
  outline-offset: 4px;
  border-radius: 12px;
}

#layer:disabled {
  cursor: default;
  opacity: 0.45;
}

#layerLabel {
  position: absolute;
  right: 32px;
  top: 9px;
  transform: translateY(-50%);

  min-width: 30px;
  padding: 4px 7px;

  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 6px;
  background: rgba(20, 20, 20, 0.8);
  color: #fff;

  font: 12px/1.2 system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
  text-align: center;
  white-space: nowrap;

  pointer-events: none;
}

#thumbnailPreview {
  position: fixed;
  left: 25px;
  bottom: 24px;

  width: 300px;
  max-width: calc(100vw - 80px);

  height: 170px;
  max-height: 28vh;

  display: flex;
  align-items: center;
  justify-content: center;

  padding: 8px;
  overflow: hidden;

  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 10px;

  background: rgba(24, 24, 24, 0.65);
  backdrop-filter: blur(8px);

  z-index: 15;
  pointer-events: none;
}

#thumbnailPreview[hidden] {
  display: none;
}

#thumbnailImage {
  display: block;

  width: 100%;
  height: 100%;

  object-fit: contain;
  object-position: center;

  user-select: none;
}

#panel {
  padding-bottom: calc(min(170px, 28vh) + 36px);
}

button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

</style>
</head>
<body>
  <div id="viewer"></div>

  <aside id="panel">
    <h1>BrickLayers</h1>
    <div class="sub">Revisa, ajusta y devuelve el G-code a OrcaSlicer</div>

    <div id="modelInfo" class="info" style="display:none">
      Modelo cargado.<br>
      Capas originales: <span id="originalLayerCount">0</span><br>
      Capas BrickLayers: <span id="layerCount">0</span><br>
      Tiempo BrickLayers: <span id="processTime">0.00</span> s
    </div>

    <div class="legend">
      <span class="dot" style="background:#ff852f"></span>
      Pared original<br>

      <span class="dot" style="background:#ffe052"></span>
      Pared BrickLayers<br>

      <span class="dot" style="background:#b9bec7"></span>
      Otras extrusiones
    </div>

    <button id="toggleParams" class="secondary">Parámetros</button>

    <div id="params" style="display:none">
      <label>Capa inicial</label>
      <input id="startAtLayer" type="number" min="0" step="1" value="3">

      <label>Multiplicador de extrusión</label>
      <input id="extrusionMultiplier" type="number" min="0.5" max="2" step="0.01" value="1.05">

      <label>Activado</label>
      <select id="enabled">
        <option value="1" selected>1 — Activado</option>
        <option value="0">0 — Desactivado</option>
      </select>
    </div>

    <div class="checks">
      <label><input id="showOriginal" type="checkbox"> Original</label>
      <label><input id="showBrick" type="checkbox" checked> BrickLayers</label>
      <label><input id="showOther" type="checkbox" checked> Resto</label>
    </div>

    <button id="applyExit" class="danger">Aplicar y cerrar</button>

    <div id="status" class="status">Procesando G-code...</div>
  </aside>

  <div id="thumbnailPreview" hidden>
    <img
      id="thumbnailImage"
      alt="Miniatura del modelo o vista isométrica de los recorridos"
      draggable="false"
    >
  </div>

  <div id="layerControl" aria-label="Selector de capa">
    <span id="layerLabel">0 / 0 · Z —</span>

    <input
      id="layer"
      type="range"
      min="0"
      max="0"
      step="1"
      value="0"
      disabled
    >
  </div>
  
  <div id="errorBanner"
       style="display:none; position:absolute; top:12px; left:50%;
              transform:translateX(-50%); z-index:100;
              max-width:90%; padding:10px 14px; border-radius:8px;
              background:#7f1d1d; color:#fff; font-size:12px;
              white-space:pre-wrap;"></div>

<script type="importmap">
{
  "imports": {
    "three": "/static/three.module.js",
    "three/addons/": "/static/"
  }
}
</script>
<script>
window.addEventListener("error", function (event) {
  const status = document.getElementById("status");
  const banner = document.getElementById("errorBanner");

  const message =
    "Error al cargar el visor:\n" +
    (event.message || "Error desconocido") +
    "\n\nRecurso: " + (event.filename || "desconocido") +
    "\nLínea: " + (event.lineno || "?");

  if (status) {
    status.textContent = message;
    status.style.color = "#ff8a80";
  }

  if (banner) {
    banner.textContent = message;
    banner.style.display = "block";
  }
});
</script>
<script>
(() => {
  async function loadThumbnail() {
    const container = document.getElementById("thumbnailPreview");
    const image = document.getElementById("thumbnailImage");

    if (!container || !image) return;

    let objectUrl = null;

    try {
      const response = await fetch("/thumbnail", {
        cache: "no-store"
      });

      if (response.status === 204) {
        console.info(
          "[BrickLayers] No hay una miniatura compatible en el G-code."
        );
        return;
      }

      if (!response.ok) {
        throw new Error(
          "No se pudo cargar la miniatura. HTTP " + response.status
        );
      }

      const blob = await response.blob();
      objectUrl = URL.createObjectURL(blob);

      image.onload = () => {
        container.hidden = false;

        URL.revokeObjectURL(objectUrl);
        objectUrl = null;
      };

      image.onerror = () => {
        container.hidden = true;

        if (objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
        }

        console.warn("[BrickLayers] Miniatura no válida.");
      };

      image.src = objectUrl;

    } catch (error) {
      container.hidden = true;

      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }

      console.warn("[BrickLayers] Error de miniatura:", error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadThumbnail, {
      once: true
    });
  } else {
    loadThumbnail();
  }
})();
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";


const $ = id => document.getElementById(id);

function setStatus(text, isError = false) {
  const status = document.getElementById("status");
  if (!status) return;

  status.textContent = text;
  status.style.color = isError ? "#ff8a80" : "#bbb";
}

window.addEventListener("error", event => {
  showError(
    "Error de JavaScript:\n" + event.message +
    "\n\nArchivo: " + (event.filename || "desconocido") +
    "\nLínea: " + (event.lineno || "?") +
    "\n\n" + (event.error && event.error.stack ? event.error.stack : "")
  );
});

window.addEventListener("unhandledrejection", event => {
  const reason = event.reason || "Error desconocido";
  const message = reason.message || String(reason);

  showError(
    "Error asincrónico:\n" + message +
    "\n\n" + (reason.stack || "")
  );
});

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0b0b);

scene.add(new THREE.HemisphereLight(
  0xffffff,
  0x666677,
  2.0
));

const filamentLight = new THREE.DirectionalLight(
  0xffffff,
  2.5
);

filamentLight.position.set(100, -150, 250);
scene.add(filamentLight);

const viewerContainer = $("viewer");

const camera = new THREE.PerspectiveCamera(
  50,
  1,
  0.01,
  100000
);


camera.lookAt(0, 0, 0);
camera.up.set(0, 0, 1);
camera.position.set(100, -150, 120);

const renderer = new THREE.WebGLRenderer({
  antialias: true,
  alpha: false
});

renderer.setPixelRatio(window.devicePixelRatio);
viewerContainer.appendChild(renderer.domElement);

renderer.domElement.style.width = "100%";
renderer.domElement.style.height = "100%";
renderer.domElement.style.display = "block";



const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, 0, 0);
controls.maxPolarAngle = Math.PI / 2 - 0.01;
controls.update();

// Declarar antes de cualquier llamada a resizeViewer().
let lineObject = null;

let lastWidth = 0;
let lastHeight = 0;

function resizeViewer() {
  const width = viewerContainer.clientWidth;
  const height = viewerContainer.clientHeight;

  if (width < 2 || height < 2) {
    return;
  }

  if (width === lastWidth && height === lastHeight) {
    return;
  }

  lastWidth = width;
  lastHeight = height;

  camera.aspect = width / height;
  camera.updateProjectionMatrix();

  renderer.setSize(width, height, false);
}

let resizeScheduled = false;

function scheduleResize() {
  if (resizeScheduled) {
    return;
  }

  resizeScheduled = true;

  requestAnimationFrame(() => {
    resizeScheduled = false;
    resizeViewer();
  });
}

resizeViewer();

const viewerResizeObserver = new ResizeObserver(scheduleResize);
viewerResizeObserver.observe(viewerContainer);

let gridObject = null;

function updateGrid(modelSize) {
  if (gridObject) {
    scene.remove(gridObject);
    gridObject.geometry.dispose();

    const materials = Array.isArray(gridObject.material)
      ? gridObject.material
      : [gridObject.material];

    materials.forEach(material => material.dispose());
  }

  const footprint = Math.max(modelSize.x, modelSize.y, 20);
  const gridSize = Math.ceil(footprint * 2 / 10) * 10;
  const divisions = 20;

  gridObject = new THREE.GridHelper(
    gridSize,
    divisions,
    0x999999,
    0x555555
  );

  // GridHelper nace en XZ: lo giramos para situarlo en XY.
  gridObject.rotation.x = Math.PI / 2;
  gridObject.position.set(0, 0, -0.02);

  scene.add(gridObject);
}

let layerData = [];

let busy = false;
let pendingProcess = false;
let autoTimer = null;

const COLORS = {
  original_inner: new THREE.Color(0x4da3ff),
  brick_inner: new THREE.Color(0xffcc33),
  other: new THREE.Color(0x666666)
};

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

function clearViewer() {
  if (!lineObject) return;

  scene.remove(lineObject);

  lineObject.traverse(object => {
    if (object.geometry) {
      object.geometry.dispose();
    }

    if (object.material) {
      const materials = Array.isArray(object.material)
        ? object.material
        : [object.material];

      materials.forEach(material => material.dispose());
    }

    if (object.isInstancedMesh) {
      object.dispose();
    }
  });

  lineObject = null;
}

const FILAMENT_VIEW = {
  width: 0.45,
  height: 0.20,
  radialSegments: 8,
  maxSegments: 250000
};

function buildModel() {
  clearViewer();

  const maxLayer = Number($("layer").value);

  const visibility = {
    original_inner: $("showOriginal").checked,
    brick_inner: $("showBrick").checked,
    other: $("showOther").checked
  };

  const palette = {
    original_inner: new THREE.Color("#ff852f"),
    brick_inner: new THREE.Color("#ffe052"),
    other: new THREE.Color("#b9bec7")
  };

  function validPoint(point) {
    return (
      Array.isArray(point) &&
      point.length >= 3 &&
      Number.isFinite(point[0]) &&
      Number.isFinite(point[1]) &&
      Number.isFinite(point[2])
    );
  }

  // Dos pasadas para evitar guardar todos los segmentos
  // como objetos adicionales en memoria.
  function visitSegments(callback) {
    for (let layerIndex = 0; layerIndex <= maxLayer; layerIndex++) {
      const layer = layerData[layerIndex];
      if (!layer) continue;

      for (const segment of layer.segments) {
        if (!visibility[segment.kind]) continue;

        const points = segment.points;

        for (let index = 0; index < points.length - 1; index++) {
          const a = points[index];
          const b = points[index + 1];

          if (!validPoint(a) || !validPoint(b)) continue;

          const dx = b[0] - a[0];
          const dy = b[1] - a[1];
          const dz = b[2] - a[2];

          if (dx * dx + dy * dy + dz * dz < 1e-12) {
            continue;
          }

          callback(a, b, segment.kind, segment.role || "default");
        }
      }
    }
  }

  let totalSegments = 0;

  visitSegments(() => {
    totalSegments++;
  });

  if (totalSegments === 0) {
    setStatus(
      "No hay segmentos visibles para esta selección.",
      true
    );
    return;
  }

  const count = Math.min(
    totalSegments,
    FILAMENT_VIEW.maxSegments
  );

  const geometry = new THREE.CylinderGeometry(
    1,
    1,
    1,
    FILAMENT_VIEW.radialSegments,
    1,
    false
  );

  const material = new THREE.MeshStandardMaterial({
    color: 0xffffff,
    roughness: 0.65,
    metalness: 0.0
  });

  const mesh = new THREE.InstancedMesh(
    geometry,
    material,
    count
  );

  const start = new THREE.Vector3();
  const end = new THREE.Vector3();
  const direction = new THREE.Vector3();
  const midpoint = new THREE.Vector3();

  const vertical = new THREE.Vector3(0, 0, 1);
  const alternate = new THREE.Vector3(0, 1, 0);

  const side = new THREE.Vector3();
  const normal = new THREE.Vector3();
  const scale = new THREE.Vector3();
  const matrix = new THREE.Matrix4();

  // Mantener todos los cordones por encima de la base visual.
  const bottomZ = -displayOffset.z;
  const fallbackColor = new THREE.Color("#b9bec7");

  let sourceIndex = 0;
  let instanceIndex = 0;

  visitSegments((a, b, kind, role) => {
    const index = sourceIndex++;

    // Si hay demasiados segmentos, distribuir la muestra por
    // todo el modelo en lugar de cortar las últimas capas.
    const selected = (
      Math.floor((index + 1) * count / totalSegments) >
      Math.floor(index * count / totalSegments)
    );

    if (!selected) return;

    const widths = viewerDimensions.widths;

    const width = widths[role] ?? widths.default;
    const height = viewerDimensions.layer_height;

    const halfWidth = width / 2;
    const halfHeight = height / 2;

    start.set(a[0], a[1], a[2]);
    end.set(b[0], b[1], b[2]);

    // Aproximar el cordón bajo la altura Z del recorrido.
    // La primera base queda apoyada en el plano visual.
    start.z = Math.max(
      bottomZ + halfHeight,
      start.z - halfHeight
    );

    end.z = Math.max(
      bottomZ + halfHeight,
      end.z - halfHeight
    );

    direction.subVectors(end, start);
    const length = direction.length();
    direction.normalize();

    // Eje longitudinal del cilindro: Y local.
    // Sección transversal: X local y Z local.
    side.crossVectors(direction, vertical);

    if (side.lengthSq() < 1e-10) {
      side.crossVectors(direction, alternate);
    }

    side.normalize();
    normal.crossVectors(side, direction).normalize();

    midpoint.addVectors(start, end).multiplyScalar(0.5);

    matrix.makeBasis(side, direction, normal);

    scale.set(
      halfWidth,
      length,
      halfHeight
    );

    matrix.scale(scale);
    matrix.setPosition(midpoint);

    mesh.setMatrixAt(instanceIndex, matrix);
    mesh.setColorAt(
      instanceIndex,
      palette[kind] || fallbackColor
    );

    instanceIndex++;
  });

  mesh.count = instanceIndex;
  mesh.instanceMatrix.needsUpdate = true;

  if (mesh.instanceColor) {
    mesh.instanceColor.needsUpdate = true;
  }

  mesh.computeBoundingBox();
  mesh.computeBoundingSphere();

  lineObject = new THREE.Group();
  lineObject.position.copy(displayOffset);
  lineObject.add(mesh);

  scene.add(lineObject);

  if (totalSegments > count) {
    console.warn(
      `[BrickLayers] Vista simplificada: ` +
      `${count} de ${totalSegments} segmentos. ` +
      "El G-code final permanece completo."
    );
  }
}

const displayOffset = new THREE.Vector3();
let displaySize = new THREE.Vector3(1, 1, 1);

function prepareModelPlacement() {
  const box = new THREE.Box3();
  const pointVector = new THREE.Vector3();

  for (const layer of layerData) {
    for (const segment of layer.segments) {
      for (const point of segment.points) {
        if (
          !Array.isArray(point) ||
          point.length < 3 ||
          !point.slice(0, 3).every(Number.isFinite)
        ) {
          continue;
        }

        pointVector.set(point[0], point[1], point[2]);
        box.expandByPoint(pointVector);
      }
    }
  }

  if (box.isEmpty()) {
    throw new Error("El visor no ha recibido coordenadas válidas.");
  }

  const center = box.getCenter(new THREE.Vector3());
  displaySize = box.getSize(new THREE.Vector3());

  // Traslación visual: centro XY en el origen, base en Z = 0.
  displayOffset.set(
    -center.x,
    -center.y,
    -box.min.z
  );
}

function fitCamera() {
  resizeViewer();

  const target = new THREE.Vector3(
    0,
    0,
    displaySize.z / 2
  );

  const radius = Math.max(displaySize.length() / 2, 1);

  const verticalFov = THREE.MathUtils.degToRad(camera.fov);
  const horizontalFov = 2 * Math.atan(
    Math.tan(verticalFov / 2) * camera.aspect
  );

  const limitingFov = Math.min(verticalFov, horizontalFov);
  const distance = radius / Math.sin(limitingFov / 2) * 1.25;

  const direction = new THREE.Vector3(1, -1.5, 1.1).normalize();

  camera.position.copy(target).addScaledVector(direction, distance);
  camera.near = Math.max(0.01, distance / 1000);
  camera.far = Math.max(1000, distance * 100);
  camera.updateProjectionMatrix();

  controls.target.copy(target);
  controls.minDistance = Math.max(0.5, radius * 0.1);
  controls.maxDistance = distance * 20;
  controls.update();
}

function updateLayerLabel() {
  const slider = $("layer");
  const label = $("layerLabel");
  const control = $("layerControl");

  const count = layerData.length;

  if (count === 0) {
    label.textContent = "0 / 0 · Z —";
    slider.setAttribute("aria-valuetext", "Sin capas");
    return;
  }

  // Los índices internos empiezan en 0.
  const index = Math.max(
    0,
    Math.min(count - 1, Number(slider.value) || 0)
  );

  slider.value = String(index);

  // La numeración visible empieza en 1.
  const layerNumber = index + 1;
  const z = Number(layerData[index].z);

  const zText = Number.isFinite(z)
    ? `${z.toLocaleString("es-ES", {
        minimumFractionDigits: 3,
        maximumFractionDigits: 5
      })} mm`
    : "—";

  label.textContent =
    `${layerNumber} / ${count} · Z ${zText}`;

  // Conservar la posición de la etiqueta junto al deslizador.
  const min = Number(slider.min);
  const max = Number(slider.max);
  const value = Number(slider.value);

  const fraction = max > min
    ? Math.max(
        0,
        Math.min(1, (value - min) / (max - min))
      )
    : 0;

  const thumbSize = control
    ? parseFloat(
        getComputedStyle(control)
          .getPropertyValue("--thumb-size")
      ) || 18
    : 18;

  const height = slider.clientHeight;
  const travel = Math.max(0, height - thumbSize);

  // El valor máximo está arriba.
  const thumbCenter =
    thumbSize / 2 + (1 - fraction) * travel;

  label.style.top = `${thumbCenter}px`;

  slider.setAttribute(
    "aria-valuetext",
    `Capa ${layerNumber} de ${count}, Z ${zText}`
  );
}

let viewerDimensions = {
  nozzle_diameter: 0.4,
  layer_height: 0.2,
  initial_layer_height: 0.2,
  widths: {
    default: 0.44,
    initial: 0.48,
    outer_wall: 0.40,
    inner_wall: 0.44,
    sparse_infill: 0.44,
    solid_infill: 0.48,
    top_surface: 0.375,
    support: 0.384
  }
};

async function process() {
  if (closingRequested) return;
  if (busy) {
    pendingProcess = true;
    return;
  }

  busy = true;

  setStatus("1/4 — Solicitando procesado a BrickLayers...");
  console.log("[BrickLayers] Iniciando /process");

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 180000);

  try {
    const response = await fetch("/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        startAtLayer: Number($("startAtLayer").value === "" ? 3 : $("startAtLayer").value),
        extrusionMultiplier: Number($("extrusionMultiplier").value || 1.05),
        enabled: Number($("enabled").value)
      }),
      signal: controller.signal
    });

    clearTimeout(timeout);

    setStatus("2/4 — Respuesta recibida. Preparando visor...");
    console.log("[BrickLayers] Respuesta recibida");

    const data = await response.json();

    console.log("[BrickLayers] Diagnóstico:", data.diagnostics);
    console.log("[BrickLayers] Primeras líneas original:", data.original_preview);
    console.log("[BrickLayers] Primeras líneas procesado:", data.processed_preview);

    if (!response.ok) {
      throw new Error(data.error || "Error al procesar el G-code.");
    }
    
    if (!Array.isArray(data.layers)) {
      throw new Error(
        "El servidor no ha devuelto una lista de capas válida."
      );
    }

    if (data.layers.length === 0) {
      throw new Error(
        "El parser no ha detectado segmentos visibles en el G-code."
      );
    }

    layerData = data.layers;

    const bricklayersEnabled = Number(data.params.enabled) === 1;

    $("showOriginal").checked = !bricklayersEnabled;
    $("showBrick").checked = bricklayersEnabled;
    $("showOther").checked = true;

    $("showBrick").disabled = !bricklayersEnabled;

    setStatus(
      `3/4 — G-code procesado.\n` +
      `Capas recibidas: ${data.layers.length}\n` +
      `Tiempo BrickLayers: ${data.seconds.toFixed(2)} s`
    );


    const slider = $("layer");
    const count = layerData.length;

    slider.min = "0";
    slider.max = String(Math.max(0, count - 1));
    slider.step = "1";
    slider.value = slider.max;
    slider.disabled = count <= 1;

    $("originalLayerCount").textContent =
      data.layer_info.original_count;

    $("layerCount").textContent =
      data.layer_info.processed_count;

    $("processTime").textContent =
      data.seconds.toFixed(2);

    updateLayerLabel();

    setStatus("4/4 — Construyendo modelo 3D...");

    await new Promise(resolve => setTimeout(resolve, 30));

    prepareModelPlacement();

    if (data.viewer_dimensions) {
      viewerDimensions = data.viewer_dimensions;
    }

    console.log(
      "[BrickLayers] Dimensiones del perfil:",
      viewerDimensions
    );

    buildModel();
    updateGrid(displaySize);
    fitCamera();

    hideError();

    $("modelInfo").style.display = "block";

    setStatus(
      `Modelo cargado.\n` +
      `Capas: ${layerData.length}\n` +
      `Tiempo BrickLayers: ${data.seconds.toFixed(2)} s`
    );


  } catch (error) {
    const message =
      error.name === "AbortError"
        ? "El procesado tardó demasiado y se canceló."
        : (error.message || String(error));

    showError("Error en /process:\n" + message);
    console.error("[BrickLayers]", error);

  } finally {
    clearTimeout(timeout);
    busy = false;
    if (pendingProcess && !closingRequested) {
      pendingProcess = false;
      setTimeout(process, 0);
    }
  }
}

$("layer").addEventListener("input", () => {
  updateLayerLabel();
  buildModel();
});

window.addEventListener("resize", updateLayerLabel);

$("showOriginal").addEventListener("change", buildModel);
$("showBrick").addEventListener("change", buildModel);
$("showOther").addEventListener("change", buildModel);

let lastParams = null;

function currentParams() {
  return JSON.stringify({
    startAtLayer: Number($("startAtLayer").value === "" ? 3 : $("startAtLayer").value),
    extrusionMultiplier: Number($("extrusionMultiplier").value || 1.05),
    enabled: Number($("enabled").value)
  });
}

function scheduleProcess() {
  const params = currentParams();

  if (params === lastParams) {
    return;
  }

  clearTimeout(autoTimer);
  autoTimer = setTimeout(() => {
    lastParams = params;
    process();
  }, 600);
}

$("startAtLayer").addEventListener("input", scheduleProcess);
$("extrusionMultiplier").addEventListener("input", scheduleProcess);
$("enabled").addEventListener("change", scheduleProcess);

$("toggleParams").addEventListener("click", () => {
  const panel = $("params");
  panel.style.display = panel.style.display === "none" ? "block" : "none";
});

let closingRequested = false;

$("applyExit").addEventListener("click", async () => {
  if (closingRequested) return;

  closingRequested = true;
  clearTimeout(autoTimer);

  const params = {
    startAtLayer: Number($("startAtLayer").value),
    extrusionMultiplier: Number($("extrusionMultiplier").value),
    enabled: Number($("enabled").value)
  };

  setStatus(
    params.enabled === 0
      ? "Devolviendo el G-code original a OrcaSlicer..."
      : "Preparando el G-code BrickLayers para OrcaSlicer..."
  );

  try {
    const response = await fetch("/apply_exit", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(params)
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(
        data.error || `Error al finalizar: HTTP ${response.status}`
      );
    }

    setStatus(
      data.enabled === 0
        ? "G-code original aplicado. Cerrando..."
        : "G-code BrickLayers aplicado. Cerrando..."
    );

    setTimeout(() => window.close(), 800);

  } catch (error) {
    closingRequested = false;

    showError(
      "No se pudo aplicar y cerrar:\n" +
      (error.message || String(error))
    );
  }
});

function showError(text) {
  const banner = $("errorBanner");
  banner.textContent = text;
  banner.style.display = "block";

  setStatus(text, true);
}

function hideError() {
  $("errorBanner").style.display = "none";
}
// Procesa automáticamente al abrir la web.
lastParams = currentParams();
process();

</script>
</body>
</html>
"""

THUMBNAIL_BEGIN_RE = re.compile(
    rb"^\s*;\s*thumbnail(?:_[A-Z0-9]+)?\s+begin"
    rb"\s+(\d+)[xX](\d+)\b[^\r\n]*$",
    re.IGNORECASE,
)

THUMBNAIL_END_RE = re.compile(
    rb"^\s*;\s*thumbnail(?:_[A-Z0-9]+)?\s+end\b",
    re.IGNORECASE,
)

THUMBNAIL_CACHE = {
    "source": None,
    "png": None,
    "kind": None,
}

THUMBNAIL_LOCK = threading.Lock()

def extract_best_thumbnail(gcode_bytes):
    """
    Devuelve la miniatura válida de mayor resolución como PNG.
    No modifica el G-code.
    """
    collecting = False
    encoded_lines = []
    best_png = None
    best_score = (-1, -1)

    for line in gcode_bytes.splitlines():
        if THUMBNAIL_BEGIN_RE.match(line):
            collecting = True
            encoded_lines = []
            continue

        if not collecting:
            continue

        if THUMBNAIL_END_RE.match(line):
            collecting = False

            try:
                encoded = b"".join(encoded_lines)
                decoded = base64.b64decode(encoded, validate=True)

                with Image.open(BytesIO(decoded)) as image:
                    width, height = image.size

                    # Primero resolución total; en empate, lado menor.
                    score = (width * height, min(width, height))

                    if score <= best_score:
                        continue

                    image.load()

                    buffer = BytesIO()
                    image.convert("RGBA").save(buffer, format="PNG")

                    best_png = buffer.getvalue()
                    best_score = score

            except (
                binascii.Error,
                UnidentifiedImageError,
                OSError,
                ValueError,
                Image.DecompressionBombError,
            ):
                continue

            continue

        stripped = line.strip()

        if stripped.startswith(b";"):
            payload = stripped[1:].strip()
            encoded_lines.append(b"".join(payload.split()))

    return best_png

def read_gcode_config(gcode_bytes):
    config = {}
    inside = False

    for raw_line in gcode_bytes.decode(
        "utf-8",
        errors="replace",
    ).splitlines():
        line = raw_line.strip()

        # Admitir marcadores con o sin punto y coma.
        if line.startswith(";"):
            line = line[1:].strip()

        if line == "CONFIG_BLOCK_START":
            inside = True
            continue

        if line == "CONFIG_BLOCK_END":
            inside = False
            continue

        if not inside or "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()

    return config


def config_dimension(value, reference):
    """
    Convierte un valor en mm o porcentaje respecto a reference.

    Esta versión usa el primer valor de listas multiextrusor.
    """
    if value is None:
        return None

    text = str(value).strip().strip('"')
    text = re.split(r"[,;]", text, maxsplit=1)[0].strip()

    try:
        if text.endswith("%"):
            result = float(text[:-1]) * reference / 100.0
        else:
            result = float(text)
    except ValueError:
        return None

    if not (0 < result < float("inf")):
        return None

    return result


def read_viewer_dimensions(gcode_bytes):
    config = read_gcode_config(gcode_bytes)

    nozzle = (
        config_dimension(config.get("nozzle_diameter"), 1.0)
        or 0.4
    )

    height = (
        config_dimension(config.get("layer_height"), nozzle)
        or 0.2
    )

    initial_height = (
        config_dimension(
            config.get("initial_layer_print_height"),
            height,
        )
        or config_dimension(
            config.get("first_layer_height"),
            height,
        )
        or height
    )

    default_width = (
        config_dimension(config.get("line_width"), nozzle)
        or nozzle
    )

    keys = {
        "initial": "initial_layer_line_width",
        "outer_wall": "outer_wall_line_width",
        "inner_wall": "inner_wall_line_width",
        "sparse_infill": "sparse_infill_line_width",
        "solid_infill": "internal_solid_infill_line_width",
        "top_surface": "top_surface_line_width",
        "support": "support_line_width",
    }

    widths = {"default": default_width}

    for role, key in keys.items():
        widths[role] = (
            config_dimension(config.get(key), nozzle)
            or default_width
        )

    return {
        "nozzle_diameter": nozzle,
        "layer_height": height,
        "initial_layer_height": initial_height,
        "widths": widths,
    }

def viewer_role_from_type(label):
    label = label.strip().lower()
    label = label.replace("_", " ").replace("-", " ")
    label = " ".join(label.split())

    if "support" in label:
        return "support"

    if (
        "outer wall" in label
        or "wall outer" in label
        or "external perimeter" in label
    ):
        return "outer_wall"

    if (
        "inner wall" in label
        or "wall inner" in label
        or label in {"perimeter", "internal perimeter"}
    ):
        return "inner_wall"

    if "top surface" in label or "top solid infill" in label:
        return "top_surface"

    if (
        "internal solid infill" in label
        or "solid infill" in label
        or "bottom surface" in label
    ):
        return "solid_infill"

    if "sparse infill" in label or label in {"infill", "internal infill"}:
        return "sparse_infill"

    return "default"

@app.get("/")
def index():
    return render_template_string(PAGE)

@app.get("/thumbnail")
def thumbnail():
    source = STATE.get("original_gcode", b"")

    if not source:
        return "", 204

    with THUMBNAIL_LOCK:
        if THUMBNAIL_CACHE["source"] is not source:
            # Primera opción: imagen incluida por el laminador.
            png = extract_best_thumbnail(source)
            thumbnail_kind = "embedded"

            # Alternativa: generar una vista de deposición XY.
            if png is None:
                png = generate_gcode_thumbnail(
                    source,
                    parser=lambda gcode: parse_gcode(
                        gcode,
                        start_marker="; START_PRINT_BRICKLAYER",
                        ),
                    size=(600, 400),
                    max_segments=120000,
                )

                thumbnail_kind = (
                    "generated-isometric"
                    if png is not None
                    else "unavailable"
                )

            THUMBNAIL_CACHE["png"] = png
            THUMBNAIL_CACHE["kind"] = thumbnail_kind

            # Actualizar la referencia después de completar el trabajo.
            THUMBNAIL_CACHE["source"] = source

        png = THUMBNAIL_CACHE["png"]
        thumbnail_kind = THUMBNAIL_CACHE.get(
            "kind",
            "embedded",
        )

    if png is None:
        return "", 204

    response = send_file(
        BytesIO(png),
        mimetype="image/png",
        download_name="thumbnail.png",
        max_age=0,
    )

    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Thumbnail-Source"] = thumbnail_kind

    return response

def update_total_layers_comment(gcode_bytes, count):
    newline = (
        b"\r\n"
        if b"\r\n" in gcode_bytes
        else b"\n"
    )

    pattern = re.compile(
        rb"^[ \t]*;[ \t]*total[ \t]+layers[ \t]+count"
        rb"[ \t]*=[ \t]*\d+[ \t]*(?=\r?$)",
        re.IGNORECASE | re.MULTILINE,
    )

    replacement = (
        f"; total layers count = {count}".encode("ascii")
    )

    if pattern.search(gcode_bytes):
        return pattern.sub(replacement, gcode_bytes)

    return replacement + newline + gcode_bytes

@app.post("/process")
def process():
    with PROCESS_LOCK:
        if READY_TO_CLOSE.is_set():
            return jsonify(
                error="El resultado ya se ha aplicado y la aplicación está cerrando."
            ), 409

        return process_preview()


def process_preview():
    data = request.get_json(silent=True) or {}
    started = time.perf_counter()

    try:
        params = {
            "startAtLayer": int(data.get("startAtLayer", 3)),
            "extrusionMultiplier": float(
                data.get("extrusionMultiplier", 1.05)
            ),
            "enabled": int(data.get("enabled", 1)),
        }

        if params["enabled"] not in (0, 1):
            raise ValueError("Activado debe ser 0 o 1.")

    except (TypeError, ValueError) as error:
        return jsonify(
            error=f"Parámetros no válidos: {error}"
        ), 400

    original = STATE.get("original_gcode", b"")

    if not original:
        return jsonify(
            error="No se ha cargado el G-code original."
        ), 400

    try:
        original_layers, original_stats = parse_gcode(original)

        declared_original_count = read_declared_layer_count(original)
        original_count = (
            declared_original_count
            if declared_original_count is not None
            else len(original_layers)
        )

        if params["enabled"] == 0:
            # No ejecutar BrickLayers ni modificar los bytes originales.
            processed = original
            processed_stats = original_stats.copy()
            processed_count = len(original_layers)

            viewer_layers = []

            for layer in original_layers:
                segments = []

                for segment in layer["segments"]:
                    segments.append({
                        "kind": (
                            "original_inner"
                            if segment["kind"] == "inner"
                            else "other"
                        ),
                        "role": segment.get("role", "default"),
                        "points": segment["points"],
                    })

                viewer_layers.append({
                    "z": layer["z"],
                    "segments": segments,
                })

        else:
            processed = run_bricklayers(
                original,
                params["startAtLayer"],
                params["extrusionMultiplier"],
                params["enabled"],
            )

            processed_layers, processed_stats = parse_gcode(processed)
            processed_count = len(processed_layers)

            processed = update_total_layers_comment(
                processed,
                len(processed_layers),
            )

            viewer_layers = build_viewer_data(
                original_layers,
                processed_layers,
            )

        # Conserva el archivo temporal para la ruta /download.
        temporary_dir = tempfile.mkdtemp(prefix="bricklayers_viewer_")
        processed_path = Path(temporary_dir) / "preview.gcode"

        try:
            processed_path.write_bytes(processed)
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

        previous_dir = STATE.get("temporary_dir")

        # Actualizar el resultado solo cuando todo haya terminado bien.
        STATE["processed_gcode"] = processed
        STATE["params"] = params
        STATE["temporary_dir"] = temporary_dir
        STATE["processed_path"] = str(processed_path)

        if previous_dir:
            shutil.rmtree(previous_dir, ignore_errors=True)

        diagnostics = {
            "original_bytes": len(original),
            "processed_bytes": len(processed),
            "original_stats": original_stats,
            "processed_stats": processed_stats,
            "bricklayers_executed": params["enabled"] == 1,
            "identical_to_original": processed == original,
        }

        viewer_dimensions = read_viewer_dimensions(
          STATE["original_gcode"]
        )

        return jsonify(
            layers=viewer_layers,
            seconds=time.perf_counter() - started,
            params=params,
            diagnostics=diagnostics,
            viewer_dimensions=viewer_dimensions,
            layer_info={
                "original_count": original_count,
                "original_detected_count": len(original_layers),
                "original_count_source": (
                    "comment"
                    if declared_original_count is not None
                    else "calculated"
                ),
                "processed_count": processed_count,
                "viewer_count": len(viewer_layers),
            },
        )

    except Exception as error:
        app.logger.exception("Error preparando el G-code para el visor")

        return jsonify(
            error=str(error)
        ), 500

@app.post("/apply_exit")
def apply_exit():
    data = request.get_json(silent=True) or {}

    try:
        params = {
            "startAtLayer": int(data.get("startAtLayer", 3)),
            "extrusionMultiplier": float(
                data.get("extrusionMultiplier", 1.05)
            ),
            "enabled": int(data.get("enabled", 1)),
        }

        if params["enabled"] not in (0, 1):
            raise ValueError("Activado debe ser 0 o 1.")

    except (TypeError, ValueError) as error:
        return jsonify(
            error=f"Parámetros no válidos: {error}"
        ), 400

    try:
        with PROCESS_LOCK:
            if READY_TO_CLOSE.is_set():
                return jsonify(
                    error="El resultado ya se ha aplicado."
                ), 409

            original = STATE.get("original_gcode", b"")
            gcode_path = STATE.get("gcode_path")

            if not original:
                return jsonify(
                    error="No se ha cargado el G-code original."
                ), 400

            if not gcode_path or not Path(gcode_path).is_file():
                return jsonify(
                    error="No se encontró el archivo G-code de OrcaSlicer."
                ), 400

            if params["enabled"] == 0:
                # Devolver exactamente los bytes originales.
                # No ejecutar BrickLayers.
                final_gcode = original

            elif (
                STATE.get("processed_gcode")
                and STATE.get("params") == params
            ):
                # Reutilizar el resultado de la previsualización
                # solo si corresponde a estos mismos parámetros.
                final_gcode = STATE["processed_gcode"]

            else:
                # No existe un resultado actualizado: generarlo ahora.
                final_gcode = run_bricklayers(
                    original,
                    params["startAtLayer"],
                    params["extrusionMultiplier"],
                    1,
                )

            if params["enabled"] == 1:
                final_layers, _ = parse_gcode(final_gcode)
                final_gcode = update_total_layers_comment(
                    final_gcode, len(final_layers)
                )

            destination = Path(gcode_path)

            temporary_path = None

            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=destination.name + ".",
                    suffix=".bricklayers_tmp",
                    dir=str(destination.parent),
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(final_gcode)

                os.replace(temporary_path, destination)

            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

            STATE["processed_gcode"] = final_gcode
            STATE["params"] = params

            READY_TO_CLOSE.set()

            return jsonify(
                ok=True,
                enabled=params["enabled"],
                saved_to=str(destination),
            )

    except Exception as error:
        app.logger.exception("Error al aplicar y cerrar")

        return jsonify(
            error=str(error)
        ), 500


@app.get("/download")
def download():
    if not STATE["processed_gcode"]:
        return jsonify(error="Todavía no hay G-code procesado."), 404

    return send_file(
        STATE["processed_path"],
        as_attachment=True,
        download_name="bricklayers_output.gcode",
    )


# ---------------------------------------------------------------------------
# Modo postprocesador de OrcaSlicer
# ---------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Interfaz web para el postprocesador BrickLayers de OrcaSlicer."
    )

    parser.add_argument(
        "-startAtLayer",
        type=int,
        default=3,
        help="Capa desde la que se activa BrickLayers.",
    )

    parser.add_argument(
        "-extrusionMultiplier",
        type=float,
        default=1.05,
        help="Multiplicador de extrusión.",
    )

    parser.add_argument(
        "-enabled",
        type=int,
        default=1,
        choices=[0, 1],
        help="Activa o desactiva BrickLayers.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Puerto local del servidor web.",
    )

    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="No abre automáticamente el navegador.",
    )

    # OrcaSlicer añade la ruta del G-code temporal como último argumento.
    parser.add_argument(
        "gcode_file",
        nargs="?",
        default=None,
        help="Archivo G-code temporal generado por OrcaSlicer.",
    )

    # Tolera argumentos adicionales que pueda añadir OrcaSlicer.
    args, unknown = parser.parse_known_args()

    if unknown:
        print(
            f"Aviso: argumentos no reconocidos ignorados: {unknown}",
            file=sys.stderr,
        )

    return args


def find_gcode_path(args):
    if args.gcode_file:
        return Path(args.gcode_file).expanduser().resolve()

    # Reserva: busca el último argumento que parezca un G-code.
    for argument in reversed(sys.argv[1:]):
        candidate = Path(argument)
        if candidate.suffix.lower() in {".gcode", ".gco", ".g", ".pp"}:
            return candidate.expanduser().resolve()

    return None

def open_viewer_window(url):
    candidates = [
        shutil.which("msedge"),
        str(
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe"
        ),
        str(
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Microsoft/Edge/Application/msedge.exe"
        ),
        str(
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft/Edge/Application/msedge.exe"
        ),
    ]

    edge = next(
        (candidate for candidate in candidates
         if candidate and Path(candidate).is_file()),
        None,
    )

    if edge is None:
        print(
            "No se encontró Edge. Se abrirá el navegador predeterminado.",
            file=sys.stderr,
        )
        webbrowser.open(url)
        return None

    profile_dir = tempfile.mkdtemp(prefix="bricklayers_browser_")

    try:
        process = subprocess.Popen(
            [
                edge,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--noerrdialogs",
                "--disable-extensions",
                "--disable-sync",
                "--disable-features=TranslateUI",
                "--start-fullscreen",
                f"--app={url}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise

    def cleanup_profile():
        process.wait()
        time.sleep(1)
        shutil.rmtree(profile_dir, ignore_errors=True)

    threading.Thread(
        target=cleanup_profile,
        daemon=True,
    ).start()

    return process

def main():
    args = parse_arguments()

    gcode_path = find_gcode_path(args)

    if gcode_path is None:
        print(
            "OrcaSlicer no proporcionó la ruta del G-code temporal.\n"
            "No añadas manualmente un nombre de archivo de entrada.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not gcode_path.exists():
        print(
            f"El archivo G-code temporal no existe: {gcode_path}",
            file=sys.stderr,
        )
        sys.exit(2)

    original_gcode = gcode_path.read_bytes()

    if not original_gcode.strip():
        print(
            f"El archivo G-code está vacío: {gcode_path}",
            file=sys.stderr,
        )
        sys.exit(2)

    STATE["original_gcode"] = original_gcode
    STATE["gcode_path"] = str(gcode_path)
    STATE["params"] = {
        "startAtLayer": args.startAtLayer,
        "extrusionMultiplier": args.extrusionMultiplier,
        "enabled": args.enabled,
    }

    READY_TO_CLOSE.clear()

    server = make_server("127.0.0.1", args.port, app, threaded=True)
    SERVER["instance"] = server

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{args.port}"
    print(f"BrickLayers Web: {url}", file=sys.stderr)

    if not args.no_browser:
        open_viewer_window(url) #webbrowser.open(url)

    try:
        READY_TO_CLOSE.wait()
    except KeyboardInterrupt:
        print("Proceso cancelado por el usuario.", file=sys.stderr)
        sys.exit(130)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    if not STATE["processed_gcode"]:
        print(
            "No se generó ningún G-code procesado.\n"
            "Pulsa 'Aplicar y salir' después de procesar.",
            file=sys.stderr,
        )
        sys.exit(3)

    # OrcaSlicer espera que el postprocesador sustituya el contenido
    # del archivo temporal por el G-code final.
    temporary_file = gcode_path.with_name(gcode_path.name + ".bricklayers_tmp")
    temporary_file.write_bytes(STATE["processed_gcode"])
    temporary_file.replace(gcode_path)

    print(
        f"BrickLayers aplicado correctamente a: {gcode_path}",
        file=sys.stderr,
    )

if __name__ == "__main__":
    main()
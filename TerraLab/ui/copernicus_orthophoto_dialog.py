"""Interactive Copernicus HRIM orthophoto area selector.

The embedded web map is deliberately limited to interaction and display.  The
authoritative bounding box, projection, pixel dimensions and download request
remain Python objects from :mod:`TerraLab.data.copernicus`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Optional

from PyQt5.QtCore import (
    QCoreApplication,
    QObject,
    QUrl,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

# Qt WebEngine expects shared OpenGL contexts to be requested before the
# QApplication is constructed.  TerraLab imports its UI modules before doing
# so; the guard also keeps importing this module safe in embedding contexts.
if QCoreApplication.instance() is None:
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

try:  # Keep the rest of the layer manager usable if WebEngine is unavailable.
    from PyQt5.QtWebChannel import QWebChannel
    from PyQt5.QtWebEngineWidgets import QWebEngineSettings, QWebEngineView

    _WEB_ENGINE_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the local Qt install
    QWebChannel = None  # type: ignore[assignment]
    QWebEngineSettings = None  # type: ignore[assignment]
    QWebEngineView = None  # type: ignore[assignment]
    _WEB_ENGINE_IMPORT_ERROR = str(exc)

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.data.copernicus import (
    ATTRIBUTION,
    BBoxWgs84,
    DATA_POLICY_URL,
    DownloadRequest,
    PRODUCT_NAME,
    PRODUCT_URL,
    SERVICE_COVERAGE_WGS84,
    WMS_URL,
    estimate_selection,
    format_bytes_dual,
)


_DIALOG_STYLE = """
QDialog#copernicusOrthophotoDialog {
    background-color: #081326;
    color: #f3f7ff;
}
QDialog#copernicusOrthophotoDialog QLabel {
    color: #e8f0ff;
}
QDialog#copernicusOrthophotoDialog QLabel#dialogTitle {
    color: #fff0a6;
    font-size: 18px;
    font-weight: 700;
}
QDialog#copernicusOrthophotoDialog QLabel#subtitle {
    color: #a9c9e8;
}
QDialog#copernicusOrthophotoDialog QLabel#mapStatus[error="true"] {
    color: #ffb2ab;
    background-color: #3b1e2a;
    border: 1px solid #985266;
    border-radius: 5px;
    padding: 6px;
}
QDialog#copernicusOrthophotoDialog QLabel#mapStatus[error="false"] {
    color: #9be4c6;
}
QDialog#copernicusOrthophotoDialog QFrame#optionsPanel {
    background-color: #101f38;
    border: 1px solid #29466f;
    border-radius: 8px;
}
QDialog#copernicusOrthophotoDialog QComboBox,
QDialog#copernicusOrthophotoDialog QDoubleSpinBox {
    background-color: #0a172b;
    color: #eef5ff;
    border: 1px solid #315783;
    border-radius: 5px;
    padding: 5px;
}
QDialog#copernicusOrthophotoDialog QPushButton {
    background-color: #1b3d68;
    color: #f4f8ff;
    border: 1px solid #3c6799;
    border-radius: 6px;
    padding: 6px 11px;
}
QDialog#copernicusOrthophotoDialog QPushButton:hover {
    background-color: #245181;
}
QDialog#copernicusOrthophotoDialog QPushButton:disabled {
    background-color: #13243b;
    color: #6f819d;
}
"""


def build_map_html() -> str:
    """Return the complete Leaflet map document used by the web view."""

    template = r"""<!doctype html>
<html lang="ca">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Selecció Copernicus HRIM</title>
  <link rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        crossorigin="">
  <link rel="stylesheet"
        href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"
        crossorigin="">
  <style>
    html, body, #map { width: 100%; height: 100%; margin: 0; }
    body { background: #0b1727; font-family: sans-serif; }
    #cursor, #selection, #map-error {
      position: absolute;
      z-index: 1000;
      left: 10px;
      color: #f7fbff;
      background: rgba(7, 20, 37, .88);
      border: 1px solid rgba(159, 200, 238, .72);
      border-radius: 4px;
      padding: 5px 8px;
      box-shadow: 0 1px 4px rgba(0, 0, 0, .45);
      pointer-events: none;
      font-size: 12px;
    }
    #cursor { bottom: 30px; }
    #selection { bottom: 60px; }
    #map-error {
      display: none;
      top: 10px;
      right: 58px;
      left: auto;
      max-width: 440px;
      color: #ffd6d1;
      background: rgba(82, 20, 31, .94);
      border-color: #d77b89;
    }
    .leaflet-control-attribution {
      max-width: 78%;
      white-space: normal;
      color: #27364a;
    }
    .terralab-reset {
      width: 30px;
      height: 30px;
      line-height: 30px;
      text-align: center;
      background: #fff;
      color: #17324f;
      text-decoration: none;
      font-size: 18px;
      font-weight: bold;
    }
  </style>
</head>
<body>
  <div id="map" role="application"
       aria-label="Mapa interactiu per seleccionar una ortofoto"></div>
  <div id="cursor">Cursor: —</div>
  <div id="selection">Rectangle: cap selecció</div>
  <div id="map-error"></div>

  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
          crossorigin=""></script>
  <script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"
          crossorigin=""></script>
  <script>
  "use strict";
  const WMS_URL = __WMS_URL__;
  const PRODUCT_NAME = __PRODUCT_NAME__;
  const PRODUCT_URL = __PRODUCT_URL__;
  const DATA_POLICY_URL = __DATA_POLICY_URL__;
  const COPERNICUS_ATTRIBUTION = __ATTRIBUTION__;
  const EUROPE_BOUNDS = __EUROPE_BOUNDS__;
  let bridge = null;
  let map = null;
  let drawnItems = null;
  let selectionLayer = null;
  let pendingError = "";

  function showMapError(message) {
    const text = String(message || "No s'ha pogut carregar el mapa.");
    const node = document.getElementById("map-error");
    node.textContent = text;
    node.style.display = "block";
    if (bridge && bridge.reportError) {
      bridge.reportError(text);
    } else {
      pendingError = text;
    }
  }

  window.onerror = function(message) {
    showMapError("Error del visor cartogràfic: " + message);
  };

  function publishBounds(bounds) {
    if (!bounds || !bounds.isValid()) return;
    const value = {
      west: bounds.getWest(),
      south: bounds.getSouth(),
      east: bounds.getEast(),
      north: bounds.getNorth()
    };
    document.getElementById("selection").textContent =
      "Rectangle (EPSG:4326): O " + value.west.toFixed(6) +
      " · S " + value.south.toFixed(6) +
      " · E " + value.east.toFixed(6) +
      " · N " + value.north.toFixed(6);
    if (bridge && bridge.selectionChanged) {
      // Deliberately explicit: longitude first, latitude second.
      bridge.selectionChanged(
        value.west, value.south, value.east, value.north
      );
    }
  }

  function publishSelection(layer) {
    if (!layer) return;
    publishBounds(layer.getBounds());
  }

  function attachLiveEdit(layer) {
    layer.on("edit", function() { publishSelection(layer); });
  }

  function replaceSelection(layer, fit) {
    if (selectionLayer) {
      drawnItems.removeLayer(selectionLayer);
    }
    selectionLayer = layer;
    drawnItems.addLayer(layer);
    attachLiveEdit(layer);
    publishSelection(layer);
    if (fit) map.fitBounds(layer.getBounds(), {padding: [28, 28]});
  }

  function clearSelection(notifyHost) {
    if (selectionLayer) drawnItems.removeLayer(selectionLayer);
    selectionLayer = null;
    document.getElementById("selection").textContent =
      "Rectangle: cap selecció";
    if (notifyHost && bridge && bridge.selectionCleared) {
      bridge.selectionCleared();
    }
  }

  window.resetEuropeView = function() {
    if (map) map.fitBounds(EUROPE_BOUNDS, {padding: [8, 8]});
  };

  window.clearSelectionFromHost = function() {
    clearSelection(false);
  };

  window.setSelectionBounds = function(west, south, east, north) {
    if (!map || !drawnItems) return;
    const rectangle = L.rectangle(
      [[south, west], [north, east]],
      {color: "#ffdf58", weight: 2, fillOpacity: 0.12}
    );
    replaceSelection(rectangle, true);
  };

  function initialiseMap() {
    if (!window.L || !L.Control || !L.Control.Draw) {
      showMapError(
        "No s'han pogut carregar Leaflet i les eines de selecció."
      );
      return;
    }
    map = L.map("map", {
      zoomControl: true,
      minZoom: 2,
      maxZoom: 20,
      worldCopyJump: false
    });

    const osm = L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        maxZoom: 20,
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">' +
          'OpenStreetMap contributors</a>'
      }
    ).addTo(map);

    const copernicus = L.tileLayer.wms(WMS_URL, {
      layers: "HRIM_HR_TrueColour_2018",
      format: "image/png",
      transparent: false,
      version: "1.3.0",
      maxZoom: 20,
      attribution:
        '<a href="' + PRODUCT_URL + '">' + PRODUCT_NAME + '</a> · ' +
        COPERNICUS_ATTRIBUTION + ' · ' +
        '<a href="' + DATA_POLICY_URL + '">Política de dades</a>'
    }).addTo(map);

    L.control.layers(
      {"OpenStreetMap": osm},
      {"Copernicus HRIM 2018 True Colour": copernicus},
      {collapsed: true}
    ).addTo(map);

    drawnItems = new L.FeatureGroup().addTo(map);
    const drawControl = new L.Control.Draw({
      position: "topleft",
      draw: {
        polygon: false,
        polyline: false,
        circle: false,
        circlemarker: false,
        marker: false,
        rectangle: {
          shapeOptions: {
            color: "#ffdf58",
            weight: 2,
            fillOpacity: 0.12
          }
        }
      },
      edit: {
        featureGroup: drawnItems,
        edit: true,
        remove: true
      }
    });
    map.addControl(drawControl);

    const ResetControl = L.Control.extend({
      options: {position: "topleft"},
      onAdd: function() {
        const container = L.DomUtil.create(
          "div", "leaflet-bar leaflet-control"
        );
        const button = L.DomUtil.create("a", "terralab-reset", container);
        button.href = "#";
        button.title = "Restablir la vista d'Europa";
        button.setAttribute("aria-label", button.title);
        button.innerHTML = "⌂";
        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.on(button, "click", function(event) {
          L.DomEvent.preventDefault(event);
          window.resetEuropeView();
        });
        return container;
      }
    });
    map.addControl(new ResetControl());

    map.on(L.Draw.Event.CREATED, function(event) {
      replaceSelection(event.layer, false);
    });
    map.on(L.Draw.Event.EDITED, function(event) {
      event.layers.eachLayer(function(layer) { publishSelection(layer); });
    });
    map.on("draw:editmove", function(event) {
      publishSelection(event.layer);
    });
    map.on("draw:editresize", function(event) {
      publishSelection(event.layer);
    });
    map.on(L.Draw.Event.DELETED, function() {
      clearSelection(true);
    });
    map.on("mousemove", function(event) {
      const lon = event.latlng.lng;
      const lat = event.latlng.lat;
      document.getElementById("cursor").textContent =
        "Cursor (EPSG:4326): lon " + lon.toFixed(6) +
        " · lat " + lat.toFixed(6);
      if (bridge && bridge.cursorChanged) {
        bridge.cursorChanged(lon, lat);
      }
      // Leaflet.draw exposes the starting corner while the rectangle tool is
      // dragging.  Publishing this transient bound keeps the native size
      // estimate live; CREATED below remains the authoritative final shape.
      const drawToolbar = drawControl._toolbars &&
        drawControl._toolbars.draw;
      const rectangleMode = drawToolbar && drawToolbar._modes &&
        drawToolbar._modes.rectangle;
      const rectangleHandler = rectangleMode && rectangleMode.handler;
      if (
        rectangleHandler &&
        rectangleHandler.enabled() &&
        rectangleHandler._startLatLng
      ) {
        publishBounds(
          L.latLngBounds(rectangleHandler._startLatLng, event.latlng)
        );
      }
    });
    map.on("draw:drawstop", function() {
      if (selectionLayer) {
        publishSelection(selectionLayer);
      } else {
        clearSelection(true);
      }
    });

    let wmsTileLoaded = false;
    copernicus.on("tileload", function() {
      if (!wmsTileLoaded) {
        wmsTileLoaded = true;
        if (bridge && bridge.imageryReady) bridge.imageryReady();
      }
    });
    copernicus.on("tileerror", function() {
      showMapError(
        "El servei WMS de Copernicus no ha retornat una tessel·la. " +
        "Comprova la connexió i torna-ho a provar."
      );
    });

    window.resetEuropeView();
    setTimeout(function() { map.invalidateSize(); }, 0);
    if (bridge && bridge.mapReady) bridge.mapReady();
  }

  if (typeof qt === "undefined" || !qt.webChannelTransport) {
    showMapError("QWebChannel no està disponible al visor.");
  } else {
    new QWebChannel(qt.webChannelTransport, function(channel) {
      bridge = channel.objects.copernicusBridge;
      if (pendingError && bridge && bridge.reportError) {
        bridge.reportError(pendingError);
      }
      initialiseMap();
    });
  }
  </script>
</body>
</html>
"""
    if hasattr(SERVICE_COVERAGE_WGS84, "as_tuple"):
        coverage_west, coverage_south, coverage_east, coverage_north = (
            SERVICE_COVERAGE_WGS84.as_tuple()
        )
    else:
        (
            coverage_west,
            coverage_south,
            coverage_east,
            coverage_north,
        ) = SERVICE_COVERAGE_WGS84
    coverage = [
        [float(coverage_south), float(coverage_west)],
        [float(coverage_north), float(coverage_east)],
    ]
    replacements = {
        "__WMS_URL__": json.dumps(WMS_URL),
        "__PRODUCT_NAME__": json.dumps(PRODUCT_NAME),
        "__PRODUCT_URL__": json.dumps(PRODUCT_URL),
        "__DATA_POLICY_URL__": json.dumps(DATA_POLICY_URL),
        "__ATTRIBUTION__": json.dumps(ATTRIBUTION),
        "__EUROPE_BOUNDS__": json.dumps(coverage),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


class CopernicusMapBridge(QObject):
    """Typed QWebChannel boundary with an unambiguous coordinate order."""

    bboxChanged = pyqtSignal(float, float, float, float)
    bboxCleared = pyqtSignal()
    cursorPositionChanged = pyqtSignal(float, float)
    ready = pyqtSignal()
    imageryLoaded = pyqtSignal()
    error = pyqtSignal(str)

    @pyqtSlot(float, float, float, float)
    def selectionChanged(
        self, west: float, south: float, east: float, north: float
    ) -> None:
        values = tuple(float(value) for value in (west, south, east, north))
        if not all(math.isfinite(value) for value in values):
            self.error.emit("El rectangle conté coordenades no vàlides.")
            return
        west_f, south_f, east_f, north_f = values
        if west_f >= east_f or south_f >= north_f:
            self.error.emit("El rectangle seleccionat és buit o està invertit.")
            return
        self.bboxChanged.emit(west_f, south_f, east_f, north_f)

    @pyqtSlot()
    def selectionCleared(self) -> None:
        self.bboxCleared.emit()

    @pyqtSlot(float, float)
    def cursorChanged(self, longitude: float, latitude: float) -> None:
        lon = float(longitude)
        lat = float(latitude)
        if math.isfinite(lon) and math.isfinite(lat):
            self.cursorPositionChanged.emit(lon, lat)

    @pyqtSlot()
    def mapReady(self) -> None:
        self.ready.emit()

    @pyqtSlot()
    def imageryReady(self) -> None:
        self.imageryLoaded.emit()

    @pyqtSlot(str)
    def reportError(self, message: str) -> None:
        self.error.emit(str(message or "Error desconegut del visor cartogràfic."))


class CopernicusOrthophotoSelectionDialog(QDialog):
    """Select and size a Copernicus HRIM export request."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        initial_bbox: Optional[BBoxWgs84] = None,
        initial_request: Optional[DownloadRequest] = None,
        web_view_factory: Optional[Callable[[QWidget], QWidget]] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("copernicusOrthophotoDialog")
        self.setWindowTitle("Descàrrega automàtica · Ortofoto Copernicus")
        self.setModal(True)
        self.resize(1260, 790)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(_DIALOG_STYLE)

        restored_bbox = (
            initial_request.bbox_wgs84
            if initial_request is not None
            else initial_bbox
        )
        self._bbox: Optional[BBoxWgs84] = restored_bbox
        self._estimate = None
        self._download_request: Optional[DownloadRequest] = None
        self._request_output_name = (
            str(initial_request.output_name)
            if initial_request is not None
            else "copernicus_hrim_2018.tif"
        )
        self._request_tile_width_px = (
            int(initial_request.tile_width_px)
            if initial_request is not None
            else 4000
        )
        self._request_tile_height_px = (
            int(initial_request.tile_height_px)
            if initial_request is not None
            else 4000
        )
        self._request_max_concurrent_requests = (
            int(initial_request.max_concurrent_requests)
            if initial_request is not None
            else 2
        )
        self._map_loaded = False
        self._map_error = ""
        self._web_channel = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        title = QLabel("Ortofoto Copernicus HR Image Mosaic 2018")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        subtitle = QLabel(
            "Examina el mosaic oficial, dibuixa un rectangle i ajusta la "
            "resolució de sortida. Les coordenades de selecció són sempre "
            "longitud/latitud en EPSG:4326."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        splitter = QSplitter(Qt.Horizontal)
        self.map_container = QFrame()
        map_layout = QVBoxLayout(self.map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        self.web_view = self._make_web_view(web_view_factory)
        map_layout.addWidget(self.web_view, 1)
        splitter.addWidget(self.map_container)

        options = QFrame()
        options.setObjectName("optionsPanel")
        options.setMinimumWidth(330)
        options.setMaximumWidth(440)
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(12, 12, 12, 12)
        options_layout.setSpacing(9)

        map_actions = QHBoxLayout()
        self.btn_reset_map = QPushButton("Restablir Europa")
        self.btn_reset_map.clicked.connect(self.reset_europe_view)
        map_actions.addWidget(self.btn_reset_map)
        self.btn_clear_selection = QPushButton("Eliminar rectangle")
        self.btn_clear_selection.clicked.connect(self.clear_selection)
        self.btn_clear_selection.setEnabled(restored_bbox is not None)
        map_actions.addWidget(self.btn_clear_selection)
        options_layout.addLayout(map_actions)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.resolution_combo = QComboBox()
        for metres in (10.0, 20.0, 40.0, 80.0):
            label = (
                "10 m — resolució original"
                if metres == 10.0
                else f"{metres:g} m — resolució reduïda"
            )
            self.resolution_combo.addItem(label, metres)
        self.resolution_combo.addItem("Personalitzada…", None)
        self.resolution_combo.currentIndexChanged.connect(
            self._resolution_mode_changed
        )
        form.addRow("Resolució:", self.resolution_combo)

        self.custom_resolution = QDoubleSpinBox()
        # The native product is 10 m.  A custom value may downsample it, but
        # asking the service for a finer grid would falsely suggest extra
        # source detail and is rejected by the request model as well.
        self.custom_resolution.setRange(10.0, 1000.0)
        self.custom_resolution.setDecimals(1)
        self.custom_resolution.setSingleStep(1.0)
        self.custom_resolution.setValue(10.0)
        self.custom_resolution.setSuffix(" m")
        self.custom_resolution.setEnabled(False)
        self.custom_resolution.valueChanged.connect(self._refresh_estimate)
        form.addRow("Valor personalitzat:", self.custom_resolution)

        self.format_combo = QComboBox()
        self.format_combo.addItem(
            "GeoTIFF RGB U16 — preservació màxima",
            ("U16", "LZ77"),
        )
        self.format_combo.addItem(
            "GeoTIFF RGB U8",
            ("U8", "NONE"),
        )
        self.format_combo.addItem(
            "GeoTIFF RGB U8 comprimit — recomanat",
            ("U8", "LZ77"),
        )
        self.format_combo.setCurrentIndex(2)
        self.format_combo.currentIndexChanged.connect(self._refresh_estimate)
        form.addRow("Format:", self.format_combo)
        options_layout.addLayout(form)
        conversion_note = QLabel(
            "Per evitar el retall blanc que l’ImageServer produeix en TIFF "
            "U8, TerraLab baixa els fragments U16 natius i aplica un únic "
            "estirament global 2–98% al mosaic U8, sense costures entre "
            "fragments."
        )
        conversion_note.setWordWrap(True)
        conversion_note.setStyleSheet("color: #aebed8;")
        options_layout.addWidget(conversion_note)

        self.selection_label = QLabel("Cap rectangle seleccionat.")
        self.selection_label.setWordWrap(True)
        self.selection_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        options_layout.addWidget(self.selection_label)

        metrics_title = QLabel("Estimació de la selecció")
        metrics_font = metrics_title.font()
        metrics_font.setBold(True)
        metrics_title.setFont(metrics_font)
        options_layout.addWidget(metrics_title)

        metrics_form = QFormLayout()
        self.metric_width = self._metric_label()
        self.metric_height = self._metric_label()
        self.metric_area = self._metric_label()
        self.metric_raster = self._metric_label()
        self.metric_pixels = self._metric_label()
        self.metric_u16 = self._metric_label()
        self.metric_u8 = self._metric_label()
        self.metric_compressed = self._metric_label()
        self.metric_fragments = self._metric_label()
        metrics_form.addRow("Amplada aprox.:", self.metric_width)
        metrics_form.addRow("Alçada aprox.:", self.metric_height)
        metrics_form.addRow("Superfície:", self.metric_area)
        metrics_form.addRow("Raster:", self.metric_raster)
        metrics_form.addRow("Píxels totals:", self.metric_pixels)
        metrics_form.addRow("RGB U16 sense compressió:", self.metric_u16)
        metrics_form.addRow("RGB U8 sense compressió:", self.metric_u8)
        metrics_form.addRow("Mida comprimida (estimació):", self.metric_compressed)
        metrics_form.addRow("Fragments de descàrrega:", self.metric_fragments)
        options_layout.addLayout(metrics_form)
        options_layout.addStretch(1)

        dataset_reference = QLabel(
            "<b>Referència del conjunt complet de teseles</b> "
            "(no s’utilitza per calcular aquesta selecció):<br>"
            "1.079 teseles × 10.980 × 10.980 píxels × 3 bandes × 2 bytes "
            "≈ 780,5 GB (≈ 727 GiB)."
        )
        dataset_reference.setWordWrap(True)
        dataset_reference.setStyleSheet(
            "color: #aebed8; background: #0d1a30; "
            "border: 1px solid #29466f; border-radius: 5px; padding: 6px;"
        )
        options_layout.addWidget(dataset_reference)

        policy = QLabel(
            '<a style="color:#9dc8ef" href="'
            + PRODUCT_URL
            + '">'
            + PRODUCT_NAME
            + "</a><br>"
            + '<a style="color:#9dc8ef" href="'
            + DATA_POLICY_URL
            + '">Política de dades de Copernicus</a><br>'
            + ATTRIBUTION
            + "<br>TerraLab no està afiliat ni avalat oficialment per la "
            "Unió Europea, Copernicus o l’European Environment Agency."
        )
        policy.setWordWrap(True)
        policy.setOpenExternalLinks(True)
        options_layout.addWidget(policy)
        splitter.addWidget(options)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 360])
        root.addWidget(splitter, 1)

        self.lbl_map_status = QLabel("Carregant el visor cartogràfic…")
        self.lbl_map_status.setObjectName("mapStatus")
        self.lbl_map_status.setProperty("error", False)
        self.lbl_map_status.setWordWrap(True)
        root.addWidget(self.lbl_map_status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Cancel | QDialogButtonBox.Ok
        )
        self.buttons.button(QDialogButtonBox.Ok).setText(
            "Iniciar descàrrega"
        )
        self.buttons.button(QDialogButtonBox.Cancel).setText("Cancel·lar")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self.bridge = CopernicusMapBridge(self)
        self.bridge.bboxChanged.connect(self._on_bbox_changed)
        self.bridge.bboxCleared.connect(self._on_bbox_cleared)
        self.bridge.cursorPositionChanged.connect(self._on_cursor_changed)
        self.bridge.ready.connect(self._on_map_ready)
        self.bridge.imageryLoaded.connect(self._on_imagery_loaded)
        self.bridge.error.connect(self._show_map_error)
        self._install_web_channel()
        self._set_empty_metrics()
        if initial_request is not None:
            self._restore_request_controls(initial_request)
        self._refresh_estimate()
        self._load_map()

    @staticmethod
    def _metric_label() -> QLabel:
        label = QLabel("—")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def _make_web_view(
        self,
        factory: Optional[Callable[[QWidget], QWidget]],
    ) -> QWidget:
        if factory is not None:
            return factory(self)
        if QWebEngineView is None:
            fallback = QLabel(
                "Qt WebEngine no està disponible. Instal·la PyQtWebEngine "
                "per utilitzar el selector cartogràfic.",
                self,
            )
            fallback.setWordWrap(True)
            return fallback
        return QWebEngineView(self)

    def _install_web_channel(self) -> None:
        if QWebChannel is None or not hasattr(self.web_view, "page"):
            return
        try:
            page = self.web_view.page()
            self._web_channel = QWebChannel(page)
            self._web_channel.registerObject("copernicusBridge", self.bridge)
            page.setWebChannel(self._web_channel)
            settings = self.web_view.settings()
            settings.setAttribute(
                QWebEngineSettings.JavascriptEnabled,
                True,
            )
            settings.setAttribute(
                QWebEngineSettings.LocalContentCanAccessRemoteUrls,
                True,
            )
            self.web_view.loadFinished.connect(self._on_load_finished)
        except Exception as exc:
            self._show_map_error(
                f"No s'ha pogut inicialitzar el canal del mapa: {exc}"
            )

    def _load_map(self) -> None:
        if _WEB_ENGINE_IMPORT_ERROR and QWebEngineView is None:
            self._show_map_error(
                "Qt WebEngine no està disponible: "
                + _WEB_ENGINE_IMPORT_ERROR
            )
            return
        setter = getattr(self.web_view, "setHtml", None)
        if not callable(setter):
            self._show_map_error("El visor web no està disponible.")
            return
        try:
            setter(build_map_html(), QUrl("https://image.discomap.eea.europa.eu/"))
        except Exception as exc:
            self._show_map_error(f"No s'ha pogut carregar el mapa: {exc}")

    @property
    def bbox_wgs84(self) -> Optional[BBoxWgs84]:
        return self._bbox

    @property
    def download_request(self) -> Optional[DownloadRequest]:
        """Return the exact request represented by the current controls."""

        return self._download_request

    def _selected_resolution(self) -> float:
        value = self.resolution_combo.currentData()
        if value is None:
            return float(self.custom_resolution.value())
        return float(value)

    def _selected_encoding(self) -> tuple[str, str]:
        value = self.format_combo.currentData()
        if (
            isinstance(value, (tuple, list))
            and len(value) == 2
        ):
            return str(value[0]), str(value[1])
        return "U8", "LZ77"

    def _restore_request_controls(self, request: DownloadRequest) -> None:
        resolution = float(request.resolution_m)
        index = self.resolution_combo.findData(resolution)
        if index >= 0:
            self.resolution_combo.setCurrentIndex(index)
            self.custom_resolution.setEnabled(False)
        else:
            self.resolution_combo.setCurrentIndex(
                self.resolution_combo.count() - 1
            )
            self.custom_resolution.setValue(resolution)
            self.custom_resolution.setEnabled(True)

        encoding = (
            str(request.pixel_type).upper(),
            str(request.compression).upper(),
        )
        for format_index in range(self.format_combo.count()):
            value = self.format_combo.itemData(format_index)
            if isinstance(value, (tuple, list)) and tuple(
                str(component).upper() for component in value
            ) == encoding:
                self.format_combo.setCurrentIndex(format_index)
                break

    def _resolution_mode_changed(self, _index: int) -> None:
        custom = self.resolution_combo.currentData() is None
        self.custom_resolution.setEnabled(custom)
        self._refresh_estimate()

    def _on_bbox_changed(
        self, west: float, south: float, east: float, north: float
    ) -> None:
        try:
            self._bbox = BBoxWgs84(
                west=float(west),
                south=float(south),
                east=float(east),
                north=float(north),
            )
        except (TypeError, ValueError) as exc:
            self._show_map_error(f"Rectangle no vàlid: {exc}")
            return
        self.btn_clear_selection.setEnabled(True)
        self._refresh_estimate()

    def _on_bbox_cleared(self) -> None:
        self._bbox = None
        self._estimate = None
        self._download_request = None
        self.btn_clear_selection.setEnabled(False)
        self.selection_label.setText("Cap rectangle seleccionat.")
        self._set_empty_metrics()
        self._update_accept_enabled()

    def _on_cursor_changed(self, longitude: float, latitude: float) -> None:
        self.lbl_map_status.setToolTip(
            f"Cursor: lon {longitude:.6f}, lat {latitude:.6f} · EPSG:4326"
        )

    def _refresh_estimate(self, *_args) -> None:
        if self._bbox is None:
            self._download_request = None
            self.selection_label.setText("Cap rectangle seleccionat.")
            self._set_empty_metrics()
            self._update_accept_enabled()
            return
        pixel_type, compression = self._selected_encoding()
        try:
            request = DownloadRequest(
                bbox_wgs84=self._bbox,
                resolution_m=self._selected_resolution(),
                pixel_type=pixel_type,
                compression=compression,
                output_name=self._request_output_name,
                tile_width_px=self._request_tile_width_px,
                tile_height_px=self._request_tile_height_px,
                max_concurrent_requests=self._request_max_concurrent_requests,
            )
            estimate = estimate_selection(request)
        except Exception as exc:
            self._download_request = None
            self._estimate = None
            self._set_empty_metrics()
            self.selection_label.setText(
                "No s'ha pogut calcular la selecció: " + str(exc)
            )
            self._update_accept_enabled()
            return

        self._download_request = request
        self._estimate = estimate
        west, south, east, north = self._bbox.as_tuple()
        self.selection_label.setText(
            "Rectangle EPSG:4326\n"
            f"west={west:.6f}, south={south:.6f}\n"
            f"east={east:.6f}, north={north:.6f}"
        )
        self.metric_width.setText(f"{float(estimate.width_m) / 1000.0:,.2f} km")
        self.metric_height.setText(
            f"{float(estimate.height_m) / 1000.0:,.2f} km"
        )
        self.metric_area.setText(f"{float(estimate.area_km2):,.2f} km²")
        self.metric_raster.setText(
            f"{int(estimate.width_px):,} × {int(estimate.height_px):,} px"
        )
        self.metric_pixels.setText(f"{int(estimate.pixel_count):,}")
        self.metric_u16.setText(
            format_bytes_dual(int(estimate.raw_u16_bytes))
        )
        self.metric_u8.setText(
            format_bytes_dual(int(estimate.raw_u8_bytes))
        )
        self.metric_compressed.setText(
            f"{format_bytes_dual(int(estimate.compressed_estimate_min_bytes))}"
            " – "
            f"{format_bytes_dual(int(estimate.compressed_estimate_max_bytes))}"
        )
        self.metric_fragments.setText(f"{int(estimate.fragment_count):,}")
        self._update_accept_enabled()

    def _set_empty_metrics(self) -> None:
        for label in (
            self.metric_width,
            self.metric_height,
            self.metric_area,
            self.metric_raster,
            self.metric_pixels,
            self.metric_u16,
            self.metric_u8,
            self.metric_compressed,
            self.metric_fragments,
        ):
            label.setText("—")

    def _update_accept_enabled(self) -> None:
        button = getattr(self, "buttons", None)
        if button is None:
            return
        ok = button.button(QDialogButtonBox.Ok)
        ok.setEnabled(
            self._download_request is not None
            and (self._map_loaded or self._bbox is not None)
        )

    def _on_load_finished(self, succeeded: bool) -> None:
        if not bool(succeeded):
            self._show_map_error(
                "No s'ha pogut carregar el visor cartogràfic. "
                "Comprova la connexió a Internet."
            )
            return
        if not self._map_error:
            self._set_map_status(
                "Visor carregat. Esperant les imatges de Copernicus.",
                error=False,
            )

    def _on_map_ready(self) -> None:
        self._map_loaded = True
        if not self._map_error:
            self._set_map_status(
                "Mapa preparat. Dibuixa o modifica un rectangle.",
                error=False,
            )
        if self._bbox is not None:
            west, south, east, north = self._bbox.as_tuple()
            self._run_javascript(
                "setSelectionBounds("
                f"{west!r}, {south!r}, {east!r}, {north!r});"
            )
        self._update_accept_enabled()

    def _on_imagery_loaded(self) -> None:
        self._map_loaded = True
        self._map_error = ""
        self._set_map_status(
            "Mosaic Copernicus carregat. Selecciona l'àrea de descàrrega.",
            error=False,
        )
        self._update_accept_enabled()

    def _show_map_error(self, message: str) -> None:
        self._map_error = str(message or "Error desconegut del mapa.")
        self._set_map_status(self._map_error, error=True)
        self._update_accept_enabled()

    def _set_map_status(self, message: str, *, error: bool) -> None:
        label = getattr(self, "lbl_map_status", None)
        if label is None:
            return
        label.setText(str(message))
        label.setProperty("error", bool(error))
        label.style().unpolish(label)
        label.style().polish(label)

    def _run_javascript(self, script: str) -> None:
        page_getter = getattr(self.web_view, "page", None)
        if not callable(page_getter):
            return
        try:
            page_getter().runJavaScript(str(script))
        except Exception as exc:
            self._show_map_error(
                f"No s'ha pogut comunicar amb el mapa: {exc}"
            )

    def reset_europe_view(self) -> None:
        self._run_javascript("resetEuropeView();")

    def clear_selection(self) -> None:
        self._run_javascript("clearSelectionFromHost();")
        self._on_bbox_cleared()

    def accept(self) -> None:
        if self._download_request is None:
            QMessageBox.warning(
                self,
                "Falta una selecció",
                "Dibuixa un rectangle vàlid abans d'iniciar la descàrrega.",
            )
            return
        super().accept()

    def closeEvent(self, event) -> None:
        stopper = getattr(self.web_view, "stop", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                log_suppressed_exception(__name__, "CopernicusOrthophotoSelectionDialog.closeEvent")
        super().closeEvent(event)


# Shorter compatibility name for callers that do not include “Selection”.
CopernicusOrthophotoDialog = CopernicusOrthophotoSelectionDialog


__all__ = [
    "CopernicusMapBridge",
    "CopernicusOrthophotoDialog",
    "CopernicusOrthophotoSelectionDialog",
    "build_map_html",
]

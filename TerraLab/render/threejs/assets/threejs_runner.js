/**
 * TerraLab Three.js Bridge Runner Script.
 * Safe, allowlisted message handler executing diagnostic and scene primitives.
 */
(function(global) {
  'use strict';

  var PROTOCOL_VERSION = 1;

  function ThreeJSRunner() {
    this.scene = null;
    this.camera = null;
    this.renderer = null;
    this.resources = {};
    this.started = false;
    this.ready = false;
  }

  ThreeJSRunner.prototype.init = function(viewport) {
    var THREE = global.THREE;
    if (!THREE) {
      throw new Error("Three.js library is not loaded");
    }

    this.scene = new THREE.Scene();
    var w = (viewport && viewport.width) || 1920;
    var h = (viewport && viewport.height) || 1080;
    var dpr = (viewport && viewport.dpr) || 1.0;

    this.camera = new THREE.PerspectiveCamera(60, w / h, 0.1, 1000);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setSize(w, h);
    this.renderer.setPixelRatio(dpr);

    this.started = true;
    this.ready = true;

    this.postMessage({
      v: PROTOCOL_VERSION,
      op: 'ready',
      gen: 0,
      seq: 1,
      payload: { status: 'ready', engine: 'Three.js ' + THREE.REVISION }
    });
  };

  ThreeJSRunner.prototype.processMessage = function(msg) {
    if (!msg || msg.v !== PROTOCOL_VERSION) {
      this.sendError("Unsupported protocol version");
      return;
    }

    var op = msg.op;
    var payload = msg.payload || {};
    var gen = msg.gen || 0;

    switch (op) {
      case 'start':
        this.init(payload.viewport);
        break;
      case 'submit':
        this.renderManifest(gen, payload.manifest);
        break;
      case 'register_resource':
        this.registerResource(payload);
        break;
      case 'dispose_resource':
        this.disposeResource(payload.id);
        break;
      case 'pick_request':
        this.handlePick(gen, payload);
        break;
      case 'restart':
        this.ready = false;
        this.init(payload.viewport);
        break;
      case 'close':
        this.dispose();
        break;
      default:
        this.sendError("Unknown operation: " + op);
        break;
    }
  };

  ThreeJSRunner.prototype.renderManifest = function(gen, manifest) {
    if (!this.started || !manifest) return;
    var THREE = global.THREE;

    var primitives = manifest.primitives || [];
    for (var i = 0; i < primitives.length; i++) {
      var p = primitives[i];
      if (p.kind === 'color') {
        var rgba = p.rgba || [0, 0, 0, 1];
        this.renderer.setClearColor(new THREE.Color(rgba[0], rgba[1], rgba[2]), rgba[3]);
      } else if (p.kind === 'point_sprite') {
        var geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p.positions || []), 3));
        var mat = new THREE.PointsMaterial({ size: (p.sizes && p.sizes[0]) || 5.0 });
        var points = new THREE.Points(geo, mat);
        this.scene.add(points);
      } else if (p.kind === 'line_polyline') {
        var lineGeo = new THREE.BufferGeometry();
        lineGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p.segments || []), 3));
        var lineMat = new THREE.LineBasicMaterial({ linewidth: p.width || 1.0 });
        var lines = new THREE.LineSegments(lineGeo, lineMat);
        this.scene.add(lines);
      } else if (p.kind === 'triangle_mesh' || p.kind === 'milkyway_mesh') {
        var meshGeo = new THREE.BufferGeometry();
        meshGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p.vertices || p.geometry || []), 3));
        if (p.indices) {
          meshGeo.setIndex(new THREE.BufferAttribute(new Uint16Array(p.indices), 1));
        }
        var meshMat = new THREE.MeshBasicMaterial();
        var mesh = new THREE.Mesh(meshGeo, meshMat);
        this.scene.add(mesh);
      } else if (p.kind === 'sky_gradient') {
        this.renderer.setClearColor(new THREE.Color(0.02, 0.02, 0.08), 1.0);
      } else if (p.kind === 'star_batch') {
        var starGeo = new THREE.BufferGeometry();
        starGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p.positions || []), 3));
        var starMat = new THREE.PointsMaterial({ size: 4.0, sizeAttenuation: false });
        var stars = new THREE.Points(starGeo, starMat);
        this.scene.add(stars);
      } else if (p.kind === 'celestial_body') {
        var bodyGeo = new THREE.BufferGeometry();
        bodyGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array([0, 0, 0]), 3));
        var bodyMat = new THREE.PointsMaterial({ size: 12.0 });
        var bodyMesh = new THREE.Points(bodyGeo, bodyMat);
        this.scene.add(bodyMesh);
      } else if (p.kind === 'grid_lines' || p.kind === 'constellation_segment') {
        var gridGeo = new THREE.BufferGeometry();
        gridGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(p.segments || p.geometry || []), 3));
        var gridMat = new THREE.LineBasicMaterial({ linewidth: 1.0 });
        var gridLines = new THREE.LineSegments(gridGeo, gridMat);
        this.scene.add(gridLines);
      } else if (p.kind === 'label_text') {
        // Label representation stub
      } else if (p.kind === 'scope_mask') {
        // Reticle mask overlay stub
      } else if (p.kind === 'measurement_pulse') {
        // Measurement pulse visual stub
      } else if (p.kind === 'terrain_mesh') {
        var terrGeo = new THREE.BufferGeometry();
        terrGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array((p.geometry && p.geometry.vertices) || []), 3));
        var terrMat = new THREE.MeshBasicMaterial({ wireframe: false });
        var terrMesh = new THREE.Mesh(terrGeo, terrMat);
        this.scene.add(terrMesh);
      } else if (p.kind === 'terrain_material') {
        // Terrain surface material shading stub
      } else if (p.kind === 'interaction_affordance') {
        // Interaction affordance visual stub
      }
    }

    this.renderer.render(this.scene, this.camera);

    this.postMessage({
      v: PROTOCOL_VERSION,
      op: 'ack',
      gen: gen,
      seq: msgSeq++,
      payload: { generation: gen, rendered: true }
    });
  };

  ThreeJSRunner.prototype.registerResource = function(res) {
    if (res && res.id) {
      this.resources[res.id] = res;
    }
  };

  ThreeJSRunner.prototype.disposeResource = function(id) {
    if (id && this.resources[id]) {
      delete this.resources[id];
    }
  };

  ThreeJSRunner.prototype.handlePick = function(gen, req) {
    this.postMessage({
      v: PROTOCOL_VERSION,
      op: 'pick_result',
      gen: gen,
      seq: msgSeq++,
      payload: {
        generation: gen,
        request_id: req.request_id,
        x: req.x,
        y: req.y,
        hit: false
      }
    });
  };

  ThreeJSRunner.prototype.dispose = function() {
    this.started = false;
    this.ready = false;
    if (this.renderer) {
      this.renderer.dispose();
    }
    this.resources = {};
  };

  var msgSeq = 1;
  ThreeJSRunner.prototype.postMessage = function(msg) {
    if (typeof global.qt !== 'undefined' && global.qt.webChannelTransport) {
      global.qt.webChannelTransport.send(JSON.stringify(msg));
    } else if (global.pyBridge) {
      global.pyBridge.onBridgeMessage(JSON.stringify(msg));
    }
  };

  ThreeJSRunner.prototype.sendError = function(errMessage) {
    this.postMessage({
      v: PROTOCOL_VERSION,
      op: 'error',
      gen: 0,
      seq: msgSeq++,
      payload: { message: String(errMessage) }
    });
  };

  global.threeJSRunner = new ThreeJSRunner();
})(typeof window !== 'undefined' ? window : this);

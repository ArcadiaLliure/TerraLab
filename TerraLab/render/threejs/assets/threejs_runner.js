/* TerraLab local Three.js host.  It consumes resolved scene plans only. */
(function (global) {
  'use strict';

  var PROTOCOL_VERSION = 2;
  var MESSAGE_SEQUENCE = 1;

  function setRendererStatus(message, state) {
    var element = document.getElementById('renderer-status');
    if (!element) { return; }
    element.hidden = false;
    element.textContent = String(message);
    element.setAttribute('data-state', state || 'loading');
  }

  function hideRendererStatus() {
    var element = document.getElementById('renderer-status');
    if (element) { element.hidden = true; }
  }

  function ThreeJSRunner() {
    this.scene = null;
    this.camera = null;
    this.renderer = null;
    this.canvas = null;
    this.channelBridge = null;
    this.resources = {};
    this.resourceViews = {};
    this.resourcePendingVersions = {};
    this.objects = {};
    this.textureCache = {};
    this.textureByResource = {};
    this.textTextureCache = {};
    this.textTextureByPrimitive = {};
    this.materials = {};
    this.terrainGeometryCache = {};
    this.terrainTileCache = {};
    this.terrainMaterialRefs = {};
    this.terrainTextureRefs = {};
    this.terrainPickProxies = {};
    this.externalTextureCache = {};
    this.externalTextureByResource = {};
    this.terrainView = { azimuth_deg: 0, zoom: 1, elevation_deg: 0, vertical_ratio: 0 };
    this.terrainTransitionPending = false;
    this.worldGroup = null;
    this.screenOverlayGroup = null;
    this.textOverlayGroup = null;
    this.interactionOverlayGroup = null;
    this.viewport = { width: 1, height: 1, dpr: 1 };
    this.lastManifest = null;
    this.pendingFrame = null;
    this.lastGeneration = 0;
    this.started = false;
    this.ready = false;
    this.suspended = true;
    this.horizonTexture = null;
    this.whiteTexture = null;
    this.quadGeometry = null;
    this.deepSkyBase = null;
    this.screenQuadBase = null;
    this._contextLost = null;
    this.raycaster = null;
    this.horizonMask = null;
    this.firstFrameReported = false;
  }

  ThreeJSRunner.prototype.bindBridge = function (bridge) {
    var self = this;
    this.channelBridge = bridge;
    setRendererStatus('Three.js bridge connected; creating the WebGL surface…');
    if (global.console && global.console.warn) {
      global.console.warn('[TerraLab Three.js] QWebChannel bridge attached');
    }
    bridge.outbound.connect(function (rawMessage) {
      try {
        self.processMessage(JSON.parse(rawMessage));
      } catch (error) {
        self.sendError('Malformed Qt message: ' + error.message, 'protocol');
      }
    });
    this.postMessage({
      v: PROTOCOL_VERSION,
      op: 'ready',
      gen: 0,
      seq: MESSAGE_SEQUENCE++,
      payload: { status: 'channel_ready' }
    });
  };

  ThreeJSRunner.prototype.init = function (viewport) {
    var THREE = global.THREE;
    if (!THREE || !THREE.WebGLRenderer) {
      throw new Error('The local Three.js WebGL library is unavailable');
    }
    this.dispose();
    this.viewport = this.normaliseViewport(viewport);
    this.scene = new THREE.Scene();
    this.raycaster = new THREE.Raycaster();
    this.worldGroup = new THREE.Group();
    this.worldGroup.name = 'world-resolved-plans';
    this.screenOverlayGroup = new THREE.Group();
    this.screenOverlayGroup.name = 'screen-overlays';
    this.textOverlayGroup = new THREE.Group();
    this.textOverlayGroup.name = 'screen-text';
    this.interactionOverlayGroup = new THREE.Group();
    this.interactionOverlayGroup.name = 'interactive-overlays';
    this.scene.add(this.worldGroup);
    this.scene.add(this.screenOverlayGroup);
    this.scene.add(this.textOverlayGroup);
    this.scene.add(this.interactionOverlayGroup);
    this.camera = new THREE.OrthographicCamera(
      0, this.viewport.width, this.viewport.height, 0, 0.1, 1000
    );
    this.camera.position.set(0, 0, 100);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(this.viewport.dpr);
    this.renderer.setSize(this.viewport.width, this.viewport.height, false);
    this.renderer.setClearColor(0x000000, 1.0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace || this.renderer.outputColorSpace;
    this.canvas = this.renderer.domElement;
    this.canvas.setAttribute('aria-label', 'TerraLab Three.js celestial surface');
    this.canvas.style.width = '100%';
    this.canvas.style.height = '100%';
    this._contextLost = this.onContextLost.bind(this);
    this.canvas.addEventListener('webglcontextlost', this._contextLost, false);
    var container = document.getElementById('canvas-container');
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }
    container.appendChild(this.canvas);
    this.started = true;
    this.ready = true;
    this.suspended = false;
    setRendererStatus('WebGL is ready; waiting for the resolved scene…');
    if (global.console && global.console.warn) {
      global.console.warn('[TerraLab Three.js] WebGL renderer initialized');
    }
    this.postMessage({
      v: PROTOCOL_VERSION,
      op: 'ready',
      gen: 0,
      seq: MESSAGE_SEQUENCE++,
      payload: {
        status: 'renderer_ready',
        engine: 'Three.js ' + THREE.REVISION,
        webgl: true
      }
    });
  };

  ThreeJSRunner.prototype.normaliseViewport = function (viewport) {
    var width = Number(viewport && viewport.width);
    var height = Number(viewport && viewport.height);
    var dpr = Number(viewport && viewport.dpr);
    return {
      width: Math.max(1, Math.round(isFinite(width) ? width : 1)),
      height: Math.max(1, Math.round(isFinite(height) ? height : 1)),
      dpr: Math.max(0.1, isFinite(dpr) ? dpr : 1)
    };
  };

  ThreeJSRunner.prototype.processMessage = function (message) {
    if (!message || message.v !== PROTOCOL_VERSION) {
      this.sendError('Unsupported protocol version', 'protocol');
      return;
    }
    try {
      var payload = message.payload || {};
      switch (message.op) {
        case 'start': this.init(payload.viewport); break;
        case 'resize': this.resize(payload.viewport); break;
        case 'visibility': this.setVisibility(payload, message.gen || 0); break;
        case 'submit': this.submitFrame(message.gen || 0, payload); break;
        case 'pick_request': this.performPick(message.gen || 0, payload); break;
        case 'register_resource':
        case 'update_resource': this.registerResource(payload); break;
        case 'dispose_resource': this.disposeResource(payload.id); break;
        case 'invalidate_resource': this.invalidateResource(payload.id); break;
        case 'restart': this.init(payload.viewport); break;
        case 'close': this.dispose(); break;
        default: throw new Error('Unknown operation: ' + message.op);
      }
    } catch (error) {
      var requestId = message.op === 'pick_request' && payload && payload.request_id ? String(payload.request_id) : null;
      this.sendError(error.message || String(error), 'operation', Number(message.gen || 0), requestId, String(message.op || 'unknown'));
    }
  };

  ThreeJSRunner.prototype.resize = function (viewport) {
    if (!this.started || !this.renderer || !this.camera) {
      throw new Error('Cannot resize before START');
    }
    this.viewport = this.normaliseViewport(viewport);
    this.renderer.setPixelRatio(this.viewport.dpr);
    this.renderer.setSize(this.viewport.width, this.viewport.height, false);
    this.camera.right = this.viewport.width;
    this.camera.top = this.viewport.height;
    this.camera.updateProjectionMatrix();
    this.resizeFullscreenLayers();
    this.renderLastManifest();
    this.ack(0, 'resize');
  };

  ThreeJSRunner.prototype.setVisibility = function (payload, generation) {
    this.suspended = Boolean(payload.suspended) || !Boolean(payload.visible);
    if (!this.suspended) {
      this.renderLastManifest();
    }
    this.ack(generation, 'visibility');
  };

  ThreeJSRunner.prototype.submitFrame = function (generation, payload) {
    var manifest;
    if (payload.mode === 'full') {
      manifest = payload.manifest;
    } else if (payload.mode === 'delta') {
      if (!this.lastManifest) {
        this.sendError('Delta received without a base manifest', 'resync_required');
        return;
      }
      if (Number(payload.base_generation) !== Number(this.lastGeneration)) {
        this.sendError(
          'Delta base generation ' + String(payload.base_generation) +
          ' does not match retained generation ' + String(this.lastGeneration),
          'resync_required'
        );
        return;
      }
      manifest = Object.assign({}, this.lastManifest, payload.patch || {});
      (payload.removed || []).forEach(function (key) { delete manifest[key]; });
    } else {
      this.sendError('Unknown frame mode', 'protocol');
      return;
    }
    if (!this.requiredResourcesReady(manifest)) {
      // Only the newest self-consistent frame may survive asynchronous
      // resource loading.  A later full replaces an obsolete pending full.
      this.pendingFrame = {
        generation: generation,
        payload: payload,
        manifest: manifest
      };
      setRendererStatus('Loading resolved GPU resources…');
      return;
    }
    // A ready frame supersedes any older frame that was waiting for buffers.
    this.pendingFrame = null;
    this.renderManifest(generation, manifest);
  };

  ThreeJSRunner.prototype.requiredResourcesReady = function (manifest) {
    var primitives = (manifest && manifest.primitives) || [];
    for (var index = 0; index < primitives.length; index += 1) {
      var buffers = primitives[index].buffers || {};
      var names = Object.keys(buffers);
      for (var offset = 0; offset < names.length; offset += 1) {
        var reference = buffers[names[offset]];
        var resource = this.resources[reference.resource_id];
        if (!resource || resource.descriptor.version !== reference.version) {
          return false;
        }
      }
    }
    return true;
  };

  ThreeJSRunner.prototype.renderManifest = function (generation, manifest) {
    if (!this.started || !this.renderer || !this.scene || !this.camera) {
      throw new Error('Cannot submit before START');
    }
    if (!manifest || !Array.isArray(manifest.primitives)) {
      throw new Error('Render manifest must contain resolved primitives');
    }
    var primitives = manifest.primitives.slice().sort(function (left, right) {
      return Number(left.layer_order || 0) - Number(right.layer_order || 0);
    });
    this.terrainView = manifest.terrain_view || { azimuth_deg: 0, zoom: 1, elevation_deg: 0, vertical_ratio: 0 };
    var sky = primitives.filter(function (primitive) {
      return primitive.kind === 'sky_gradient';
    })[0];
    this.horizonTexture = sky ? this.textureForBuffer(sky, 'rgba', sky.sample_width, sky.sample_height) : this.getWhiteTexture();
    this.horizonMask = sky ? {
      values: this.bufferView(sky, 'rgba'), width: Number(sky.sample_width), height: Number(sky.sample_height)
    } : null;
    var active = {};
    for (var index = 0; index < primitives.length; index += 1) {
      var primitive = primitives[index];
      var id = primitive.primitive_id || ('primitive:' + index + ':' + primitive.kind);
      active[id] = true;
      this.upsertPrimitive(primitive, id);
    }
    this.removeInactiveObjects(active);
    this.lastManifest = manifest;
    this.lastGeneration = generation;
    if (!this.suspended) {
      this.renderer.render(this.scene, this.camera);
      if (!this.firstFrameReported && global.console && global.console.warn) {
        this.firstFrameReported = true;
        global.console.warn('[TerraLab Three.js] First resolved frame rendered', {
          generation: generation,
          primitives: primitives.length
        });
      }
      hideRendererStatus();
    } else if (global.console && global.console.warn) {
      global.console.warn('[TerraLab Three.js] Frame received while surface is suspended', {
        generation: generation,
        primitives: primitives.length
      });
    }
    this.ack(generation, 'render', { rendered: !this.suspended });
  };

  ThreeJSRunner.prototype.upsertPrimitive = function (primitive, id) {
    var kind = primitive.kind;
    if (kind === 'color') {
      this.applyColor(primitive);
    } else if (kind === 'sky_gradient') {
      this.upsertSky(primitive, id);
    } else if (kind === 'star_batch') {
      this.upsertStars(primitive, id);
    } else if (kind === 'celestial_body') {
      this.upsertBodies(primitive, id);
    } else if (kind === 'milkyway_mesh') {
      this.upsertMilkyWay(primitive, id);
    } else if (kind === 'deep_sky_batch') {
      this.upsertDeepSky(primitive, id);
    } else if (kind === 'grid_lines') {
      this.upsertLines(primitive, id, false);
    } else if (kind === 'trail_lines') {
      this.upsertLines(primitive, id, true);
    } else if (kind === 'terrain_mesh') {
      this.upsertTerrain(primitive, id);
    } else if (kind === 'screen_line_batch') {
      this.upsertScreenLines(primitive, id);
    } else if (kind === 'screen_circle_batch') {
      this.upsertScreenCircles(primitive, id);
    } else if (kind === 'screen_rect_batch') {
      this.upsertScreenRects(primitive, id);
    } else if (kind === 'text_batch') {
      this.upsertTextBatch(primitive, id);
    } else if (kind === 'scope_mask') {
      this.upsertScopeMask(primitive, id);
    } else if (kind === 'interaction_affordance') {
      this.upsertInteractionAffordances(primitive, id);
    } else if (kind === 'point_sprite') {
      this.upsertGenericPoints(primitive, id);
    } else if (kind === 'line_polyline') {
      this.upsertGenericLines(primitive, id);
    } else if (kind === 'triangle_mesh') {
      this.upsertGenericMesh(primitive, id);
    } else if (kind === 'image') {
      this.upsertGenericImage(primitive, id);
    } else if (kind === 'text') {
      this.upsertGenericText(primitive, id);
    } else if (kind === 'clip_blend') {
      this.applyClipBlend(primitive);
    } else {
      throw new Error('Unsupported Three.js primitive: ' + String(kind));
    }
  };

  ThreeJSRunner.prototype.applyColor = function (primitive) {
    var THREE = global.THREE;
    var rgba = primitive.rgba || [0.0, 0.0, 0.0, 1.0];
    this.renderer.setClearColor(new THREE.Color(rgba[0], rgba[1], rgba[2]), rgba[3]);
  };

  ThreeJSRunner.prototype.bufferView = function (primitive, name) {
    var reference = primitive && primitive.buffers && primitive.buffers[name];
    if (!reference) {
      throw new Error('Missing resolved buffer ' + name);
    }
    var resource = this.resources[reference.resource_id];
    if (!resource || resource.descriptor.version !== reference.version) {
      throw new Error('Missing binary resource ' + reference.resource_id);
    }
    if (resource.descriptor.components !== reference.components) {
      throw new Error('Unexpected binary resource component count');
    }
    var key = reference.resource_id + '@' + reference.version;
    if (this.resourceViews[key]) {
      return this.resourceViews[key];
    }
    var result;
    if (resource.descriptor.dtype === 'float32') {
      result = new Float32Array(resource.buffer);
    } else if (resource.descriptor.dtype === 'uint32') {
      result = new Uint32Array(resource.buffer);
    } else if (resource.descriptor.dtype === 'uint8') {
      result = new Uint8Array(resource.buffer);
    } else {
      throw new Error('String-table resources cannot become GPU attributes');
    }
    this.resourceViews[key] = result;
    return result;
  };

  ThreeJSRunner.prototype.bufferKey = function (primitive, name) {
    var reference = primitive.buffers && primitive.buffers[name];
    if (!reference) {
      return '';
    }
    return reference.resource_id + '@' + reference.version;
  };

  ThreeJSRunner.prototype.replaceAttribute = function (geometry, primitive, name, components, normalized, dynamic, bufferName) {
    var THREE = global.THREE;
    var source = bufferName || name;
    var key = this.bufferKey(primitive, source);
    var existing = geometry.getAttribute(name);
    if (existing && existing._terralabResourceKey === key) {
      return existing;
    }
    var attribute = new THREE.BufferAttribute(this.bufferView(primitive, source), components, Boolean(normalized));
    if (dynamic) {
      attribute.setUsage(THREE.DynamicDrawUsage);
    }
    // BufferAttribute is not an Object3D and has no userData in Three.js.
    // A namespaced property keeps the retained-resource identity on the
    // attribute itself without depending on an unsupported API.
    attribute._terralabResourceKey = key;
    geometry.setAttribute(name, attribute);
    return attribute;
  };

  ThreeJSRunner.prototype.getWhiteTexture = function () {
    var THREE = global.THREE;
    if (!this.whiteTexture) {
      this.whiteTexture = new THREE.DataTexture(new Uint8Array([255, 255, 255, 255]), 1, 1, THREE.RGBAFormat, THREE.UnsignedByteType);
      this.whiteTexture.needsUpdate = true;
    }
    return this.whiteTexture;
  };

  ThreeJSRunner.prototype.textureForBuffer = function (primitive, name, width, height) {
    var THREE = global.THREE;
    var reference = primitive.buffers && primitive.buffers[name];
    if (!reference) {
      return this.getWhiteTexture();
    }
    var key = reference.resource_id + '@' + reference.version;
    if (this.textureCache[key]) {
      return this.textureCache[key];
    }
    var oldKey = this.textureByResource[reference.resource_id];
    if (oldKey && oldKey !== key && this.textureCache[oldKey]) {
      this.textureCache[oldKey].dispose();
      delete this.textureCache[oldKey];
    }
    var texture = new THREE.DataTexture(
      this.bufferView(primitive, name), Number(width), Number(height),
      THREE.RGBAFormat, THREE.UnsignedByteType
    );
    texture.needsUpdate = true;
    texture.flipY = false;
    texture.colorSpace = THREE.SRGBColorSpace || texture.colorSpace;
    this.textureCache[key] = texture;
    this.textureByResource[reference.resource_id] = key;
    return texture;
  };

  ThreeJSRunner.prototype.ensureQuad = function () {
    var THREE = global.THREE;
    if (!this.quadGeometry) {
      this.quadGeometry = new THREE.PlaneGeometry(1, 1);
    }
    return this.quadGeometry;
  };

  ThreeJSRunner.prototype.sharedMaterial = function (key, create) {
    if (!this.materials[key]) {
      this.materials[key] = create();
    }
    return this.materials[key];
  };

  ThreeJSRunner.prototype.setFullscreenTransform = function (mesh) {
    mesh.position.set(this.viewport.width * 0.5, this.viewport.height * 0.5, 0);
    mesh.scale.set(this.viewport.width, this.viewport.height, 1);
  };

  ThreeJSRunner.prototype.resizeFullscreenLayers = function () {
    var self = this;
    Object.keys(this.objects).forEach(function (id) {
      var object = self.objects[id];
      if (object && object.userData.fullscreen) {
        self.setFullscreenTransform(object);
      }
      if (object && object.material && object.material.uniforms) {
        self.setViewportUniforms(object.material);
      }
    });
  };

  ThreeJSRunner.prototype.setViewportUniforms = function (material) {
    if (!material || !material.uniforms) {
      return;
    }
    if (material.uniforms.uViewport) {
      material.uniforms.uViewport.value.set(this.viewport.width, this.viewport.height);
    }
    if (material.uniforms.uDpr) {
      material.uniforms.uDpr.value = this.viewport.dpr;
    }
    if (material.uniforms.uHorizon) {
      material.uniforms.uHorizon.value = this.horizonTexture || this.getWhiteTexture();
    }
  };

  ThreeJSRunner.prototype.horizonFragment = function () {
    return 'float horizonAlpha(){ vec2 uv=vec2(gl_FragCoord.x/(uViewport.x*uDpr),1.0-gl_FragCoord.y/(uViewport.y*uDpr)); return texture2D(uHorizon,uv).a; }';
  };

  ThreeJSRunner.prototype.makeSkyMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: { uTexture: { value: this.getWhiteTexture() } },
      vertexShader: 'varying vec2 vUv; void main(){vUv=uv; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: 'uniform sampler2D uTexture; varying vec2 vUv; void main(){gl_FragColor=texture2D(uTexture,vec2(vUv.x,1.0-vUv.y));}',
      transparent: true,
      depthTest: false,
      depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertSky = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(this.ensureQuad(), this.makeSkyMaterial());
      mesh.userData.fullscreen = true;
      this.objects[id] = mesh;
      this.worldGroup.add(mesh);
    }
    mesh.material.uniforms.uTexture.value = this.textureForBuffer(primitive, 'rgba', primitive.sample_width, primitive.sample_height);
    mesh.userData.version = primitive.version;
    mesh.renderOrder = Number(primitive.layer_order || 0);
    this.setFullscreenTransform(mesh);
  };

  ThreeJSRunner.prototype.makeStarMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: {
        uHorizon: { value: this.getWhiteTexture() },
        uViewport: { value: new THREE.Vector2(1, 1) },
        uDpr: { value: 1.0 },
        uPureColors: { value: false }
      },
      vertexShader: 'attribute vec4 color; attribute float radius; attribute float halo_bins; attribute float style; varying vec4 vColor; varying float vHalo; varying float vStyle; void main(){vColor=color; vHalo=halo_bins; vStyle=style; gl_PointSize=max(1.0,radius*2.0*(1.0+0.28*halo_bins)); gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: this.horizonFragment() + 'uniform bool uPureColors; varying vec4 vColor; varying float vHalo; varying float vStyle; void main(){if(horizonAlpha()<=0.001) discard; vec2 p=gl_PointCoord*2.0-1.0; float r=length(p); float core=1.0-smoothstep(0.18,0.58,r); float halo=(vStyle>1.5&&!uPureColors)?exp(-r*r*(2.2+0.38*vHalo))*0.42:0.0; float cross=max(1.0-smoothstep(0.025,0.11,abs(p.x)),1.0-smoothstep(0.025,0.11,abs(p.y))); float spikes=(vStyle>1.5&&!uPureColors)?cross*(1.0-smoothstep(0.18,1.0,r))*0.25:0.0; float alpha=vColor.a*max(core,max(halo,spikes)); if(alpha<0.01) discard; vec3 rgb=mix(vColor.rgb,vec3(1.0),uPureColors?0.0:core*0.32); gl_FragColor=vec4(rgb,alpha);}',
      transparent: true,
      depthTest: false,
      depthWrite: false,
      blending: THREE.NormalBlending
    });
  };

  ThreeJSRunner.prototype.upsertStars = function (primitive, id) {
    var points = this.objects[id];
    if (!points) {
      points = new global.THREE.Points(
        new global.THREE.BufferGeometry(),
        this.sharedMaterial('stars', this.makeStarMaterial.bind(this))
      );
      points.userData.sharedMaterial = true;
      this.objects[id] = points;
      this.worldGroup.add(points);
    }
    var geometry = points.geometry;
    this.replaceAttribute(geometry, primitive, 'position', 3, false, true, 'positions');
    this.replaceAttribute(geometry, primitive, 'color', 4, true, true, 'colors');
    this.replaceAttribute(geometry, primitive, 'radius', 1, false, true, 'radii');
    this.replaceAttribute(geometry, primitive, 'halo_bins', 1, false, true, 'halo_bins');
    this.replaceAttribute(geometry, primitive, 'style', 1, false, true, 'style');
    this.replaceAttribute(geometry, primitive, 'pick_catalog_index', 1, false, true, 'pick_catalog_indices');
    points.material.uniforms.uPureColors.value = Boolean(primitive.pure_colors);
    this.setViewportUniforms(points.material);
    points.renderOrder = Number(primitive.layer_order || 0);
    points.userData.version = primitive.version;
    points.userData.pickSpec = {
      kind: 'star', priority: 70,
      catalogIndices: this.bufferView(primitive, 'pick_catalog_indices'),
      altitudes: primitive.buffers.pick_altitude_deg ? this.bufferView(primitive, 'pick_altitude_deg') : null,
      azimuths: primitive.buffers.pick_azimuth_deg ? this.bufferView(primitive, 'pick_azimuth_deg') : null,
      magnitudes: primitive.buffers.pick_magnitude ? this.bufferView(primitive, 'pick_magnitude') : null
    };
    geometry.computeBoundingSphere();
  };

  ThreeJSRunner.prototype.replaceIndex = function (geometry, primitive, name) {
    var THREE = global.THREE;
    var key = this.bufferKey(primitive, name);
    var existing = geometry.getIndex();
    if (existing && existing._terralabResourceKey === key) {
      return existing;
    }
    var index = new THREE.BufferAttribute(this.bufferView(primitive, name), 1, false);
    index._terralabResourceKey = key;
    geometry.setIndex(index);
    return index;
  };

  ThreeJSRunner.prototype.terrainBlendMode = function (blendMode) {
    var THREE = global.THREE;
    if (blendMode === 'add') { return THREE.AdditiveBlending; }
    if (blendMode === 'multiply') { return THREE.MultiplyBlending; }
    return THREE.NormalBlending;
  };

  ThreeJSRunner.prototype.makeTerrainMaterial = function (blendMode) {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: {
        uViewport: { value: new THREE.Vector2(1, 1) },
        uAzimuthDeg: { value: 0.0 },
        uZoom: { value: 1.0 },
        uElevationDeg: { value: 0.0 },
        uVerticalRatio: { value: 0.0 },
        uProjectionPlane: { value: 0.0 },
        uTexture: { value: this.getWhiteTexture() },
        uUseTexture: { value: 0.0 },
        uOpacity: { value: 1.0 },
        uTileFade: { value: 1.0 }
      },
      vertexShader: 'attribute vec4 color; attribute float alpha; attribute vec2 uv; attribute vec3 normal; attribute float elevations_m; attribute float class_ids; attribute float categorical_flags; uniform vec2 uViewport; uniform float uAzimuthDeg; uniform float uZoom; uniform float uElevationDeg; uniform float uVerticalRatio; uniform float uProjectionPlane; varying vec4 vColor; varying float vAlpha; varying vec2 vUv; void main(){ vec2 screen=position.xy; if(uProjectionPlane>0.5){ float azimuth=radians(uAzimuthDeg); float side=position.x*cos(azimuth)-position.z*sin(azimuth); float forward=position.z*cos(azimuth)+position.x*sin(azimuth); float denominator=1.0+forward; if(denominator<=0.000001){gl_Position=vec4(2.0,2.0,0.0,1.0);return;} float scale=0.5*uViewport.y*uZoom; float elevationCenter=2.0*tan(radians(uElevationDeg)*0.5); screen=vec2(0.5*uViewport.x+(2.0*side/denominator)*scale,0.5*uViewport.y-uViewport.y*uVerticalRatio+((2.0*position.y/denominator)-elevationCenter)*scale); } vColor=color; vAlpha=alpha; vUv=uv; gl_Position=projectionMatrix*modelViewMatrix*vec4(screen,0.0,1.0); }',
      fragmentShader: 'uniform sampler2D uTexture; uniform float uUseTexture; uniform float uOpacity; uniform float uTileFade; varying vec4 vColor; varying float vAlpha; varying vec2 vUv; void main(){ vec4 resolved=vColor; if(uUseTexture>0.5){ resolved*=texture2D(uTexture,vUv); } float alpha=resolved.a*vAlpha*uOpacity*uTileFade; if(alpha<0.002) discard; gl_FragColor=vec4(resolved.rgb,alpha); }',
      transparent: blendMode !== 'opaque',
      depthTest: true,
      depthWrite: blendMode === 'opaque',
      blending: this.terrainBlendMode(blendMode),
      vertexColors: true
    });
  };

  ThreeJSRunner.prototype.terrainGeometry = function (primitive, id) {
    var key = id + '@' + String(primitive.geometry_version);
    var cached = this.terrainGeometryCache[key];
    if (cached) { return cached; }
    var THREE = global.THREE;
    var geometry = new THREE.BufferGeometry();
    this.replaceAttribute(geometry, primitive, 'position', 3, false, false, 'positions');
    this.replaceAttribute(geometry, primitive, 'normal', 3, false, false, 'normals');
    this.replaceIndex(geometry, primitive, 'indices');
    cached = { key: key, geometry: geometry };
    this.terrainGeometryCache[key] = cached;
    return cached;
  };

  ThreeJSRunner.prototype.releaseTerrainGeometry = function (key) {
    var cached = this.terrainGeometryCache[key];
    if (!cached) { return; }
    cached.geometry.dispose();
    delete this.terrainGeometryCache[key];
  };

  ThreeJSRunner.prototype.acquireTerrainMaterial = function (key, blendMode) {
    var material = this.sharedMaterial(key, this.makeTerrainMaterial.bind(this, blendMode));
    this.terrainMaterialRefs[key] = Number(this.terrainMaterialRefs[key] || 0) + 1;
    return material;
  };

  ThreeJSRunner.prototype.releaseTerrainMaterial = function (key) {
    if (!key) { return; }
    var remaining = Number(this.terrainMaterialRefs[key] || 0) - 1;
    if (remaining > 0) {
      this.terrainMaterialRefs[key] = remaining;
      return;
    }
    delete this.terrainMaterialRefs[key];
    if (this.materials[key]) {
      this.materials[key].dispose();
      delete this.materials[key];
    }
  };

  ThreeJSRunner.prototype.terrainTexture = function (primitive) {
    var THREE = global.THREE;
    var descriptor = primitive.texture_resource;
    var parameters = primitive.texture;
    if (!descriptor || !parameters) { return this.getWhiteTexture(); }
    var source = String(descriptor.source || '');
    var allowed = /^data:image\//.test(source) || /\.(png|jpe?g|webp|bmp)([?#].*)?$/i.test(source);
    if (!allowed) {
      throw new Error('Terrain textures must be CPU-prepared RGB image resources');
    }
    var resourceId = String(descriptor.texture_id || parameters.texture_id);
    var key = resourceId + '@' + String(descriptor.version || parameters.version);
    if (this.externalTextureCache[key]) { return this.externalTextureCache[key]; }
    var previous = this.externalTextureByResource[resourceId];
    if (previous && previous !== key && this.externalTextureCache[previous]) {
      if (this.externalTextureCache[previous] !== this.whiteTexture) {
        this.externalTextureCache[previous].dispose();
      }
      delete this.externalTextureCache[previous];
    }
    var texture = this.getWhiteTexture();
    this.externalTextureCache[key] = texture;
    this.externalTextureByResource[resourceId] = key;
    var self = this;
    new THREE.TextureLoader().load(source, function (loaded) {
      loaded.colorSpace = descriptor.color_space === 'linear' ? THREE.LinearSRGBColorSpace : (THREE.SRGBColorSpace || loaded.colorSpace);
      loaded.flipY = Boolean(parameters.flip_y);
      loaded.wrapS = self.terrainTextureWrap(parameters.wrap_u);
      loaded.wrapT = self.terrainTextureWrap(parameters.wrap_v);
      loaded.minFilter = self.terrainTextureFilter(parameters.min_filter);
      loaded.magFilter = self.terrainTextureFilter(parameters.mag_filter);
      if (self.externalTextureCache[key] === texture) {
        self.externalTextureCache[key] = loaded;
        Object.keys(self.objects).forEach(function (id) {
          var object = self.objects[id];
          if (object && object.userData.terrainTextureKey === key && object.material && object.material.uniforms) {
            object.material.uniforms.uTexture.value = loaded;
            object.material.uniforms.uUseTexture.value = 1.0;
          }
        });
        self.renderLastManifest();
      } else {
        loaded.dispose();
      }
    }, undefined, function (error) { self.sendError(String(error || 'Unable to load terrain texture'), 'terrain_texture'); });
    return texture;
  };

  ThreeJSRunner.prototype.terrainTextureWrap = function (value) {
    var THREE = global.THREE;
    if (value === 'repeat') { return THREE.RepeatWrapping; }
    if (value === 'mirror') { return THREE.MirroredRepeatWrapping; }
    return THREE.ClampToEdgeWrapping;
  };

  ThreeJSRunner.prototype.terrainTextureFilter = function (value) {
    var THREE = global.THREE;
    if (value === 'nearest') { return THREE.NearestFilter; }
    if (value === 'mipmap_linear') { return THREE.LinearMipmapLinearFilter; }
    return THREE.LinearFilter;
  };

  ThreeJSRunner.prototype.acquireTerrainTexture = function (key) {
    if (!key) { return; }
    this.terrainTextureRefs[key] = Number(this.terrainTextureRefs[key] || 0) + 1;
  };

  ThreeJSRunner.prototype.releaseTerrainTexture = function (key, resourceId) {
    if (!key) { return; }
    var remaining = Number(this.terrainTextureRefs[key] || 0) - 1;
    if (remaining > 0) {
      this.terrainTextureRefs[key] = remaining;
      return;
    }
    delete this.terrainTextureRefs[key];
    if (this.externalTextureCache[key]) {
      if (this.externalTextureCache[key] !== this.whiteTexture) { this.externalTextureCache[key].dispose(); }
      delete this.externalTextureCache[key];
    }
    if (resourceId && this.externalTextureByResource[resourceId] === key) {
      delete this.externalTextureByResource[resourceId];
    }
  };

  ThreeJSRunner.prototype.syncTerrainTiles = function (primitive, id, changed) {
    var active = {};
    (primitive.tiles || []).forEach(function (tile) {
      var key = id + ':' + String(tile.tile_id);
      active[key] = true;
      var current = this.terrainTileCache[key];
      var version = String(primitive.geometry_version) + ':' + String(primitive.material_version);
      if (!current || current.version !== version || current.firstIndex !== Number(tile.first_index) || current.indexCount !== Number(tile.index_count)) {
        this.terrainTileCache[key] = {
          version: version,
          firstIndex: Number(tile.first_index),
          indexCount: Number(tile.index_count),
          bounds: tile.bounds || null,
          transitionStarted: Date.now()
        };
        changed.value = true;
      }
    }, this);
    var prefix = id + ':';
    Object.keys(this.terrainTileCache).forEach(function (key) {
      if (key.indexOf(prefix) === 0 && !active[key]) { delete this.terrainTileCache[key]; }
    }, this);
  };

  ThreeJSRunner.prototype.syncTerrainPickProxy = function (primitive, id, mesh) {
    var THREE = global.THREE;
    var key = id + '@' + String(primitive.geometry_version);
    var proxy = this.terrainPickProxies[id];
    if (!proxy || proxy.userData.geometryKey !== key) {
      if (proxy) {
        if (proxy.parent) { proxy.parent.remove(proxy); }
        proxy.geometry.dispose(); proxy.material.dispose();
      }
      var geometry = new THREE.BufferGeometry();
      this.replaceAttribute(geometry, primitive, 'position', 3, false, false, 'pick_positions');
      this.replaceIndex(geometry, primitive, 'indices');
      proxy = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false, depthTest: false }));
      proxy.renderOrder = -1000;
      proxy.userData.geometryKey = key;
      this.terrainPickProxies[id] = proxy;
      this.interactionOverlayGroup.add(proxy);
    }
    proxy.userData.pickSpec = {
      kind: 'terrain', priority: 10, primitiveId: id,
      worldGeometry: mesh.geometry, tiles: primitive.tiles || []
    };
    return proxy;
  };

  ThreeJSRunner.prototype.upsertTerrain = function (primitive, id) {
    var THREE = global.THREE;
    var mesh = this.objects[id];
    var geometryRecord = this.terrainGeometry(primitive, id);
    var materialInfo = primitive.material || {};
    var blendMode = String(materialInfo.blend_mode || 'alpha');
    var textureInfo = primitive.texture || null;
    var materialKey = 'terrain:' + String(primitive.material_version) + ':' + blendMode + ':' + (textureInfo ? String(textureInfo.texture_id) + '@' + String(textureInfo.version) : 'vertex-rgb');
    var tileChanged = { value: false };
    if (!mesh) {
      mesh = new THREE.Mesh(geometryRecord.geometry, this.acquireTerrainMaterial(materialKey, blendMode));
      mesh.userData.sharedMaterial = true;
      mesh.userData.terrainGeometryKey = geometryRecord.key;
      mesh.userData.terrainMaterialKey = materialKey;
      this.objects[id] = mesh;
      this.worldGroup.add(mesh);
      tileChanged.value = true;
    } else {
      if (mesh.userData.terrainGeometryKey !== geometryRecord.key) {
        this.releaseTerrainGeometry(mesh.userData.terrainGeometryKey);
        mesh.geometry = geometryRecord.geometry;
        mesh.userData.terrainGeometryKey = geometryRecord.key;
        tileChanged.value = true;
      }
      if (mesh.userData.terrainMaterialKey !== materialKey) {
        this.releaseTerrainMaterial(mesh.userData.terrainMaterialKey);
        mesh.material = this.acquireTerrainMaterial(materialKey, blendMode);
        mesh.userData.terrainMaterialKey = materialKey;
        tileChanged.value = true;
      }
    }
    var geometry = mesh.geometry;
    this.replaceAttribute(geometry, primitive, 'color', 4, true, false, 'colors');
    this.replaceAttribute(geometry, primitive, 'alpha', 1, false, false, 'alphas');
    this.replaceAttribute(geometry, primitive, 'uv', 2, false, false, 'uv');
    this.replaceAttribute(geometry, primitive, 'elevations_m', 1, false, false, 'elevations_m');
    this.replaceAttribute(geometry, primitive, 'class_ids', 1, false, false, 'class_ids');
    this.replaceAttribute(geometry, primitive, 'categorical_flags', 1, false, false, 'categorical_flags');
    this.syncTerrainTiles(primitive, id, tileChanged);
    var material = mesh.material;
    material.uniforms.uAzimuthDeg.value = Number(this.terrainView.azimuth_deg || 0);
    material.uniforms.uZoom.value = Number(this.terrainView.zoom || 1);
    material.uniforms.uElevationDeg.value = Number(this.terrainView.elevation_deg || 0);
    material.uniforms.uVerticalRatio.value = Number(this.terrainView.vertical_ratio || 0);
    material.uniforms.uProjectionPlane.value = primitive.coordinate_space === 'terrain_direction' ? 1.0 : 0.0;
    material.uniforms.uOpacity.value = Number(materialInfo.opacity == null ? 1.0 : materialInfo.opacity);
    material.uniforms.uTexture.value = this.terrainTexture(primitive);
    material.uniforms.uUseTexture.value = textureInfo ? 1.0 : 0.0;
    this.setViewportUniforms(material);
    var textureKey = textureInfo ? String(textureInfo.texture_id) + '@' + String(textureInfo.version) : '';
    if (mesh.userData.terrainTextureKey !== textureKey) {
      this.releaseTerrainTexture(mesh.userData.terrainTextureKey, mesh.userData.terrainTextureResourceId);
      this.acquireTerrainTexture(textureKey);
      mesh.userData.terrainTextureKey = textureKey;
      mesh.userData.terrainTextureResourceId = textureInfo ? String(textureInfo.texture_id) : '';
    }
    if (tileChanged.value) {
      mesh.userData.terrainTransitionStarted = Date.now();
      mesh.userData.terrainFade = 0.0;
    }
    mesh.onBeforeRender = function (renderer, scene, camera, activeGeometry, activeMaterial) {
      if (activeMaterial && activeMaterial.uniforms && activeMaterial.uniforms.uTileFade) {
        activeMaterial.uniforms.uTileFade.value = Number(mesh.userData.terrainFade == null ? 1.0 : mesh.userData.terrainFade);
      }
    };
    mesh.renderOrder = Number(primitive.layer_order || 60);
    mesh.userData.version = primitive.version;
    this.syncTerrainPickProxy(primitive, id, mesh);
  };

  ThreeJSRunner.prototype.releaseTerrainPrimitive = function (id, mesh) {
    var prefix = id + ':';
    Object.keys(this.terrainTileCache).forEach(function (key) {
      if (key.indexOf(prefix) === 0) { delete this.terrainTileCache[key]; }
    }, this);
    this.releaseTerrainGeometry(mesh.userData.terrainGeometryKey);
    this.releaseTerrainMaterial(mesh.userData.terrainMaterialKey);
    this.releaseTerrainTexture(mesh.userData.terrainTextureKey, mesh.userData.terrainTextureResourceId);
    var proxy = this.terrainPickProxies[id];
    if (proxy) {
      if (proxy.parent) { proxy.parent.remove(proxy); }
      proxy.geometry.dispose(); proxy.material.dispose();
      delete this.terrainPickProxies[id];
    }
  };

  ThreeJSRunner.prototype.advanceTerrainTransitions = function () {
    var self = this;
    var now = Date.now();
    var pending = false;
    Object.keys(this.objects).forEach(function (id) {
      var mesh = self.objects[id];
      if (!mesh || !mesh.userData.terrainTransitionStarted || !mesh.material || !mesh.material.uniforms) { return; }
      var fade = Math.max(0, Math.min(1, (now - mesh.userData.terrainTransitionStarted) / 180));
      mesh.userData.terrainFade = fade;
      if (fade < 1) { pending = true; } else { delete mesh.userData.terrainTransitionStarted; }
    });
    if (pending && !this.terrainTransitionPending) {
      this.terrainTransitionPending = true;
      global.requestAnimationFrame(function () {
        self.terrainTransitionPending = false;
        self.renderLastManifest();
      });
    }
  };

  ThreeJSRunner.prototype.makeMilkyWayMaterial = function (blendMode) {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: {
        uTexture: { value: this.getWhiteTexture() }, uOpacity: { value: 1.0 },
        uHorizon: { value: this.getWhiteTexture() }, uViewport: { value: new THREE.Vector2(1, 1) }, uDpr: { value: 1.0 }
      },
      vertexShader: 'varying vec2 vUv; void main(){vUv=uv; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: this.horizonFragment() + 'uniform sampler2D uTexture; uniform float uOpacity; varying vec2 vUv; void main(){if(horizonAlpha()<=0.001) discard; vec4 sampleColor=texture2D(uTexture,vec2(vUv.x,1.0-vUv.y)); sampleColor.a*=uOpacity; if(sampleColor.a<0.005) discard; gl_FragColor=sampleColor;}',
      transparent: true, depthTest: false, depthWrite: false,
      blending: blendMode === 'add' ? THREE.AdditiveBlending : THREE.NormalBlending
    });
  };

  ThreeJSRunner.prototype.upsertMilkyWay = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(this.ensureQuad(), this.makeMilkyWayMaterial(String(primitive.blend_mode || 'alpha')));
      mesh.userData.fullscreen = true;
      this.objects[id] = mesh;
      this.worldGroup.add(mesh);
    }
    mesh.material.uniforms.uTexture.value = this.textureForBuffer(primitive, 'rgba', primitive.width, primitive.height);
    mesh.material.uniforms.uOpacity.value = Number(primitive.opacity || 0);
    this.setViewportUniforms(mesh.material);
    mesh.renderOrder = Number(primitive.layer_order || 0);
    mesh.userData.version = primitive.version;
    this.setFullscreenTransform(mesh);
  };

  ThreeJSRunner.prototype.makeDeepSkyGeometry = function () {
    var THREE = global.THREE;
    if (!this.deepSkyBase) {
      this.deepSkyBase = new THREE.InstancedBufferGeometry();
      this.deepSkyBase.setAttribute('position', new THREE.BufferAttribute(new Float32Array([-1,-1, 1,-1, 1,1, -1,-1, 1,1, -1,1]), 2));
    }
    return this.deepSkyBase.clone();
  };

  ThreeJSRunner.prototype.makeDeepSkyMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: { uHorizon: { value: this.getWhiteTexture() }, uViewport: { value: new THREE.Vector2(1,1) }, uDpr: { value: 1.0 } },
      vertexShader: 'attribute vec2 iPosition; attribute vec2 iRadius; attribute float iRotation; attribute vec4 iColor; attribute vec2 iFlags; varying vec2 vLocal; varying vec4 vColor; varying vec2 vFlags; void main(){float a=radians(iRotation); mat2 r=mat2(cos(a),-sin(a),sin(a),cos(a)); vLocal=position.xy; vColor=iColor; vFlags=iFlags; vec2 p=iPosition+r*(position.xy*iRadius); gl_Position=projectionMatrix*modelViewMatrix*vec4(p,0.0,1.0);}',
      fragmentShader: this.horizonFragment() + 'varying vec2 vLocal; varying vec4 vColor; varying vec2 vFlags; void main(){if(horizonAlpha()<=0.001) discard; float d=length(vLocal); float rim=smoothstep(0.80,0.98,d)-smoothstep(0.98,1.04,d); float cross=max(1.0-smoothstep(0.035,0.10,abs(vLocal.x)),1.0-smoothstep(0.035,0.10,abs(vLocal.y))); float ticks=max(1.0-smoothstep(0.04,0.11,abs(vLocal.y))*step(0.92,abs(vLocal.x)),1.0-smoothstep(0.04,0.11,abs(vLocal.x))*step(0.92,abs(vLocal.y))); float alpha=max(rim,max(cross*vFlags.x*0.8,ticks*vFlags.y*0.8))*vColor.a; if(alpha<0.01) discard; gl_FragColor=vec4(vColor.rgb,alpha);}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertDeepSky = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(
        this.makeDeepSkyGeometry(),
        this.sharedMaterial('deep-sky', this.makeDeepSkyMaterial.bind(this))
      );
      mesh.userData.sharedMaterial = true;
      this.objects[id] = mesh;
      this.worldGroup.add(mesh);
    }
    var geometry = mesh.geometry;
    this.replaceAttribute(geometry, primitive, 'iPosition', 2, false, true, 'positions');
    this.replaceAttribute(geometry, primitive, 'iRadius', 2, false, true, 'radii');
    this.replaceAttribute(geometry, primitive, 'iRotation', 1, false, true, 'rotations');
    this.replaceAttribute(geometry, primitive, 'iColor', 4, true, true, 'colors');
    this.replaceAttribute(geometry, primitive, 'iFlags', 2, false, true, 'flags');
    geometry.instanceCount = primitive.buffers.positions.element_count;
    this.setViewportUniforms(mesh.material);
    mesh.renderOrder = Number(primitive.layer_order || 0);
    mesh.userData.version = primitive.version;
    mesh.userData.pickSpec = {
      kind: 'deep_sky_batch', priority: 80,
      records: primitive.pick_records || [],
      positions: this.bufferView(primitive, 'positions'),
      radii: this.bufferView(primitive, 'radii')
    };
  };

  ThreeJSRunner.prototype.makeLineMaterial = function (vertexColors) {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: { uColor: { value: new THREE.Vector4(1,1,1,1) }, uHorizon: { value: this.getWhiteTexture() }, uViewport: { value: new THREE.Vector2(1,1) }, uDpr: { value: 1.0 } },
      vertexShader: vertexColors ? 'attribute vec4 color; varying vec4 vColor; void main(){vColor=color; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}' : 'void main(){gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: this.horizonFragment() + (vertexColors ? 'varying vec4 vColor; void main(){if(horizonAlpha()<=0.001) discard; if(vColor.a<0.01) discard; gl_FragColor=vColor;}' : 'uniform vec4 uColor; void main(){if(horizonAlpha()<=0.001) discard; if(uColor.a<0.01) discard; gl_FragColor=uColor;}'),
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertLines = function (primitive, id, vertexColors) {
    var lines = this.objects[id];
    if (!lines) {
      lines = new global.THREE.LineSegments(
        new global.THREE.BufferGeometry(),
        this.sharedMaterial(
          vertexColors ? 'trails' : 'grid',
          this.makeLineMaterial.bind(this, vertexColors)
        )
      );
      lines.userData.sharedMaterial = true;
      this.objects[id] = lines;
      this.worldGroup.add(lines);
    }
    this.replaceAttribute(lines.geometry, primitive, 'position', 3, false, true, 'positions');
    if (vertexColors) {
      this.replaceAttribute(lines.geometry, primitive, 'color', 4, true, true, 'colors');
    } else {
      var rgba = primitive.rgba || [255, 255, 255, 255];
      lines.material.uniforms.uColor.value.set(rgba[0] / 255, rgba[1] / 255, rgba[2] / 255, rgba[3] / 255);
    }
    this.setViewportUniforms(lines.material);
    lines.renderOrder = Number(primitive.layer_order || 0);
    lines.userData.version = primitive.version;
  };

  ThreeJSRunner.prototype.ensureScreenQuadBase = function () {
    var THREE = global.THREE;
    if (!this.screenQuadBase) {
      this.screenQuadBase = new THREE.InstancedBufferGeometry();
      this.screenQuadBase.setAttribute('position', new THREE.BufferAttribute(new Float32Array([
        0,-1, 1,-1, 1,1,
        0,-1, 1,1, 0,1
      ]), 2));
    }
    return this.screenQuadBase;
  };

  ThreeJSRunner.prototype.makeScreenLineMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: {
        uColor: { value: new THREE.Vector4(1,1,1,1) },
        uWidth: { value: 1.0 },
        uDashed: { value: false }
      },
      vertexShader: 'attribute vec2 iStart; attribute vec2 iEnd; varying float vDistance; void main(){vec2 delta=iEnd-iStart; float lineLength=max(0.0001,length(delta)); vec2 normal=vec2(-delta.y,delta.x)/lineLength; vec2 point=mix(iStart,iEnd,position.x)+normal*position.y*uWidth*0.5; vDistance=position.x*lineLength; gl_Position=projectionMatrix*modelViewMatrix*vec4(point,0.0,1.0);}',
      fragmentShader: 'uniform vec4 uColor; uniform bool uDashed; varying float vDistance; void main(){if(uDashed&&mod(vDistance,8.0)>4.0) discard; if(uColor.a<0.01) discard; gl_FragColor=uColor;}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertScreenLines = function (primitive, id) {
    var lines = this.objects[id];
    if (!lines) {
      lines = new global.THREE.Mesh(this.ensureScreenQuadBase().clone(), this.makeScreenLineMaterial());
      this.objects[id] = lines;
      this.screenOverlayGroup.add(lines);
    }
    var geometry = lines.geometry;
    this.replaceAttribute(geometry, primitive, 'iStart', 2, false, false, 'starts');
    this.replaceAttribute(geometry, primitive, 'iEnd', 2, false, false, 'ends');
    geometry.instanceCount = primitive.buffers.starts.element_count;
    var rgba = primitive.rgba || [255,255,255,255];
    lines.material.uniforms.uColor.value.set(rgba[0]/255, rgba[1]/255, rgba[2]/255, rgba[3]/255);
    lines.material.uniforms.uWidth.value = Math.max(0.5, Number(primitive.width_px || 1));
    lines.material.uniforms.uDashed.value = Boolean(primitive.dashed);
    lines.renderOrder = Number(primitive.layer_order || 0);
    lines.userData.version = primitive.version;
    lines.userData.pickSpec = primitive.pick_kind ? {
      kind: 'screen_lines', objectKind: String(primitive.pick_kind),
      priority: Number(primitive.pick_priority || 100), primitiveId: id,
      starts: this.bufferView(primitive, 'starts'), ends: this.bufferView(primitive, 'ends'),
      width: Number(primitive.width_px || 1)
    } : null;
  };

  ThreeJSRunner.prototype.makeScreenCircleMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: { uDpr: { value: 1.0 } },
      vertexShader: 'attribute vec4 fill; attribute vec4 stroke; attribute float radius; attribute float strokeWidth; attribute float dashed; varying vec4 vFill; varying vec4 vStroke; varying float vRadius; varying float vStrokeWidth; varying float vDashed; uniform float uDpr; void main(){vFill=fill; vStroke=stroke; vRadius=radius; vStrokeWidth=strokeWidth; vDashed=dashed; gl_PointSize=max(1.0,(radius+strokeWidth+1.0)*2.0*uDpr); gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: 'varying vec4 vFill; varying vec4 vStroke; varying float vRadius; varying float vStrokeWidth; varying float vDashed; void main(){vec2 p=gl_PointCoord*2.0-1.0; float r=length(p); float outer=1.0-smoothstep(0.96,1.0,r); float width=max(0.002,vStrokeWidth/max(1.0,vRadius)); float ring=smoothstep(1.0-width,1.0,r)-smoothstep(1.0,1.0+width,r); if(vDashed>0.5){float angle=atan(p.y,p.x); if(mod((angle+3.14159265)*vRadius,8.0)>4.0) ring=0.0;} vec4 color=mix(vFill,vStroke,clamp(ring,0.0,1.0)); color.a=max(vFill.a*outer,vStroke.a*ring); if(color.a<0.01) discard; gl_FragColor=color;}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertScreenCircles = function (primitive, id) {
    var points = this.objects[id];
    if (!points) {
      points = new global.THREE.Points(new global.THREE.BufferGeometry(), this.sharedMaterial('screen-circles', this.makeScreenCircleMaterial.bind(this)));
      points.userData.sharedMaterial = true;
      this.objects[id] = points;
      this.screenOverlayGroup.add(points);
    }
    var geometry = points.geometry;
    this.replaceAttribute(geometry, primitive, 'position', 3, false, false, 'positions');
    this.replaceAttribute(geometry, primitive, 'fill', 4, true, false, 'fills');
    this.replaceAttribute(geometry, primitive, 'stroke', 4, true, false, 'strokes');
    this.replaceAttribute(geometry, primitive, 'radius', 1, false, false, 'radii');
    this.replaceAttribute(geometry, primitive, 'strokeWidth', 1, false, false, 'stroke_widths');
    this.replaceAttribute(geometry, primitive, 'dashed', 1, false, false, 'dashed');
    this.setViewportUniforms(points.material);
    points.renderOrder = Number(primitive.layer_order || 0);
    points.userData.version = primitive.version;
    points.userData.pickSpec = primitive.pick_kind ? {
      kind: 'screen_circles', objectKind: String(primitive.pick_kind),
      priority: Number(primitive.pick_priority || 100), primitiveId: id,
      positions: this.bufferView(primitive, 'positions'), radii: this.bufferView(primitive, 'radii')
    } : null;
  };

  ThreeJSRunner.prototype.makeScreenRectMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      vertexShader: 'attribute vec4 iBounds; attribute vec4 iFill; attribute vec4 iStroke; attribute float iStrokeWidth; attribute float iCornerRadius; varying vec2 vLocal; varying vec2 vSize; varying vec4 vFill; varying vec4 vStroke; varying float vStrokeWidth; varying float vCornerRadius; void main(){vLocal=vec2(position.x,position.y*0.5+0.5); vSize=iBounds.zw; vFill=iFill; vStroke=iStroke; vStrokeWidth=iStrokeWidth; vCornerRadius=iCornerRadius; vec2 point=iBounds.xy+vLocal*iBounds.zw; gl_Position=projectionMatrix*modelViewMatrix*vec4(point,0.0,1.0);}',
      fragmentShader: 'varying vec2 vLocal; varying vec2 vSize; varying vec4 vFill; varying vec4 vStroke; varying float vStrokeWidth; varying float vCornerRadius; void main(){float scale=max(1.0,min(vSize.x,vSize.y)); float aa=max(fwidth(vLocal.x),fwidth(vLocal.y))*1.5; vec2 p=(vLocal-0.5); float edge=min(min(vLocal.x,1.0-vLocal.x),min(vLocal.y,1.0-vLocal.y)); float radiusFraction=clamp(vCornerRadius/scale,0.0,0.5); vec2 q=abs(p)-vec2(0.5-radiusFraction); float rounded=1.0-smoothstep(0.0,aa,length(max(q,0.0))-radiusFraction); float border=smoothstep(0.0,aa,max(vStrokeWidth,0.0)/scale-edge); vec4 color=mix(vFill,vStroke,border*step(0.01,vStroke.a)); color.a*=rounded; if(color.a<0.01) discard; gl_FragColor=color;}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertScreenRects = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(this.ensureScreenQuadBase().clone(), this.sharedMaterial('screen-rects', this.makeScreenRectMaterial.bind(this)));
      mesh.userData.sharedMaterial = true;
      this.objects[id] = mesh;
      this.screenOverlayGroup.add(mesh);
    }
    var geometry = mesh.geometry;
    this.replaceAttribute(geometry, primitive, 'iBounds', 4, false, false, 'bounds');
    this.replaceAttribute(geometry, primitive, 'iFill', 4, true, false, 'fills');
    this.replaceAttribute(geometry, primitive, 'iStroke', 4, true, false, 'strokes');
    this.replaceAttribute(geometry, primitive, 'iStrokeWidth', 1, false, false, 'stroke_widths');
    this.replaceAttribute(geometry, primitive, 'iCornerRadius', 1, false, false, 'corner_radii');
    geometry.instanceCount = primitive.buffers.bounds.element_count;
    mesh.renderOrder = Number(primitive.layer_order || 0);
    mesh.userData.version = primitive.version;
    mesh.userData.pickSpec = primitive.pick ? {
      kind: 'scope', priority: 100, primitiveId: id, definition: primitive
    } : null;
  };

  ThreeJSRunner.prototype.stringTable = function (primitive, name) {
    var reference = primitive && primitive.buffers && primitive.buffers[name];
    if (!reference) { throw new Error('Missing resolved text resource ' + name); }
    var resource = this.resources[reference.resource_id];
    if (!resource || resource.descriptor.version !== reference.version || resource.descriptor.dtype !== 'utf8_string_table') {
      throw new Error('Missing binary text resource ' + reference.resource_id);
    }
    var view = new DataView(resource.buffer);
    var bytes = new Uint8Array(resource.buffer);
    var decoder = new TextDecoder('utf-8');
    var values = [];
    var offset = 0;
    while (offset < view.byteLength) {
      var length = view.getUint32(offset, true); offset += 4;
      values.push(decoder.decode(bytes.subarray(offset, offset + length)));
      offset += length;
    }
    return values;
  };

  ThreeJSRunner.prototype.rgbaCss = function (rgba) {
    var c = rgba || [255,255,255,255];
    return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (Number(c[3]) / 255.0) + ')';
  };

  ThreeJSRunner.prototype.roundedCanvasRect = function (context, x, y, width, height, radius) {
    var r = Math.max(0, Math.min(Number(radius || 0), Math.min(width, height) * 0.5));
    context.beginPath();
    context.moveTo(x + r, y); context.lineTo(x + width - r, y);
    context.quadraticCurveTo(x + width, y, x + width, y + r);
    context.lineTo(x + width, y + height - r);
    context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
    context.lineTo(x + r, y + height);
    context.quadraticCurveTo(x, y + height, x, y + height - r);
    context.lineTo(x, y + r); context.quadraticCurveTo(x, y, x + r, y);
    context.closePath();
  };

  ThreeJSRunner.prototype.makeTextMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: { uTexture: { value: this.getWhiteTexture() }, uDpr: { value: 1.0 } },
      vertexShader: 'attribute vec4 iBounds; attribute vec4 iClip; attribute vec4 iUv; varying vec2 vUv; varying vec4 vClip; void main(){vec2 local=vec2(position.x,position.y*0.5+0.5); vUv=mix(iUv.xy,iUv.zw,local); vClip=iClip; vec2 point=iBounds.xy+local*iBounds.zw; gl_Position=projectionMatrix*modelViewMatrix*vec4(point,0.0,1.0);}',
      fragmentShader: 'uniform sampler2D uTexture; uniform float uDpr; varying vec2 vUv; varying vec4 vClip; void main(){vec2 point=gl_FragCoord.xy/uDpr; if(point.x<vClip.x||point.y<vClip.y||point.x>vClip.x+vClip.z||point.y>vClip.y+vClip.w) discard; vec4 color=texture2D(uTexture,vUv); if(color.a<0.01) discard; gl_FragColor=color;}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.buildTextAtlas = function (primitive) {
    var THREE = global.THREE;
    var key = primitive.primitive_id + '@' + primitive.version + '@' + this.viewport.dpr;
    if (this.textTextureCache[key]) { return this.textTextureCache[key]; }
    var bounds = this.bufferView(primitive, 'bounds');
    var baselines = this.bufferView(primitive, 'baselines');
    var strings = this.stringTable(primitive, 'strings');
    var styles = primitive.styles || [];
    var count = Math.min(strings.length, styles.length, primitive.buffers.bounds.element_count);
    var maxWidth = Math.max(256, Math.floor(2048 / Math.max(1, this.viewport.dpr)));
    var placements = []; var x = 2; var y = 2; var rowHeight = 0;
    for (var index = 0; index < count; index += 1) {
      var width = Math.max(1, Math.ceil(bounds[index * 4 + 2]));
      var height = Math.max(1, Math.ceil(bounds[index * 4 + 3]));
      if (x + width + 2 > maxWidth && x > 2) { x = 2; y += rowHeight + 2; rowHeight = 0; }
      placements.push({ x: x, y: y, width: width, height: height });
      x += width + 2; rowHeight = Math.max(rowHeight, height);
    }
    var atlasHeight = Math.max(1, y + rowHeight + 2);
    var canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.ceil(maxWidth * this.viewport.dpr));
    canvas.height = Math.max(1, Math.ceil(atlasHeight * this.viewport.dpr));
    var context = canvas.getContext('2d'); context.scale(this.viewport.dpr, this.viewport.dpr);
    var uv = new Float32Array(count * 4);
    for (var itemIndex = 0; itemIndex < count; itemIndex += 1) {
      var item = placements[itemIndex]; var style = styles[itemIndex] || {};
      var fontStyle = style.italic ? 'italic ' : '';
      context.font = fontStyle + String(style.weight || 400) + ' ' + String(style.pixel_size || 12) + 'px ' + String(style.family || 'sans-serif');
      if (style.background_rgba) {
        context.fillStyle = this.rgbaCss(style.background_rgba);
        this.roundedCanvasRect(context, item.x, item.y, item.width, item.height, style.corner_radius_px);
        context.fill();
      }
      context.fillStyle = this.rgbaCss(style.foreground_rgba);
      context.textBaseline = 'alphabetic';
      var localBaselineY = item.height - (baselines[itemIndex * 2 + 1] - bounds[itemIndex * 4 + 1]);
      context.fillText(strings[itemIndex], item.x + baselines[itemIndex * 2] - bounds[itemIndex * 4], item.y + localBaselineY);
      uv[itemIndex * 4] = item.x / maxWidth;
      uv[itemIndex * 4 + 1] = 1.0 - (item.y + item.height) / atlasHeight;
      uv[itemIndex * 4 + 2] = (item.x + item.width) / maxWidth;
      uv[itemIndex * 4 + 3] = 1.0 - item.y / atlasHeight;
    }
    var texture = new THREE.CanvasTexture(canvas); texture.needsUpdate = true; texture.flipY = false;
    texture.colorSpace = THREE.SRGBColorSpace || texture.colorSpace;
    var atlas = { key: key, texture: texture, uv: uv };
    this.textTextureCache[key] = atlas;
    return atlas;
  };

  ThreeJSRunner.prototype.releaseTextAtlas = function (primitiveId) {
    var key = this.textTextureByPrimitive[primitiveId];
    if (key && this.textTextureCache[key]) {
      this.textTextureCache[key].texture.dispose();
      delete this.textTextureCache[key];
    }
    delete this.textTextureByPrimitive[primitiveId];
  };

  ThreeJSRunner.prototype.upsertTextBatch = function (primitive, id) {
    var mesh = this.objects[id];
    var sameVersion = mesh && mesh.userData.version === primitive.version && mesh.userData.dpr === this.viewport.dpr;
    if (!mesh) {
      mesh = new global.THREE.Mesh(this.ensureScreenQuadBase().clone(), this.makeTextMaterial());
      this.objects[id] = mesh;
      this.textOverlayGroup.add(mesh);
    }
    if (!sameVersion) {
      var nextAtlasKey = primitive.primitive_id + '@' + primitive.version + '@' + this.viewport.dpr;
      if (this.textTextureByPrimitive[id] && this.textTextureByPrimitive[id] !== nextAtlasKey) {
        this.releaseTextAtlas(id);
      }
      var atlas = this.buildTextAtlas(primitive);
      this.replaceAttribute(mesh.geometry, primitive, 'iBounds', 4, false, false, 'bounds');
      this.replaceAttribute(mesh.geometry, primitive, 'iClip', 4, false, false, 'clips');
      mesh.geometry.setAttribute('iUv', new global.THREE.InstancedBufferAttribute(atlas.uv, 4));
      mesh.geometry.instanceCount = primitive.buffers.bounds.element_count;
      mesh.userData.atlas = atlas;
      this.textTextureByPrimitive[id] = atlas.key;
      mesh.material.uniforms.uTexture.value = atlas.texture;
      mesh.userData.version = primitive.version;
      mesh.userData.dpr = this.viewport.dpr;
    }
    this.setViewportUniforms(mesh.material);
    mesh.renderOrder = Number(primitive.layer_order || 0);
  };

  ThreeJSRunner.prototype.makeScopeMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: {
        uCenter: { value: new THREE.Vector2(0,0) }, uShape: { value: 0.0 },
        uRadius: { value: 0.0 }, uRectSize: { value: new THREE.Vector2(0,0) },
        uMask: { value: new THREE.Vector4(0,0,0,0) }, uOutline: { value: new THREE.Vector4(1,1,1,1) },
        uOutlineWidth: { value: 1.0 }, uDpr: { value: 1.0 }
      },
      vertexShader: 'void main(){gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: 'uniform vec2 uCenter; uniform float uShape; uniform float uRadius; uniform vec2 uRectSize; uniform vec4 uMask; uniform vec4 uOutline; uniform float uOutlineWidth; uniform float uDpr; void main(){vec2 point=gl_FragCoord.xy/uDpr; float boundary; if(uShape<0.5){boundary=length(point-uCenter)-uRadius;}else{vec2 q=abs(point-uCenter)-uRectSize*0.5; boundary=max(q.x,q.y);} float outside=step(0.0,boundary); float outline=1.0-smoothstep(uOutlineWidth,uOutlineWidth+1.0,abs(boundary)); vec4 color=mix(vec4(0.0),uMask,outside); color=mix(color,uOutline,outline); if(color.a<0.01) discard; gl_FragColor=color;}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.upsertScopeMask = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(this.ensureQuad(), this.makeScopeMaterial());
      mesh.userData.fullscreen = true;
      this.objects[id] = mesh;
      this.screenOverlayGroup.add(mesh);
    }
    var uniforms = mesh.material.uniforms;
    var center = primitive.center || [0,0]; var size = primitive.rect_size_px || [0,0];
    uniforms.uCenter.value.set(Number(center[0]), Number(center[1]));
    uniforms.uShape.value = primitive.shape === 'rectangle' ? 1.0 : 0.0;
    uniforms.uRadius.value = Number(primitive.radius_px || 0);
    uniforms.uRectSize.value.set(Number(size[0] || 0), Number(size[1] || 0));
    uniforms.uMask.value.copy(this.vector4FromRgba(primitive.mask_rgba));
    uniforms.uOutline.value.copy(this.vector4FromRgba(primitive.outline_rgba));
    uniforms.uOutlineWidth.value = Math.max(0.5, Number(primitive.outline_width_px || 1));
    this.setViewportUniforms(mesh.material);
    this.setFullscreenTransform(mesh);
    mesh.renderOrder = Number(primitive.layer_order || 0);
    mesh.userData.version = primitive.version;
  };

  ThreeJSRunner.prototype.upsertInteractionAffordances = function (primitive, id) {
    var group = this.objects[id];
    if (!group) {
      group = new global.THREE.Group(); this.objects[id] = group;
      this.interactionOverlayGroup.add(group);
    }
    group.userData.affordances = primitive.affordances || [];
    group.userData.version = primitive.version;
    group.renderOrder = Number(primitive.layer_order || 0);
    group.userData.pickSpec = {
      kind: 'affordances', priority: 130, primitiveId: id,
      affordances: primitive.affordances || []
    };
  };

  ThreeJSRunner.prototype.makeBodyMaterial = function () {
    var THREE = global.THREE;
    return new THREE.ShaderMaterial({
      uniforms: { uCore: { value: new THREE.Vector4(1,1,1,1) }, uEdge: { value: new THREE.Vector4(1,1,1,1) }, uCorona: { value: 0.0 }, uHorizon: { value: this.getWhiteTexture() }, uViewport: { value: new THREE.Vector2(1,1) }, uDpr: { value: 1.0 } },
      vertexShader: 'varying vec2 vUv; void main(){vUv=uv*2.0-1.0; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: this.horizonFragment() + 'uniform vec4 uCore; uniform vec4 uEdge; uniform float uCorona; varying vec2 vUv; void main(){if(horizonAlpha()<=0.001) discard; float r=length(vUv); float disc=1.0-smoothstep(0.94,1.0,r); float halo=exp(-r*r*2.2)*uCorona*(1.0-smoothstep(0.8,1.45,r)); float a=max(disc*uCore.a,halo*uCore.a); if(a<0.01) discard; vec3 rgb=mix(uCore.rgb,uEdge.rgb,smoothstep(0.0,1.0,min(r,1.0))); gl_FragColor=vec4(rgb,a);}',
      transparent: true, depthTest: false, depthWrite: false
    });
  };

  ThreeJSRunner.prototype.vector4FromRgba = function (rgba) {
    var values = rgba || [255,255,255,255];
    return new global.THREE.Vector4(values[0]/255, values[1]/255, values[2]/255, values[3]/255);
  };

  ThreeJSRunner.prototype.addDisc = function (group, disc, core, edge, corona, order, pick) {
    if (!disc) { return; }
    var mesh = new global.THREE.Mesh(this.ensureQuad(), this.makeBodyMaterial());
    mesh.position.set(disc.center[0], disc.center[1], 0);
    mesh.scale.set(disc.radii[0] * 2.0, disc.radii[1] * 2.0, 1);
    mesh.material.uniforms.uCore.value.copy(this.vector4FromRgba(core));
    mesh.material.uniforms.uEdge.value.copy(this.vector4FromRgba(edge || core));
    mesh.material.uniforms.uCorona.value = corona ? Number(corona.strength || 0) : 0;
    this.setViewportUniforms(mesh.material);
    mesh.renderOrder = order;
    if (pick) {
      mesh.userData.pickSpec = { kind: 'record', priority: 90, record: pick };
    }
    group.add(mesh);
  };

  ThreeJSRunner.prototype.addMoon = function (group, moon, order) {
    if (!moon || !moon.disc) { return; }
    this.addDisc(group, moon.disc, moon.night ? [18,21,28,105] : [10,12,16,80], [10,12,16,0], null, order, moon.pick);
    if (!moon.eclipsing && moon.lit_outline && moon.lit_outline.length >= 3) {
      var THREE = global.THREE;
      var shape = new THREE.Shape();
      shape.moveTo(moon.lit_outline[0][0], moon.lit_outline[0][1]);
      for (var index = 1; index < moon.lit_outline.length; index += 1) {
        shape.lineTo(moon.lit_outline[index][0], moon.lit_outline[index][1]);
      }
      shape.closePath();
      var material = new THREE.MeshBasicMaterial({ color: moon.night ? 0xefefe9 : 0xdae2e8, transparent: true, opacity: Number(moon.visibility_alpha || 0), depthTest: false, depthWrite: false });
      var mesh = new THREE.Mesh(new THREE.ShapeGeometry(shape), material);
      mesh.renderOrder = order + 0.01;
      group.add(mesh);
    }
  };

  ThreeJSRunner.prototype.addPlanets = function (group, planets, order) {
    if (!planets || !planets.length) { return; }
    var THREE = global.THREE;
    var positions = new Float32Array(planets.length * 3);
    var colors = new Uint8Array(planets.length * 4);
    var radii = new Float32Array(planets.length);
    for (var index = 0; index < planets.length; index += 1) {
      var planet = planets[index];
      positions[index*3] = planet.center[0]; positions[index*3+1] = planet.center[1];
      colors.set(planet.rgba, index * 4); radii[index] = Number(planet.radius_px);
    }
    var geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 4, true));
    geometry.setAttribute('radius', new THREE.BufferAttribute(radii, 1));
    var material = new THREE.ShaderMaterial({
      vertexShader: 'attribute vec4 color; attribute float radius; varying vec4 vColor; void main(){vColor=color; gl_PointSize=max(1.0,radius*2.0); gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: 'varying vec4 vColor; void main(){vec2 p=gl_PointCoord*2.0-1.0; float alpha=(1.0-smoothstep(0.92,1.0,length(p)))*vColor.a; if(alpha<0.01) discard; gl_FragColor=vec4(vColor.rgb,alpha);}',
      transparent: true, depthTest: false, depthWrite: false
    });
    var points = new THREE.Points(geometry, material);
    points.renderOrder = order;
    points.userData.pickSpec = { kind: 'body_points', priority: 90, records: planets.map(function (planet) { return planet.pick || null; }) };
    geometry.computeBoundingSphere();
    group.add(points);
  };

  ThreeJSRunner.prototype.upsertBodies = function (primitive, id) {
    var group = this.objects[id];
    if (!group) {
      group = new global.THREE.Group();
      this.objects[id] = group;
      this.worldGroup.add(group);
    }
    if (group.userData.version === primitive.version) { return; }
    this.disposeChildren(group);
    if (primitive.sun) {
      this.addDisc(group, primitive.sun.disc, primitive.sun.core_rgba, primitive.sun.edge_rgba, primitive.sun.corona, Number(primitive.layer_order || 0), primitive.sun.pick);
    }
    this.addMoon(group, primitive.moon, Number(primitive.layer_order || 0) + 0.02);
    this.addPlanets(group, primitive.planets, Number(primitive.layer_order || 0) + 0.04);
    group.userData.version = primitive.version;
    group.renderOrder = Number(primitive.layer_order || 0);
  };

  ThreeJSRunner.prototype.upsertGenericPoints = function (primitive, id) {
    var points = this.objects[id];
    if (!points) {
      points = new global.THREE.Points(new global.THREE.BufferGeometry(), new global.THREE.PointsMaterial({ size: Number((primitive.sizes || [7])[0]), transparent: true, sizeAttenuation: false }));
      this.objects[id] = points; this.scene.add(points);
    }
    var positions = primitive.buffers ? this.bufferView(primitive, 'positions') : new Float32Array(primitive.positions || []);
    points.geometry.setAttribute('position', new global.THREE.BufferAttribute(positions, 3));
  };

  ThreeJSRunner.prototype.upsertGenericLines = function (primitive, id) {
    var lines = this.objects[id];
    if (!lines) {
      lines = new global.THREE.LineSegments(new global.THREE.BufferGeometry(), new global.THREE.LineBasicMaterial({ color: 0xffffff }));
      this.objects[id] = lines; this.scene.add(lines);
    }
    var positions = primitive.buffers ? this.bufferView(primitive, 'segments') : new Float32Array(primitive.segments || []);
    lines.geometry.setAttribute('position', new global.THREE.BufferAttribute(positions, 3));
  };

  ThreeJSRunner.prototype.upsertGenericMesh = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(new global.THREE.BufferGeometry(), new global.THREE.MeshBasicMaterial({ color: 0xffffff, side: global.THREE.DoubleSide }));
      this.objects[id] = mesh; this.scene.add(mesh);
    }
    var positions = primitive.buffers ? this.bufferView(primitive, 'vertices') : new Float32Array(primitive.vertices || []);
    mesh.geometry.setAttribute('position', new global.THREE.BufferAttribute(positions, 3));
  };

  ThreeJSRunner.prototype.upsertGenericImage = function (primitive, id) {
    var mesh = this.objects[id];
    if (!mesh) {
      mesh = new global.THREE.Mesh(this.ensureQuad(), new global.THREE.MeshBasicMaterial({ color: 0x4678a6, transparent: true }));
      this.objects[id] = mesh; this.scene.add(mesh);
    }
    var bounds = primitive.bounds || [-20,-20,12,12];
    mesh.position.set(bounds[0] + bounds[2] * 0.5, bounds[1] + bounds[3] * 0.5, 0);
    mesh.scale.set(bounds[2], bounds[3], 1);
  };

  ThreeJSRunner.prototype.upsertGenericText = function (primitive, id) {
    var sprite = this.objects[id];
    if (!sprite) {
      var canvas = document.createElement('canvas'); canvas.width = 512; canvas.height = 64;
      var context = canvas.getContext('2d'); context.font = (primitive.font_size || 16) + 'px sans-serif'; context.fillStyle = '#ffffff'; context.textAlign = 'center'; context.fillText(String(primitive.text || ''), 256, 40);
      var texture = new global.THREE.CanvasTexture(canvas);
      sprite = new global.THREE.Sprite(new global.THREE.SpriteMaterial({ map: texture, transparent: true }));
      this.objects[id] = sprite; this.scene.add(sprite);
    }
    var position = primitive.position || [0,0,0]; sprite.position.set(position[0], position[1], position[2] || 0); sprite.scale.set(160,20,1);
  };

  ThreeJSRunner.prototype.applyClipBlend = function (primitive) {
    var bounds = primitive.clip_bounds;
    if (!bounds || bounds.length !== 4) { this.renderer.setScissorTest(false); return; }
    var width = Math.max(1, Math.min(this.viewport.width, Number(bounds[2])));
    var height = Math.max(1, Math.min(this.viewport.height, Number(bounds[3])));
    this.renderer.setScissor(0, 0, width, height); this.renderer.setScissorTest(true);
  };

  ThreeJSRunner.prototype.removeInactiveObjects = function (active) {
    var self = this;
    Object.keys(this.objects).forEach(function (id) {
      if (!active[id]) {
        self.releaseTextAtlas(id);
        var object = self.objects[id];
        if (object.userData && object.userData.terrainGeometryKey) {
          self.releaseTerrainPrimitive(id, object);
        } else {
          self.disposeObject(object);
        }
        if (object.parent) { object.parent.remove(object); }
        delete self.objects[id];
      }
    });
  };

  ThreeJSRunner.prototype.disposeChildren = function (group) {
    while (group.children.length) {
      var child = group.children[0];
      group.remove(child); this.disposeObject(child);
    }
  };

  ThreeJSRunner.prototype.disposeObject = function (object) {
    if (!object) { return; }
    this.disposeChildren(object);
    if (object.geometry && object.geometry !== this.quadGeometry) { object.geometry.dispose(); }
    if (object.material && !object.userData.sharedMaterial) {
      var materials = Array.isArray(object.material) ? object.material : [object.material];
      materials.forEach(function (material) {
        if (material.map && !material.map.isDataTexture) { material.map.dispose(); }
        material.dispose();
      });
    }
  };

  ThreeJSRunner.prototype.registerResource = function (resource) {
    var self = this;
    if (!resource || !resource.id || !resource.uri) { throw new Error('A binary resource requires an id and URI'); }
    if (typeof resource.uri !== 'string' || resource.uri.indexOf('resources/') !== 0) {
      throw new Error('Three.js binary resources must use a local host resource URI');
    }
    this.resourcePendingVersions[resource.id] = resource.version;
    global.fetch(resource.uri).then(function (response) {
      if (!response.ok && response.status !== 0) { throw new Error('Unable to load binary resource: ' + response.status); }
      return response.arrayBuffer();
    }).then(function (buffer) {
      if (buffer.byteLength !== Number(resource.byte_length)) { throw new Error('Binary resource length mismatch'); }
      if (self.resourcePendingVersions[resource.id] !== resource.version) { return; }
      var previous = self.resources[resource.id];
      if (previous && previous.descriptor.version !== resource.version) {
        Object.keys(self.resourceViews).forEach(function (key) {
          if (key.indexOf(resource.id + '@') === 0) { delete self.resourceViews[key]; }
        });
      }
      self.resources[resource.id] = { descriptor: resource, buffer: buffer };
      delete self.resourcePendingVersions[resource.id];
      self.ack(0, 'resource_ready', { resource_id: resource.id, version: resource.version, byte_length: buffer.byteLength });
      if (self.pendingFrame && self.requiredResourcesReady(self.pendingFrame.manifest)) {
        var pending = self.pendingFrame; self.pendingFrame = null; self.submitFrame(pending.generation, pending.payload);
      }
    }).catch(function (error) { self.sendError(error.message || String(error), 'binary_resource'); });
  };

  ThreeJSRunner.prototype.disposeResource = function (resourceId) {
    if (!resourceId) { return; }
    var oldKey = this.textureByResource[resourceId];
    if (oldKey && this.textureCache[oldKey]) { this.textureCache[oldKey].dispose(); delete this.textureCache[oldKey]; }
    delete this.textureByResource[resourceId]; delete this.resources[resourceId]; delete this.resourcePendingVersions[resourceId];
    var external = this.externalTextureByResource[resourceId];
    if (external && this.externalTextureCache[external]) {
      if (this.externalTextureCache[external] !== this.whiteTexture) { this.externalTextureCache[external].dispose(); }
      delete this.externalTextureCache[external];
    }
    delete this.externalTextureByResource[resourceId];
    var self = this;
    Object.keys(this.resourceViews).forEach(function (key) { if (key.indexOf(resourceId + '@') === 0) { delete self.resourceViews[key]; } });
  };

  ThreeJSRunner.prototype.invalidateResource = function (resourceId) { this.disposeResource(resourceId); };

  ThreeJSRunner.prototype.pickRay = function (x, y) {
    var THREE = global.THREE;
    var pointer = new THREE.Vector2(
      (Number(x) / Math.max(1, this.viewport.width)) * 2.0 - 1.0,
      1.0 - (Number(y) / Math.max(1, this.viewport.height)) * 2.0
    );
    this.raycaster.setFromCamera(pointer, this.camera);
    return pointer;
  };

  ThreeJSRunner.prototype.isSkyPointVisible = function (x, yUp) {
    var mask = this.horizonMask;
    if (!mask || !mask.values || !mask.width || !mask.height) { return true; }
    var px = Math.max(0, Math.min(mask.width - 1, Math.floor(Number(x) / Math.max(1, this.viewport.width) * mask.width)));
    var py = Math.max(0, Math.min(mask.height - 1, Math.floor((1.0 - Number(yUp) / Math.max(1, this.viewport.height)) * mask.height)));
    return Number(mask.values[(py * mask.width + px) * 4 + 3]) > 0;
  };

  ThreeJSRunner.prototype.recordCandidate = function (record, priority, distance) {
    if (!record || !record.object_id || !record.object_kind) { return null; }
    return {
      priority: Number(priority), distance: Number(distance), objectId: String(record.object_id),
      objectKind: String(record.object_kind), key: record.key || record.object_id,
      name: record.name || record.object_id, alt: record.alt, az: record.az,
      magnitude: record.magnitude, metadata: record.metadata || {}, worldPoint: null,
      surfaceCoordinates: null
    };
  };

  ThreeJSRunner.prototype.starCandidate = function (spec, intersection) {
    var index = Number(intersection.index);
    if (index < 0 || index >= spec.catalogIndices.length || !this.isSkyPointVisible(intersection.point.x, intersection.point.y)) { return null; }
    var catalogIndex = Number(spec.catalogIndices[index]);
    var magnitude = spec.magnitudes && index < spec.magnitudes.length ? Number(spec.magnitudes[index]) : null;
    return {
      priority: Number(spec.priority), distance: Number(intersection.distance), objectId: 'star:' + catalogIndex,
      objectKind: 'star', key: String(catalogIndex), name: 'Gaia #' + catalogIndex,
      alt: spec.altitudes && index < spec.altitudes.length ? Number(spec.altitudes[index]) : null,
      az: spec.azimuths && index < spec.azimuths.length ? Number(spec.azimuths[index]) : null,
      magnitude: magnitude, metadata: { catalog_index: catalogIndex, magnitude: magnitude },
      star: { id: catalogIndex, mag: magnitude }, worldPoint: null, surfaceCoordinates: null
    };
  };

  ThreeJSRunner.prototype.terrainCandidate = function (spec, intersection) {
    var proxy = intersection.object;
    var index = proxy.geometry.getIndex();
    var faceIndex = Number(intersection.faceIndex);
    if (!index || faceIndex < 0) { return null; }
    var a = Number(index.getX(faceIndex * 3));
    var b = Number(index.getX(faceIndex * 3 + 1));
    var c = Number(index.getX(faceIndex * 3 + 2));
    var position = proxy.geometry.getAttribute('position');
    var pa = { x: position.getX(a), y: position.getY(a) };
    var pb = { x: position.getX(b), y: position.getY(b) };
    var pc = { x: position.getX(c), y: position.getY(c) };
    var denominator = (pb.y - pc.y) * (pa.x - pc.x) + (pc.x - pb.x) * (pa.y - pc.y);
    var u = denominator === 0 ? 1.0 : ((pb.y - pc.y) * (intersection.point.x - pc.x) + (pc.x - pb.x) * (intersection.point.y - pc.y)) / denominator;
    var v = denominator === 0 ? 0.0 : ((pc.y - pa.y) * (intersection.point.x - pc.x) + (pa.x - pc.x) * (intersection.point.y - pc.y)) / denominator;
    var w = 1.0 - u - v;
    var world = spec.worldGeometry.getAttribute('position');
    var worldPoint = world ? [
      u * world.getX(a) + v * world.getX(b) + w * world.getX(c),
      u * world.getY(a) + v * world.getY(b) + w * world.getY(c),
      u * world.getZ(a) + v * world.getZ(b) + w * world.getZ(c)
    ] : null;
    var classes = spec.worldGeometry.getAttribute('class_ids');
    var classId = classes ? Number(classes.getX(a)) : null;
    if (classId === 4294967295) { classId = null; }
    var triangleOffset = faceIndex * 3;
    var tileId = null;
    (spec.tiles || []).some(function (tile) {
      if (triangleOffset >= Number(tile.first_index) && triangleOffset < Number(tile.first_index) + Number(tile.index_count)) { tileId = String(tile.tile_id); return true; }
      return false;
    });
    return {
      priority: Number(spec.priority), distance: Number(intersection.distance),
      objectId: 'terrain:' + String(spec.primitiveId) + ':' + faceIndex, objectKind: 'terrain',
      key: String(spec.primitiveId) + ':' + faceIndex, name: 'Terrain', alt: null, az: null,
      magnitude: null, metadata: { primitive_id: String(spec.primitiveId), tile_id: tileId, class_id: classId },
      worldPoint: worldPoint,
      surfaceCoordinates: { triangle_index: faceIndex, barycentric: [u, v, w], tile_id: tileId }
    };
  };

  ThreeJSRunner.prototype.rayCandidate = function (intersection) {
    var spec = intersection.object && intersection.object.userData && intersection.object.userData.pickSpec;
    if (!spec) { return null; }
    if (spec.kind === 'star') { return this.starCandidate(spec, intersection); }
    if (spec.kind === 'record') {
      if (!this.isSkyPointVisible(intersection.point.x, intersection.point.y)) { return null; }
      return this.recordCandidate(spec.record, spec.priority, intersection.distance);
    }
    if (spec.kind === 'body_points') {
      if (!this.isSkyPointVisible(intersection.point.x, intersection.point.y)) { return null; }
      return this.recordCandidate(spec.records[Number(intersection.index)], spec.priority, intersection.distance);
    }
    if (spec.kind === 'terrain') { return this.terrainCandidate(spec, intersection); }
    return null;
  };

  ThreeJSRunner.prototype.distanceToSegment = function (x, y, ax, ay, bx, by) {
    var dx = bx - ax; var dy = by - ay;
    var denom = dx * dx + dy * dy;
    var t = denom <= 0.000001 ? 0.0 : Math.max(0.0, Math.min(1.0, ((x - ax) * dx + (y - ay) * dy) / denom));
    return Math.hypot(x - (ax + dx * t), y - (ay + dy * t));
  };

  ThreeJSRunner.prototype.screenCandidates = function (x, yUp, radius, purpose) {
    var candidates = [];
    var self = this;
    Object.keys(this.objects).forEach(function (id) {
      var object = self.objects[id];
      var spec = object && object.userData && object.userData.pickSpec;
      if (!spec) { return; }
      if (spec.kind === 'deep_sky_batch') {
        for (var i = 0; i < spec.records.length; i += 1) {
          var px = Number(spec.positions[i * 2]); var py = Number(spec.positions[i * 2 + 1]);
          var d = Math.hypot(x - px, yUp - py);
          if (d <= Math.max(Number(radius), Number(spec.radii[i * 2] || 0), Number(spec.radii[i * 2 + 1] || 0)) && self.isSkyPointVisible(px, py)) {
            var record = self.recordCandidate(spec.records[i], spec.priority, d); if (record) { candidates.push(record); }
          }
        }
      } else if (spec.kind === 'screen_lines') {
        for (var line = 0; line < spec.starts.length / 2; line += 1) {
          var distance = self.distanceToSegment(x, yUp, spec.starts[line * 2], spec.starts[line * 2 + 1], spec.ends[line * 2], spec.ends[line * 2 + 1]);
          if (distance <= Number(radius) + Number(spec.width) * 0.5) {
            candidates.push({ priority: spec.priority, distance: distance, objectId: spec.objectKind + ':' + spec.primitiveId + ':' + line, objectKind: spec.objectKind, key: String(line), name: spec.objectKind, alt: null, az: null, magnitude: null, metadata: { primitive_id: spec.primitiveId, instance_index: line }, worldPoint: null, surfaceCoordinates: null });
          }
        }
      } else if (spec.kind === 'screen_circles') {
        for (var circle = 0; circle < spec.radii.length; circle += 1) {
          var cx = Number(spec.positions[circle * 3]); var cy = Number(spec.positions[circle * 3 + 1]);
          var circleDistance = Math.hypot(x - cx, yUp - cy);
          if (circleDistance <= Number(spec.radii[circle]) + Number(radius)) {
            candidates.push({ priority: spec.priority, distance: circleDistance, objectId: spec.objectKind + ':' + spec.primitiveId + ':' + circle, objectKind: spec.objectKind, key: String(circle), name: spec.objectKind, alt: null, az: null, magnitude: null, metadata: { primitive_id: spec.primitiveId, instance_index: circle }, worldPoint: null, surfaceCoordinates: null });
          }
        }
      } else if (spec.kind === 'scope') {
        var definition = spec.definition; var center = definition.center || [0, 0]; var boundary;
        if (definition.shape === 'rectangle') {
          var size = definition.rect_size_px || [0, 0]; boundary = Math.max(Math.abs(x - Number(center[0])) - Number(size[0]) * 0.5, Math.abs(yUp - Number(center[1])) - Number(size[1]) * 0.5);
        } else { boundary = Math.abs(Math.hypot(x - Number(center[0]), yUp - Number(center[1])) - Number(definition.radius_px || 0)); }
        if (Math.abs(boundary) <= Number(radius) + Number(definition.outline_width_px || 1)) {
          candidates.push({ priority: spec.priority, distance: Math.abs(boundary), objectId: String(definition.pick.object_id), objectKind: String(definition.pick.object_kind), key: String(definition.pick.object_id), name: 'Scope', alt: null, az: null, magnitude: null, metadata: definition.pick.metadata || {}, worldPoint: null, surfaceCoordinates: null });
        }
      } else if (spec.kind === 'affordances' && purpose === 'interaction') {
        (spec.affordances || []).forEach(function (affordance) {
          var bounds = affordance.bounds || []; var minimum = bounds[0] || []; var maximum = bounds[1] || [];
          if (affordance.active && x >= Number(minimum[0]) && x <= Number(maximum[0]) && yUp >= Number(minimum[1]) && yUp <= Number(maximum[1])) {
            candidates.push({ priority: spec.priority, distance: 0, objectId: 'interaction:' + String(affordance.affordance_id), objectKind: 'interaction', key: String(affordance.affordance_id), name: String(affordance.action), alt: null, az: null, magnitude: null, metadata: { action: String(affordance.action) }, worldPoint: null, surfaceCoordinates: null });
          }
        });
      }
    });
    return candidates;
  };

  ThreeJSRunner.prototype.performPick = function (generation, request) {
    if (!this.started || !this.raycaster || !this.camera) {
      this.sendError('Pick requested before the Three.js surface is ready', 'pick_unavailable', generation, request && request.request_id, 'pick_request');
      return;
    }
    if (Number(generation) !== Number(this.lastGeneration)) {
      this.sendError('Pick request generation is stale', 'stale_pick_request', generation, request && request.request_id, 'pick_request');
      return;
    }
    var requestId = String(request && request.request_id || '');
    if (!requestId) { this.sendError('Pick request requires request_id', 'invalid_pick_request', generation, null, 'pick_request'); return; }
    var x = Number(request.x); var y = Number(request.y); var radius = Math.max(0, Number(request.radius || 0));
    if (!isFinite(x) || !isFinite(y)) { this.sendError('Pick coordinates must be finite', 'invalid_pick_request', generation, requestId, 'pick_request'); return; }
    this.pickRay(x, y);
    this.raycaster.params.Points.threshold = Math.max(1.0, radius);
    var targets = [];
    Object.keys(this.objects).forEach(function (id) {
      var object = this.objects[id];
      if (!object) { return; }
      object.traverse(function (child) { if (child.userData && child.userData.pickSpec && ['star', 'record', 'body_points'].indexOf(child.userData.pickSpec.kind) >= 0) { targets.push(child); } });
    }, this);
    Object.keys(this.terrainPickProxies).forEach(function (id) { targets.push(this.terrainPickProxies[id]); }, this);
    var candidates = this.screenCandidates(x, this.viewport.height - y, radius, String(request.purpose || 'select'));
    var intersections = this.raycaster.intersectObjects(targets, false);
    for (var index = 0; index < intersections.length; index += 1) {
      var candidate = this.rayCandidate(intersections[index]); if (candidate) { candidates.push(candidate); }
    }
    candidates.sort(function (left, right) { return Number(right.priority) - Number(left.priority) || Number(left.distance) - Number(right.distance); });
    var hit = candidates.length ? candidates[0] : null;
    var payload = hit ? {
      request_id: requestId, hit: true, object_id: hit.objectId, object_kind: hit.objectKind,
      distance: hit.distance, world_point: hit.worldPoint, surface_coordinates: hit.surfaceCoordinates,
      metadata: hit.metadata || {}, key: hit.key, name: hit.name, alt: hit.alt, az: hit.az,
      magnitude: hit.magnitude, star: hit.star || null
    } : {
      request_id: requestId, hit: false, object_id: null, object_kind: null,
      distance: null, world_point: null, surface_coordinates: null, metadata: {}
    };
    this.postMessage({ v: PROTOCOL_VERSION, op: 'pick_result', gen: Number(generation), seq: MESSAGE_SEQUENCE++, payload: payload });
  };

  ThreeJSRunner.prototype.renderLastManifest = function () {
    if (!this.suspended && this.renderer && this.scene && this.camera) {
      this.advanceTerrainTransitions();
      this.renderer.render(this.scene, this.camera);
    }
  };

  ThreeJSRunner.prototype.onContextLost = function (event) {
    event.preventDefault(); this.ready = false; this.sendError('WebGL context lost', 'webgl_context_lost');
  };

  ThreeJSRunner.prototype.dispose = function () {
    var self = this;
    Object.keys(this.objects).forEach(function (id) {
      var object = self.objects[id];
      if (object.userData && object.userData.terrainGeometryKey) {
        self.releaseTerrainPrimitive(id, object);
      } else {
        self.disposeObject(object);
      }
    });
    this.objects = {};
    Object.keys(this.materials).forEach(function (key) { self.materials[key].dispose(); });
    this.materials = {};
    Object.keys(this.textureCache).forEach(function (key) { self.textureCache[key].dispose(); });
    this.textureCache = {}; this.textureByResource = {}; this.resourceViews = {};
    Object.keys(this.externalTextureCache).forEach(function (key) {
      var texture = self.externalTextureCache[key];
      if (texture && texture !== self.whiteTexture) { texture.dispose(); }
    });
    this.externalTextureCache = {}; this.externalTextureByResource = {};
    this.terrainGeometryCache = {}; this.terrainTileCache = {}; this.terrainMaterialRefs = {}; this.terrainTextureRefs = {}; this.terrainPickProxies = {};
    Object.keys(this.textTextureCache).forEach(function (key) { self.textTextureCache[key].texture.dispose(); });
    this.textTextureCache = {}; this.textTextureByPrimitive = {};
    if (this.whiteTexture) { this.whiteTexture.dispose(); }
    if (this.quadGeometry) { this.quadGeometry.dispose(); }
    if (this.deepSkyBase) { this.deepSkyBase.dispose(); }
    if (this.screenQuadBase) { this.screenQuadBase.dispose(); }
    if (this.canvas && this._contextLost) { this.canvas.removeEventListener('webglcontextlost', this._contextLost); }
    if (this.renderer) { this.renderer.setAnimationLoop(null); this.renderer.dispose(); this.renderer.forceContextLoss(); }
    if (this.canvas && this.canvas.parentNode) { this.canvas.parentNode.removeChild(this.canvas); }
    this.scene = null; this.camera = null; this.renderer = null; this.canvas = null; this.resources = {}; this.resourcePendingVersions = {};
    this.worldGroup = null; this.screenOverlayGroup = null; this.textOverlayGroup = null; this.interactionOverlayGroup = null;
    this.lastManifest = null; this.pendingFrame = null; this.horizonTexture = null; this.horizonMask = null; this.whiteTexture = null; this.quadGeometry = null; this.deepSkyBase = null; this.screenQuadBase = null; this.terrainTransitionPending = false; this.raycaster = null;
    this.started = false; this.ready = false; this.suspended = true;
  };

  ThreeJSRunner.prototype.ack = function (generation, operation, extra) {
    var payload = extra || {}; payload.operation = operation;
    this.postMessage({ v: PROTOCOL_VERSION, op: 'ack', gen: generation, seq: MESSAGE_SEQUENCE++, payload: payload });
  };

  ThreeJSRunner.prototype.postMessage = function (message) {
    if (this.channelBridge && this.channelBridge.receive) { this.channelBridge.receive(JSON.stringify(message)); }
  };

  ThreeJSRunner.prototype.sendError = function (message, code, generation, requestId, operation) {
    setRendererStatus('Three.js error: ' + String(message), 'error');
    if (global.console && global.console.error) {
      global.console.error('[TerraLab Three.js][' + String(code || 'unknown') + '] ' + String(message));
    }
    var payload = { message: String(message), code: String(code || 'unknown') };
    if (requestId) { payload.request_id = String(requestId); }
    if (operation) { payload.operation = String(operation); }
    this.postMessage({ v: PROTOCOL_VERSION, op: 'error', gen: Number(generation || 0), seq: MESSAGE_SEQUENCE++, payload: payload });
  };

  global.threeJSRunner = new ThreeJSRunner();
  if (!global.QWebChannel || !global.qt || !global.qt.webChannelTransport) {
    setRendererStatus('Three.js error: QWebChannel is unavailable', 'error');
    if (global.console && global.console.error) {
      global.console.error('QWebChannel is unavailable');
    }
    global.threeJSRunner.sendError('QWebChannel is unavailable', 'channel'); return;
  }
  new global.QWebChannel(global.qt.webChannelTransport, function (channel) {
    if (!channel.objects || !channel.objects.threeBridge) {
      if (global.console && global.console.error) {
        global.console.error('Three.js QWebChannel bridge is unavailable');
      }
      global.threeJSRunner.sendError('Three.js QWebChannel bridge is unavailable', 'channel');
      return;
    }
    global.threeJSRunner.bindBridge(channel.objects.threeBridge);
  });
}(typeof window !== 'undefined' ? window : this));

/* orb.js — Crush Three.js sphere (vanilla JS, spec v2)
 *
 * window.createCrushOrb(canvas, opts) → { setState, setAudioLevel, setAudioBands, setZoom, resize, destroy }
 * window.CrushOrb(canvas)             → same (backwards compat with home.js `new CrushOrb(…)`)
 *
 * Requires Three.js loaded before this script.
 */
(function () {
  "use strict";

  function createOrbInternal(canvas, opts) {
    const THREE = window.THREE;
    const S     = window.SPHERE_STYLE;

    // ── Opts d'extension (séquence de réveil) ────────────────────────
    // Le montage dashboard ne passe pas d'opts → comportement strictement
    // inchangé. La séquence passe { frozen: true, onTick: fn } pour piloter
    // positions/rotation/opacité depuis l'extérieur pendant boot→ignite.
    // setFrozen(false) à l'entrée ONLINE bascule sur le drift dashboard
    // sans bascule d'instance (l'orbe est partagé).
    opts = opts || {};
    let FROZEN    = !!opts.frozen;
    const ON_TICK = (typeof opts.onTick === "function") ? opts.onTick : null;

    let destroyed = false;
    let state = "idle";
    let lastState = "idle";
    let audioLevel = 0;
    // Bandes de frequences — reprises du projet precedent, ou la parole
    // pilotait separement le rayon (basses), la pulsation des particules
    // (mediums) et la densite d'arcs. Un niveau global unique donne un
    // gonflement uniforme, sans vie.
    let bass = 0, mid = 0, treble = 0;

    // ── Renderer ─────────────────────────────────────────────────────
    let w = canvas.offsetWidth  || 720;
    let h = canvas.offsetHeight || 720;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, S.RENDER.PIXEL_RATIO_MAX));
    renderer.setSize(w, h, false);
    renderer.setClearColor(S.RENDER.ORB_CLEAR_COLOR_HEX, S.RENDER.ORB_CLEAR_ALPHA);

    // ── Camera ───────────────────────────────────────────────────────
    const camera = new THREE.PerspectiveCamera(S.CAMERA.FOV, w / h, S.CAMERA.NEAR, S.CAMERA.FAR);
    camera.position.z = S.CAMERA.ZCAM;
    let cameraZ       = S.CAMERA.ZCAM;
    let cameraZTarget = S.CAMERA.ZCAM;
    if (FROZEN) {
      // Caméra strictement statique en frozen : la séquence peut muter
      // camera.position.z via onTick pour le dolly d'ignition.
      camera.position.set(0, 0, S.CAMERA.ZCAM);
      camera.lookAt(0, 0, 0);
    }

    const scene = new THREE.Scene();

    // ── Sprite (circular particle, avoids square quads) ──────────────
    const sprite = (() => {
      const sz = S.SPRITE.SIZE_PX;
      const c = document.createElement("canvas");
      c.width = sz; c.height = sz;
      const cc = c.getContext("2d");
      const grad = cc.createRadialGradient(sz / 2, sz / 2, 0, sz / 2, sz / 2, sz / 2);
      S.SPRITE.STOPS.forEach(([offset, color]) => grad.addColorStop(offset, color));
      cc.fillStyle = grad;
      cc.fillRect(0, 0, sz, sz);
      return new THREE.CanvasTexture(c);
    })();

    // ── Particles ────────────────────────────────────────────────────
    const N = S.GEOMETRY.PARTICLE_COUNT;
    const pos   = new Float32Array(N * 3);
    const vel   = new Float32Array(N * 3);
    const phase = new Float32Array(N);

    for (let i = 0; i < N; i++) {
      const theta = Math.random() * Math.PI * 2;
      const phi   = Math.acos(2 * Math.random() - 1);
      const r     = S.GEOMETRY.SHELL_R_MIN + Math.random() * S.GEOMETRY.SHELL_THICKNESS;
      pos[i*3]   = r * Math.sin(phi) * Math.cos(theta);
      pos[i*3+1] = r * Math.sin(phi) * Math.sin(theta);
      pos[i*3+2] = r * Math.cos(phi);
      phase[i]   = Math.random() * 1000;
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));

    const mat = new THREE.PointsMaterial({
      color: S.COLOR.BASE_HEX,
      size: 0.6,                              // valeur de boot (1 frame) ; mat.size est réécrit chaque frame vers POINT_SIZE_IDLE
      transparent: S.MATERIAL.TRANSPARENT,
      opacity: 0.8,                           // valeur de boot (1 frame) ; mat.opacity est réécrit chaque frame vers OPACITY_IDLE
      sizeAttenuation: S.MATERIAL.SIZE_ATTENUATION,
      blending: THREE.AdditiveBlending,
      depthWrite: S.MATERIAL.DEPTH_WRITE,
      map: sprite,
      alphaTest: S.MATERIAL.ALPHA_TEST,
    });
    const points = new THREE.Points(geo, mat);
    scene.add(points);


    // ── Lines ────────────────────────────────────────────────────────
    const MAX_LINES = 8000;
    const linePos = new Float32Array(MAX_LINES * 6);
    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute("position", new THREE.BufferAttribute(linePos, 3));
    lineGeo.setDrawRange(0, 0);

    const lineMat = new THREE.LineBasicMaterial({
      color: S.COLOR.BASE_HEX,
      transparent: true,
      opacity: 0.0,
      blending: THREE.AdditiveBlending,
      depthWrite: S.MATERIAL.DEPTH_WRITE,
    });
    const lines = new THREE.LineSegments(lineGeo, lineMat);
    scene.add(lines);

    // ── Electrons ────────────────────────────────────────────────────
    const electronGeo = new THREE.BufferGeometry();
    const electronPos = new Float32Array(200 * 3);
    electronGeo.setAttribute("position", new THREE.BufferAttribute(electronPos, 3));
    electronGeo.setDrawRange(0, 0);

    const electronMat = new THREE.PointsMaterial({
      color: 0xffffff,
      size: 1.2,
      transparent: true,
      opacity: 1.0,
      sizeAttenuation: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      map: sprite,
      alphaTest: 0.001,
    });
    const electronPoints = new THREE.Points(electronGeo, electronMat);
    scene.add(electronPoints);

    const activeElectrons   = [];
    const activeConnections = [];
    let lastElectronSpawn   = 0;

    // ── Lerped state parameters ───────────────────────────────────────
    // Valeurs initiales = buffers de boot ; les cibles idle/listening/thinking/speaking
    // sont fixées dans le switch d'animate() ci-dessous.
    let targetRadius = 28,     currentRadius = 28;
    let targetSpeed  = 0.2,    currentSpeed  = 0.2;
    let targetBright = 0.74,   currentBright = 0.74;
    let targetSize   = 0.58,   currentSize   = 0.58;
    let targetLineAmount   = 0.15, currentLineAmount   = 0.15;
    let targetElectronRate = 0,    electronSpawnRate   = 0;
    let targetGlow = 0.45,     currentGlow = 0.45;

    // ── Music beat ───────────────────────────────────────────────────
    let musicTempo  = 0;   // BPM, 0 = pas de musique
    let musicEnergy = 0;   // 0-1
    let musicBeatPhase = 0;

    // ── Rotation / drag ──────────────────────────────────────────────
    let autoRotY = 0;
    let dragRotX = 0, dragRotY = 0;
    let dragVelX = 0, dragVelY = 0;
    let dragging = false, lastDragX = 0, lastDragY = 0;

    // ── Zoom ─────────────────────────────────────────────────────────
    let zoomImpulse = 0;

    // ── Breath / cloud Z ─────────────────────────────────────────────
    let cloudZ = 0, cloudZVel = 0;

    // Zoom : borne basse abaissee a 18 (contre 40) pour autoriser une
    // approche ou la sphere deborde du cadre — comportement demande.
    const ZOOM_MIN = 18, ZOOM_MAX = 180;

    // ── Time ─────────────────────────────────────────────────────────
    let t = 0;

    // ── Event handlers (stored for cleanup) ──────────────────────────
    // Événements POINTEUR et non souris : ils couvrent doigt, stylet et souris
    // d'un seul jeu de gestionnaires. Avec `mousedown`/`mousemove`, l'orbe
    // n'était pas manipulable au doigt — aucun de ces événements ne se
    // déclenche sur un écran tactile.
    const pointeurs = new Map();   // pointerId → {x, y}, pour le pincement
    let ecartPincement = 0;

    function onPointerDown(e) {
      pointeurs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointeurs.size === 1) {
        dragging = true;
        lastDragX = e.clientX; lastDragY = e.clientY;
        canvas.style.cursor = "grabbing";
        // Le pointeur reste capté même si le doigt sort du canvas, sinon un
        // glissement rapide interrompt la rotation en plein mouvement.
        try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
      } else if (pointeurs.size === 2) {
        dragging = false;               // deux doigts = zoom, pas rotation
        ecartPincement = distancePointeurs();
      }
    }

    function distancePointeurs() {
      const p = Array.from(pointeurs.values());
      if (p.length < 2) return 0;
      return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
    }

    function onPointerMove(e) {
      if (!pointeurs.has(e.pointerId)) return;
      pointeurs.set(e.pointerId, { x: e.clientX, y: e.clientY });

      if (pointeurs.size >= 2) {
        // Pincement — l'équivalent tactile de la molette.
        const ecart = distancePointeurs();
        if (ecartPincement > 0 && ecart > 0) {
          cameraZTarget += (ecartPincement - ecart) * 0.35;
          cameraZTarget = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, cameraZTarget));
        }
        ecartPincement = ecart;
        return;
      }

      if (!dragging) return;
      const dx = e.clientX - lastDragX;
      const dy = e.clientY - lastDragY;
      dragRotY += dx * 0.006;
      dragRotX += dy * 0.006;
      dragRotX = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, dragRotX));
      dragVelX += dy * 0.0015;
      dragVelY += dx * 0.0015;
      lastDragX = e.clientX; lastDragY = e.clientY;
    }

    function onPointerUp(e) {
      pointeurs.delete(e.pointerId);
      if (pointeurs.size < 2) ecartPincement = 0;
      if (pointeurs.size === 0) {
        dragging = false;
        canvas.style.cursor = "grab";
      }
      try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
    }

    function onWheel(e) {
      e.preventDefault();
      const delta = e.deltaY * 0.18;
      cameraZTarget += delta;
      cameraZTarget = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, cameraZTarget));
    }

    // En mode frozen, le pointeur de la séquence est réservé au skip.
    // Pas de drag, pas de zoom.
    // Sans `touchAction: none`, le navigateur mobile interprète le glissement
    // comme un défilement de page et l'orbe ne bouge jamais.
    if (!FROZEN) attacherPointeur();

    // Attache/detache regroupes : la liste etait dupliquee sur trois sites
    // (init, setFrozen, destroy), ce qui avait deja laisse des ecouteurs
    // souris orphelins apres le passage au pointeur.
    function attacherPointeur() {
      canvas.addEventListener("pointerdown", onPointerDown);
      canvas.addEventListener("pointermove", onPointerMove);
      canvas.addEventListener("pointerup", onPointerUp);
      canvas.addEventListener("pointercancel", onPointerUp);
      canvas.addEventListener("pointerleave", onPointerUp);
      canvas.addEventListener("wheel", onWheel, { passive: false });
      canvas.style.cursor = "grab";
      canvas.style.touchAction = "none";
    }

    function detacherPointeur() {
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("pointerleave", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      canvas.style.cursor = "";
      canvas.style.touchAction = "";
      pointeurs.clear();
    }

    // ── Resize ───────────────────────────────────────────────────────
    const resizeTarget = canvas.parentElement || canvas;
    const ro = new ResizeObserver(() => {
      const nw = resizeTarget.clientWidth  || canvas.offsetWidth;
      const nh = resizeTarget.clientHeight || canvas.offsetHeight;
      if (!nw || !nh) return;
      renderer.setSize(nw, nh, false);
      camera.aspect = nw / nh;
      camera.updateProjectionMatrix();
    });
    ro.observe(resizeTarget);

    // ── Animation loop ───────────────────────────────────────────────
    function animate() {
      if (destroyed) return;
      requestAnimationFrame(animate);

      t += 0.016;

      const lerp = (a, b, k) => a + (b - a) * k;
      const LK = 0.02;

      // State targets
      switch (state) {
        case "idle":
          targetRadius = S.GEOMETRY.R;                  // 28
          targetSpeed  = 0.18;
          targetBright = S.MATERIAL.OPACITY_IDLE;       // 0.72
          targetSize   = S.MATERIAL.POINT_SIZE_IDLE;    // 0.58
          targetLineAmount   = S.ANIM_IDLE.LINE_AMOUNT_IDLE; // 0.12
          targetElectronRate = 0;
          targetGlow   = 0.42;
          break;
        case "listening":
          targetRadius = 27; targetSpeed = 0.26; targetBright = 0.80;
          targetSize = 0.61; targetLineAmount = 0.28; targetElectronRate = 0;
          targetGlow = 0.54;
          break;
        case "thinking":
          targetRadius = 25; targetSpeed = 0.38; targetBright = 0.85;
          targetSize = 0.57; targetLineAmount = 0.72; targetElectronRate = 0.012;
          targetGlow = 0.68;
          break;
        case "speaking":
          targetRadius = 27; targetSpeed = 0.20; targetBright = 0.86;
          targetSize = 0.63; targetLineAmount = 0.48; targetElectronRate = 0;
          targetGlow = 0.62;
          break;
      }

      currentRadius     = lerp(currentRadius,     targetRadius,     LK);
      currentSpeed      = lerp(currentSpeed,       targetSpeed,      LK);
      currentBright     = lerp(currentBright,      targetBright,     LK);
      currentSize       = lerp(currentSize,        targetSize,       LK);
      currentLineAmount = lerp(currentLineAmount,  targetLineAmount, LK);
      electronSpawnRate = lerp(electronSpawnRate,  targetElectronRate, LK);
      currentGlow       = lerp(currentGlow,        targetGlow,       LK);

      // Hue lerp per state — dérivé de l'accent du thème (BASE_HEX), éclairci
      // pour thinking/speaking. Suit donc la couleur choisie dans Apparence.
      const _white = new THREE.Color(0xffffff);
      if (state === "thinking") {
        const c = new THREE.Color(S.COLOR.BASE_HEX).lerp(_white, 0.35);  // éclairci (glacé)
        mat.color.lerp(c, 0.015);
        lineMat.color.lerp(c, 0.015);
      } else if (state === "speaking") {
        const c = new THREE.Color(S.COLOR.BASE_HEX).lerp(_white, 0.18);  // légèrement éclairci
        mat.color.lerp(c, 0.015);
        lineMat.color.lerp(c, 0.015);
      } else {
        mat.color.lerp(new THREE.Color(S.COLOR.BASE_HEX), 0.015);     // accent exact
        lineMat.color.lerp(new THREE.Color(S.COLOR.BASE_HEX), 0.015);
      }

      // ── Music beat modulation ─────────────────────────────────────
      let beatMod = 0;
      if (musicTempo > 0) {
        musicBeatPhase += (musicTempo / 60) * 0.016;
        beatMod = Math.sin(musicBeatPhase * Math.PI * 2);
      }
      const beatAmp = musicEnergy * 0.12;

      mat.size    = currentSize   + beatMod * musicEnergy * 0.03;
      mat.opacity = Math.min(1, currentBright + beatMod * beatAmp);

      // Auto-rotation + impulse decay
      if (!FROZEN) {
        autoRotY += 0.0015;
        dragVelX *= 0.88;
        dragVelY *= 0.88;
      }

      // ── Particle simulation ────────────────────────────────────────
      // En mode frozen, le drift est entièrement délégué à onTick. La
      // boucle interne est court-circuitée pour libérer le budget CPU au
      // pilotage externe (convergence, settle, etc.).
      if (!FROZEN) {
        const a = pos;
        // Les basses dilatent nettement plus que l'ancien niveau global
        // (0,15) : c'est ce qui donne l'impression que la sphere respire.
        const souffle = audioLevel * 0.15 + bass * 0.30;
        const effectiveRadius = currentRadius * (1 + souffle) + beatMod * musicEnergy * 1.8;

        for (let i = 0; i < N; i++) {
          const i3 = i * 3;
          const x  = a[i3], y = a[i3+1], z = a[i3+2];
          const px = phase[i];

          // 0. Pulsation sur les mediums : chaque particule est poussee vers
          //    l'exterieur au rythme de la voix, avec un dephasage propre —
          //    l'ensemble ondule au lieu de gonfler d'un bloc.
          if (mid > 0.01) {
            const dist = Math.sqrt(x*x + y*y + z*z) || 1;
            const impulsion = Math.sin(t * 8 + px) * mid * 0.022;
            vel[i3]   += (x / dist) * impulsion;
            vel[i3+1] += (y / dist) * impulsion;
            vel[i3+2] += (z / dist) * impulsion;
          }

          // 1. Noise drift (two harmonic layers, 3 axes)
          vel[i3]   += Math.sin(t * 0.05  + px)                 * 0.001  * currentSpeed;
          vel[i3+1] += Math.cos(t * 0.06  + px * 1.3)           * 0.001  * currentSpeed;
          vel[i3+2] += Math.sin(t * 0.055 + px * 0.7)           * 0.001  * currentSpeed;
          vel[i3]   += Math.sin(t * 0.02  + px * 2.1 + y * 0.1) * 0.0008 * currentSpeed;
          vel[i3+1] += Math.cos(t * 0.025 + px * 1.7 + z * 0.1) * 0.0008 * currentSpeed;
          vel[i3+2] += Math.sin(t * 0.022 + px * 0.9 + x * 0.1) * 0.0008 * currentSpeed;

          // 2. Radial spring — bidirectional Hooke around the shell
          //    pulls in from outside, pushes out from inside → no center collapse
          const dist = Math.sqrt(x*x + y*y + z*z) || 0.01;
          const spring = (dist - effectiveRadius) * 0.003;
          vel[i3]   -= (x / dist) * spring;
          vel[i3+1] -= (y / dist) * spring;
          vel[i3+2] -= (z / dist) * spring;

          // 3. Damping
          vel[i3]   *= 0.989;
          vel[i3+1] *= 0.989;
          vel[i3+2] *= 0.989;

          // 4. Drag impulse — tangential (perpendicular to radius)
          //    yaw  → tangent Y-axis: (-z,  0,  x) / r
          //    pitch → tangent X-axis: ( 0,  z, -y) / r
          if (Math.abs(dragVelX) > 0.001 || Math.abs(dragVelY) > 0.001) {
            vel[i3]   += (-z / dist) * dragVelY * 0.28;
            vel[i3+2] += ( x / dist) * dragVelY * 0.28;
            vel[i3+1] += ( z / dist) * dragVelX * 0.28;
            vel[i3+2] += (-y / dist) * dragVelX * 0.28;
          }

          // Integrate
          a[i3]   += vel[i3];
          a[i3+1] += vel[i3+1];
          a[i3+2] += vel[i3+2];
        }
        geo.attributes.position.needsUpdate = true;
      }

      // ── Lines + active connections (skip in frozen) ────────────────
      if (!FROZEN) {
        const a = pos;
        // Les basses etirent la portee des liaisons : le maillage se densifie
        // quand la voix porte.
        const lineDistance = 8 * (1 + bass * 0.6);
        const maxDistSq = lineDistance * lineDistance;
        const step = Math.max(1, Math.floor(N / 600)); // = 10 for N=6000

        let lineCount = 0;
        activeConnections.length = 0;

        for (let i = 0; i < N && lineCount < MAX_LINES; i += step) {
          const x1 = a[i*3], y1 = a[i*3+1], z1 = a[i*3+2];
          for (let j = i + step; j < N && lineCount < MAX_LINES; j += step) {
            const dx = a[j*3] - x1, dy = a[j*3+1] - y1, dz = a[j*3+2] - z1;
            if (dx*dx + dy*dy + dz*dz < maxDistSq) {
              const idx = lineCount * 6;
              linePos[idx]   = x1;       linePos[idx+1] = y1;       linePos[idx+2] = z1;
              linePos[idx+3] = a[j*3];   linePos[idx+4] = a[j*3+1]; linePos[idx+5] = a[j*3+2];
              if (activeConnections.length < 500) {
                activeConnections.push({ x1, y1, z1, x2: a[j*3], y2: a[j*3+1], z2: a[j*3+2] });
              }
              lineCount++;
            }
          }
        }
        lineGeo.setDrawRange(0, lineCount * 2);
        lineGeo.attributes.position.needsUpdate = true;
        lineMat.opacity = currentLineAmount * S.ANIM_IDLE.LINE_OPACITY_K;

        // Electrons (thinking only)
        if (activeConnections.length > 0 && electronSpawnRate > 0.005) {
          if (activeElectrons.length < 3 && (t - lastElectronSpawn) > 1.0) {
            const conn = activeConnections[Math.floor(Math.random() * activeConnections.length)];
            activeElectrons.push({
              sx: conn.x1, sy: conn.y1, sz: conn.z1,
              ex: conn.x2, ey: conn.y2, ez: conn.z2,
              t: 0,
              speed: 0.003 + Math.random() * 0.003,
            });
            lastElectronSpawn = t;
          }
        }

        let ek = 0;
        for (let e = activeElectrons.length - 1; e >= 0; e--) {
          const el = activeElectrons[e];
          el.t += el.speed;
          if (el.t >= 1) { activeElectrons.splice(e, 1); continue; }
          electronPos[ek*3]   = el.sx + (el.ex - el.sx) * el.t;
          electronPos[ek*3+1] = el.sy + (el.ey - el.sy) * el.t;
          electronPos[ek*3+2] = el.sz + (el.ez - el.sz) * el.t;
          ek++;
        }
        electronGeo.setDrawRange(0, ek);
        electronGeo.attributes.position.needsUpdate = true;
      } else {
        // En frozen, masquer lignes et électrons (cohérent avec un orbe en cours d'assemblage).
        lineGeo.setDrawRange(0, 0);
        electronGeo.setDrawRange(0, 0);
        lineMat.opacity = 0;
      }


      // ── Breath Z — skip en frozen ──────────────────────────────────
      if (!FROZEN) {
        let zTarget = Math.sin(t * 0.10) * 0.8;
        if (state === "thinking")      zTarget = Math.sin(t * 0.16) * 1.4 + Math.sin(t * 0.41) * 0.5;
        else if (state === "speaking") zTarget = Math.sin(t * 0.11) * 1.0;

        cloudZVel += (zTarget - cloudZ) * 0.008;
        cloudZVel *= 0.94;
        cloudZ    += cloudZVel;

        points.position.z       = cloudZ;
        lines.position.z        = cloudZ;
        electronPoints.position.z = cloudZ;
      }


      // ── Rotation (auto + drag) — skip en frozen, géré par onTick ────
      if (!FROZEN) {
        const rotY = dragRotY + autoRotY;
        const rotX = dragRotX;

        points.rotation.y         = rotY;
        lines.rotation.y          = rotY;
        electronPoints.rotation.y = rotY;
        points.rotation.x         = rotX;
        lines.rotation.x          = rotX;
        electronPoints.rotation.x = rotX;
      }

      // ── Camera — skip en frozen, séquence pilote z (dolly) via onTick ──
      if (!FROZEN) {
        cameraZ += (cameraZTarget - cameraZ) * 0.18;
        camera.position.z = cameraZ;
        camera.position.x = Math.sin(t * S.CAMERA.OSC_X_FREQ) * S.CAMERA.OSC_X_AMPL;
        camera.position.y = Math.cos(t * S.CAMERA.OSC_Y_FREQ) * S.CAMERA.OSC_Y_AMPL;
        camera.lookAt(0, 0, cloudZ * 0.2);
      }

      // ── Hook séquence — appelé AVANT render, peut muter positions/rotation/camera/material
      if (ON_TICK) {
        ON_TICK({
          positions: pos,
          geometry: geo,
          material: mat,
          points: points,
          scene: scene,
          camera: camera,
          renderer: renderer,
          t: t,
          phase: phase,
        });
      }

      renderer.render(scene, camera);
    }

    animate();

    // ── Public API ───────────────────────────────────────────────────
    return {
      setState(s) {
        lastState = state;
        state = (s === "idle" || s === "listening" || s === "thinking" || s === "speaking") ? s : "idle";
      },
      setAudioLevel(v) {
        audioLevel = Math.max(0, Math.min(1, v));
      },
      setZoom(z) {
        // Regle la distance de camera. `cameraZ` est mis a jour en meme temps
        // que sa cible : sans cela, la boucle interpolerait depuis l'ancienne
        // valeur et l'orbe entrerait en scene par un zoom parasite.
        cameraZTarget = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
        cameraZ = cameraZTarget;
        camera.position.z = cameraZ;
      },
      setAudioBands(b, m, tr) {
        bass   = Math.max(0, Math.min(1, b  || 0));
        mid    = Math.max(0, Math.min(1, m  || 0));
        treble = Math.max(0, Math.min(1, tr || 0));
      },
      setMusicBeat(tempo, energy) {
        musicTempo  = Math.max(0, tempo  || 0);
        musicEnergy = Math.max(0, Math.min(1, energy || 0));
        if (musicTempo === 0) musicBeatPhase = 0;
      },
      resize(nw, nh) {
        // Les dimensions sont FACULTATIVES : l'API publiee en tete de fichier
        // est `resize()`, et un appel sans argument passait `undefined` a
        // setSize, ce qui reduisait le tampon de rendu a 0x0 — canvas a la
        // bonne taille CSS, mais plus rien de dessine.
        if (nw === undefined || nh === undefined) {
          nw = resizeTarget.clientWidth || canvas.offsetWidth;
          nh = resizeTarget.clientHeight || canvas.offsetHeight;
        }
        // Meme garde que l'observateur de redimensionnement : une taille nulle
        // pendant un reflow ne doit pas detruire le rendu.
        if (!nw || !nh) return;
        renderer.setSize(nw, nh, false);
        camera.aspect = nw / nh;
        camera.updateProjectionMatrix();
      },
      // ── Hooks séquence ────────────────────────────────────────────
      // Disponibles que opts.frozen soit vrai ou non, mais ne sont
      // exploités par la séquence qu'en frozen.
      getPositions:     function () { return pos; },
      getPhase:         function () { return phase; },          // randoms init par particule
      commitPositions:  function () { geo.attributes.position.needsUpdate = true; },
      getPoints:        function () { return points; },         // rotation, position.z (cloudZ)
      getGeometry:      function () { return geo; },
      getMaterial:      function () { return mat; },            // opacity, size, color
      getCamera:        function () { return camera; },         // dolly
      getScene:         function () { return scene; },          // ajout d'overlays (beam, nucleus, ocean)
      getParticleCount: function () { return N; },
      isFrozen:         function () { return FROZEN; },
      setFrozen:        function (value) {
        const next = !!value;
        if (next === FROZEN) return;
        const wasFrozen = FROZEN;
        FROZEN = next;
        if (wasFrozen && !FROZEN) {
          // Unfreeze : on rattache drag + zoom, on remet cameraZ à sa cible.
          attacherPointeur();
        } else if (!wasFrozen && FROZEN) {
          detacherPointeur();
        }
      },
      destroy() {
        destroyed = true;
        if (!FROZEN) detacherPointeur();
        ro.disconnect();
        renderer.dispose();
        geo.dispose();
        mat.dispose();
        lineGeo.dispose();
        lineMat.dispose();
        electronGeo.dispose();
        electronMat.dispose();
        sprite.dispose();
      },
    };
  }

  window.createCrushOrb = function (canvas, opts) {
    return createOrbInternal(canvas, opts);
  };

  // `new CrushOrb(canvas)` still works — constructor returning an object
  // causes `new` to return that object instead of `this`
  window.CrushOrb = function (canvas, opts) {
    return createOrbInternal(canvas, opts);
  };
})();

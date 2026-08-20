/* home.js — page Home runtime */
(function () {
  "use strict";

  const J = window.Crush;

  // ── Atmosphere + Mission Control ──────────────────────────────────
  J.mountAtmosphere();
  J.mountRooms({ mode: "home", pages: [], activePage: null, onNav: () => {} });

  // ── Pont voix → widget chat (voice_client.js) ──────────────────────
  // voice_client.js pousse les transcriptions (toi) et les réponses agent via
  // addMsg / checkForMindmap globaux. On délègue au MÊME renderer que le chat
  // texte (appendChatMsg, défini plus bas — hoisté) pour que la conversation
  // vocale s'affiche dans le widget, qu'il soit ouvert ou non. Les rôles voix
  // ('vous'/'crush') sont normalisés vers user/assistant.
  window.addMsg = function (role, text = "", streaming = false) {
    const normalized = role === "crush" || role === "assistant" ? "assistant" : "user";
    return appendChatMsg(normalized, text, streaming);
  };
  window.checkForMindmap = function () {};

  // ── WebSocket session ID / send (pour mediapipe_vision.js) ───────
  let _ws = null;
  window._crushSessionId = () => null;
  window._crushWsSend = (data) => {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify(data));
    }
  };

  // ── Clock + date ──────────────────────────────────────────────────
  // Horloge et date retirees de l'accueil : l'orbe est seul en scene. Le timer
  // qui les rafraichissait chaque seconde tombe avec elles.

  // ── État orbe ─────────────────────────────────────────────────────
  const STATE_DOTS = {
    idle:      { cls: "blue",   label: "AU REPOS" },
    listening: { cls: "blue",   label: "EN ÉCOUTE" },
    thinking:  { cls: "purple", label: "RÉFLEXION" },
    speaking:  { cls: "purple", label: "PARLE" },
    success:   { cls: "green",  label: "SUCCÈS" },
    offline:   { cls: "gray",   label: "HORS LIGNE" },
  };

  let _orb = null;
  let _currentState = "idle";

  function setOrbState(state) {
    _currentState = state;
    const meta = STATE_DOTS[state] || STATE_DOTS.idle;
    const dot = document.getElementById("status-dot");
    const lbl = document.getElementById("status-label");
    if (dot) dot.className = "status-dot " + meta.cls;
    if (lbl) lbl.textContent = meta.label;
    if (_orb) _orb.setState(state);
  }
  // Exposé pour les scripts externes (ex. voice_client.js pilote l'orbe via ça).
  window.__crushSetOrbState = setOrbState;
  // Meme motif : voice_client.js analyse l'audio et pousse l'amplitude ici
  // pour que l'orbe respire au rythme de la parole.
  window.__crushSetAudioLevel = function (v) { if (_orb) _orb.setAudioLevel(v); };
  window.__crushSetAudioBands = function (b, m, a) {
    if (_orb && _orb.setAudioBands) _orb.setAudioBands(b, m, a);
  };

  function initOrb() {
    if (typeof THREE === "undefined" || typeof CrushOrb === "undefined") {
      setTimeout(initOrb, 100);
      return;
    }
    // Cold boot wake : la séquence va mounter l'orbe sur #orb-canvas en
    // frozen, puis nous le rendra via injectWakeOrb() à l'entrée ONLINE.
    if (window.__crushWakeActive) return;
    const canvas = document.getElementById("orb-canvas");
    if (!canvas) return;
    _orb = new CrushOrb(canvas);
    setOrbState("idle");
  }
  initOrb();

  // ── Bascule depuis la séquence de réveil ────────────────────────────
  // wake_sequence.js appelle ça à l'entrée ONLINE : l'orbe partagé est
  // remis à home.js, qui prend la main pour les states voice/audio/music.
  window.__crushInjectOrb = function (orb) {
    _orb = orb;
    setOrbState("idle");
  };

  // ── body.view-active : source de vérité unique pour toutes les vues ─
  const _origViewActivate   = J.views.activate.bind(J.views);
  const _origViewDeactivate = J.views.deactivate.bind(J.views);
  J.views.activate = function (id, params) {
    _origViewActivate(id, params);
    document.body.classList.add("view-active");
  };
  J.views.deactivate = function (id) {
    _origViewDeactivate(id);
    // Différé : si activate() enchaîne, _active est déjà re-setté quand le check tourne
    Promise.resolve().then(() => {
      if (!J.views._active) document.body.classList.remove("view-active");
    });
  };

  // ── Sync état orbe depuis les events voice.js ─────────────────────
  const VOICE_ORB_MAP = {
    vad_start:   "listening",
    stt_done:    "thinking",
    llm_start:   "thinking",
    tts_start:   "speaking",
    tts_done:    "idle",
    interrupted: "listening",
  };
  window.addEventListener("crush:ws", (e) => {
    const msg = e.detail;
    if (VOICE_ORB_MAP[msg.type]) setOrbState(VOICE_ORB_MAP[msg.type]);
    if (msg.type === "tts_done" || msg.type === "done") {
      setTimeout(() => setOrbState("idle"), 1200);
    }
  });

  // ── Controls (pictos haut-gauche) ─────────────────────────────────
  const CTRL_DEFS = [
    { key: "mic",    id: "hc-mic" },
    { key: "screen", id: "hc-screen" },
    { key: "cam",    id: "hc-cam" },
    { key: "files",  id: "hc-files" },
    { key: "music",  id: "hc-music",  widget: "hc-widget-music" },
    { key: "chat",   id: "hc-chat",   widget: "hc-widget-chat" },
  ];

  const _ctrlState = {};
  CTRL_DEFS.forEach(c => { _ctrlState[c.key] = false; });

  function setCtrl(key, val) {
    _ctrlState[key] = val;
    const def = CTRL_DEFS.find(c => c.key === key);
    if (!def) return;
    const btn = document.getElementById(def.id);
    if (btn) btn.classList.toggle("active", val);
    if (def.widget) {
      const w = document.getElementById(def.widget);
      if (w) w.classList.toggle("is-open", val);
    }
  }

  function showVoiceChrome(active) {
    const dot = document.getElementById("status-dot");
    const lbl = document.getElementById("status-label");
    if (dot) dot.style.display = active ? "" : "none";
    if (lbl) lbl.style.display = active ? "" : "none";
  }

  async function toggleCtrl(key) {
    const next = !_ctrlState[key];

    if (key === "mic") {
      const vc = window._voiceClient;
      if (!vc) {
        J.notify({ kind: "err", text: "Mode vocal non initialisé — recharge la page." });
        return;
      }
      if (next) {
        setCtrl("mic", true);
        showVoiceChrome(true);
        try {
          await vc._start();
          setCtrl("chat", true);
          loadChatWidget();
        } catch (err) {
          console.error("[Mic] Erreur demarrage :", err);
          vc._stop();
          setCtrl("mic", false);
          showVoiceChrome(false);
          setOrbState("offline");
          J.notify({ kind: "err", text: err.message || "Mode vocal indisponible" });
          setTimeout(() => setOrbState("idle"), 2000);
        }
      } else {
        vc._stop();
        setCtrl("mic", false);
        showVoiceChrome(false);
        setOrbState("idle");
      }
      return;
    }

    if (key === "cam") {
      if (next) {
        setCtrl("cam", true);  // marque actif immédiatement pour que le 2e clic puisse désactiver
        const ok = await startCamera();
        if (!ok) {
          setCtrl("cam", false);
          return;
        }
        J.api.patch("/api/permissions/camera", { enabled: true }).catch(() => {});
      } else {
        stopCamera();
        setCtrl("cam", false);
        J.api.patch("/api/permissions/camera", { enabled: false }).catch(() => {});
      }
      return;
    }

    if (key === "screen") {
      setCtrl("screen", next);
      J.api.patch("/api/permissions/screen", { enabled: next }).catch(() => {});
      return;
    }

    if (key === "files") {
      setCtrl("files", next);
      J.api.patch("/api/permissions/files", { enabled: next }).catch(() => {});
      return;
    }

    if (key === "music") {
      setCtrl("music", next);
      if (next) startMusicPoll();
      else stopMusicPoll();
      return;
    }

    if (key === "chat") {
      setCtrl("chat", next);
      if (next) loadChatWidget();
      return;
    }
  }

  // Charge l'etat initial des permissions depuis le serveur
  (async function loadPermissions() {
    try {
      const perms = await J.api.get("/api/permissions");
      if (perms.screen   != null) setCtrl("screen", perms.screen);
      if (perms.camera   != null) setCtrl("cam",    perms.camera);
      if (perms.files    != null) setCtrl("files",  perms.files);
    } catch (_) {}
  })();

  CTRL_DEFS.forEach(c => {
    const btn = document.getElementById(c.id);
    if (btn) btn.addEventListener("click", () => toggleCtrl(c.key));
  });

  function wireVoiceClient() {
    const vc = window._voiceClient;
    if (!vc) return;
    vc._onActiveChange = (active) => {
      setCtrl("mic", active);
      showVoiceChrome(active);
      if (!active) setOrbState("idle");
    };
  }

  wireVoiceClient();
  document.addEventListener("DOMContentLoaded", wireVoiceClient, { once: true });

  // ── Caméra (MediaPipe) ─────────────────────────────────────────────
  let _camStream = null;

  async function startCamera() {
    try {
      _camStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
      });
      const video = document.getElementById("cam-video");
      if (video) {
        video.srcObject = _camStream;
        await video.play();
      }
      document.getElementById("cam-overlay")?.classList.add("is-open");
      // Initialise MediaPipe (charge les modèles si nécessaire)
      if (typeof mpInit === "function") {
        await mpInit();
        mpStart();
      }
      return true;
    } catch (err) {
      console.error("[Camera] Erreur :", err);
      return false;
    }
  }

  function stopCamera() {
    if (typeof mpStop === "function") mpStop();
    if (_camStream) {
      _camStream.getTracks().forEach(t => t.stop());
      _camStream = null;
    }
    const video = document.getElementById("cam-video");
    if (video) video.srcObject = null;
    document.getElementById("cam-overlay")?.classList.remove("is-open");
  }


  // ── Widget Musique ─────────────────────────────────────────────────
  let _musicPollTimer = null;
  let _musicPlaying   = false;

  function startMusicPoll() {
    fetchMusicStatus();
    _musicPollTimer = setInterval(fetchMusicStatus, 5000);
  }

  function stopMusicPoll() {
    clearInterval(_musicPollTimer);
    _musicPollTimer = null;
  }

  async function fetchMusicStatus() {
    try {
      const s = await J.api.get("/api/music/status");
      updateMusicWidget(s);
    } catch (_) {}
  }

  function updateMusicWidget(s) {
    const trackEl  = document.getElementById("hcw-music-track");
    const artistEl = document.getElementById("hcw-music-artist");
    const srcEl    = document.getElementById("hcw-music-source");
    const artEl    = document.getElementById("hcw-album-art");
    const fillEl   = document.getElementById("hcw-progress-fill");
    const playIcon = document.getElementById("hcw-play-icon");

    if (!s || !s.connected) {
      if (trackEl)  trackEl.textContent  = "Non connecté";
      if (artistEl) artistEl.textContent = s?.provider ? "Configurer dans les paramètres" : "Aucun service configuré";
      if (srcEl)    srcEl.textContent    = s?.provider || "—";
      return;
    }

    _musicPlaying = s.is_playing || false;

    const providerLabel = (s.provider || "").toUpperCase();
    if (srcEl) srcEl.textContent = providerLabel || "—";

    if (!s.track) {
      if (trackEl)  trackEl.textContent  = "Aucune lecture";
      if (artistEl) artistEl.textContent = providerLabel ? providerLabel + " CONNECTÉ" : "—";
    } else {
      if (trackEl)  trackEl.textContent  = s.track;
      if (artistEl) artistEl.textContent = s.last_played
        ? (s.artist || "") + (s.artist ? " — DERNIER JOUÉ" : "DERNIER JOUÉ")
        : (s.artist || "");
    }

    // Album art
    if (artEl) {
      if (s.album_art) {
        artEl.src = s.album_art;
        artEl.classList.add("loaded");
      } else {
        artEl.classList.remove("loaded");
      }
    }

    // Barre de progression
    if (fillEl && s.duration_ms > 0) {
      const pct = Math.min(100, (s.progress_ms / s.duration_ms) * 100);
      fillEl.style.width = pct + "%";
    }

    // Icône play/pause
    if (playIcon) {
      playIcon.innerHTML = _musicPlaying
        ? '<line x1="5" y1="2" x2="5" y2="13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="10" y1="2" x2="10" y2="13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
        : '<path fill="currentColor" d="M3 2l10 4.5L3 11V2z"/>';
    }
  }

  // ── Spotify Web Playback SDK ───────────────────────────────────────
  let _spotifyPlayer   = null;
  let _spotifyDeviceId = null;
  let _sdkProgressTimer = null;
  let _currentTrackId  = null;

  async function fetchTrackBeat(trackId) {
    // Fallback immédiat — l'API audio-features est dépréciée pour les nouvelles apps
    const applyBeat = (tempo, energy) => { if (_orb) _orb.setMusicBeat(tempo, energy); };
    try {
      const { token } = await (await fetch("/api/spotify/token", { headers: J.authHeaders ? J.authHeaders() : {} })).json();
      if (!token) return applyBeat(120, 0.6);
      const resp = await fetch(
        `https://api.spotify.com/v1/audio-features/${trackId}`,
        { headers: { Authorization: `Bearer ${token}` } }
      );
      if (!resp.ok) return applyBeat(120, 0.6);
      const f = await resp.json();
      applyBeat(f.tempo || 120, f.energy ?? 0.6);
    } catch (_) {
      applyBeat(120, 0.6);
    }
  }

  function _tickSDKProgress() {
    if (!_spotifyPlayer || !_musicPlaying) return;
    _spotifyPlayer.getCurrentState().then(state => {
      if (!state) return;
      const fillEl = document.getElementById("hcw-progress-fill");
      if (fillEl && state.duration) {
        fillEl.style.width = Math.min(100, (state.position / state.duration) * 100) + "%";
      }
    }).catch(() => {});
  }

  async function initSpotifySDK() {
    if (_spotifyPlayer || window._sdkInitStarted) return;
    window._sdkInitStarted = true;

    try {
      const status = await J.api.get("/api/music/provider-status");
      if (status.provider !== "spotify") return;
    } catch (_) { return; }

    window.onSpotifyWebPlaybackSDKReady = () => {
      _spotifyPlayer = new Spotify.Player({
        name: window.CRUSH_ASSISTANT_NAME || "CRUSH",
        getOAuthToken: async (cb) => {
          try {
            const res = await fetch("/api/spotify/token", { headers: J.authHeaders ? J.authHeaders() : {} });
            const data = await res.json();
            cb(data.token || "");
          } catch (_) { cb(""); }
        },
        volume: 0.7,
      });

      _spotifyPlayer.addListener("ready", ({ device_id }) => {
        _spotifyDeviceId = device_id;
        // Enregistre le device sans transférer la lecture
        fetch("/api/spotify/transfer", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(J.authHeaders ? J.authHeaders() : {}) },
          body: JSON.stringify({ device_id }),
        }).catch(() => {});
        // Charge l'état initial (dernier joué)
        if (_ctrlState.music) fetchMusicStatus();
      });

      _spotifyPlayer.addListener("not_ready", () => {
        _spotifyDeviceId = null;
      });

      _spotifyPlayer.addListener("player_state_changed", (state) => {
        if (!state) return;
        const track = state.track_window?.current_track;
        if (!track) return;
        _musicPlaying = !state.paused;

        // Beat sync
        const tid = track.id;
        const wasPlaying = _musicPlaying;
        if (tid && (tid !== _currentTrackId || (!wasPlaying && !state.paused))) {
          _currentTrackId = tid;
          if (!state.paused) fetchTrackBeat(tid);
        }
        if (state.paused && _orb) _orb.setMusicBeat(0, 0);

        updateMusicWidget({
          connected: true,
          is_playing: !state.paused,
          track: track.name,
          artist: track.artists?.map(a => a.name).join(", ") || "",
          album: track.album?.name || "",
          album_art: track.album?.images?.[0]?.url || null,
          progress_ms: state.position,
          duration_ms: track.duration_ms,
          provider: "spotify",
        });
      });

      _spotifyPlayer.connect();
      _sdkProgressTimer = setInterval(_tickSDKProgress, 1000);
    };

    const script = document.createElement("script");
    script.src = "https://sdk.scdn.co/spotify-player.js";
    document.head.appendChild(script);
  }

  // Démarrage SDK au chargement de la page
  initSpotifySDK();

  // Boutons du widget musique — SDK en priorité, API en fallback
  document.getElementById("hcw-prev")?.addEventListener("click", () => {
    if (_spotifyPlayer) { _spotifyPlayer.previousTrack().catch(() => {}); return; }
    J.api.post("/api/music/prev").then(fetchMusicStatus).catch(() => {});
  });
  document.getElementById("hcw-play")?.addEventListener("click", () => {
    if (_spotifyPlayer) { _spotifyPlayer.togglePlay().catch(() => {}); return; }
    J.api.post(_musicPlaying ? "/api/music/pause" : "/api/music/play")
      .then(fetchMusicStatus).catch(() => {});
  });
  document.getElementById("hcw-next")?.addEventListener("click", () => {
    if (_spotifyPlayer) { _spotifyPlayer.nextTrack().catch(() => {}); return; }
    J.api.post("/api/music/next").then(fetchMusicStatus).catch(() => {});
  });


  // ── Widget Chat ────────────────────────────────────────────────────
  async function loadChatWidget() {
    const msgsEl  = document.getElementById("hcw-chat-msgs");
    const countEl = document.getElementById("hcw-chat-count");
    if (!msgsEl) return;
    try {
      const sessions = await J.api.get("/api/sessions");
      if (!sessions.length) {
        msgsEl.innerHTML = '<div class="hcw-empty">Aucune session</div>';
        return;
      }
      const msgs = await J.api.get("/api/sessions/" + sessions[0].id + "/messages?limit=30");
      if (countEl) countEl.textContent = msgs.length + " msg";
      msgsEl.innerHTML = "";
      const recent = msgs.slice(-14);
      if (!recent.length) {
        msgsEl.innerHTML = '<div class="hcw-empty">Aucun message</div>';
        return;
      }
      recent.forEach(m => {
        const text = m.content || m.text || "";
        if (!text) return;
        const wrap = document.createElement("div");
        wrap.className = "hcw-msg";
        const role = document.createElement("span");
        role.className = "hcw-msg-role " + (m.role === "assistant" ? "assistant" : "");
        role.textContent = m.role === "assistant" ? (window.CRUSH_ASSISTANT_NAME || "CRUSH") : "VOUS";
        const body = document.createElement("span");
        body.className = "hcw-msg-text";
        body.textContent = text.length > 220 ? text.slice(0, 217) + "…" : text;
        wrap.appendChild(role);
        wrap.appendChild(body);
        msgsEl.appendChild(wrap);
      });
      msgsEl.scrollTop = msgsEl.scrollHeight;
    } catch (_) {
      msgsEl.innerHTML = '<div class="hcw-empty">Erreur de chargement</div>';
    }
  }

  // ── Widget Chat : envoi de message texte ──────────────────────────
  let _chatSending = false;

  function appendChatMsg(role, text, streaming) {
    const msgsEl = document.getElementById("hcw-chat-msgs");
    if (!msgsEl) return null;
    const empty = msgsEl.querySelector(".hcw-empty");
    if (empty) empty.remove();

    const wrap = document.createElement("div");
    wrap.className = "hcw-msg";
    const roleEl = document.createElement("span");
    roleEl.className = "hcw-msg-role" + (role === "assistant" ? " assistant" : "");
    roleEl.textContent = role === "assistant" ? (window.CRUSH_ASSISTANT_NAME || "CRUSH") : "VOUS";
    const bodyEl = document.createElement("span");
    bodyEl.className = "hcw-msg-text";
    bodyEl.textContent = text;
    wrap.appendChild(roleEl);
    wrap.appendChild(bodyEl);
    msgsEl.appendChild(wrap);
    msgsEl.scrollTop = msgsEl.scrollHeight;
    if (streaming) return bodyEl;
    return null;
  }

  async function sendChatMessage() {
    if (_chatSending) return;
    const input = document.getElementById("hcw-input");
    const sendBtn = document.getElementById("hcw-send");
    const text = (input?.value || "").trim();
    if (!text) return;

    _chatSending = true;
    if (sendBtn) sendBtn.disabled = true;
    input.value = "";

    appendChatMsg("user", text, false);

    const streamTarget = appendChatMsg("assistant", "", true);
    setOrbState("thinking");

    try {
      const sessionId = localStorage.getItem("crush_voice_session") || null;
      const resp = await fetch("/api/voice/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(J.authHeaders ? J.authHeaders() : {}) },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      const returnedSid = resp.headers.get("x-session-id");
      if (returnedSid) localStorage.setItem("crush_voice_session", returnedSid);

      if (!resp.ok || !resp.body) throw new Error("Erreur réseau");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let full = "";
      setOrbState("speaking");

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        full += decoder.decode(value, { stream: true });
        if (streamTarget) {
          streamTarget.textContent = full;
          document.getElementById("hcw-chat-msgs").scrollTop = 9999;
        }
      }

      const countEl = document.getElementById("hcw-chat-count");
      if (countEl) {
        const cur = parseInt(countEl.textContent) || 0;
        countEl.textContent = (cur + 2) + " msg";
      }

      // Dire la réponse à voix haute. Le chemin écrit n'en produisait AUCUNE :
      // il passait l'orbe en « speaking » puis se contentait de streamer du
      // texte, si bien que l'interface annonçait une parole qui n'existait
      // pas. Seul le micro faisait parler l'assistant, ce qui n'a rien
      // d'évident quand on lui écrit depuis la même page.
      //
      // La lecture réutilise la file du client vocal : même ordonnancement,
      // même repli si le graphe audio est indisponible, et l'orbe respire au
      // rythme réel de la voix plutôt qu'en aveugle.
      if (full.trim()) await parlerTexte(full);
    } catch (err) {
      if (streamTarget) streamTarget.textContent = "Erreur de communication.";
    } finally {
      _chatSending = false;
      if (sendBtn) sendBtn.disabled = false;
      setTimeout(() => setOrbState("idle"), 1200);
    }
  }

  // Synthèse d'une réponse écrite, jouée par le client vocal.
  //
  // La voix n'est demandée qu'après la fin du flux texte : la synthétiser au
  // fil de l'eau doublerait les requêtes et couperait les phrases au milieu,
  // le serveur ne segmentant que pour le canal vocal.
  async function parlerTexte(texte) {
    const vc = window._voiceClient;
    if (!vc || typeof vc._jouer !== "function") return;
    try {
      const r = await fetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(J.authHeaders ? J.authHeaders() : {}) },
        body: JSON.stringify({ text: texte.slice(0, 2000) }),
      });
      if (!r.ok) return;
      const d = await r.json();
      // Une synthèse muette ne doit pas laisser l'orbe figé en « speaking ».
      if (d.audio_b64) vc._jouer(d.audio_b64, "audio/mpeg");
    } catch (e) {
      // Pas de voix n'empêche pas de lire la réponse : on n'alerte pas.
    }
  }

  document.getElementById("hcw-new-session")?.addEventListener("click", () => {
    localStorage.removeItem("crush_voice_session");
    const msgsEl  = document.getElementById("hcw-chat-msgs");
    const countEl = document.getElementById("hcw-chat-count");
    if (msgsEl)  msgsEl.innerHTML = '<div class="hcw-empty">Nouvelle conversation</div>';
    if (countEl) countEl.textContent = "—";
    document.getElementById("hcw-input")?.focus();
  });

  document.getElementById("hcw-send")?.addEventListener("click", sendChatMessage);

  document.getElementById("hcw-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChatMessage();
    }
  });


  // ── WebSocket — sync état orbe + canal passif ──────────────────────
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      _ws = new WebSocket(proto + "//" + location.host + "/ws");
    } catch (_) { return; }

    _ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }

      if (data.type === "voice_state") setOrbState(data.state || "idle");
      if (data.type === "audio_level" && _orb) _orb.setAudioLevel(data.level || 0);
      if (data.type === "wake_up") window.dispatchEvent(new CustomEvent("crush-wake", { detail: data }));

      if (data.type === "message" && data.role === "assistant" && data.text) {
        showChannel(data.text);
        if (_ctrlState.chat) loadChatWidget();
      }

      // ── View routing ──────────────────────────────────────────────
      if (data.type === "reload_views") {
        const _prevActive = J.views._active;
        if (_prevActive) J.views.deactivate(_prevActive);
        // Supprime aussi les scripts sans data-view-skill (chargés avant le fix)
        document.querySelectorAll('script[src*="/skills/"]').forEach(el => el.remove());
        document.querySelectorAll('link[href*="/skills/"]').forEach(el => el.remove());
        window.loadViewSkills?.().then?.(() => {
          if (_prevActive) J.views.activate(_prevActive);
        });
      }
      if (data.type === "show_home")  { const a = J.views._active; if (a) J.views.deactivate(a); }
      if (data.type === "show_view")    J.views.activate(data.view_id, data.params);
      if (data.type === "hide_view")    J.views.deactivate(data.view_id);
      if (data.type === "view_command") J.views.dispatch(data.view_id, data.command, data.params);

      // Backward compat (map_control legacy events)
      if (data.type === "map_fly_to")    { J.views.activate("globe"); J.views.dispatch("globe", "fly_to", data); }
      if (data.type === "map_zoom_out")  J.views.dispatch("globe", "zoom_out", {});
      if (data.type === "map_zoom_in")   J.views.dispatch("globe", "zoom_in", {});
      if (data.type === "map_globe_view"){ J.views.activate("globe"); J.views.dispatch("globe", "globe_view", {}); }
      if (data.type === "toggle_panels") J.views.dispatch("globe", "toggle_panels", {});
    };

    _ws.onclose = () => setTimeout(connectWS, 3000);
  }
  connectWS();

  // ── Canal passif (bas-droite) ──────────────────────────────────────
  let _channelTs    = null;
  let _channelTimer = null;

  function relativeTime(ts) {
    const mins = Math.round((Date.now() - ts) / 60000);
    if (mins < 1)  return "À L'INSTANT";
    if (mins === 1) return "IL Y A 1 MIN";
    if (mins < 60) return "IL Y A " + mins + " MIN";
    return "IL Y A " + Math.floor(mins / 60) + " H";
  }

  function updateChannelTime() {
    if (!_channelTs) return;
    const el = document.getElementById("channel-time");
    if (el) el.textContent = relativeTime(_channelTs);
  }

  function showChannel(text) {
    const el  = document.getElementById("home-channel");
    const msg = document.getElementById("channel-msg");
    if (!el || !msg) return;
    msg.textContent = text.length > 160 ? text.slice(0, 157) + "…" : text;
    _channelTs = Date.now();
    updateChannelTime();
    el.style.display = "";
    if (_channelTimer) clearInterval(_channelTimer);
    _channelTimer = setInterval(updateChannelTime, 30000);
  }

  (async function loadLastMessage() {
    try {
      const sessions = await J.api.get("/api/sessions");
      if (!sessions.length) return;
      const msgs = await J.api.get("/api/sessions/" + sessions[0].id + "/messages?limit=10");
      const last = [...msgs].reverse().find(m => m.role === "assistant");
      if (last && (last.content || last.text)) showChannel(last.content || last.text);
    } catch (_) {}
  })();

  // ── Navigation iframe — shell persistant ─────────────────────────
  const _frame = document.getElementById("page-frame");
  let _frameActive = false;

  Crush.navigateFrame = function (url) {
    if (!url || url === "/" || url === window.location.origin + "/") {
      // Retour home — cacher l'iframe
      if (_frame) {
        _frame.classList.remove("is-active");
        _frame.src = "about:blank";
      }
      _frameActive = false;
      history.replaceState(null, "", "/");
    } else {
      // Fermer toute vue active (globe, etc.) avant de naviguer
      Crush.views.deactivate();
      if (_frame) {
        _frame.src = url;
        _frame.classList.add("is-active");
      }
      _frameActive = true;
      history.pushState({ frame: url }, "", url);
    }
  };

  // Bouton retour navigateur
  window.addEventListener("popstate", (e) => {
    if (e.state?.frame) {
      if (_frame) { _frame.src = e.state.frame; _frame.classList.add("is-active"); }
      _frameActive = true;
    } else {
      if (_frame) { _frame.classList.remove("is-active"); _frame.src = "about:blank"; }
      _frameActive = false;
    }
  });

  // ── Touche espace → dashboard (ou ferme l'iframe si déjà ouvert) ──
  document.addEventListener("keydown", (e) => {
    if (e.key === " " && !e.metaKey && !e.ctrlKey) {
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      e.preventDefault();
      if (_frameActive) {
        Crush.navigateFrame("/");
      } else {
        Crush.navigateFrame("/dashboard");
      }
    }
  });

  // ── Helper drag-and-drop pour widgets / overlay ────────────────────
  function makeDraggable(el, handle) {
    if (!el) return;
    const grip = handle || el;

    grip.addEventListener("mousedown", (e) => {
      if (e.target.closest("button, input, textarea, .hcw-resize-handle")) return;
      e.preventDefault();
      let ex = e.clientX, ey = e.clientY;
      el.classList.add("dragging");

      function onMove(e) {
        const dx = e.clientX - ex;
        const dy = e.clientY - ey;
        ex = e.clientX; ey = e.clientY;
        const rect = el.getBoundingClientRect();
        el.style.left   = (rect.left + dx) + "px";
        el.style.top    = (rect.top  + dy) + "px";
        el.style.right  = "auto";
        el.style.bottom = "auto";
      }

      function onUp() {
        el.classList.remove("dragging");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup",   onUp);
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup",   onUp);
    });
  }

  // ── Helper redimensionnement pour widgets ──────────────────────────
  function makeResizable(el, handleEl, minW, minH) {
    if (!el || !handleEl) return;
    minW = minW || 180;
    minH = minH || 80;

    handleEl.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();

      // Ancrer en top/left au moment du resize
      const rect = el.getBoundingClientRect();
      el.style.left   = rect.left   + "px";
      el.style.top    = rect.top    + "px";
      el.style.right  = "auto";
      el.style.bottom = "auto";
      el.style.width  = rect.width  + "px";
      el.style.height = rect.height + "px";

      const startX = e.clientX;
      const startY = e.clientY;
      const startW = rect.width;
      const startH = rect.height;

      function onMove(e) {
        const w = Math.max(minW, startW + (e.clientX - startX));
        const h = Math.max(minH, startH + (e.clientY - startY));
        el.style.width  = w + "px";
        el.style.height = h + "px";
        // Enleve max-height sur le corps messages si present
        const msgs = el.querySelector(".hcw-messages");
        if (msgs) msgs.style.maxHeight = "none";
      }

      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup",   onUp);
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup",   onUp);
    });
  }

  // Attacher drag + resize aux widgets
  makeDraggable(document.getElementById("cam-overlay"), document.getElementById("cam-drag-handle"));
  makeResizable(document.getElementById("cam-overlay"), document.getElementById("cam-resize-handle"), 180, 120);

  makeDraggable(document.getElementById("hc-widget-music"));
  makeResizable(document.getElementById("hc-widget-music"), document.getElementById("music-resize-handle"), 180, 100);

  makeDraggable(document.getElementById("hc-widget-chat"));
  makeResizable(document.getElementById("hc-widget-chat"), document.getElementById("chat-resize-handle"), 220, 160);
})();

"use strict";

/* Client vocal de l'interface bureau — remplace le client LiveKit.
 *
 * Même pipeline que l'interface mobile : le micro du navigateur alimente
 * `/ws/voice`, qui transcrit, répond via le Gateway et renvoie l'audio. Aucun
 * serveur de signalisation, aucun WebRTC — cf. `interfaces/api/voice_ws.py`.
 *
 * DIFFÉRENCE AVEC LE MOBILE
 * =========================
 *
 * Le mobile fonctionne au maintien du bouton : l'utilisateur délimite lui-même
 * sa prise de parole. Ici le bouton est une BASCULE — on active le mode vocal
 * et on parle les mains libres. Il faut donc découper le flux, d'où la
 * détection d'activité vocale ci-dessous.
 *
 * Ce fichier conserve la surface publique de l'ancien client
 * (`window._voiceClient`, `window.crush`, `_onActiveChange`) pour que
 * `home.js` et l'orbe continuent de fonctionner sans modification.
 */

// ── Détection d'activité vocale ──────────────────────────────────────────────
//
// Seuils obtenus empiriquement sur micro de portable en pièce calme.
// `SEUIL_PAROLE` en RMS normalisé : au-dessus, on considère qu'on parle.
// Le silence de fin doit être assez long pour ne pas couper au milieu d'une
// phrase (on marque une pause en parlant), assez court pour rester réactif.
const SEUIL_PAROLE = 0.018;
const SILENCE_FIN_MS = 900;
const DUREE_MIN_MS = 350; // en dessous, c'est un bruit, pas une phrase
const DUREE_MAX_MS = 20000; // garde-fou : on coupe et on envoie

function showVoiceStatus(texte, duree = 0) {
  const el = document.getElementById("voice-status");
  if (!el) return;
  el.textContent = texte;
  el.classList.toggle("visible", texte.length > 0);
  if (duree > 0 && texte.length > 0) {
    setTimeout(() => {
      if (el.textContent === texte) el.classList.remove("visible");
    }, duree);
  }
}

function bindMicButton(client) {
  const homeBtn = document.getElementById("hc-mic");
  const legacyBtn = document.getElementById("perm-microphone");
  client._btn = homeBtn || legacyBtn || client._btn;
}

function typeAudioSupporte() {
  const candidats = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg;codecs=opus",
  ];
  return candidats.find((t) => MediaRecorder.isTypeSupported?.(t)) || "";
}

// ── Amplitude de la voix → orbe ──────────────────────────────────────────────
//
// `setAudioLevel` existait dans orb.js et dilate le rayon de 15 %, mais rien ne
// l'alimentait : le seul emetteur prevu (`audio_level` sur le WebSocket) venait
// de l'ancien pipeline LiveKit. On analyse donc l'audio ICI, dans le
// navigateur, pour que l'orbe respire au rythme reel de la parole.

// ── Restitution de la parole, via le graphe Web Audio ───────────────────────
//
// Un element <audio> reclame le focus audio du systeme, ce qui met en pause les
// autres lecteurs — dont le lecteur Spotify que cette page heberge. Un
// BufferSource passe pour un son d'interface et ne le prend pas. Le meme graphe
// fournit au passage l'amplitude qui anime l'orbe, sans dependre de
// `createMediaElementSource` (utilisable une seule fois par element, donc
// fragile avec un fragment audio par phrase).

var ctxAudio = null;

// Un contexte audio nait SUSPENDU tant qu'aucun geste utilisateur n'a eu lieu,
// et `resume()` n'aboutit que pendant un tel geste. Auparavant le son sortait
// d'un element <audio>, independant du contexte : meme suspendu, on entendait.
// Depuis le passage au BufferSource, tout en depend — un contexte reste
// suspendu et l'assistant devient muet sans un mot d'erreur.
//
// On le debloque donc au premier geste, quel qu'il soit, avant d'en avoir
// besoin.
["pointerdown", "keydown", "touchstart"].forEach(function (evt) {
  document.addEventListener(evt, function debloquer() {
    var ctx = contexteAudioPartage();
    if (ctx && ctx.state === "suspended") ctx.resume();
  }, { once: false, passive: true });
});

function contexteAudioPartage() {
  var C = window.AudioContext || window.webkitAudioContext;
  if (!C) return null;
  if (!ctxAudio) ctxAudio = new C();
  // Suspendu tant qu'aucun geste utilisateur n'a eu lieu ; l'appui sur le
  // micro le debloque.
  if (ctxAudio.state === "suspended") ctxAudio.resume();
  return ctxAudio;
}

function base64EnOctets(base64) {
  var brut = atob(base64);
  var buf = new Uint8Array(brut.length);
  for (var i = 0; i < brut.length; i++) buf[i] = brut.charCodeAt(i);
  return buf.buffer;
}

function analyserPourOrbe(ctx, source) {
  var analyseur = ctx.createAnalyser();
  analyseur.fftSize = 256;
  source.connect(analyseur);
  // Indispensable : sans ce branchement vers la sortie, relier la source au
  // graphe la rend muette.
  analyseur.connect(ctx.destination);

  var tampon = new Uint8Array(analyseur.frequencyBinCount);
  var fini = false;
  source.addEventListener("ended", function () {
    fini = true;
    // Retour au repos : sans cette remise a zero, l'orbe resterait fige dans
    // la derniere posture de la phrase.
    if (window.__crushSetAudioLevel) window.__crushSetAudioLevel(0);
    if (window.__crushSetAudioBands) window.__crushSetAudioBands(0, 0, 0);
  });

  var boucle = function () {
    if (fini) return;
    analyseur.getByteFrequencyData(tampon);
    // Decoupage en trois bandes, comme l'ancien projet : les basses portent le
    // souffle de la voix, les mediums son articulation, les aigus ses
    // consonnes. Un niveau global unique donnerait un gonflement uniforme.
    var n = tampon.length;
    var basses = 0, mediums = 0, aigus = 0;
    var coupeB = Math.floor(n * 0.12), coupeM = Math.floor(n * 0.45);
    for (var i = 0; i < n; i++) {
      if (i < coupeB) basses += tampon[i];
      else if (i < coupeM) mediums += tampon[i];
      else aigus += tampon[i];
    }
    basses  = Math.min(1, basses  / (coupeB * 255) * 1.6);
    mediums = Math.min(1, mediums / ((coupeM - coupeB) * 255) * 2.2);
    aigus   = Math.min(1, aigus   / ((n - coupeM) * 255) * 3.0);
    var niveau = Math.min(1, (basses + mediums + aigus) / 3 * 2.0);
    if (window.__crushSetAudioLevel) window.__crushSetAudioLevel(niveau);
    if (window.__crushSetAudioBands) window.__crushSetAudioBands(basses, mediums, aigus);
    requestAnimationFrame(boucle);
  };
  requestAnimationFrame(boucle);
}


class CrushVoiceClient {
  constructor() {
    this._ws = null;
    this._flux = null;
    this._enregistreur = null;
    this._morceaux = [];
    this._audioCtx = null;
    this._analyseur = null;
    this._boucleVad = null;
    this._connected = false;
    this._isSpeaking = false;
    this._enParole = false;
    this._debutParole = 0;
    this._dernierSon = 0;
    this._agentBubble = null;
    this._lecture = null;
    this._fileAudio = [];
    this._lectureEnCours = false;
    this._reco = null;
    this._btn =
      document.getElementById("perm-microphone") || document.getElementById("hc-mic");
    this._onActiveChange = null;

    window.crush = {
      get isSpeaking() {
        return window._voiceClient?._isSpeaking ?? false;
      },
      stopAudio: () => window._voiceClient?.stopAudio(),
      setState: (s) => window._voiceClient?._setSphereState(s),
      appendCrushMessage: (text) => window._voiceClient?._appendAgentText(text),
      appendUserMessage: (text) => {
        if (text && typeof addMsg === "function") addMsg("vous", text);
      },
    };
  }

  // ── Cycle de vie ───────────────────────────────────────────────────────────

  async _start() {
    if (this._connected) return;
    bindMicButton(this);
    showVoiceStatus("Activation du micro…");

    // Le micro d'abord : inutile d'ouvrir un WebSocket si l'utilisateur refuse
    // l'autorisation ou si la page n'est pas en contexte sécurisé.
    if (!navigator.mediaDevices?.getUserMedia) {
      showVoiceStatus("");
      throw new Error(
        "Micro indisponible : la page doit être servie en HTTPS " +
          "(utilise l'adresse Tailscale, pas l'IP locale).",
      );
    }

    const deviceId = window.CrushMic && CrushMic.getSelectedDeviceId();
    try {
      this._flux = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          ...(deviceId ? { deviceId } : {}),
        },
      });
    } catch (e) {
      showVoiceStatus("");
      throw new Error(
        e.name === "NotAllowedError"
          ? "Micro refusé — autorise-le dans le navigateur, puis réessaie."
          : `Micro inaccessible (${e.name}). Vérifie le périphérique dans Réglages → Audio & voix.`,
      );
    }

    await this._ouvrirWebSocket();

    // Chemin rapide : le navigateur transcrit lui-meme, en continu. On evite
    // l'aller-retour « televerser puis transcrire sur le Pi » (1 a 2,3 s) ET
    // notre propre detection d'activite vocale — l'API Web Speech fait deja
    // le decoupage par silence, mieux que notre seuil RMS.
    if (this._sttNavigateurDispo()) {
      this._demarrerReconnaissance();
    } else {
      this._demarrerVad();
    }

    this._connected = true;
    this._setState("listening");
    this._setSphereState("IDLE");
    showVoiceStatus((window.CRUSH_ASSISTANT_NAME || "Assistant") + " vous écoute", 2500);
    if (this._onActiveChange) this._onActiveChange(true);
  }

  _stop() {
    if (this._reco) {
      const reco = this._reco;
      this._reco = null; // avant stop(), sinon `end` relancerait l'ecoute
      try { reco.stop(); } catch (_) {}
    }
    this._arreterVad();

    if (this._enregistreur?.state === "recording") {
      // onstop enverrait la prise de parole en cours : on le neutralise avant.
      this._enregistreur.onstop = null;
      this._enregistreur.stop();
    }
    this._enregistreur = null;

    this._flux?.getTracks().forEach((t) => t.stop());
    this._flux = null;

    if (this._ws) {
      this._ws.onclose = null; // sinon la fermeture volontaire relancerait une reconnexion
      this._ws.close();
      this._ws = null;
    }

    this.stopAudio();
    this._connected = false;
    this._isSpeaking = false;
    this._agentBubble = null;

    this._setState("idle");
    this._setSphereState("IDLE");
    showVoiceStatus("");

    if (window._perms) window._perms.microphone = false;
    document.getElementById("perm-microphone")?.classList.remove("active");
    if (this._onActiveChange) this._onActiveChange(false);
  }

  // ── WebSocket ──────────────────────────────────────────────────────────────

  _ouvrirWebSocket() {
    return new Promise((resolve, reject) => {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws/voice`);
      // Le cookie de session est joint automatiquement au handshake
      // same-origin : rien à transmettre à la main.
      ws.onopen = () => {
        this._ws = ws;
        resolve();
      };
      ws.onerror = () =>
        reject(new Error("Connexion au pipeline vocal impossible — session expirée ?"));
      ws.onmessage = (evt) => this._surMessage(evt);
      ws.onclose = () => {
        if (this._connected) {
          showVoiceStatus("Connexion vocale perdue.", 4000);
          this._stop();
        }
      };
    });
  }

  _surMessage(evt) {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch {
      return;
    }

    switch (msg.type) {
      case "transcript":
        if (msg.text && typeof addMsg === "function") addMsg("vous", msg.text);
        this._setSphereState("THINKING");
        showVoiceStatus((window.CRUSH_ASSISTANT_NAME || "Assistant") + " réfléchit…");
        break;
      case "chunk":
        this._appendAgentText(msg.content);
        break;
      case "audio":
        this._jouer(msg.data, msg.mime);
        break;
      case "error":
        showVoiceStatus(msg.content, 6000);
        this._setSphereState("IDLE");
        break;
      case "done":
        this._agentBubble?.classList.remove("streaming");
        this._agentBubble = null;
        if (!this._isSpeaking) {
          this._setSphereState("IDLE");
          showVoiceStatus("");
        }
        break;
    }
  }

  // ── Détection d'activité vocale ────────────────────────────────────────────

  _demarrerVad() {
    this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = this._audioCtx.createMediaStreamSource(this._flux);
    this._analyseur = this._audioCtx.createAnalyser();
    this._analyseur.fftSize = 1024;
    source.connect(this._analyseur);

    const tampon = new Float32Array(this._analyseur.fftSize);

    const examiner = () => {
      if (!this._analyseur) return;
      this._analyseur.getFloatTimeDomainData(tampon);

      let somme = 0;
      for (const v of tampon) somme += v * v;
      const rms = Math.sqrt(somme / tampon.length);
      const maintenant = performance.now();

      // On n'écoute pas pendant que l'assistant parle : sans cela, sa propre
      // voix sortant des haut-parleurs relancerait une prise de parole.
      if (this._isSpeaking) {
        this._boucleVad = requestAnimationFrame(examiner);
        return;
      }

      if (rms > SEUIL_PAROLE) {
        this._dernierSon = maintenant;
        if (!this._enParole) this._debuterPrise(maintenant);
      } else if (this._enParole) {
        const silence = maintenant - this._dernierSon;
        const duree = maintenant - this._debutParole;
        if (silence > SILENCE_FIN_MS || duree > DUREE_MAX_MS) {
          this._terminerPrise(duree);
        }
      }

      this._boucleVad = requestAnimationFrame(examiner);
    };
    this._boucleVad = requestAnimationFrame(examiner);
  }

  _arreterVad() {
    if (this._boucleVad) cancelAnimationFrame(this._boucleVad);
    this._boucleVad = null;
    this._analyseur = null;
    this._audioCtx?.close().catch(() => {});
    this._audioCtx = null;
    this._enParole = false;
  }

  _debuterPrise(maintenant) {
    this._enParole = true;
    this._debutParole = maintenant;
    this._morceaux = [];

    const type = typeAudioSupporte();
    this._enregistreur = new MediaRecorder(
      this._flux,
      type ? { mimeType: type } : undefined,
    );
    this._enregistreur.ondataavailable = (e) => {
      if (e.data.size) this._morceaux.push(e.data);
    };
    this._enregistreur.onstop = () => this._envoyer();
    this._enregistreur.start();

    this._setSphereState("LISTENING");
    showVoiceStatus("…");
  }

  _terminerPrise(duree) {
    this._enParole = false;
    const trop_court = duree < DUREE_MIN_MS;
    if (this._enregistreur?.state === "recording") {
      if (trop_court) this._enregistreur.onstop = null; // bruit : on jette
      this._enregistreur.stop();
    }
    if (trop_court) {
      this._setSphereState("IDLE");
      showVoiceStatus("");
    }
  }

  async _envoyer() {
    const blob = new Blob(this._morceaux, {
      type: this._enregistreur?.mimeType || "audio/webm",
    });
    this._morceaux = [];
    if (blob.size < 1200 || this._ws?.readyState !== WebSocket.OPEN) return;

    const base64 = await new Promise((resolve, reject) => {
      const l = new FileReader();
      l.onerror = reject;
      l.onload = () => resolve(String(l.result).split(",", 2)[1]);
      l.readAsDataURL(blob);
    });

    this._ws.send(
      JSON.stringify({
        audio: base64,
        mime: blob.type || "audio/webm",
        session_id:
          typeof window._crushSessionId === "function" ? window._crushSessionId() : null,
        want_audio: true,
      }),
    );
  }

  // ── Reconnaissance par le navigateur ──────────────────────────────────────

  _sttNavigateurDispo() {
    return (
      (window.SpeechRecognition || window.webkitSpeechRecognition) &&
      window.CRUSH_STT_NAVIGATEUR !== false
    );
  }

  _demarrerReconnaissance() {
    const API = window.SpeechRecognition || window.webkitSpeechRecognition;
    const reco = new API();
    reco.lang = window.CRUSH_STT_LANGUE || "fr-FR";
    // `continuous` : mode mains libres, on enchaine les prises de parole sans
    // retoucher au bouton. C'est ce qui distingue le bureau du mobile.
    reco.continuous = true;
    reco.interimResults = true;
    reco.maxAlternatives = 1;

    reco.addEventListener("result", (evt) => {
      let interim = "";
      let final = "";
      for (let i = evt.resultIndex; i < evt.results.length; i++) {
        const t = evt.results[i][0].transcript;
        if (evt.results[i].isFinal) final += t;
        else interim += t;
      }
      if (interim) {
        this._setSphereState("LISTENING");
        showVoiceStatus(interim.slice(-60));
      }
      const texte = final.trim();
      if (!texte) return;
      // On ne s'ecoute pas parler : la voix de l'assistant sortant des
      // haut-parleurs serait transcrite et relancerait une question.
      if (this._isSpeaking) return;
      if (typeof addMsg === "function") addMsg("vous", texte);
      this._setSphereState("THINKING");
      showVoiceStatus((window.CRUSH_ASSISTANT_NAME || "Assistant") + " reflechit…");
      this._envoyerTexte(texte);
    });

    reco.addEventListener("error", (evt) => {
      if (evt.error === "no-speech" || evt.error === "aborted") return;
      showVoiceStatus(`Reconnaissance vocale : ${evt.error}`, 5000);
    });

    // En mode continu, le navigateur coupe malgre tout apres un long silence.
    // On relance tant que l'utilisateur n'a pas quitte le mode vocal.
    reco.addEventListener("end", () => {
      if (this._connected && this._reco === reco) {
        try {
          reco.start();
        } catch (_) {
          /* deja en cours de redemarrage */
        }
      }
    });

    this._reco = reco;
    reco.start();
  }

  _envoyerTexte(texte) {
    if (this._ws?.readyState !== WebSocket.OPEN) return;
    this.stopAudio();
    this._ws.send(
      JSON.stringify({
        text: texte,
        session_id:
          typeof window._crushSessionId === "function" ? window._crushSessionId() : null,
        want_audio: true,
      }),
    );
  }

  // ── Restitution ────────────────────────────────────────────────────────────

  _jouer(base64, mime) {
    // Le serveur emet une phrase a la fois, pendant que le LLM ecrit la suite.
    // Sans file d'attente, chaque fragment couperait le precedent et on
    // n'entendrait que la derniere phrase.
    this._mimeCourant = mime || "audio/wav";
    this._fileAudio.push(base64);
    if (!this._lectureEnCours) this._enchainer();
  }

  // La parole passe par un BufferSource du graphe Web Audio, et NON par un
  // element <audio>. Un element media reclame le focus audio du systeme, ce
  // qui met en pause les autres lecteurs — dont le lecteur Spotify que cette
  // meme page heberge. Symptome observe : « lance Iris » demarrait la musique,
  // l'assistant confirmait a voix haute, et la musique s'arretait deux
  // secondes plus tard. Un BufferSource est traite comme un son d'interface et
  // ne prend pas ce focus.
  //
  // Le client mobile avait deja ete corrige ainsi ; cette page etait restee
  // sur l'ancienne mecanique.
  _enchainer() {
    const brut = this._fileAudio.shift();
    if (!brut) {
      this._lectureEnCours = false;
      this._isSpeaking = false;
      this._setSphereState("IDLE");
      showVoiceStatus("");
      if (window.__crushSetAudioLevel) window.__crushSetAudioLevel(0);
      if (window.__crushSetAudioBands) window.__crushSetAudioBands(0, 0, 0);
      return;
    }
    this._lectureEnCours = true;
    this._isSpeaking = true;
    this._setSphereState("SPEAKING");

    const suivant = () => this._enchainer();
    // Repli commun : un element <audio> reprend le focus audio et coupera la
    // musique, mais rester muet est pire. Toute defaillance du graphe y mene.
    const parElement = () => {
      this._lecture = new Audio(`data:${this._mimeCourant || "audio/wav"};base64,${brut}`);
      this._lecture.onended = suivant;
      this._lecture.onerror = suivant;
      this._lecture.play().catch(suivant);
    };

    const ctx = contexteAudioPartage();
    if (!ctx) {
      parElement();  // navigateur sans Web Audio
      return;
    }

    // `resume()` est asynchrone : demarrer une source dans un contexte encore
    // suspendu joue dans le vide, sans erreur ni `ended` — la file se bloque
    // et l'assistant devient definitivement muet.
    const pret = ctx.state === "suspended" ? ctx.resume().catch(() => {}) : Promise.resolve();
    pret.then(() => {
      if (ctx.state !== "running") {
        parElement();
        return;
      }
      let octets;
      try {
        octets = base64EnOctets(brut);
      } catch (e) {
        parElement();
        return;
      }
      ctx.decodeAudioData(
        octets,
        (memoire) => {
          const source = ctx.createBufferSource();
          source.buffer = memoire;
          analyserPourOrbe(ctx, source);
          this._sourceEnCours = source;
          source.onended = suivant;
          source.start();
        },
        // Format que le graphe ne sait pas decoder : on parle quand meme.
        parElement,
      );
    });
  }

  stopAudio() {
    this._fileAudio = [];
    this._lectureEnCours = false;
    if (this._sourceEnCours) {
      // `onended` rappellerait _enchainer et relancerait la file qu'on vide.
      this._sourceEnCours.onended = null;
      try { this._sourceEnCours.stop(); } catch (e) { /* deja terminee */ }
      this._sourceEnCours = null;
    }
    if (this._lecture) {
      this._lecture.pause();
      this._lecture = null;
    }
    this._isSpeaking = false;
  }

  // ── Affichage ──────────────────────────────────────────────────────────────

  _appendAgentText(text) {
    if (!text?.trim() || typeof addMsg !== "function") return;
    if (!this._agentBubble) this._agentBubble = addMsg("crush", "", true);
    this._agentBubble.textContent += text;
    const chat = document.getElementById("chat");
    if (chat) chat.scrollTop = chat.scrollHeight;
  }

  _setSphereState(state) {
    if (typeof sphereState !== "undefined") sphereState = state;
    if (typeof window.__crushSetOrbState === "function") {
      window.__crushSetOrbState(String(state).toLowerCase());
    }
  }

  _setState(state) {
    bindMicButton(this);
    if (!this._btn) return;
    this._btn.dataset.state = state;
    this._btn.classList.toggle("active", state !== "idle" && state !== "error");
  }
}

function initVoiceClient() {
  if (!window._voiceClient) window._voiceClient = new CrushVoiceClient();
  bindMicButton(window._voiceClient);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initVoiceClient);
} else {
  initVoiceClient();
}

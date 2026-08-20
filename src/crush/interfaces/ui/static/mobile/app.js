"use strict";

/* Interface mobile — orbe animé, micro, WebSocket vocal.
 *
 * Script CLASSIQUE, pas un module ES. Un module qui échoue à se charger meurt
 * en silence : rien ne s'affiche, et il n'y a pas de console à ouvrir sur un
 * téléphone. Ici toute erreur remonte à `window.onerror` et s'affiche sur la
 * page — c'est ce qui rend une panne diagnosticable à distance.
 *
 * La reconnaissance vocale se fait dans le navigateur quand il la propose
 * (Chrome, Edge, Safari récent) : le texte est prêt à l'instant où l'on se
 * tait, contre 1 à 2 s pour l'aller-retour « téléverser puis transcrire ».
 * Repli sur l'envoi d'audio ailleurs — le serveur accepte les deux formes.
 */

// ── Diagnostic visible ───────────────────────────────────────────────────────

window.onerror = function (message, source, ligne) {
  var el = document.getElementById("erreur");
  if (!el) return;
  el.textContent = "JS : " + message + " (" + String(source).split("/").pop() + ":" + ligne + ")";
  el.hidden = false;
};

function $(sel) { return document.querySelector(sel); }

var micro, etatLien, indice, marque, dit;

var ws = null;
var orbe = null;
var reco = null;
var enregistreur = null;
var morceaux = [];
var sessionId = null;
var fileAudio = [];
var lectureEnCours = false;
var reconnexionMs = 500;
var reponseEnCours = "";

// ── Orbe ─────────────────────────────────────────────────────────────────────
//
// Même orbe Three.js que l'interface bureau. `home.js` attend que THREE soit
// défini par tentatives successives : on reprend ce motif, éprouvé dans ce
// projet, plutôt que de supposer l'ordre de chargement.

function initOrbe(essais) {
  essais = essais || 0;
  var canvas = $("#orbe");
  if (!canvas) return;

  // SPHERE_STYLE compte autant que THREE : orb.js le lit des sa construction.
  if (
    typeof THREE === "undefined" ||
    typeof window.createCrushOrb !== "function" ||
    !window.SPHERE_STYLE
  ) {
    if (essais < 40) {          // ~4 s de patience avant d'abandonner
      setTimeout(function () { initOrbe(essais + 1); }, 100);
      return;
    }
    replierOrbe(
      canvas,
      "manquant : " +
        [
          typeof THREE === "undefined" ? "THREE" : null,
          typeof window.createCrushOrb !== "function" ? "orb.js" : null,
          !window.SPHERE_STYLE ? "sphereStyle" : null,
        ].filter(Boolean).join(", "),
    );
    return;
  }

  try {
    orbe = window.createCrushOrb(canvas, {});
    cadrerOrbe();
    etatOrbe("idle");
    // Aucun resize differe ici : orb.js observe deja son parent
    // (ResizeObserver) et se recale seul. Les appels que j'avais ajoutes par
    // precaution invoquaient `resize()` sans argument, ce qui reduisait le
    // tampon a 0x0 sur toute version de orb.js anterieure au correctif —
    // l'orbe s'affichait une fraction de seconde puis disparaissait.
  } catch (e) {
    replierOrbe(canvas, String(e && e.message ? e.message : e));
    return;
  }

  window.addEventListener("resize", function () {
    if (orbe && orbe.resize) orbe.resize();
    cadrerOrbe();
  });
  window.addEventListener("orientationchange", function () {
    setTimeout(function () {
      if (orbe && orbe.resize) orbe.resize();
      cadrerOrbe();   // le rapport largeur/hauteur s'inverse : il faut recadrer
    }, 250);
  });
  // Batterie : une scène WebGL qui tourne en arrière-plan vide le téléphone
  // pour rien.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) etatOrbe("idle");
  });

  // Un navigateur mobile peut retirer le contexte WebGL sous pression memoire.
  // Sans gestionnaire, le canvas reste noir sans la moindre explication.
  canvas.addEventListener("webglcontextlost", function (e) {
    e.preventDefault();
    orbe = null;
    replierOrbe(canvas, "contexte WebGL perdu (mémoire)");
  });
}

function cadrerOrbe() {
  // Le champ de vision de la camera est VERTICAL. Sur un ecran de telephone
  // (rapport ~0,46), la largeur visible vaut donc moins de la moitie de la
  // hauteur visible, et la sphere deborde lateralement. On recule la camera
  // en proportion pour qu'elle tienne, avec une marge.
  if (!orbe || !orbe.setZoom || !window.SPHERE_STYLE) return;
  var S = window.SPHERE_STYLE;
  var base = S.ZCAM || (S.CAMERA && S.CAMERA.ZCAM);
  if (!base) return;
  var rapport = window.innerWidth / window.innerHeight;
  // Au-dela de 1, l'ecran est deja plus large que haut : rien a corriger.
  var facteur = rapport >= 1 ? 1 : 1 / rapport;
  orbe.setZoom(base * facteur * 0.82);
}

function replierOrbe(canvas, motif) {
  canvas.hidden = true;
  var repli = $("#repli-orbe");
  if (repli) { repli.hidden = false; repli.removeAttribute("aria-hidden"); }
  console.warn("Orbe WebGL indisponible :", motif);
  // AFFICHE la raison : un repli silencieux est indiscernable d'un orbe qui
  // marche mal, et sur telephone il n'y a pas de console a ouvrir.
  alerte("Orbe 3D indisponible — " + motif);
}

function diagnosticOrbe() {
  var canvas = $("#orbe");
  var r = canvas ? canvas.getBoundingClientRect() : { width: 0, height: 0 };
  var gl = null;
  try {
    var essai = document.createElement("canvas");
    gl = essai.getContext("webgl2") || essai.getContext("webgl");
  } catch (e) { /* contexte refuse */ }
  return [
    "THREE=" + (typeof THREE !== "undefined" ? "ok" : "ABSENT"),
    "createCrushOrb=" + (typeof window.createCrushOrb === "function" ? "ok" : "ABSENT"),
    "SPHERE_STYLE=" + (window.SPHERE_STYLE ? "ok" : "ABSENT"),
    "webgl=" + (gl ? "ok" : "REFUSE"),
    "orbe=" + (orbe ? "actif" : "repli"),
    "canvas=" + Math.round(r.width) + "x" + Math.round(r.height),
  ].join("  ");
}

function etatOrbe(nom) {
  if (orbe && orbe.setState) orbe.setState(nom);
  document.body.dataset.etat = nom;
}

// ── Affichage ────────────────────────────────────────────────────────────────

function afficherDit(texte) {
  if (!dit) return;
  dit.textContent = texte || "";
  dit.classList.toggle("visible", !!texte);
}

function alerte(msg) {
  var el = $("#erreur");
  el.textContent = msg || "";
  el.hidden = !msg;
}

function etat(nom) {
  micro.classList.toggle("ecoute", nom === "ecoute");
  micro.classList.toggle("reflechit", nom === "reflechit");
  micro.classList.toggle("parle", nom === "parle");
  // Le bouton reste actif pendant qu'il parle : c'est ce qui permet de
  // l'interrompre en reprenant la parole.
  micro.disabled = nom === "reflechit";
  indice.textContent =
    { ecoute: "Je t'écoute…", reflechit: "Un instant…", parle: "" }[nom] ||
    "Maintiens pour parler";
  etatOrbe({ ecoute: "listening", reflechit: "thinking", parle: "speaking" }[nom] || "idle");
}

// ── WebSocket ────────────────────────────────────────────────────────────────
//
// Le cookie de session est joint automatiquement au handshake same-origin.

function connecter() {
  var proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws/voice");

  ws.onopen = function () {
    etatLien.className = "";
    reconnexionMs = 500;
  };

  ws.onmessage = function (evt) {
    var m;
    try { m = JSON.parse(evt.data); } catch (e) { return; }

    if (m.type === "transcript") {
      if (m.text) afficherDit(m.text);
    } else if (m.type === "session") {
      sessionId = m.session_id;
    } else if (m.type === "chunk") {
      reponseEnCours += m.content;
    } else if (m.type === "audio") {
      // La reponse arrive : on efface la transcription, l'assistant a la parole.
      afficherDit("");
      jouer(m.data, m.mime);
    } else if (m.type === "error") {
      alerte(m.content);
      etat("pret");
    } else if (m.type === "done") {
      reponseEnCours = "";
      if (!lectureEnCours) etat("pret");
    }
  };

  ws.onclose = function () {
    etatLien.className = "hors-ligne";
    etat("pret");
    // Réessai à intervalle croissant, plafonné : un Pi qui redémarre revient
    // en quelques secondes, inutile de marteler.
    setTimeout(connecter, reconnexionMs);
    reconnexionMs = Math.min(reconnexionMs * 2, 15000);
  };

  ws.onerror = function () { etatLien.className = "erreur"; };
}

function envoyerTexte(texte) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    alerte("Connexion perdue — reconnexion en cours.");
    etat("pret");
    return;
  }
  reponseEnCours = "";
  ws.send(JSON.stringify({ text: texte, session_id: sessionId, want_audio: true }));
}

// ── Restitution audio ────────────────────────────────────────────────────────
//
// La reponse est jouee par l'API Web Audio, PAS par un element <audio>.
//
// Deux raisons. D'abord Android : un element <audio> demande le focus media au
// systeme, ce qui met en pause les autres applications — Spotify s'arretait
// quelques secondes apres avoir demarre, des que l'assistant repondait. Un
// BufferSource est traite comme un son d'interface et ne prend pas ce focus.
//
// Ensuite l'analyse : le meme graphe fournit directement l'amplitude pour
// l'orbe, sans passer par `createMediaElementSource` (utilisable une seule
// fois par element, donc fragile avec un fragment par phrase).

var ctxAudio = null;
var sourceEnCours = null;

function contexteAudio() {
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

function jouer(base64, mime) {
  fileAudio.push(base64);
  if (!lectureEnCours) enchainer();
}

function enchainer() {
  var base64 = fileAudio.shift();
  if (!base64) {
    lectureEnCours = false;
    if (orbe && orbe.setAudioLevel) orbe.setAudioLevel(0);
    if (orbe && orbe.setAudioBands) orbe.setAudioBands(0, 0, 0);
    etat("pret");
    return;
  }

  var ctx = contexteAudio();
  if (!ctx) { lectureEnCours = false; etat("pret"); return; }

  lectureEnCours = true;
  etat("parle");

  ctx.decodeAudioData(
    base64EnOctets(base64),
    function (mem) {
      var source = ctx.createBufferSource();
      source.buffer = mem;
      var analyseur = ctx.createAnalyser();
      analyseur.fftSize = 256;
      source.connect(analyseur);
      analyseur.connect(ctx.destination);
      sourceEnCours = source;

      var tampon = new Uint8Array(analyseur.frequencyBinCount);
      var fini = false;
      var boucle = function () {
        if (fini) return;
        analyseur.getByteFrequencyData(tampon);
        // Trois bandes : les basses portent le souffle, les mediums
        // l'articulation, les aigus les consonnes.
        var n = tampon.length, b = 0, m = 0, a = 0;
        var cB = Math.floor(n * 0.12), cM = Math.floor(n * 0.45);
        for (var i = 0; i < n; i++) {
          if (i < cB) b += tampon[i];
          else if (i < cM) m += tampon[i];
          else a += tampon[i];
        }
        b = Math.min(1, b / (cB * 255) * 1.6);
        m = Math.min(1, m / ((cM - cB) * 255) * 2.2);
        a = Math.min(1, a / ((n - cM) * 255) * 3.0);
        if (orbe && orbe.setAudioLevel) orbe.setAudioLevel(Math.min(1, (b + m + a) / 3 * 2));
        if (orbe && orbe.setAudioBands) orbe.setAudioBands(b, m, a);
        requestAnimationFrame(boucle);
      };
      requestAnimationFrame(boucle);

      source.onended = function () { fini = true; sourceEnCours = null; enchainer(); };
      source.start(0);
    },
    function () {
      // Decodage impossible : on passe au fragment suivant plutot que de
      // bloquer toute la reponse sur un morceau corrompu.
      enchainer();
    },
  );
}

function couperAudio() {
  fileAudio = [];
  lectureEnCours = false;
  if (sourceEnCours) {
    try { sourceEnCours.onended = null; sourceEnCours.stop(); } catch (e) { /* deja arretee */ }
    sourceEnCours = null;
  }
  if (orbe && orbe.setAudioLevel) orbe.setAudioLevel(0);
}

// ── Écoute ───────────────────────────────────────────────────────────────────

var ReconnaissanceVocale = window.SpeechRecognition || window.webkitSpeechRecognition;

function demarrerEcoute() {
  couperAudio();          // interruption : parler coupe la réponse en cours
  alerte("");
  afficherDit("");

  if (ReconnaissanceVocale && window.CRUSH_STT_NAVIGATEUR !== false) {
    demarrerReconnaissance();
  } else {
    demarrerEnregistrement();
  }
}

function demarrerReconnaissance() {
  reco = new ReconnaissanceVocale();
  reco.lang = window.CRUSH_STT_LANGUE || "fr-FR";
  reco.continuous = false;
  reco.interimResults = true;   // retour immédiat pendant qu'on parle
  reco.maxAlternatives = 1;

  var final = "";
  reco.onresult = function (evt) {
    var interim = "";
    for (var i = evt.resultIndex; i < evt.results.length; i++) {
      if (evt.results[i].isFinal) final += evt.results[i][0].transcript;
      else interim += evt.results[i][0].transcript;
    }
    // Affichage au fil de la parole : c'est ce retour immediat qui donne
    // l'impression que l'assistant ecoute vraiment.
    afficherDit((final + interim).trim());
  };
  reco.onerror = function (evt) {
    reco = null;
    etat("pret");
    // `no-speech` et `aborted` ne sont pas des pannes : l'utilisateur n'a rien
    // dit, ou a relâché tout de suite.
    if (evt.error === "no-speech" || evt.error === "aborted") return;
    alerte(
      evt.error === "not-allowed"
        ? "Micro refusé — autorise-le dans le navigateur."
        : "Reconnaissance vocale : " + evt.error,
    );
  };
  reco.onend = function () {
    reco = null;
    var texte = final.trim();
    if (!texte) { afficherDit(""); etat("pret"); return; }
    etat("reflechit");
    envoyerTexte(texte);
  };

  reco.start();
  etat("ecoute");
}

function demarrerEnregistrement() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    alerte("Micro indisponible : la page doit être servie en HTTPS.");
    return;
  }
  navigator.mediaDevices
    .getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
    .then(function (flux) {
      var types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
      var type = "";
      for (var i = 0; i < types.length; i++) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported(types[i])) {
          type = types[i];
          break;
        }
      }
      enregistreur = type
        ? new MediaRecorder(flux, { mimeType: type })
        : new MediaRecorder(flux);
      morceaux = [];
      enregistreur.ondataavailable = function (e) { if (e.data.size) morceaux.push(e.data); };
      enregistreur.onstop = function () {
        // On libère le micro sans attendre : sinon l'indicateur
        // d'enregistrement du téléphone reste allumé pendant la transcription.
        flux.getTracks().forEach(function (t) { t.stop(); });
        envoyerAudio(new Blob(morceaux, { type: enregistreur.mimeType }));
      };
      enregistreur.start();
      etat("ecoute");
    })
    .catch(function (err) {
      alerte(
        err.name === "NotAllowedError"
          ? "Micro refusé — autorise-le dans le navigateur."
          : "Micro inaccessible : " + err.name,
      );
    });
}

function arreterEcoute() {
  if (reco) { reco.stop(); return; }
  if (enregistreur && enregistreur.state === "recording") {
    enregistreur.stop();
    etat("reflechit");
  }
}

function envoyerAudio(blob) {
  if (!ws || ws.readyState !== WebSocket.OPEN) { etat("pret"); return; }
  if (blob.size < 1200) { etat("pret"); return; }   // appui trop bref : du silence
  var lecteur = new FileReader();
  lecteur.onload = function () {
    reponseEnCours = "";
    ws.send(JSON.stringify({
      audio: String(lecteur.result).split(",")[1],
      mime: blob.type || "audio/webm",
      session_id: sessionId,
      want_audio: true,
    }));
  };
  lecteur.readAsDataURL(blob);
}

// ── Démarrage ────────────────────────────────────────────────────────────────

function demarrer() {
  micro = $("#micro");
  etatLien = $("#etat-lien");
  indice = $("#indice");
  marque = $("#marque");
  dit = $("#dit");
  // Le nom vient de la config injectee dans le HTML : renommer l'assistant
  // dans .env doit suffire a changer la marque affichee.
  if (marque) {
    marque.textContent = (window.CRUSH_ASSISTANT_NAME || "Crush").toUpperCase();
  }

  // pointerdown/up couvre doigt, stylet et souris d'un seul jeu d'événements,
  // sans le double déclenchement des paires touch + mouse.
  micro.addEventListener("pointerdown", function (e) { e.preventDefault(); demarrerEcoute(); });
  micro.addEventListener("pointerup", function (e) { e.preventDefault(); arreterEcoute(); });
  micro.addEventListener("pointercancel", arreterEcoute);
  micro.addEventListener("pointerleave", arreterEcoute);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/mobile/sw.js").catch(function () {});
  }

  // Appui long sur le titre : affiche l'etat de l'orbe. Sans console sur
  // telephone, c'est le seul moyen de savoir ce qui bloque.
  var titre = marque;
  if (titre) {
    var minuterie = null;
    titre.addEventListener("pointerdown", function () {
      minuterie = setTimeout(function () { alerte(diagnosticOrbe()); }, 600);
    });
    titre.addEventListener("pointerup", function () { clearTimeout(minuterie); });
    titre.addEventListener("pointerleave", function () { clearTimeout(minuterie); });
  }

  initOrbe();
  connecter();
  etat("pret");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", demarrer);
} else {
  demarrer();
}

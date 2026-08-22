/* apercu.js — « dans quel état est mon assistant, là, maintenant ? »
 *
 * L'Écosystème est une liste de contrôle qu'on ouvre quand ça cloche. Le Graphe
 * montre la forme de l'assistant, pas son état. Le Pilotage montre ce qui
 * demande une décision. Aucune ne répond à la question qu'on se pose en
 * s'asseyant — et ces réponses existaient, éparpillées sur cinq pages.
 *
 * UN SEUL APPEL RÉSEAU : `/api/apercu` agrège tout côté serveur. Six requêtes
 * depuis un téléphone en 4G, c'est un écran qui se remplit par morceaux pendant
 * deux secondes, sur la page qu'on ouvre justement pour un coup d'œil.
 *
 * CE QUI EST DÉLIBÉRÉMENT ABSENT : aucune valeur approchée. Un champ que le
 * serveur rend `null` s'affiche « — », jamais une estimation plausible : on
 * cesserait de vérifier ailleurs.
 */
(function () {
  "use strict";

  const J = window.Crush, el = J.el;

  /* La devise vient de l'API, elle n'est pas supposee ici. Le tracker compte en
   * DOLLARS (`cost_usd`) : afficher « € » etait un montant juste dans la
   * mauvaise monnaie, ce qui est plus trompeur qu'un champ vide. */
  function argent(v, devise) {
    if (v == null) return "—";
    const symbole = devise === "EUR" ? " €" : " $";
    return Number(v).toLocaleString("fr-FR", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    }) + symbole;
  }

  function mo(octets) {
    if (octets == null) return "—";
    return (octets / 1048576).toFixed(1) + " Mo";
  }

  function dans(iso) {
    if (!iso) return "";
    const cible = new Date(iso), delta = (cible - Date.now()) / 1000;
    if (delta < 0) return "passé";
    if (delta < 3600) return "dans " + Math.round(delta / 60) + " min";
    if (delta < 86400) return "dans " + Math.round(delta / 3600) + " h";
    return "dans " + Math.round(delta / 86400) + " j";
  }

  /* ── Briques ──────────────────────────────────────────────────────────────── */

  function tuile(etiquette, valeur, lignes, accent) {
    const t = el("div", { class: "a-tuile" });
    t.appendChild(el("div", { class: "a-tuile-lbl", text: etiquette }));
    const v = el("div", { class: "a-tuile-val", text: valeur });
    if (accent) v.style.color = accent;
    t.appendChild(v);
    (lignes || []).forEach(function (l) {
      if (l == null || l === "") return;
      t.appendChild(el("div", { class: "a-tuile-sub", text: l }));
    });
    return t;
  }

  function section(titre, compte, contenu) {
    const s = el("div", { class: "a-sec" });
    const h = el("div", { class: "a-sec-hd" });
    h.appendChild(el("span", { class: "a-sec-t", text: titre }));
    if (compte) h.appendChild(el("span", { class: "a-sec-n", text: compte }));
    s.appendChild(h);
    s.appendChild(contenu);
    return s;
  }

  function pastilles(liste) {
    const bloc = el("div", { class: "a-pastilles" });
    liste.forEach(function (r) {
      const p = el("span", { class: "a-pastille" });
      p.dataset.actif = String(!!r.actif);
      p.appendChild(el("span", { class: "a-point" }));
      p.appendChild(el("span", { text: r.nom }));
      if (r.detail) p.title = r.detail;
      bloc.appendChild(p);
    });
    return bloc;
  }

  /* ── Rendu ────────────────────────────────────────────────────────────────── */

  async function render(hote) {
    hote.innerHTML = "";
    hote.appendChild(el("div", { class: "a-charge", text: "Relevé en cours…" }));

    let d;
    try {
      d = await J.api.get("/api/apercu");
    } catch (e) {
      hote.innerHTML = "";
      hote.appendChild(el("div", { class: "a-vide", text: "Relevé impossible : " + e.message }));
      return;
    }

    hote.innerHTML = "";
    const page = el("div", { class: "a-page" });

    /* En-tête */
    const tete = el("div", { class: "a-tete" });
    const gauche = el("div");
    gauche.appendChild(el("h1", { class: "a-titre", text: d.assistant }));
    gauche.appendChild(el("div", {
      class: "a-sous",
      text: (d.presence && d.presence.resume) || "état de joignabilité inconnu",
    }));
    tete.appendChild(gauche);
    tete.appendChild(el("div", {
      class: "a-heure",
      text: new Date(d.horodatage).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }),
    }));
    page.appendChild(tete);

    /* Trois tuiles */
    const tuiles = el("div", { class: "a-tuiles" });
    tuiles.appendChild(tuile(
      "Cerveau",
      d.cerveau.backend,
      [d.cerveau.modele, d.cerveau.reflexion ? "réflexion active" : "réflexion inactive",
       "voix · " + d.cerveau.voix],
    ));

    const memLignes = [
      mo(d.memoire.octets),
      d.memoire.sauvegarde_lisible === "inconnu"
        ? "aucune sauvegarde"
        : "sauvegarde " + d.memoire.sauvegarde_lisible,
      d.memoire.archives ? d.memoire.archives + " archive(s)" : "",
    ];
    tuiles.appendChild(tuile(
      "Mémoire",
      d.memoire.faits == null ? "—" : d.memoire.faits + " faits",
      memLignes,
      d.memoire.sauvegarde_inquiete ? "#d99a3c" : null,
    ));

    const coutTuile = tuile(
      "Coût",
      argent(d.cout.aujourd_hui, d.cout.devise),
      ["aujourd'hui", d.cout.mois != null ? argent(d.cout.mois, d.cout.devise) + " ce mois" : ""],
    );
    if (d.cout.serie && d.cout.serie.length > 1) {
      const sp = J.sparkline(d.cout.serie, { width: 132, height: 24, color: "#d4af6a" });
      const enveloppe = el("div", { class: "a-spark" });
      enveloppe.appendChild(sp);
      enveloppe.appendChild(el("div", { class: "a-tuile-sub", text: "7 derniers jours" }));
      coutTuile.appendChild(enveloppe);
    }
    tuiles.appendChild(coutTuile);
    page.appendChild(tuiles);

    /* À traiter */
    const init = el("div", { class: "a-liste" });
    if (d.initiatives.en_attente) {
      d.initiatives.liste.forEach(function (i) {
        const l = el("a", { class: "a-ligne", href: "/command" });
        l.appendChild(el("span", { class: "a-fleche", text: "▸" }));
        l.appendChild(el("span", { class: "a-ligne-t", text: i.titre }));
        const marque = el("span", { class: "a-marque" });
        marque.dataset.chaud = String(i.priorite === "high");
        marque.textContent = i.decision ? "décision" : i.priorite;
        l.appendChild(marque);
        init.appendChild(l);
      });
      if (d.initiatives.en_attente > d.initiatives.liste.length) {
        init.appendChild(el("div", {
          class: "a-plus",
          text: "et " + (d.initiatives.en_attente - d.initiatives.liste.length) + " autre(s)",
        }));
      }
    } else {
      init.appendChild(el("div", {
        class: "a-vide",
        text: d.initiatives.en_attente === null
          ? "file des initiatives illisible"
          : "rien qui demande ton attention",
      }));
    }
    page.appendChild(section(
      "À traiter",
      d.initiatives.en_attente
        ? d.initiatives.en_attente + (d.initiatives.haute ? " · " + d.initiatives.haute + " urgente(s)" : "")
        : "",
      init,
    ));

    /* À regarder — vient de l'Écosystème, pas d'un second comptage : deux
       chiffres qui divergent enverraient chercher une panne au mauvais endroit. */
    const eco = d.ecosysteme || {};
    const aRegarder = el("div", { class: "a-liste" });
    (eco.a_regarder || []).forEach(function (m) {
      const l = el("a", { class: "a-ligne", href: "/capabilities#ecosysteme" });
      l.appendChild(el("span", { class: "a-etat", text: m.etat === "degrade" ? "!" : "○" }));
      const corps = el("div", { class: "a-ligne-corps" });
      corps.appendChild(el("div", { class: "a-ligne-t", text: m.nom + " — " + m.detail }));
      if (m.remede) corps.appendChild(el("div", { class: "a-remede", text: "→ " + m.remede }));
      l.appendChild(corps);
      aRegarder.appendChild(l);
    });
    if (!(eco.a_regarder || []).length) {
      aRegarder.appendChild(el("div", { class: "a-vide", text: "tous les maillons tiennent" }));
    }
    page.appendChild(section(
      "À regarder",
      eco.ok != null ? eco.ok + " ok · " + (eco.degrade || 0) + " dégradé · " + (eco.absent || 0) + " absent" : "",
      aRegarder,
    ));

    /* Relié / dormant */
    const relies = (d.relie || []).filter(function (r) { return r.actif; });
    const dorment = (d.relie || []).filter(function (r) { return !r.actif; });
    const liens = el("div");
    if (relies.length) {
      liens.appendChild(el("div", { class: "a-etiq", text: "relié" }));
      liens.appendChild(pastilles(relies));
    }
    if (dorment.length) {
      liens.appendChild(el("div", { class: "a-etiq", text: "dormant" }));
      liens.appendChild(pastilles(dorment));
    }
    page.appendChild(section("Ce qui est branché", relies.length + " / " + (d.relie || []).length, liens));

    /* Boucles */
    const boucles = el("div", { class: "a-liste" });
    (d.boucles || []).forEach(function (b) {
      const l = el("div", { class: "a-ligne" });
      l.appendChild(el("span", { class: "a-ligne-t", text: b.nom }));
      l.appendChild(el("span", { class: "a-cadence", text: b.cadence }));
      l.appendChild(el("span", { class: "a-quand", text: dans(b.quand) }));
      if (b.detail) l.title = b.detail;
      boucles.appendChild(l);
    });
    const silence = d.silence || {};
    if (silence.plage) {
      boucles.appendChild(el("div", {
        class: "a-plus",
        text: "Silence " + silence.plage
          + (silence.urgent_passe ? " — les décisions urgentes passent quand même" : " — rien ne part"),
      }));
    }
    page.appendChild(section("Ce qui tourne tout seul", (d.boucles || []).length + " boucles", boucles));

    hote.appendChild(page);
  }

  window.CrushApercu = { render: render };
})();

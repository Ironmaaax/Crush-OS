/* graphe.js — la vue Cerveau : de quoi Crush est fait, et ce qui est relié.
 *
 * POURQUOI PAS UNE BIBLIOTHÈQUE DE GRAPHE
 *
 * Les habituelles (3d-force-graph, cytoscape) arrivent par CDN, or tout ce qui
 * venait de l'extérieur a été rapatrié et verrouillé par empreinte. En vendoriser
 * une de plus, c'est 200 Ko et une empreinte à maintenir pour une simulation qui
 * tient en cinquante lignes à cette échelle. three.js, lui, était déjà là.
 *
 * LA SIMULATION
 *
 * Répulsion entre toutes les paires, ressorts le long des arêtes, rappel vers le
 * centre, et un refroidissement qui fige la disposition. C'est du O(n²) par pas :
 * assumé, parce qu'on est à quelques centaines de nœuds et que le coût réel est
 * d'environ une milliseconde. Au-delà de _SEUIL_LOURD, la page le DIT au lieu de
 * ramer sans expliquer pourquoi.
 *
 * CE QUI EST DÉLIBÉRÉMENT ABSENT
 *
 * Aucun lissage esthétique de la disposition. Deux nœuds proches à l'écran sont
 * proches dans le graphe, point — sinon on ne peut plus rien lire d'une position.
 */
(function () {
  "use strict";

  const J = window.Crush, el = J.el;

  /* Une couleur par type. Elles servent AUSSI de légende dans le panneau de
   * filtres : la même valeur est utilisée pour la sphère et la pastille. */
  const COULEURS = {
    racine:      0xf2f4f8,
    document:    0xe8913a,
    fait:        0x4a90e2,
    verbe:       0xa06ee0,
    outil:       0xe0559b,
    integration: 0x2fbf71,
    canal:       0x3fc4d4,
    appareil:    0xe8c33a,
    skill:       0x5ad2c0,
  };
  const NOMS_TYPES = {
    racine: "Racine", document: "Documents", fait: "Souvenirs", verbe: "Verbes",
    outil: "Outils", integration: "Services", canal: "Canaux",
    appareil: "Machines", skill: "Skills",
  };
  /* Les arêtes déduites d'une correspondance de nom sont les seules qui relèvent
   * d'une supposition : elles sont tracées plus discrètement que les autres. */
  const ARETES = {
    contenu:  { teinte: [.42, .58, .82], force: .58 },
    predicat: { teinte: [.58, .44, .78], force: .34 },
    partie:   { teinte: [.52, .56, .64], force: .26 },
    pilote:   { teinte: [.24, .72, .50], force: .62 },
    memoire:  { teinte: [.95, .72, .34], force: .85 },
    // La seule aretededuite d'une correspondance de nom : tracee plus
    // discretement que les autres, parce qu'elle releve d'une supposition.
    nom:      { teinte: [.70, .60, .40], force: .20 },
  };
  const ARETE_DEFAUT = { teinte: [.45, .52, .64], force: .3 };

  const _SEUIL_LOURD = 800;

  let donnees = null;
  let noeuds = [], liens = [], parId = new Map(), voisins = new Map();
  let selection = null, ancre = null, chemin = new Set();
  let typesCaches = new Set();
  let recherche = "";
  let plat = false;

  let scene, camera, rendu, spheres = [], halos = [], aretes = null, geoAretes = null;
  let alpha = 1, animation = null;
  const conteneur = document.getElementById("graphe-scene");
  const calqueLabels = document.getElementById("graphe-labels");
  let labels = [];

  /* ── Chargement ─────────────────────────────────────────────────────────── */

  /* La room Cerveau a deux sous-pages : l'etat (Apercu) et la forme (Graphe).
   *
   * On DEMONTE entierement la scene en quittant le graphe, et on la reconstruit
   * en y revenant. Garder la scene vivante derriere l'apercu ferait tourner une
   * boucle de rendu 3D pour rien -- sur un telephone, c'est la batterie qui
   * paie. Le cout du retour est un fetch et une remise en place de la
   * disposition : quelques centaines de millisecondes, une fois. */
  function demonterGraphe() {
    if (animation !== null) {
      cancelAnimationFrame(animation);
      animation = null;
    }
    if (rendu) {
      // `dispose()` libere le contexte WebGL. Sans lui, un aller-retour repete
      // finit par epuiser le nombre de contextes que le navigateur accorde, et
      // la scene ne s'affiche plus du tout sans message d'erreur.
      rendu.dispose();
      if (rendu.domElement && rendu.domElement.parentNode) {
        rendu.domElement.parentNode.removeChild(rendu.domElement);
      }
    }
    rendu = null; scene = null; camera = null;
    spheres = []; halos = []; aretes = null; geoAretes = null;
    labels = [];
    calqueLabels.innerHTML = "";
    document.getElementById("graphe-ui").innerHTML = "";
    selection = null; ancre = null; chemin = new Set();
    typesCaches = new Set(); recherche = ""; survole = null;
    conteneur.hidden = true;
    calqueLabels.hidden = true;
  }

  async function allerA(page) {
    const apercu = document.getElementById("graphe-apercu");
    if (page === "apercu") {
      demonterGraphe();
      apercu.hidden = false;
      if (window.CrushApercu) await window.CrushApercu.render(apercu);
      return;
    }
    apercu.hidden = true;
    apercu.innerHTML = "";
    conteneur.hidden = false;
    calqueLabels.hidden = false;
    await construireGraphe();
  }

  async function demarrer() {
    J.mountRooms({
      mode: "cerveau",
      pages: [{ id: "apercu", label: "Aperçu" }, { id: "graphe", label: "Graphe" }],
      activePage: "apercu",
      onNav: function (id) { allerA(id); },
    });
    await allerA("apercu");
  }

  async function construireGraphe() {
    try {
      donnees = await J.api.get("/api/graphe");
    } catch (e) {
      conteneur.appendChild(el("div", {
        style: { position: "absolute", top: "50%", left: "50%", transform: "translate(-50%,-50%)",
                 color: "#8a919c", fontFamily: "Geist, sans-serif", fontSize: "14px" },
        text: "Le graphe n'a pas pu être lu : " + e.message,
      }));
      return;
    }

    preparer();
    construireScene();
    construireInterface();
    disposer();
    boucle();
  }

  function preparer() {
    noeuds = donnees.noeuds.map(function (n) {
      /* Départ sur une sphère plutôt qu'un cube : un cube laisse des amas dans
       * les coins que la simulation met longtemps à défaire. */
      const r = 40 + Math.random() * 120;
      const th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
      return Object.assign({}, n, {
        x: r * Math.sin(ph) * Math.cos(th),
        y: r * Math.sin(ph) * Math.sin(th),
        z: r * Math.cos(ph),
        vx: 0, vy: 0, vz: 0,
        visible: true,
      });
    });
    parId = new Map(noeuds.map(function (n) { return [n.id, n]; }));
    liens = donnees.liens.filter(function (l) { return parId.has(l.de) && parId.has(l.vers); });

    voisins = new Map(noeuds.map(function (n) { return [n.id, []]; }));
    liens.forEach(function (l) {
      voisins.get(l.de).push(l.vers);
      voisins.get(l.vers).push(l.de);
    });
  }

  /* ── Disposition ────────────────────────────────────────────────────────── */

  function disposer() { alpha = 1; }

  function pas() {
    const actifs = noeuds.filter(function (n) { return n.visible; });
    const n = actifs.length;
    if (!n) return;

    /* Ressort plus court et plus raide, repulsion plus forte : la premiere
     * version produisait de longs filaments et des noeuds perdus au loin, parce
     * qu'un ressort mou sur une longue distance ne ramene rien. Le rappel vers
     * le centre est aussi renforce, sinon les branches derivent hors du champ. */
    const REPULSION = 3600, RESSORT = 0.026, LONGUEUR = 34, CENTRE = 0.0032, FROTTEMENT = 0.80;
    const VITESSE_MAX = 9;

    for (let i = 0; i < n; i++) {
      const a = actifs[i];
      for (let j = i + 1; j < n; j++) {
        const b = actifs[j];
        let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
        let d2 = dx * dx + dy * dy + dz * dz;
        /* Le plancher évite la division par presque zéro quand deux nœuds
         * partent au même endroit : sans lui, ils s'expulsent à l'infini. */
        if (d2 < 1) { d2 = 1; dx = Math.random() - .5; dy = Math.random() - .5; dz = Math.random() - .5; }
        const f = REPULSION / d2;
        const d = Math.sqrt(d2);
        const ux = dx / d * f, uy = dy / d * f, uz = dz / d * f;
        a.vx += ux; a.vy += uy; a.vz += uz;
        b.vx -= ux; b.vy -= uy; b.vz -= uz;
      }
    }

    liens.forEach(function (l) {
      const a = parId.get(l.de), b = parId.get(l.vers);
      if (!a.visible || !b.visible) return;
      const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      const f = (d - LONGUEUR) * RESSORT;
      const ux = dx / d * f, uy = dy / d * f, uz = dz / d * f;
      a.vx += ux; a.vy += uy; a.vz += uz;
      b.vx -= ux; b.vy -= uy; b.vz -= uz;
    });

    actifs.forEach(function (p) {
      p.vx -= p.x * CENTRE; p.vy -= p.y * CENTRE; p.vz -= p.z * CENTRE;
      p.vx *= FROTTEMENT; p.vy *= FROTTEMENT; p.vz *= FROTTEMENT;
      const v = Math.sqrt(p.vx * p.vx + p.vy * p.vy + p.vz * p.vz);
      if (v > VITESSE_MAX) {
        const k = VITESSE_MAX / v;
        p.vx *= k; p.vy *= k; p.vz *= k;
      }
      p.x += p.vx * alpha; p.y += p.vy * alpha;
      p.z = plat ? p.z * 0.86 : p.z + p.vz * alpha;
    });

    alpha *= 0.985;
    if (alpha < 0.008) alpha = 0;
  }

  /* ── Scène ──────────────────────────────────────────────────────────────── */

  function construireScene() {
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(52, window.innerWidth / window.innerHeight, 1, 4000);
    rendu = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    rendu.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    rendu.setSize(window.innerWidth, window.innerHeight);
    conteneur.appendChild(rendu.domElement);

    scene.add(new THREE.AmbientLight(0xffffff, 0.85));
    const lumiere = new THREE.PointLight(0xffffff, 0.65);
    lumiere.position.set(200, 300, 400);
    scene.add(lumiere);

    /* Une seule géométrie pour toutes les sphères ; le rayon vient de l'échelle,
     * ce qui évite autant de géométries que de tailles. */
    const geo = new THREE.SphereGeometry(1, 20, 16);
    // Une seconde geometrie, plus grossiere, pour les halos : ils sont flous par
    // nature, 12 segments suffisent, et c'est deux fois moins de triangles a
    // dessiner sur des centaines de noeuds.
    const geoHalo = new THREE.SphereGeometry(1, 12, 9);
    noeuds.forEach(function (nd) {
      const couleur = COULEURS[nd.type] || 0x8a919c;
      const mat = new THREE.MeshLambertMaterial({
        color: couleur,
        emissive: couleur,
        // Un peu d'emissif : sans lui, la face non eclairee d'une sphere est
        // noire, et la moitie du nuage disparait selon l'angle de la camera.
        emissiveIntensity: .35,
        transparent: true,
        opacity: 1,
      });
      const m = new THREE.Mesh(geo, mat);
      m.scale.setScalar(rayon(nd));
      m.userData.id = nd.id;
      scene.add(m);
      spheres.push(m);

      const halo = new THREE.Mesh(geoHalo, new THREE.MeshBasicMaterial({
        color: couleur,
        transparent: true,
        opacity: .1,
        blending: THREE.AdditiveBlending,
        depthWrite: false,  // sinon le halo masque les noeuds derriere lui
      }));
      halo.scale.setScalar(rayon(nd) * 2.7);
      scene.add(halo);
      halos.push(halo);
    });

    geoAretes = new THREE.BufferGeometry();
    geoAretes.setAttribute("position", new THREE.BufferAttribute(new Float32Array(liens.length * 6), 3));
    geoAretes.setAttribute("color", new THREE.BufferAttribute(new Float32Array(liens.length * 6), 3));
    aretes = new THREE.LineSegments(geoAretes, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: .8,
    }));
    scene.add(aretes);

    camera.position.set(0, 0, 420);
    installerControles();
    window.addEventListener("resize", function () {
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
      rendu.setSize(window.innerWidth, window.innerHeight);
    });
  }

  function rayon(nd) {
    if (nd.type === "racine") return 9;
    /* La racine à part, la taille suit le nombre de liens — le moyeu se voit
     * sans avoir à lire le panneau. Racine cubique pour que le plus relié ne
     * devienne pas une planète, mais avec assez d'ecart pour que la difference
     * entre un fait isole et un document se voie : la premiere version rendait
     * 3,2 contre 6,5, ce qui se lisait comme des billes toutes pareilles. */
    return 2.6 + Math.cbrt(nd.degre || 1) * 2.3;
  }

  /* ── Contrôles ──────────────────────────────────────────────────────────── */

  let azimut = 0.5, polaire = 1.25, distance = 420, cible = { x: 0, y: 0, z: 0 };

  function installerControles() {
    const c = rendu.domElement;
    let presse = false, dernier = null, pointeurs = new Map();

    c.addEventListener("pointerdown", function (e) {
      pointeurs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      presse = true; dernier = { x: e.clientX, y: e.clientY, t: Date.now() };
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", function (e) {
      survol(e);
      if (!presse || !dernier) return;
      const dx = e.clientX - dernier.x, dy = e.clientY - dernier.y;
      dernier.x = e.clientX; dernier.y = e.clientY;
      azimut -= dx * 0.005;
      polaire = Math.max(0.08, Math.min(Math.PI - 0.08, polaire - dy * 0.005));
    });
    c.addEventListener("pointerup", function (e) {
      pointeurs.delete(e.pointerId);
      /* Un clic est un pointerup RAPIDE et SANS déplacement : sans ces deux
       * conditions, chaque rotation de caméra finirait par sélectionner un nœud. */
      if (dernier && Date.now() - dernier.t < 320) {
        const bouge = Math.abs(e.clientX - (pointeurs.get(e.pointerId) || dernier).x);
        if (bouge < 5) cliquer(e);
      }
      presse = false; dernier = null;
    });
    c.addEventListener("pointercancel", function () { presse = false; dernier = null; });
    c.addEventListener("wheel", function (e) {
      e.preventDefault();
      distance = Math.max(60, Math.min(2200, distance * (1 + Math.sign(e.deltaY) * 0.12)));
    }, { passive: false });
  }

  function majCamera() {
    camera.position.set(
      cible.x + distance * Math.sin(polaire) * Math.cos(azimut),
      cible.y + distance * Math.cos(polaire),
      cible.z + distance * Math.sin(polaire) * Math.sin(azimut)
    );
    camera.lookAt(cible.x, cible.y, cible.z);
  }

  /* ── Désignation ────────────────────────────────────────────────────────── */

  const rayonSouris = new THREE.Raycaster();
  const souris = new THREE.Vector2();
  let survole = null;

  function viser(e) {
    souris.x = (e.clientX / window.innerWidth) * 2 - 1;
    souris.y = -(e.clientY / window.innerHeight) * 2 + 1;
    rayonSouris.setFromCamera(souris, camera);
    const cibles = spheres.filter(function (m) { return m.visible; });
    const touches = rayonSouris.intersectObjects(cibles, false);
    return touches.length ? touches[0].object.userData.id : null;
  }

  function survol(e) {
    const id = viser(e);
    if (id === survole) return;
    survole = id;
    rendu.domElement.style.cursor = id ? "pointer" : "grab";
  }

  function cliquer(e) {
    const id = viser(e);
    if (!id) { selection = null; ancre = null; chemin.clear(); majFiche(); return; }
    if (e.shiftKey && selection && selection !== id) {
      ancre = selection;
      chemin = tracerChemin(ancre, id);
      selection = id;
    } else {
      ancre = null; chemin.clear();
      selection = id;
      /* La caméra vise le nœud choisi : sans ça, isoler un nœud à l'autre bout
       * du nuage laisse regarder un espace vide. */
      const nd = parId.get(id);
      cible = { x: nd.x, y: nd.y, z: nd.z };
    }
    majFiche();
  }

  /* Chemin le plus court en largeur d'abord. Non pondéré : toutes les arêtes se
   * valent ici, il n'y a pas de « distance » plus vraie qu'une autre. */
  function tracerChemin(de, vers) {
    const vus = new Set([de]), file = [[de]];
    while (file.length) {
      const route = file.shift();
      const dernierNoeud = route[route.length - 1];
      if (dernierNoeud === vers) return new Set(route);
      (voisins.get(dernierNoeud) || []).forEach(function (v) {
        if (vus.has(v) || !parId.get(v).visible) return;
        vus.add(v);
        file.push(route.concat([v]));
      });
    }
    return new Set();
  }

  /* ── Rendu ──────────────────────────────────────────────────────────────── */

  function ensembleEnAvant() {
    if (chemin.size) return chemin;
    if (!selection) return null;
    const s = new Set([selection]);
    (voisins.get(selection) || []).forEach(function (v) { s.add(v); });
    return s;
  }

  function dessiner() {
    const ensemble = ensembleEnAvant();
    const motif = recherche.trim().toLowerCase();

    noeuds.forEach(function (nd, i) {
      const m = spheres[i], h = halos[i];
      m.visible = nd.visible;
      if (h) h.visible = nd.visible;
      if (!nd.visible) return;
      m.position.set(nd.x, nd.y, nd.z);
      let op = 1;
      if (ensemble) op = ensemble.has(nd.id) ? 1 : 0.07;
      if (motif) op = nd.label.toLowerCase().indexOf(motif) >= 0 ? 1 : Math.min(op, 0.08);
      m.material.opacity = op;
      const grossi = nd.id === selection ? 1.5 : (nd.id === survole ? 1.2 : 1);
      m.scale.setScalar(rayon(nd) * grossi);
      if (h) {
        h.position.set(nd.x, nd.y, nd.z);
        // Le halo s'eteint plus vite que la sphere : au-dela d'une poignee de
        // noeuds eclaires, la somme des halos delaves fait un brouillard qui
        // masque ce qu'on cherchait a isoler.
        h.material.opacity = .1 * op * op;
        h.scale.setScalar(rayon(nd) * 2.7 * grossi);
      }
    });

    const pos = geoAretes.attributes.position.array;
    const col = geoAretes.attributes.color.array;
    liens.forEach(function (l, i) {
      const a = parId.get(l.de), b = parId.get(l.vers);
      const k = i * 6;
      const montre = a.visible && b.visible;
      if (!montre) {
        for (let z = 0; z < 6; z++) { pos[k + z] = 0; col[k + z] = 0; }
        return;
      }
      pos[k] = a.x; pos[k + 1] = a.y; pos[k + 2] = a.z;
      pos[k + 3] = b.x; pos[k + 4] = b.y; pos[k + 5] = b.z;

      const style = ARETES[l.origine] || ARETE_DEFAUT;
      let force = style.force;
      if (ensemble) force = (ensemble.has(l.de) && ensemble.has(l.vers)) ? 1 : .02;
      const teinte = chemin.size && chemin.has(l.de) && chemin.has(l.vers)
        ? [1, .80, .38]
        : style.teinte;
      for (let s = 0; s < 2; s++) {
        col[k + s * 3] = teinte[0] * force;
        col[k + s * 3 + 1] = teinte[1] * force;
        col[k + s * 3 + 2] = teinte[2] * force;
      }
    });
    geoAretes.attributes.position.needsUpdate = true;
    geoAretes.attributes.color.needsUpdate = true;

    majLabels(ensemble);
    rendu.render(scene, camera);
  }

  /* On n'écrit que les nœuds qui méritent d'être lus : tout étiqueter donne une
   * bouillie illisible dès la centaine de nœuds. */
  function majLabels(ensemble) {
    const aEcrire = noeuds.filter(function (nd) {
      if (!nd.visible) return false;
      if (nd.id === selection || (chemin.size && chemin.has(nd.id))) return true;
      if (nd.id === survole) return true;
      if (ensemble) return false;
      return nd.type === "racine" || nd.type === "document" || nd.degre >= 3;
    });
    // Du plus relie au moins : quand deux libelles se disputent la meme place,
    // c'est le moyeu qui gagne. Sans cet ordre, l'un des deux l'emportait selon
    // l'ordre d'arrivee des donnees.
    aEcrire.sort(function (a, b) { return (b.degre || 0) - (a.degre || 0); });

    while (labels.length < aEcrire.length) {
      const d = el("div", { class: "g-label" });
      calqueLabels.appendChild(d);
      labels.push(d);
    }
    /* Anti-chevauchement. Dans la premiere version « Tools » et « uses » se
     * superposaient exactement, et les deux devenaient illisibles : deux textes
     * empiles ne donnent pas un texte a moitie lisible, ils donnent une tache.
     *
     * Rectangles approches plutot que mesures : `getBoundingClientRect()` sur
     * chaque libelle a chaque image forcerait un recalcul de mise en page par
     * image, ce qui coute bien plus que la geometrie qu'on evite. Une largeur
     * estimee a 6,4 px par caractere suffit a decider qui cede la place. */
    const places = [];
    let ecrits = 0;
    aEcrire.forEach(function (nd) {
      if (ecrits >= labels.length) return;
      const v = new THREE.Vector3(nd.x, nd.y, nd.z).project(camera);
      if (v.z > 1) return;
      const texte = nd.label.length > 34 ? nd.label.slice(0, 33) + "…" : nd.label;
      const x = (v.x * 0.5 + 0.5) * window.innerWidth;
      const y = (-v.y * 0.5 + 0.5) * window.innerHeight - rayon(nd) - 10;
      const demiL = Math.max(18, texte.length * 3.2), demiH = 9;

      // La selection et le chemin passent toujours : ce sont eux qu'on regarde.
      const prioritaire = nd.id === selection || nd.id === survole
        || (chemin.size && chemin.has(nd.id));
      if (!prioritaire) {
        const gene = places.some(function (r) {
          return Math.abs(r.x - x) < r.demiL + demiL && Math.abs(r.y - y) < r.demiH + demiH;
        });
        if (gene) return;
      }
      places.push({ x: x, y: y, demiL: demiL, demiH: demiH });

      const d = labels[ecrits++];
      d.style.display = "";
      d.textContent = texte;
      d.dataset.fort = (nd.id === selection || nd.type === "racine") ? "true" : "false";
      d.style.transform = "translate(-50%,-50%) translate(" + x + "px," + y + "px)";
    });
    for (let i = ecrits; i < labels.length; i++) labels[i].style.display = "none";
  }

  function boucle() {
    animation = requestAnimationFrame(boucle);
    if (alpha > 0) pas();
    majCamera();
    dessiner();
  }

  /* ── Interface ──────────────────────────────────────────────────────────── */

  let ficheDiv, moyeuxDiv, filtresDiv;

  function construireInterface() {
    const ui = document.getElementById("graphe-ui");

    /* Barre d'outils */
    const outils = el("div", { class: "g-panneau", id: "g-outils" });
    outils.appendChild(bouton("Recadrer", function () {
      selection = null; ancre = null; chemin.clear();
      cible = { x: 0, y: 0, z: 0 }; distance = 420;
      majFiche();
    }));
    const btPlat = bouton("2D", function () {
      plat = !plat;
      btPlat.dataset.actif = String(plat);
      btPlat.textContent = plat ? "3D" : "2D";
      alpha = Math.max(alpha, 0.6);
    });
    outils.appendChild(btPlat);
    outils.appendChild(bouton("Redisposer", function () { alpha = 1; }));
    ui.appendChild(outils);

    /* Panneau gauche */
    const gauche = el("div", { class: "g-panneau", id: "g-gauche" });
    const entete = el("div");
    entete.appendChild(el("h1", { class: "g-titre", text: "Le cerveau" }));
    entete.appendChild(el("div", {
      class: "g-sous",
      text: donnees.total.noeuds + " nœuds · " + donnees.total.liens + " liens",
    }));
    gauche.appendChild(entete);

    if (donnees.total.noeuds > _SEUIL_LOURD) {
      gauche.appendChild(el("div", {
        class: "g-avertissement",
        text: donnees.total.noeuds + " nœuds : la mise en place va être lente. "
            + "Masque des types à droite pour alléger.",
      }));
    }

    const champ = el("input", { id: "g-recherche", type: "text", placeholder: "Chercher dans le cerveau…" });
    champ.addEventListener("input", function () { recherche = champ.value; });
    gauche.appendChild(champ);

    ficheDiv = el("div", { id: "g-fiche" });
    gauche.appendChild(ficheDiv);

    const bMoyeux = el("div");
    bMoyeux.appendChild(el("div", { class: "g-etiquette", text: "Par quoi tout passe" }));
    moyeuxDiv = el("div", { class: "g-liste" });
    bMoyeux.appendChild(moyeuxDiv);
    gauche.appendChild(bMoyeux);

    gauche.appendChild(el("div", {
      class: "g-aide",
      text: "Clic : isoler un nœud et ses liens. Maj+clic sur un second : tracer le chemin entre les deux. Glisser : tourner. Molette : approcher.",
    }));
    ui.appendChild(gauche);

    /* Panneau droit — filtres */
    const droite = el("div", { class: "g-panneau", id: "g-droite" });
    droite.appendChild(el("div", { class: "g-etiquette", text: "Types" }));
    filtresDiv = el("div", { class: "g-liste" });
    droite.appendChild(filtresDiv);
    if (donnees.isoles && donnees.isoles.length) {
      droite.appendChild(el("div", {
        class: "g-aide",
        text: donnees.isoles.length + " nœud(s) relié(s) à rien : " + donnees.isoles.slice(0, 6).join(", "),
      }));
    }
    ui.appendChild(droite);

    majMoyeux();
    majFiltres();
    majFiche();
  }

  function bouton(texte, action) {
    const b = el("button", { class: "g-btn", text: texte });
    b.addEventListener("click", action);
    return b;
  }

  function hexCss(type) {
    return "#" + (COULEURS[type] || 0x8a919c).toString(16).padStart(6, "0");
  }

  function majMoyeux() {
    moyeuxDiv.innerHTML = "";
    donnees.moyeux.forEach(function (m) {
      const b = el("button", { class: "g-ligne" });
      b.appendChild(el("span", { class: "g-pastille", style: { background: hexCss(m.type) } }));
      b.appendChild(el("span", { class: "g-ligne-nom", text: m.label }));
      b.appendChild(el("span", { class: "g-ligne-n", text: String(m.degre) }));
      b.addEventListener("click", function () { choisir(m.id); });
      moyeuxDiv.appendChild(b);
    });
  }

  function majFiltres() {
    filtresDiv.innerHTML = "";
    Object.keys(donnees.par_type).sort().forEach(function (t) {
      const b = el("button", { class: "g-ligne" });
      b.dataset.eteint = String(typesCaches.has(t));
      b.appendChild(el("span", { class: "g-pastille", style: { background: hexCss(t) } }));
      b.appendChild(el("span", { class: "g-ligne-nom", text: NOMS_TYPES[t] || t }));
      b.appendChild(el("span", { class: "g-ligne-n", text: String(donnees.par_type[t]) }));
      b.addEventListener("click", function () {
        if (typesCaches.has(t)) typesCaches.delete(t); else typesCaches.add(t);
        noeuds.forEach(function (nd) { nd.visible = !typesCaches.has(nd.type); });
        if (selection && !parId.get(selection).visible) { selection = null; chemin.clear(); majFiche(); }
        /* On réchauffe : masquer un type laisse un trou, et une disposition
         * calculée pour les nœuds absents n'a plus de sens. */
        alpha = Math.max(alpha, 0.75);
        majFiltres();
      });
      filtresDiv.appendChild(b);
    });
  }

  function choisir(id) {
    const nd = parId.get(id);
    if (!nd || !nd.visible) return;
    selection = id; ancre = null; chemin.clear();
    cible = { x: nd.x, y: nd.y, z: nd.z };
    majFiche();
  }

  function majFiche() {
    ficheDiv.innerHTML = "";
    if (!selection) {
      ficheDiv.appendChild(el("div", {
        class: "g-f-detail",
        text: "Clique une sphère pour n'éclairer qu'elle et ses liens, et lire ce qu'elle est.",
      }));
      return;
    }
    const nd = parId.get(selection);
    const nom = el("div", { class: "g-f-nom" });
    nom.appendChild(el("span", { class: "g-pastille", style: { background: hexCss(nd.type) } }));
    nom.appendChild(el("span", { text: nd.label }));
    ficheDiv.appendChild(nom);

    const liste = voisins.get(nd.id) || [];
    ficheDiv.appendChild(el("div", {
      class: "g-f-type",
      text: (NOMS_TYPES[nd.type] || nd.type) + " · " + liste.length + " lien(s)",
    }));
    if (nd.detail) ficheDiv.appendChild(el("div", { class: "g-f-detail", text: nd.detail }));

    if (chemin.size && ancre) {
      ficheDiv.appendChild(el("div", {
        class: "g-f-detail",
        style: { marginTop: "9px", color: "#d4af6a" },
        text: "Chemin depuis « " + parId.get(ancre).label + " » : " + (chemin.size - 1) + " pas.",
      }));
    }

    const bloc = el("div", { class: "g-f-voisins" });
    liste.slice(0, 14).forEach(function (vid) {
      const v = parId.get(vid);
      const b = el("button", { class: "g-voisin", text: "→ " + v.label });
      b.addEventListener("click", function () { choisir(vid); });
      bloc.appendChild(b);
    });
    if (liste.length > 14) {
      bloc.appendChild(el("div", { class: "g-ligne-n", text: "et " + (liste.length - 14) + " autre(s)" }));
    }
    ficheDiv.appendChild(bloc);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", demarrer);
  } else {
    demarrer();
  }
})();

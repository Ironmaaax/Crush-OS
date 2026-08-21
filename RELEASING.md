# Publier une version (bundle offline Windows)

Le bundle offline Windows est **buildé et publié automatiquement** par GitHub
Actions sur **push d'un tag `vX.Y.Z`**. Plus de build manuel ni d'upload à la main.

## Étapes

1. Mets à jour `CHANGELOG.md`.
2. Commit + push sur `main`.
3. Tag la version et pousse le tag :

   ```bash
   git tag v0.3.5
   git push origin v0.3.5
   ```

4. Le workflow **« Build Windows offline bundle »** se déclenche
   (`.github/workflows/build-windows-bundle.yml`) :
   - build le bundle sur un runner **Windows** (`scripts/release/build_bundle.ps1`),
   - vérifie qu'il est complet,
   - zippe le projet + `bundle/` en **`crush-offline-windows-v0.3.5.zip`**,
   - crée la **release `v0.3.5`** et y attache le zip.

   Durée : ~15-25 min (téléchargement Python + deps + modèles).

5. Vérifie la release sur GitHub : l'asset `crush-offline-windows-v0.3.5.zip`
   (~650 Mo) doit être présent, accompagné de son sidecar
   `crush-offline-windows-v0.3.5.zip.sha256`.

6. **Épingle l'empreinte**, sinon le téléchargement du bundle ne vérifie que la
   taille — et une taille identique ne prouve rien sur 650 Mo de binaires qui
   seront exécutés chez l'utilisateur :

   ```bash
   python scripts/pin_bundle.py v0.3.5
   git add src/crush/kernel/bundle_download.py
   git commit -m "release: epingler l'empreinte du bundle v0.3.5"
   ```

   Le script lit la taille exacte dans l'API des releases et l'empreinte dans le
   sidecar : aucun téléchargement des 650 Mo. `--check` compare sans écrire.

## Important

- **Le bundle est un SNAPSHOT FIGÉ du code au tag.** Pousser du code sur `main`
  **après** le tag ne met **pas** à jour le bundle des utilisateurs — il faut un
  **nouveau tag** pour reconstruire et republier.
- Le **build manuel reste possible** et inchangé :
  `scripts/release/build_bundle.ps1` (Windows) ou `build_bundle.sh` (Linux/macOS)
  → produit `bundle/`. Le workflow ne remplace pas le script, il l'automatise et
  ajoute le zip + la release.

## Assets front : quand regénérer le verrou

`scripts/vendor_assets.py` épingle les runtimes MediaPipe et les modèles à une
version exacte, vérifiée par SHA-256. Si tu changes une des constantes de version
en tête du script (`TASKS_VISION_NEW`, `TASKS_VISION_OLD`, `FACE_MESH`) :

```bash
python scripts/vendor_assets.py --update-lock   # re-télécharge et regénère les empreintes
git add scripts/vendor_assets.lock.json
```

Le verrou **doit** être committé : c'est lui qui rend les assets vérifiables. Sans
lui, le préflight ne peut plus distinguer un fichier légitime d'un fichier
substitué, et le script refuse de tourner. Ne regénère jamais le verrou sans
regarder ce qui a changé — c'est le seul moment où tu accordes ta confiance à du
code tiers qui sera exécuté dans le navigateur.

## Tester le workflow sans polluer les vraies releases

Pousse un tag jetable, puis supprime la release + le tag après vérification :

```bash
git tag v0.0.0-test && git push origin v0.0.0-test
# ... vérifier que le workflow build et attache le zip ...
gh release delete v0.0.0-test --yes
git push origin :refs/tags/v0.0.0-test   # supprime le tag distant
git tag -d v0.0.0-test
```

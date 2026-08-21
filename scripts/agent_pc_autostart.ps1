# Copyright (C) 2026 Maxime Song
#
# Fait démarrer l'agent PC à l'ouverture de session, et le rend diagnosticable.
#
# LE PROBLÈME
#
# L'agent (scripts/agent_pc.py) se connecte AU serveur. Tant qu'il ne tourne pas,
# l'assistant répond « aucun ordinateur n'est connecté » à toute demande visant
# cette machine — et il ne tourne pas après un redémarrage, puisque rien ne le
# lance. On le découvre au pire moment : en demandant quelque chose.
#
# POURQUOI PAS pythonw.exe
#
# C'est le réflexe pour éviter la fenêtre de console, mais sous pythonw
# `sys.stdout` ET `sys.stderr` valent None. L'agent signale ses ennuis par
# `print(..., file=sys.stderr)` : ces messages partent alors dans le vide, sans
# lever d'erreur. Un jeton refusé ou un serveur injoignable deviendrait donc
# parfaitement silencieux, sur un composant dont la seule panne visible est
# justement son absence.
#
# On lance donc python.exe dans une fenêtre MASQUÉE par un script VBS, avec les
# sorties redirigées vers un fichier. Invisible de la même façon, mais il reste
# une trace à lire quand ça ne marche pas.
#
#   powershell -ExecutionPolicy Bypass -File scripts\agent_pc_autostart.ps1
#   ... -Retirer   pour désinstaller
#   ... -Etat      pour voir ce qui est en place, sans rien changer

param(
    [switch]$Retirer,
    [switch]$Etat,
    # Laisse l'agent dire sur quoi tu travailles (titres des fenetres, jamais une
    # image). Volontairement un choix a faire, pas un defaut : le drapeau existe
    # cote agent pour que cette capacite ne s'active pas par inadvertance, et le
    # reproduire ici garderait le meme sens.
    [switch]$Ecran
)

$ErrorActionPreference = 'Stop'

$Projet  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python  = Join-Path $Projet '.venv\Scripts\python.exe'
$Agent   = Join-Path $Projet 'scripts\agent_pc.py'
$Demarr  = [Environment]::GetFolderPath('Startup')
$Vbs     = Join-Path $Demarr 'crush-agent-pc.vbs'
$DossLog = Join-Path $env:LOCALAPPDATA 'Crush'
$Log     = Join-Path $DossLog 'agent_pc.log'

function Vert($m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Alerte($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }
function Mort($m) { Write-Host "  [x] $m" -ForegroundColor Red; exit 1 }

# ── État ──────────────────────────────────────────────────────────────────────

function Processus {
    # La ligne de commande, pas le nom du processus : « python.exe » tourne pour
    # dix raisons sur cette machine, et on ne veut que cet agent. Le cmd.exe de
    # redirection compte aussi : sans lui, l'arreter laisserait un orphelin.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'agent_pc\.py' }
}

function Instances {
    # Un venv cree par uv installe un python.exe TRAMPOLINE, qui re-execute le
    # vrai interpreteur (celui de ~\AppData\Roaming\uv\python\...) comme
    # processus FILS. Un seul agent se presente donc comme deux python.exe, plus
    # le cmd.exe de redirection : compter les processus annoncait « 3 instances »
    # la ou il n'y en avait qu'une, et faisait crier la garde anti-doublon sur un
    # etat parfaitement sain.
    #
    # On ne retient donc que les RACINES : les processus dont le parent n'est pas
    # lui-meme un processus de l'agent. Vrai quelle que soit la profondeur de la
    # chaine, donc robuste si uv change sa mecanique de trampoline.
    $tous = @(Processus)
    $ids = @($tous | ForEach-Object { $_.ProcessId })
    @($tous | Where-Object { $ids -notcontains $_.ParentProcessId })
}

# -Encoding UTF8 partout sur le journal : l'agent recale sa sortie en UTF-8
# (sa console est en cp1252 en France), et Get-Content de PS 5.1 lit en ANSI
# par defaut. Sans ca le diagnostic s'affiche en charabia -- et un outil de
# diagnostic illisible ne sert a rien.
if ($Etat) {
    Write-Host "  lanceur au demarrage : $(if (Test-Path $Vbs) { $Vbs } else { 'ABSENT' })"
    Write-Host "  instances en cours   : $((Instances | Measure-Object).Count)"
    Write-Host "  journal              : $(if (Test-Path $Log) { $Log } else { 'aucun' })"
    if (Test-Path $Log) {
        Write-Host "  dernieres lignes :"
        Get-Content $Log -Encoding UTF8 -Tail 5 | ForEach-Object { Write-Host "    $_" }
    }
    exit 0
}

# ── Désinstallation ───────────────────────────────────────────────────────────

if ($Retirer) {
    if (Test-Path $Vbs) { Remove-Item $Vbs -Force; Vert "lanceur retire du dossier Demarrage" }
    else { Alerte "aucun lanceur a retirer" }
    $enCours = Processus
    if ($enCours) {
        $enCours | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Vert "agent arrete"
    }
    Write-Host ""
    Write-Host "  L'assistant repondra de nouveau « aucun ordinateur connecte »."
    exit 0
}

# ── Vérifications ─────────────────────────────────────────────────────────────

if (-not (Test-Path $Python)) { Mort "python du venv introuvable : $Python" }
if (-not (Test-Path $Agent))  { Mort "agent introuvable : $Agent" }

# `websockets` est la SEULE dependance dure de l'agent. Verifier maintenant evite
# un lanceur qui echoue en silence a chaque ouverture de session.
& $Python -c "import websockets" 2>$null
if ($LASTEXITCODE -ne 0) { Mort "le module « websockets » manque dans le venv : .venv\Scripts\pip install websockets" }
Vert "python du venv et websockets presents"

# La configuration (adresse du serveur + jeton) est propre a l'utilisateur.
$Conf = Join-Path $env:USERPROFILE '.assistant_agent.json'
if (-not (Test-Path $Conf)) {
    Mort "agent non configure — lancer d'abord : $Python $Agent --configurer"
}
Vert "configuration presente (~\.assistant_agent.json)"

if (-not (Test-Path $DossLog)) { New-Item -ItemType Directory -Path $DossLog -Force | Out-Null }

# ── Le lanceur ────────────────────────────────────────────────────────────────

# Deux choses que le VBS fait et qu'un simple raccourci ne peut pas :
#
#  1. GARDE ANTI-DOUBLON. Le registre du serveur indexe les agents par nom : une
#     seconde instance du meme nom remplace la premiere, dont la connexion reste
#     ouverte sans plus servir a rien. Ca arrive pour de vrai avec le changement
#     rapide d'utilisateur ou une session verrouillee puis reouverte.
#  2. ROTATION SUR UNE GENERATION. Le journal est reparti a zero a chaque
#     ouverture de session, mais le precedent est conserve : on redemarre
#     souvent PARCE QUE quelque chose n'allait pas, et ecraser la trace serait
#     effacer la seule preuve au moment ou l'on en a besoin.
$modele = @'
' Lanceur de l'agent PC de Crush - genere par scripts\agent_pc_autostart.ps1.
' Ne pas editer ici : relancer le script, sinon la prochaine installation
' ecrasera les retouches.
Option Explicit
Dim shell, fso, requete, procs, dejaLa, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 1. Un agent tourne-t-il deja ?
dejaLa = False
On Error Resume Next
Set requete = GetObject("winmgmts:\\.\root\cimv2").ExecQuery( _
    "SELECT CommandLine FROM Win32_Process WHERE Name = 'python.exe'")
If Err.Number = 0 Then
    Dim p
    For Each p In requete
        If Not IsNull(p.CommandLine) Then
            If InStr(p.CommandLine, "agent_pc.py") > 0 Then dejaLa = True
        End If
    Next
End If
On Error GoTo 0
If dejaLa Then WScript.Quit 0

' 2. Rotation du journal, une generation conservee.
On Error Resume Next
If fso.FileExists("__LOG__") Then
    If fso.FileExists("__LOG__.1") Then fso.DeleteFile "__LOG__.1", True
    fso.MoveFile "__LOG__", "__LOG__.1"
End If
On Error GoTo 0

' 3. Lancement, fenetre masquee (0), sans attendre la fin (False).
' -u : sans lui, Python bloc-bufferise vers un fichier et le journal reste
' vide jusqu'a 8 Ko accumules -- donc vide precisement quand on vient y
' chercher pourquoi l'agent ne s'est pas connecte. Meme raison que le
' PYTHONUNBUFFERED=1 de l'unite systemd de la Pi.
cmd = "cmd /c """"__PYTHON__"" -u ""__AGENT__""__DRAPEAUX__ > ""__LOG__"" 2>&1"""
shell.Run cmd, 0, False
'@

$drapeaux = ''
if ($Ecran) { $drapeaux = ' --autoriser-ecran' }
$vbsContenu = $modele.Replace('__PYTHON__', $Python).Replace('__AGENT__', $Agent).Replace('__LOG__', $Log).Replace('__DRAPEAUX__', $drapeaux)
# ASCII : un VBS enregistre en UTF-8 avec BOM fait echouer wscript sur un
# « caractere invalide » a la premiere ligne. Les commentaires sont donc sans
# accents, et l'encodage force.
Set-Content -Path $Vbs -Value $vbsContenu -Encoding ascii
Vert "lanceur ecrit : $Vbs"

# ── Épreuve ───────────────────────────────────────────────────────────────────

# On ne se contente pas d'avoir ecrit un fichier : on le fait tourner pour de
# vrai. Un lanceur qui ne demarre pas serait annonce comme installe et ne se
# verrait qu'au prochain « aucun ordinateur connecte ».
Processus | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
& wscript.exe $Vbs
Start-Sleep -Seconds 8

$n = (Instances | Measure-Object).Count
if ($n -lt 1) {
    Alerte "l'agent n'est pas monte — journal :"
    if (Test-Path $Log) { Get-Content $Log -Encoding UTF8 -Tail 12 | ForEach-Object { Write-Host "    $_" } }
    Mort "installation incomplete"
}
if ($n -gt 1) { Alerte "$n instances detectees — la garde anti-doublon n'a pas joue" }
Vert "agent lance ($n instance)"

if (Test-Path $Log) {
    $t = Get-Content $Log -Encoding UTF8 -Raw
    if ($t -match 'connect') { Vert "connexion confirmee par le journal" }
    elseif ($t -match 'refus|Error|error') { Alerte "le journal signale un probleme :"; Get-Content $Log -Encoding UTF8 -Tail 8 | ForEach-Object { Write-Host "    $_" } }
}

Write-Host ""
Write-Host "  L'agent demarrera desormais a chaque ouverture de session."
if ($Ecran) { Write-Host "  Lecture de l'ecran ACTIVEE (titres des fenetres)." }
else { Write-Host "  Lecture de l'ecran desactivee. Pour l'activer : relancer avec -Ecran." }
Write-Host "  Journal : $Log  (le precedent : $Log.1)"
Write-Host "  Pour retirer : powershell -ExecutionPolicy Bypass -File scripts\agent_pc_autostart.ps1 -Retirer"
Write-Host ""

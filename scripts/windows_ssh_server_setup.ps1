# Copyright (C) 2026 Maxime Song
#
# Installe OpenSSH Server sur ce PC, restreint à une seule chose : recevoir des
# fichiers de sauvegarde depuis le Raspberry Pi. À exécuter UNE FOIS, dans un
# PowerShell OUVERT EN ADMINISTRATEUR.
#
# CE QUE CE SCRIPT AUTORISE, ET RIEN DE PLUS
#
# La clé ajoutée ci-dessous porte `restrict,command="internal-sftp"` : elle ne
# peut RIEN exécuter d'autre qu'un transfert de fichiers SFTP. Pas de shell,
# pas de commande arbitraire — même si cette clé venait à être copiée hors du
# Pi, elle ne donnerait jamais accès à un terminal sur cette machine.
#
# Ce compte est administrateur : Windows exige donc que la clé autorisée vive
# dans C:\ProgramData\ssh\administrators_authorized_keys (et ignore le
# ~\.ssh\authorized_keys habituel pour ce type de compte), avec des permissions
# strictes — seuls SYSTEM et les administrateurs peuvent le lire ou l'écrire.
# sshd refuse de démarrer avec des permissions trop larges sur ce fichier.

$ErrorActionPreference = "Stop"

Write-Host "Installation d'OpenSSH Server…"
$capacite = Get-WindowsCapability -Online -Name "OpenSSH.Server*"
if ($capacite.State -ne "Installed") {
    Add-WindowsCapability -Online -Name $capacite.Name | Out-Null
}
Write-Host "  OK"

Write-Host "Démarrage et activation du service sshd…"
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Write-Host "  OK"

Write-Host "Règle pare-feu (port 22)…"
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
Write-Host "  OK"

Write-Host "Autorisation de la clé de sauvegarde (SFTP uniquement)…"
$cheminCles = "C:\ProgramData\ssh\administrators_authorized_keys"
$ligne = 'restrict,command="internal-sftp" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPWgWhBlMDy0vSxDijXQdsnAavwAOiCJCgxOE3z4EkcV crush-backup@jarvis -> max-pc'

if ((Test-Path $cheminCles) -and (Select-String -Path $cheminCles -SimpleMatch "crush-backup@jarvis" -Quiet)) {
    Write-Host "  Déjà présente, rien à faire."
} else {
    Add-Content -Path $cheminCles -Value $ligne -Encoding UTF8
    # ACL stricte : sans elle, sshd refuse le fichier ("bad permissions").
    icacls $cheminCles /inheritance:r | Out-Null
    icacls $cheminCles /grant "SYSTEM:F" "Administrateurs:F" | Out-Null
    Write-Host "  OK"
}

Write-Host "Redémarrage de sshd pour appliquer…"
Restart-Service sshd
Write-Host "  OK"

Write-Host ""
Write-Host "Terminé. Le Pi peut maintenant pousser ses sauvegardes ici, et rien d'autre." -ForegroundColor Green

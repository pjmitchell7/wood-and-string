# Prepare a local CA for review. Does not run a server or install any trust.
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $appRoot '.venv\Scripts\python.exe'
& $python (Join-Path $PSScriptRoot 'prepare_caddy.py')
if ($LASTEXITCODE -ne 0) { throw 'Configuration preparation failed' }
$dataPath = [System.IO.Path]::GetFullPath((Join-Path $appRoot 'data'))
$pkiPath = [System.IO.Path]::GetFullPath((Join-Path $dataPath 'private-pki'))
if ([System.IO.Path]::GetDirectoryName($pkiPath) -ne $dataPath) { throw 'Unexpected private directory' }
$reportPath = Join-Path $dataPath 'network\root-certificate.json'
$authorityPath = Join-Path $pkiPath 'pki\authorities\local'
$names = @('root.crt','root.key','intermediate.crt','intermediate.key')
$found = @($names | Where-Object { Test-Path -LiteralPath (Join-Path $authorityPath $_) })
if (($found.Count -ne 0 -and $found.Count -ne 4) -or ((Test-Path -LiteralPath $reportPath) -and $found.Count -ne 4)) {
    throw 'Private certificate state is incomplete. Restore it before validation; do not replace the trusted root.'
}

New-Item -ItemType Directory -Path $pkiPath -Force | Out-Null
# Read and persist only the DACL: Set-Acl with a descriptor loaded by Get-Acl
# can attempt unrelated security sections and require SeSecurityPrivilege.
$pkiDirectory = [System.IO.DirectoryInfo]::new($pkiPath)
if ($pkiDirectory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Private directory must not be a link' }
$ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$allowedSids = @($ownerSid.Value,'S-1-5-18','S-1-5-32-544')
function Test-PrivateDirectoryAccess($acl) {
    $rules = @($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]))
    if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne $allowedSids.Count) { return $false }
    foreach ($sidText in $allowedSids) {
        $matches = @($rules | Where-Object { $_.IdentityReference.Value -eq $sidText })
        if ($matches.Count -ne 1) { return $false }
        $entry = $matches[0]
        if ($entry.IsInherited -or $entry.AccessControlType -ne 'Allow' -or $entry.FileSystemRights -ne 'FullControl' -or $entry.InheritanceFlags -ne 'ContainerInherit, ObjectInherit' -or $entry.PropagationFlags -ne 'None') { return $false }
    }
    return $true
}
$pkiAcl = [System.Security.AccessControl.DirectorySecurity]::new($pkiPath,[System.Security.AccessControl.AccessControlSections]::Access)
if (-not (Test-PrivateDirectoryAccess $pkiAcl)) {
    # A fresh descriptor marks only Access as modified; never supply owner,
    # group or audit sections and never enable privileges to apply it.
    $restrictedAcl = [System.Security.AccessControl.DirectorySecurity]::new()
    $restrictedAcl.SetAccessRuleProtection($true,$false)
    foreach ($sidText in $allowedSids) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
        $restrictedAcl.AddAccessRule($rule)
    }
    try {
        if ($pkiDirectory.PSObject.Methods['SetAccessControl']) {
            $pkiDirectory.SetAccessControl($restrictedAcl) # Windows PowerShell / .NET Framework
        } else {
            [System.IO.FileSystemAclExtensions]::SetAccessControl($pkiDirectory,$restrictedAcl)
        }
    } catch {
        throw "Cannot restrict the private directory DACL with current access; validation stopped without requesting privileges. $($_.Exception.Message)"
    }
    $verifiedAcl = [System.Security.AccessControl.DirectorySecurity]::new($pkiPath,[System.Security.AccessControl.AccessControlSections]::Access)
    if (-not (Test-PrivateDirectoryAccess $verifiedAcl)) { throw 'Private directory DACL verification failed; validation stopped' }
}
function Read-TrustRoots {
    @('Cert:\CurrentUser\Root','Cert:\LocalMachine\Root') | ForEach-Object {
        $store = $_
        Get-ChildItem -LiteralPath $store | ForEach-Object { $store + ':' + $_.Thumbprint }
    } | Sort-Object
}
$before = @(Read-TrustRoots)
$caddy = Join-Path $PSScriptRoot 'vendor\caddy-2.11.7\caddy.exe'
$config = Join-Path $dataPath 'network\caddy-local.json'
& $caddy validate --config $config 2>&1 | Tee-Object -FilePath (Join-Path $dataPath 'network\caddy-validation.log')
$validationExit = $LASTEXITCODE
$after = @(Read-TrustRoots)
if (@(Compare-Object -ReferenceObject $before -DifferenceObject $after).Count -ne 0) {
    throw 'Windows root stores changed during validation. Inspect before proceeding.'
}
if ($validationExit -ne 0) { throw 'Caddy validation failed; private files have been preserved for inspection' }

$pem = Get-Content -LiteralPath (Join-Path $authorityPath 'root.crt') -Raw
$der = [Convert]::FromBase64String(($pem -replace '-----BEGIN CERTIFICATE-----|-----END CERTIFICATE-----|\s',''))
$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($der)
$fingerprint = $certificate.GetCertHashString([System.Security.Cryptography.HashAlgorithmName]::SHA256)
if (Test-Path -LiteralPath $reportPath) {
    $prior = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
    if ($prior.sha256 -ne $fingerprint) { throw 'Existing certificate fingerprint changed. Stop and restore the protected identity.' }
}
[IO.File]::WriteAllBytes((Join-Path $dataPath 'network\wood-string-public-root.cer'),$der)
$keyRules = @(Get-Acl -LiteralPath (Join-Path $authorityPath 'root.key') | ForEach-Object { $_.Access } | ForEach-Object {
    [pscustomobject]@{ identity=$_.IdentityReference.Value; rights=$_.FileSystemRights.ToString(); inherited=$_.IsInherited }
})
$report = [ordered]@{
    subject=$certificate.Subject
    sha256=$fingerprint
    valid_from_utc=$certificate.NotBefore.ToUniversalTime().ToString('o')
    valid_until_utc=$certificate.NotAfter.ToUniversalTime().ToString('o')
    public_certificate='wood-string-public-root.cer'
    windows_root_stores_unchanged=$true
    private_key_access=$keyRules
    status='Validated only; no server started and no device trust installed'
}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath
$report | ConvertTo-Json -Depth 5

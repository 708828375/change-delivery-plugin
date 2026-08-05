[CmdletBinding()]
param([string] $SourceRoot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

& (Join-Path $PSScriptRoot 'install-shared-plugin.ps1') @PSBoundParameters
exit $LASTEXITCODE

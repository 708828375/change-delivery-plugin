$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = '1'
python -m change_delivery tick --plugin-root $pluginRoot
exit $LASTEXITCODE

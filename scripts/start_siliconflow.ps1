param(
    [ValidateSet('dev','test')][string]$Environment = 'dev',
    [ValidateSet('conservative','rules')][string]$RoutingPolicy = 'conservative'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = 'E:\Work\Envs\Conda\fit\python.exe'
$settings = @{
    FITLIFE_MODEL_PROFILES_FILE = Join-Path $projectRoot 'config\siliconflow.models.json'
    FITLIFE_ROUTING_POLICY = $RoutingPolicy
    FITLIFE_ROUTING_RULES_FILE = Join-Path $projectRoot 'config\routing.example.json'
    FITLIFE_ROUTER_MODEL = 'control'
    FITLIFE_PLANNER_MODEL = 'control'
    FITLIFE_SYNTHESIS_MODEL = 'control'
    FITLIFE_FALLBACK_PROFILE = 'local'
    FITLIFE_MODEL_RETRIES = '1'
    FITLIFE_MAX_OUTPUT_TOKENS = '1024'
    FITLIFE_MASTER_RUN_TIMEOUT = '180'
    PYTHONIOENCODING = 'utf-8'
}
$saved = @{}
foreach ($name in $settings.Keys) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$saved['SILI_API_KEY'] = [Environment]::GetEnvironmentVariable('SILI_API_KEY', 'Process')
try {
    if ([string]::IsNullOrWhiteSpace($env:SILI_API_KEY)) {
        $env:SILI_API_KEY = [Environment]::GetEnvironmentVariable('SILI_API_KEY', 'User')
    }
    if ([string]::IsNullOrWhiteSpace($env:SILI_API_KEY)) {
        throw 'SILI_API_KEY is missing from process/user configuration.'
    }
    foreach ($name in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
    }
    Push-Location -LiteralPath $projectRoot
    try {
        & $python -m fitlife.server --environment $Environment --redis-credentials-file E:\Work\Config\Redis\.env
        if ($LASTEXITCODE -ne 0) { throw 'FitLife server exited with an error.' }
    } finally { Pop-Location }
} finally {
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
    }
}

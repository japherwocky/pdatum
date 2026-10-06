# pdatum installer for Windows PowerShell.
#
#   irm https://raw.githubusercontent.com/japherwocky/pdatum/main/install/install.ps1 | iex
#
# Installs the pdatum CLI as an isolated tool -- with uv if you have it, else
# pipx, else it installs uv first (uv brings its own Python, so none is needed
# beforehand) -- and puts it on your PATH for new windows.
#
# Set PDATUM_NO_MODIFY_PATH=1 to leave your PATH alone.
#
# Runs under `iex`, so it is in the caller's own session: it never calls
# `exit` (that would close their window) and keeps its work inside a function.
# Written for Windows PowerShell 5.1: no `&&`, `??` or ternaries.

function Install-Pdatum {
    $ErrorActionPreference = 'Stop'

    $modifyPath = -not $env:PDATUM_NO_MODIFY_PATH

    function Test-Command($name) {
        return [bool](Get-Command $name -ErrorAction SilentlyContinue)
    }

    # Where the tools put binaries: ~\.local\bin unless UV_TOOL_BIN_DIR or
    # PIPX_BIN_DIR moves it. Asked of each tool rather than assumed.
    function Get-BinDirs {
        $dirs = @()
        if (Test-Command 'uv') { $dirs += (uv tool dir --bin) }
        if (Test-Command 'pipx') { $dirs += (pipx environment --value PIPX_BIN_DIR) }
        $dirs += (Join-Path $env:USERPROFILE '.local\bin')
        return $dirs | Where-Object { $_ }
    }

    # Installers add their directory to the user PATH in the registry, which
    # this session has already read, so add it here too or `uv` and `pdatum`
    # will not resolve.
    function Update-SessionPath {
        foreach ($bin in (Get-BinDirs)) {
            if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$bin;$env:Path" }
        }
    }

    # The pdatum this script installed, not whichever one PATH finds first
    # (an activated virtualenv's, say).
    function Find-Pdatum {
        foreach ($bin in (Get-BinDirs)) {
            $exe = Join-Path $bin 'pdatum.exe'
            if (Test-Path $exe) { return $exe }
        }
        return $null
    }

    if (Test-Command 'uv') {
        $via = 'uv'
    } elseif (Test-Command 'pipx') {
        $via = 'pipx'
    } else {
        Write-Host 'Installing uv (a Python tool installer, from astral.sh)...'
        if (-not $modifyPath) { $env:UV_NO_MODIFY_PATH = '1' }
        powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' | Out-Null
        Update-SessionPath
        if (-not (Test-Command 'uv')) {
            Write-Error 'uv installed but is not on PATH; open a new terminal and rerun.'
            return
        }
        $via = 'uv'
    }

    Write-Host "Installing pdatum with $via..."
    if ($via -eq 'uv') {
        uv tool install --quiet --upgrade pdatum
    } else {
        pipx install --quiet --force pdatum
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Installing pdatum failed; see the output above.'
        return
    }

    $exe = Find-Pdatum
    if (-not $exe) {
        Write-Error "pdatum installed but cannot be found; open a new terminal and run 'pdatum --version'."
        return
    }
    $binDir = Split-Path $exe

    # Indexed, not `| Select-Object -First 1`: stopping the pipe early leaves
    # $LASTEXITCODE at -1 in the caller's window, as if the install failed.
    $version = @(& $exe --version)[0]

    Write-Host ''
    Write-Host "Installed $version"

    # New windows read PATH from the registry: is the directory there?
    $saved = @()
    foreach ($scope in 'User', 'Machine') {
        $value = [Environment]::GetEnvironmentVariable('Path', $scope)
        if ($value) { $saved += ($value -split ';') }
    }
    if ($saved -notcontains $binDir) {
        Write-Host ''
        $added = $false
        if ($modifyPath) {
            # Through cmd to silence it: in PowerShell 5.1, `2>&1` on a native
            # command turns its stderr into errors, which 'Stop' then throws.
            if ($via -eq 'uv') { cmd /c 'uv tool update-shell >nul 2>&1' } else { cmd /c 'pipx ensurepath >nul 2>&1' }
            $added = ($LASTEXITCODE -eq 0)
        }
        if ($added) {
            Write-Host "Added $binDir to your PATH for new windows."
        } else {
            Write-Host "$binDir is not on your PATH; add it to use 'pdatum' in new windows."
        }
    }
    # Ready in this window either way: `iex` runs in the caller's session.
    if (($env:Path -split ';') -notcontains $binDir) { $env:Path = "$binDir;$env:Path" }

    Write-Host ''
    Write-Host 'Next, save your API key (it is checked with the server first):'
    Write-Host '  pdatum key save pdatum_...'
    Write-Host ''
    Write-Host "Setting this up for an AI agent? 'pdatum skill' prints instructions for it to save."
}

Install-Pdatum

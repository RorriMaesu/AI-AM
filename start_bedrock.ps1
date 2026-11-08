#Requires -Version 5.1
# Preview is read-only. -StartLocal runs a bounded, guarded proxy session.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [string]$JavaExe = '',
    [string]$Address = '',
    [switch]$Plan,
    [switch]$StartLocal,
    [ValidateRange(30,300)][int]$DurationSeconds = 300,
    [ValidateRange(10,120)][int]$StartupTimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($Plan -and $StartLocal) { throw 'Choose -Plan or -StartLocal, not both.' }

function Checked-Path([string]$Value, [string]$Label) {
    if ($Value -notmatch '^[A-Za-z]:\\') { throw "$Label must be an absolute drive-letter path." }
    $full = [IO.Path]::GetFullPath($Value)
    if ($full.Substring(0,2) -ieq 'C:') { throw "$Label must be outside C:." }
    if ($full.Substring(3) -match '[<>:"|?*\x00-\x1F]') { throw "$Label contains unsupported path characters." }
    if (-not (Test-Path -LiteralPath ([IO.Path]::GetPathRoot($full)))) { throw "$Label drive is unavailable." }
    $part = $full
    while ($part) {
        if (Test-Path -LiteralPath $part) {
            if ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label crosses a reparse point: $part" }
        }
        $parent = [IO.Path]::GetDirectoryName($part.TrimEnd('\'))
        if ($parent -eq $part) { break }
        $part = $parent
    }
    return $full
}
function IPv4-Number([string]$Value) {
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($Value,[ref]$ip) -or $ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $ip.ToString() -ne $Value) { throw "Expected a canonical IPv4 address: $Value" }
    $b = $ip.GetAddressBytes()
    return ([uint64]$b[0] * 16777216 + [uint64]$b[1] * 65536 + [uint64]$b[2] * 256 + [uint64]$b[3])
}
function Number-IPv4([uint64]$Value) {
    return (@((($Value -shr 24) -band 255), (($Value -shr 16) -band 255), (($Value -shr 8) -band 255), ($Value -band 255)) -join '.')
}
function Private-IPv4([string]$Value) {
    $n = IPv4-Number $Value
    return (($n -ge 167772160 -and $n -le 184549375) -or ($n -ge 2886729728 -and $n -le 2887778303) -or ($n -ge 3232235520 -and $n -le 3232301055))
}
function Active-Addresses {
    $found = @()
    foreach ($nic in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($nic.OperationalStatus -ne [Net.NetworkInformation.OperationalStatus]::Up) { continue }
        foreach ($unicast in $nic.GetIPProperties().UnicastAddresses) {
            if ($unicast.Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
                $value = $unicast.Address.ToString()
                if (Private-IPv4 $value) { $found += $value }
            }
        }
    }
    return @($found | Sort-Object -Unique)
}
function Remote-Complement([string]$HostAddress) {
    $n = IPv4-Number $HostAddress
    $result = @()
    for ($prefix = 1; $prefix -le 32; $prefix++) {
        $bit = [uint64]1 -shl (32-$prefix)
        $mask = ([uint64]4294967295 -shl (32-$prefix)) -band [uint64]4294967295
        $network = ($n -bxor $bit) -band $mask
        $result += "$(Number-IPv4 $network)/$prefix"
    }
    return @($result + '::/1' + '8000::/1')
}
function Canonical-Addresses($Values) {
    $result = foreach ($value in @($Values)) {
        $s = ([string]$value).Trim().ToLowerInvariant()
        $parts = $s.Split('/')
        if ($parts[0].Contains(':')) {
            $address = [Net.IPAddress]::Parse($parts[0]).ToString()
            if ($parts.Count -ne 2 -or $parts[1] -ne '1' -or $address -notin @('::','8000::')) { throw "Unexpected firewall address: $s" }
            "$address/1"
            continue
        }
        $n = IPv4-Number $parts[0]
        $prefix = 32
        if ($parts.Count -eq 2) {
            if ($parts[1] -match '^\d+$') { $prefix = [int]$parts[1] }
            else {
                $maskValue = IPv4-Number $parts[1]
                $prefix = 0
                $zeroSeen = $false
                for ($bit = 31; $bit -ge 0; $bit--) {
                    $set = (($maskValue -band ([uint64]1 -shl $bit)) -ne 0)
                    if ($set -and $zeroSeen) { throw "Non-contiguous firewall mask: $s" }
                    if ($set) { $prefix++ } else { $zeroSeen = $true }
                }
            }
        } elseif ($parts.Count -ne 1) { throw "Unexpected firewall address: $s" }
        if ($prefix -lt 0 -or $prefix -gt 32) { throw "Unexpected firewall prefix: $s" }
        $mask = if ($prefix -eq 0) { [uint64]0 } else { ([uint64]4294967295 -shl (32-$prefix)) -band [uint64]4294967295 }
        "$(Number-IPv4 ($n -band $mask))/$prefix"
    }
    return @($result | Sort-Object -Unique)
}
function Quote-Native([string]$Value) {
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}
function Test-Backend {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1',25565,$null,$null)
        if (-not $pending.AsyncWaitHandle.WaitOne(1500)) { throw 'Java backend 127.0.0.1:25565 is not listening.' }
        $client.EndConnect($pending)
    } finally { $client.Dispose() }
}
function Assert-PortsFree {
    $properties = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
    if (@($properties.GetActiveTcpListeners() | Where-Object { $_.Port -in @(19132,25568) }).Count) { throw 'TCP port 19132 or 25568 is already in use.' }
    if (@($properties.GetActiveUdpListeners() | Where-Object { $_.Port -eq 19132 }).Count) { throw 'UDP port 19132 is already in use.' }
}
function Assert-FirewallPolicy {
    $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    if ($profiles.Count -ne 3 -or @($profiles | Where-Object { [string]$_.Enabled -ne 'True' -or [string]$_.AllowLocalFirewallRules -eq 'False' }).Count) { throw 'All three Windows Firewall profiles must be enabled and accept local rules.' }
    $allows = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound -Action Allow -ErrorAction Stop)
    $overrides = @($allows | Get-NetFirewallSecurityFilter -ErrorAction Stop | Where-Object { [string]$_.OverrideBlockRules -eq 'True' })
    if ($overrides.Count) { throw 'An effective inbound allow rule can override blocks.' }
}
function Assert-OwnedRule([string]$Name,[string]$Protocol,$Remote) {
    $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $Name -ErrorAction Stop)
    if ($rules.Count -ne 1) { throw "Cannot verify effective rule $Name." }
    $r = $rules[0]
    $enforcement = @($r.EnforcementStatus | ForEach-Object { [string]$_ })
    # Rules covering all profiles can report both an enforced active profile
    # and inactive profiles. Require positive enforcement and no other flags.
    $isEnforced = ($enforcement -contains 'Full' -or $enforcement -contains 'Enforced') -and
        -not @($enforcement | Where-Object { $_ -notin @('Full','Enforced','ProfileInactive','InactiveProfile') }).Count
    if ([string]$r.Enabled -ne 'True' -or [string]$r.Direction -ne 'Inbound' -or [string]$r.Action -ne 'Block' -or [string]$r.Profile -ne 'Any' -or [string]$r.PrimaryStatus -ne 'OK' -or -not $isEnforced) { throw "Firewall rule is not fully effective: $Name (status $($r.PrimaryStatus), enforcement $($r.EnforcementStatus), profile $($r.Profile))." }
    $p = $r | Get-NetFirewallPortFilter -ErrorAction Stop
    $a = $r | Get-NetFirewallAddressFilter -ErrorAction Stop
    $app = $r | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $service = $r | Get-NetFirewallServiceFilter -ErrorAction Stop
    $number = if ($Protocol -eq 'TCP') { '6' } else { '17' }
    if ([string]$p.Protocol -notin @($Protocol,$number) -or [string]$p.LocalPort -ne '19132' -or [string]$p.RemotePort -ne 'Any' -or [string]$a.LocalAddress -ne 'Any' -or [string]$app.Program -ne 'Any' -or [string]$service.Service -ne 'Any') { throw "Unexpected effective firewall scope for $Name." }
    if (([string]::Join('|',(Canonical-Addresses $a.RemoteAddress))) -ne ([string]::Join('|',(Canonical-Addresses $Remote)))) { throw "Unexpected effective remote addresses for $Name." }
}

$root = Checked-Path $DataDir 'DataDir'
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'DataDir must be an existing prepared Bedrock directory.' }
$manifestPath = Checked-Path (Join-Path $root 'prepared-bedrock.json') 'Manifest'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if (-not $JavaExe) { $JavaExe = [string]$manifest.java_exe }
$java = Checked-Path $JavaExe 'JavaExe'
if (-not (Test-Path -LiteralPath $java -PathType Leaf) -or [IO.Path]::GetFileName($java) -ine 'java.exe') { throw 'JavaExe must name an existing non-C java.exe.' }
$jar = Checked-Path (Join-Path $root 'ViaProxy-3.4.14.jar') 'ViaProxy JAR'
$plugin = Checked-Path (Join-Path $root 'plugins\Geyser-ViaProxy.jar') 'Geyser JAR'
foreach ($entry in @(
    @{Path=$jar; Hash='2894bbfb2f4342f8fde887af6c689313efd5b0e46a8006775eea0e21e2948ab5'},
    @{Path=$plugin; Hash='3145fa5bcd55bc1e87be1e909ffe93d80bb11415e6bf8ebb13d887e24a497654'}
)) {
    if (-not (Test-Path -LiteralPath $entry.Path -PathType Leaf) -or (Get-FileHash -LiteralPath $entry.Path -Algorithm SHA256).Hash -ine $entry.Hash) { throw "Missing or changed pinned JAR: $($entry.Path)" }
}
$plugins = @(Get-ChildItem -LiteralPath (Join-Path $root 'plugins') -Filter '*.jar' -File)
if ($plugins.Count -ne 1 -or $plugins[0].FullName -ine $plugin) { throw 'Only the verified Geyser plugin may be installed.' }
$config = Checked-Path (Join-Path $root 'viaproxy.yml') 'ViaProxy configuration'
$lines = @(Get-Content -LiteralPath $config | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
$expected = @("bind-address: '127.0.0.1:25568'", "target-address: '127.0.0.1:25565'", "target-version: '1.21.4'", 'proxy-online-mode: false', 'auth-method: NONE')
if ([string]::Join('|',$lines) -cne [string]::Join('|',$expected)) { throw 'ViaProxy configuration is not the fixed staged loopback profile.' }
$geyserConfig = Checked-Path (Join-Path $root 'plugins\Geyser\config.yml') 'Geyser configuration'
$geyserLines = @(Get-Content -LiteralPath $geyserConfig | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') })
$expectedGeyser = @('config-version: 12', 'bedrock:', "  address: '127.0.0.1'", '  port: 19132',
    '  webrtc-port: 0', '  clone-remote-port: false', '  transport: nethernet', '  signaling:',
    '    mode: builtin', '    stun-discovery: false', 'java:', '  auth-type: offline')
if ([string]::Join('|',$geyserLines) -cne [string]::Join('|',$expectedGeyser)) { throw 'Geyser configuration is not the fixed staged profile.' }
$choices = @(Active-Addresses)
if ($Address) {
    if (-not (Private-IPv4 $Address) -or $Address -notin $choices) { throw 'Address must be an active local RFC1918 IPv4 address.' }
} elseif ($choices.Count -eq 1) { $Address = $choices[0] }
if ($StartLocal -and -not $Address) { throw "Choose -Address from: $($choices -join ', ')" }
Test-Backend
Assert-PortsFree
$remote = if ($Address) { @(Remote-Complement $Address) } else { @() }
if (-not $StartLocal) {
    [ordered]@{
        mode='plan'; data_dir=$root; java_exe=$java; address=$Address; available_addresses=$choices;
        duration_seconds=$DurationSeconds; startup_timeout_seconds=$StartupTimeoutSeconds;
        requires_admin_for_start=$true; block_remote_addresses=$remote;
        services_started=$false; firewall_rules_created=$false
    } | ConvertTo-Json -Depth 5
    return
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw '-StartLocal requires an elevated PowerShell.' }
Assert-FirewallPolicy
$sessionId = [Guid]::NewGuid().ToString('N')
$prefix = "AIAM-Bedrock-$sessionId"
$session = Checked-Path (Join-Path $root "runs\$sessionId") 'Session directory'
$temp = Checked-Path (Join-Path $session 'temp') 'Temporary directory'
$homeDir = Checked-Path (Join-Path $session 'home') 'Java home directory'
$owned = New-Object 'Collections.Generic.List[string]'
$process = $null
$stopped = $true
$oldEnvironment = @{}
$remaining = @()
try {
    foreach ($protocol in @('TCP','UDP')) {
        $name = "$prefix-$protocol"
        $owned.Add($name)
        $ruleParameters = @{
            PolicyStore='PersistentStore'; Name=$name; DisplayName=$name; Group='AI-AM temporary Bedrock isolation';
            Enabled='True'; Direction='Inbound'; Action='Block'; Profile='Any'; Protocol=$protocol;
            LocalPort=19132; RemotePort='Any'; LocalAddress='Any'; RemoteAddress=$remote;
            Program='Any'; Service='Any'; InterfaceType='Any'; EdgeTraversalPolicy='Block'; ErrorAction='Stop'
        }
        New-NetFirewallRule @ruleParameters | Out-Null
    }
    Assert-FirewallPolicy
    Assert-OwnedRule "$prefix-TCP" 'TCP' $remote
    Assert-OwnedRule "$prefix-UDP" 'UDP' $remote
    New-Item -ItemType Directory -Path $session,$temp,$homeDir -ErrorAction Stop | Out-Null
    $activeConfig = Join-Path $session 'viaproxy.yml'
    Copy-Item -LiteralPath $config -Destination $activeConfig -ErrorAction Stop
    foreach ($key in @('TEMP','TMP','TMPDIR','JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS')) {
        $oldEnvironment[$key] = [Environment]::GetEnvironmentVariable($key,'Process')
        $value = if ($key -in @('TEMP','TMP','TMPDIR')) { $temp } else { $null }
        [Environment]::SetEnvironmentVariable($key,$value,'Process')
    }
    $arguments = @("-Djava.io.tmpdir=$temp", "-Duser.home=$homeDir", '-Djava.net.preferIPv4Stack=true',
        "-DgeyserUdpAddress=$Address", "-DgeyserAdvertiseAddresses=$Address", '-DgeyserUdpPort=19132',
        '-DgeyserTransport=nethernet', '-DgeyserSignalingMode=builtin', '-DgeyserStunDiscovery=false',
        '-Xms128M', '-Xmx768M', '-jar', $jar, 'config', $activeConfig)
    $startParameters = @{
        FilePath=$java; ArgumentList=(($arguments | ForEach-Object { Quote-Native $_ }) -join ' ');
        WorkingDirectory=$root; WindowStyle='Hidden'; PassThru=$true;
        RedirectStandardOutput=(Join-Path $session 'proxy.stdout.log'); RedirectStandardError=(Join-Path $session 'proxy.stderr.log')
    }
    $process = Start-Process @startParameters
    $stopped = $false
    Write-Output "Owned proxy PID $($process.Id); rules $($owned -join ', '); logs $session"
    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $process.Refresh()
        if ($process.HasExited) { throw "Proxy exited during startup. Read $session." }
        try {
            $response = Invoke-WebRequest -Uri "http://$($Address):19132/v1/join" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($response.StatusCode -eq 200 -and $null -ne ($response.Content | ConvertFrom-Json)) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "NetherNet signaling was not ready within $StartupTimeoutSeconds seconds. Read $session." }
    Write-Output "AIAM_BEDROCK_READY address=$Address port=19132 pid=$($process.Id) duration=$DurationSeconds"
    $until = [DateTime]::UtcNow.AddSeconds($DurationSeconds)
    while ([DateTime]::UtcNow -lt $until) {
        $process.Refresh()
        if ($process.HasExited) { throw "Proxy exited before the session ended. Read $session." }
        Start-Sleep -Seconds 1
    }
} finally {
    if ($null -ne $process) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) { Stop-Process -InputObject $process -Force -ErrorAction Stop }
            $stopped = $process.WaitForExit(10000)
        } catch { $stopped = $false; Write-Warning "Could not confirm owned proxy stopped: $($_.Exception.Message)" }
    }
    if ($stopped) {
        foreach ($name in $owned) {
            try {
                $persistent = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop | Where-Object { $_.Name -eq $name })
                foreach ($rule in $persistent) { $rule | Remove-NetFirewallRule -ErrorAction Stop }
                $effective = @(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop | Where-Object { $_.Name -eq $name })
                if ($effective.Count) { throw 'Rule still exists in ActiveStore.' }
            } catch { $remaining += $name; Write-Warning "Cleanup could not confirm removal of $($name): $($_.Exception.Message)" }
        }
    } else {
        $remaining = $owned.ToArray()
        Write-Warning "Owned proxy stop is unconfirmed; protective rules retained: $($remaining -join ', ')"
    }
    foreach ($key in $oldEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key,$oldEnvironment[$key],'Process') }
    if ($null -ne $process) { $process.Dispose() }
    if ($remaining.Count -or -not $stopped) { throw "Cleanup incomplete. Check owned proxy and firewall rules: $($remaining -join ', ')" }
    Write-Output 'AIAM_BEDROCK_CLEANUP_OK'
}

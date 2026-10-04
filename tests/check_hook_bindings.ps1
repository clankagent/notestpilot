param(
    [Parameter(Mandatory = $true)][string]$ManagedPath,
    [Parameter(Mandatory = $true)][string]$BepInExCorePath,
    [string]$SteamworksPath = (Join-Path $ManagedPath 'com.rlabrecque.steamworks.net.dll'),
    [string]$RuntimeSourcePath = (Join-Path $PSScriptRoot '..\src\NOTestPilot.Runtime')
)

$ErrorActionPreference = 'Stop'
$managed = [IO.Path]::GetFullPath($ManagedPath)
$bepinex = [IO.Path]::GetFullPath($BepInExCorePath)
$runtime = [IO.Path]::GetFullPath($RuntimeSourcePath)
$assemblyPath = Join-Path $managed 'Assembly-CSharp.dll'
$unityPath = Join-Path $managed 'UnityEngine.CoreModule.dll'
$miragePath = Join-Path $managed 'Mirage.dll'
$unitaskPath = Join-Path $managed 'UniTask.dll'
$steamworksPathExact = [IO.Path]::GetFullPath($SteamworksPath)
$harmonyPath = Join-Path $bepinex '0Harmony.dll'
foreach ($path in @($assemblyPath, $unityPath, $miragePath, $unitaskPath, $steamworksPathExact, $harmonyPath, (Join-Path $runtime 'Events.cs'), (Join-Path $runtime 'MockPlayers.cs'), (Join-Path $runtime 'MockLifecycle.cs'), (Join-Path $runtime 'LocalHostAdapter.cs'))) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required binding input missing: $path" }
}

$game = [Reflection.Assembly]::LoadFrom($assemblyPath)
$unity = [Reflection.Assembly]::LoadFrom($unityPath)
$mirage = [Reflection.Assembly]::LoadFrom($miragePath)
$unitask = [Reflection.Assembly]::LoadFrom($unitaskPath)
$steamworks = [Reflection.Assembly]::LoadFrom($steamworksPathExact)
$harmonyAssembly = [Reflection.Assembly]::LoadFrom($harmonyPath)
$typeAssemblies = @($game, $unity, $mirage, $unitask, $steamworks, $harmonyAssembly)
$typeIndex = @{}
foreach ($assembly in $typeAssemblies) {
    try { $types = $assembly.GetTypes() }
    catch [Reflection.ReflectionTypeLoadException] { $types = $_.Exception.Types | Where-Object { $_ -ne $null } }
    foreach ($type in $types) {
        if (!$typeIndex.ContainsKey($type.Name)) { $typeIndex[$type.Name] = @() }
        $typeIndex[$type.Name] += $type
    }
}

function Resolve-Type([string]$name) {
    $name = $name.Trim() -replace '\?$', ''
    if ($name.EndsWith('[]')) { return (Resolve-Type $name.Substring(0, $name.Length - 2)).MakeArrayType() }
    switch ($name) {
        'void' { return [void] }
        'bool' { return [bool] }
        'byte' { return [byte] }
        'int' { return [int] }
        'uint' { return [uint32] }
        'long' { return [long] }
        'float' { return [single] }
        'double' { return [double] }
        'string' { return [string] }
        'object' { return [object] }
        'Action' { return [Action] }
        'Type' { return [Type] }
        'IDictionary' { return [System.Collections.IDictionary] }
        'IEnumerable' { return [System.Collections.IEnumerable] }
        'Exception' { return [Exception] }
    }
    if ($name.Contains('.')) {
        foreach ($assembly in $typeAssemblies) {
            $found = $assembly.GetType($name, $false, $false)
            if ($found) { return $found }
        }
    } elseif ($typeIndex.ContainsKey($name)) {
        $choices = @($typeIndex[$name] | Sort-Object FullName -Unique)
        if ($choices.Count -eq 1) { return $choices[0] }
    }
    throw "Cannot resolve source type '$name' from the supplied exact reference inputs"
}

function Split-CSharpParameters([string]$text) {
    $parts = [System.Collections.Generic.List[string]]::new()
    $start = 0; $depth = 0
    for ($index = 0; $index -lt $text.Length; $index++) {
        if ($text[$index] -eq '<') { $depth++ }
        elseif ($text[$index] -eq '>') { $depth-- }
        elseif ($text[$index] -eq ',' -and $depth -eq 0) {
            $parts.Add($text.Substring($start, $index - $start)); $start = $index + 1
        }
    }
    if ($start -lt $text.Length) { $parts.Add($text.Substring($start)) }
    return ,$parts.ToArray()
}

function Read-Methods([string]$path) {
    $source = [IO.File]::ReadAllText($path)
    $pattern = '(?m)\b(?:private|public|protected|internal)\s+(?:(?:static|virtual|override|sealed|async|new)\s+)*([\w.<>,?\[\]]+)\s+(\w+)\s*\(([^()]*)\)'
    $result = @{}
    foreach ($match in [regex]::Matches($source, $pattern)) {
        $methodName = $match.Groups[2].Value
        $params = @()
        foreach ($raw in (Split-CSharpParameters $match.Groups[3].Value)) {
            $raw = $raw.Trim()
            if (!$raw) { continue }
            $parameter = [regex]::Match($raw, '^(?:(ref|out|in|params)\s+)?([\w.<>,?\[\]]+)\s+(@?\w+)')
            if (!$parameter.Success) { throw "Cannot parse callback parameter '$raw' in $path" }
            $params += [pscustomobject]@{
                Modifier = $parameter.Groups[1].Value
                TypeName = $parameter.Groups[2].Value
                Name = $parameter.Groups[3].Value.TrimStart('@')
            }
        }
        $result[$methodName] = [pscustomobject]@{ Name = $methodName; ReturnTypeName = $match.Groups[1].Value; Parameters = $params }
    }
    return $result
}

function Resolve-TargetType([string]$name) { return Resolve-Type $name }
function Resolve-TargetMethod([Type]$type, [string]$name, [Type[]]$argumentTypes) {
    $flags = [Reflection.BindingFlags]::Instance -bor [Reflection.BindingFlags]::Static -bor [Reflection.BindingFlags]::Public -bor [Reflection.BindingFlags]::NonPublic
    $methods = @($type.GetMethods($flags) | Where-Object { $_.Name -eq $name })
    if ($argumentTypes) {
        $methods = @($methods | Where-Object {
            $actual = @($_.GetParameters() | ForEach-Object { $_.ParameterType })
            if ($actual.Count -ne $argumentTypes.Count) { return $false }
            for ($index = 0; $index -lt $actual.Count; $index++) { if ($actual[$index] -ne $argumentTypes[$index]) { return $false } }
            return $true
        })
    }
    if ($methods.Count -ne 1) { throw "Expected one exact target method $($type.FullName).$name; found $($methods.Count)" }
    return $methods[0]
}

function Check-Hook([string]$kind, [Reflection.MethodInfo]$target, [string]$callbackName,
    [hashtable]$callbacks, [System.Collections.Generic.Dictionary[string,string]]$states) {
    if (!$callbacks.ContainsKey($callbackName)) { throw "Hook callback '$callbackName' was not found in its source file" }
    $callback = $callbacks[$callbackName]
    $original = @($target.GetParameters())
    foreach ($parameter in $callback.Parameters) {
        if ($parameter.Name -eq '__instance') {
            $parameterType = Resolve-Type $parameter.TypeName
            if ($parameterType -ne $target.DeclaringType) { throw "$callbackName.__instance type $($parameterType.FullName) does not match $($target.DeclaringType.FullName)" }
        } elseif ($parameter.Name -match '^__(\d+)$') {
            $index = [int]$Matches[1]
            if ($index -ge $original.Count) { throw "$callbackName uses $($parameter.Name), but $($target.Name) has only $($original.Count) arguments" }
            $parameterType = Resolve-Type $parameter.TypeName
            if ($parameterType -ne $original[$index].ParameterType) {
                throw "$callbackName.$($parameter.Name) type $($parameterType.FullName) != argument[$index] $($original[$index].ParameterType.FullName)"
            }
        } elseif ($parameter.Name -eq '__result') {
            $parameterType = Resolve-Type $parameter.TypeName
            if ($parameter.Modifier -notin @('ref','out') -or $parameterType -ne $target.ReturnType) {
                throw "$callbackName.__result must be ref/out $($target.ReturnType.FullName)"
            }
        } elseif ($parameter.Name -eq '__exception') {
            $parameterType = Resolve-Type $parameter.TypeName
            if ($kind -ne 'finalizer' -or $parameterType -ne [Exception]) { throw "$callbackName.__exception is only valid as Exception in a finalizer" }
        } elseif ($parameter.Name -eq '__state') {
            $key = "$($target.DeclaringType.FullName).$($target.Name)"
            if ($states.ContainsKey($key) -and $states[$key] -ne $parameter.TypeName) { throw "$key __state type mismatch" }
            $states[$key] = $parameter.TypeName
        } else {
            $matching = @($original | Where-Object { $_.Name -eq $parameter.Name })
            $parameterType = Resolve-Type $parameter.TypeName
            if ($matching.Count -ne 1 -or $parameterType -ne $matching[0].ParameterType) {
                throw "$callbackName.$($parameter.Name) does not exactly match a named original parameter of $($target.Name)"
            }
        }
    }
    $callbackReturnType = Resolve-Type $callback.ReturnTypeName
    if ($kind -eq 'prefix' -and $callbackReturnType -ne [void] -and $callbackReturnType -ne [bool]) {
        throw "$callbackName prefix return must be void or bool"
    }
    if ($kind -eq 'postfix' -and $callbackReturnType -ne [void]) {
        throw "$callbackName postfix return must be void"
    }
    return "$($target.DeclaringType.Name).$($target.Name) -> $callbackName ($kind)"
}

$eventsPath = Join-Path $runtime 'Events.cs'
$mockPath = Join-Path $runtime 'MockPlayers.cs'
$lifecyclePath = Join-Path $runtime 'MockLifecycle.cs'
$eventsCallbacks = Read-Methods $eventsPath
$mockCallbacks = Read-Methods $mockPath
$lifecycleCallbacks = Read-Methods $lifecyclePath
$states = [System.Collections.Generic.Dictionary[string,string]]::new()
$checked = [System.Collections.Generic.List[string]]::new()

# Parse the actual Events.Install patch calls and their exact typeof argument lists.
$eventsText = [IO.File]::ReadAllText($eventsPath)
$eventCallPattern = 'Patch\(harmony,\s*typeof\((?<type>\w+)\),\s*(?<method>[^,]+),\s*(?:new\s*\[\]\s*\{(?<args>[^}]*)\}|(?<empty>Type\.EmptyTypes))(?<hooks>[^;]*?)\);'
$eventCalls = [regex]::Matches($eventsText, $eventCallPattern)
if ($eventCalls.Count -ne [regex]::Matches($eventsText, '\bPatch\(harmony,').Count) { throw 'An Events.Install patch call was not parsed for binding validation' }
foreach ($match in $eventCalls) {
    $targetType = Resolve-TargetType $match.Groups['type'].Value
    $methodExpr = $match.Groups['method'].Value.Trim()
    if ($methodExpr -match '^nameof\(\w+\.(\w+)\)$') { $methodName = $Matches[1] }
    elseif ($methodExpr -match '^"([^"]+)"$') { $methodName = $Matches[1] }
    else { throw "Cannot parse Events patch target expression '$methodExpr'" }
    $argumentTypes = @()
    if (!$match.Groups['empty'].Success) {
        $argumentTypes = @([regex]::Matches($match.Groups['args'].Value, 'typeof\(([^)]+)\)') | ForEach-Object { Resolve-Type $_.Groups[1].Value })
    }
    $target = Resolve-TargetMethod $targetType $methodName $argumentTypes
    $hookPattern = '(prefix|postfix|finalizer):\s*nameof\((\w+)\)'
    foreach ($hook in [regex]::Matches($match.Groups['hooks'].Value, $hookPattern)) {
        $checked.Add((Check-Hook $hook.Groups[1].Value $target $hook.Groups[2].Value $eventsCallbacks $states))
    }
}

# MockPlayers uses a local prefix helper for most patches; read those calls verbatim.
$mockText = [IO.File]::ReadAllText($mockPath)
$mockCallPattern = 'Patch\(harmony,\s*typeof\((?<type>\w+)\),\s*"(?<method>[^"]+)",\s*nameof\((?<hook>\w+)\)\)'
$mockCalls = [regex]::Matches($mockText, $mockCallPattern)
if ($mockCalls.Count -ne [regex]::Matches($mockText, '\bPatch\(harmony,').Count) { throw 'A MockPlayers helper patch call was not parsed for binding validation' }
foreach ($match in $mockCalls) {
    $targetType = Resolve-TargetType $match.Groups['type'].Value
    $target = Resolve-TargetMethod $targetType $match.Groups['method'].Value $null
    $checked.Add((Check-Hook 'prefix' $target $match.Groups['hook'].Value $mockCallbacks $states))
}

# Include the direct OnDestroy postfix that does not use the helper.
$destroyPattern = 'harmony\.Patch\(AccessTools\.Method\(typeof\((?<type>\w+)\),\s*"(?<method>[^"]+)"\),\s*postfix:\s*new HarmonyMethod\(typeof\(MockPlayers\),\s*nameof\((?<hook>\w+)\)\)\)'
$destroyCalls = [regex]::Matches($mockText, $destroyPattern)
if ($destroyCalls.Count -ne [regex]::Matches($mockText, 'harmony\.Patch\(AccessTools\.Method').Count) { throw 'A direct MockPlayers Harmony patch was not parsed for binding validation' }
foreach ($match in $destroyCalls) {
    $targetType = Resolve-TargetType $match.Groups['type'].Value
    $target = Resolve-TargetMethod $targetType $match.Groups['method'].Value $null
    $checked.Add((Check-Hook 'postfix' $target $match.Groups['hook'].Value $mockCallbacks $states))
}

# MockLifecycle uses direct Harmony.Patch calls because it validates exact overloads first.
$lifecycleText = [IO.File]::ReadAllText($lifecyclePath)
$lifecycleTargets = @(
    [pscustomobject]@{ Type = 'FactionHQ'; Method = 'RewardPlayer'; Args = @('Player','Unit','float','float','RewardType'); Hooks = @([pscustomobject]@{ Kind = 'prefix'; Callback = 'BeforeRewardPlayer' },[pscustomobject]@{ Kind = 'finalizer'; Callback = 'AfterRewardPlayer' }) },
    [pscustomobject]@{ Type = 'MessageManager'; Method = 'TargetCreditMessage'; Args = @('INetworkPlayer','PersistentID','float','RewardType'); Hooks = @([pscustomobject]@{ Kind = 'prefix'; Callback = 'BeforeTargetCreditMessage' }) }
)
if (([regex]::Matches($lifecycleText, 'harmony\.Patch\(')).Count -ne $lifecycleTargets.Count) { throw 'Unexpected or unparsed MockLifecycle Harmony patch site' }
foreach ($entry in $lifecycleTargets) {
    $targetType = Resolve-TargetType $entry.Type
    $argumentTypes = @($entry.Args | ForEach-Object { Resolve-Type $_ })
    $target = Resolve-TargetMethod $targetType $entry.Method $argumentTypes
    foreach ($hookInfo in $entry.Hooks) {
        $checked.Add((Check-Hook $hookInfo.Kind $target $hookInfo.Callback $lifecycleCallbacks $states))
    }
}

$localHostPath = Join-Path $runtime 'LocalHostAdapter.cs'
$localHostText = [IO.File]::ReadAllText($localHostPath)
$localHostCallbacks = Read-Methods $localHostPath
# The first hook resolves its exact overload into a variable before patching.
# Check that source expression as well as both patch sites, so a new/unparsed
# hook cannot disappear behind the expected callback total.
$connectedPattern = 'var connected = AccessTools\.Method\(typeof\(NetworkAuthenticatorNuclearOption\),\s*"OnClientConnected",\s*new\s*\[\]\s*\{\s*typeof\(INetworkPlayer\)\s*\}\s*\)'
$connectedHookPattern = 'harmony\.Patch\(connected,\s*prefix:\s*new HarmonyMethod\(typeof\(LocalHostAdapter\),\s*nameof\((?<hook>\w+)\)\)\)'
$nameHookPattern = 'harmony\.Patch\(AccessTools\.Method\(typeof\(Player\),\s*nameof\(Player\.GetPlayerName\)\),\s*prefix:\s*new HarmonyMethod\(typeof\(LocalHostAdapter\),\s*nameof\((?<hook>\w+)\)\)\)'
$connectedCalls = [regex]::Matches($localHostText, $connectedHookPattern)
$nameCalls = [regex]::Matches($localHostText, $nameHookPattern)
if ([regex]::Matches($localHostText, $connectedPattern).Count -ne 1 -or $connectedCalls.Count -ne 1 -or $nameCalls.Count -ne 1 -or
    [regex]::Matches($localHostText, 'harmony\.Patch\(').Count -ne ($connectedCalls.Count + $nameCalls.Count)) {
    throw 'Unexpected or unparsed LocalHostAdapter target or Harmony patch site'
}
$connectedTarget = Resolve-TargetMethod (Resolve-Type 'NuclearOption.Networking.Authentication.NetworkAuthenticatorNuclearOption') 'OnClientConnected' @((Resolve-Type 'Mirage.INetworkPlayer'))
if ($connectedTarget.ReturnType -ne (Resolve-Type 'UniTaskVoid')) { throw 'Local-host callback return type is not the reviewed UniTaskVoid' }
$checked.Add((Check-Hook 'prefix' $connectedTarget $connectedCalls[0].Groups['hook'].Value $localHostCallbacks $states))
$nameTarget = Resolve-TargetMethod (Resolve-Type 'Player') 'GetPlayerName' $null
$checked.Add((Check-Hook 'prefix' $nameTarget $nameCalls[0].Groups['hook'].Value $localHostCallbacks $states))

if ($checked.Count -ne 24) { throw "Unexpected Harmony callback count: $($checked.Count)" }
Write-Output "PASS: validated $($checked.Count) Events/MockPlayers/MockLifecycle/LocalHostAdapter Harmony callback bindings against $([IO.Path]::GetFileName($assemblyPath))"
$checked | ForEach-Object { Write-Output "  $_" }
